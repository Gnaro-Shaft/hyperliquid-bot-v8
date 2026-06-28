import pandas as pd
from pymongo import MongoClient
from datetime import datetime

from config import (
    MONGO_URL, MONGO_DB, MONGO_COLLECTION_1M, MONGO_COLLECTION_15M, MONGO_COLLECTION_1H,
    MONGO_COLLECTION_FUNDING, MONGO_COLLECTION_OI, MONGO_COLLECTION_ORDERBOOK,
    LEVELS, SL_PCT, TP_PCT, MIN_TP_PCT, DEBUG, SIGNAL_THRESHOLD_DEFAULT,
    REGIME_ADAPTIVE, REGIME_HIGH_VOL_ATR_PCT,
)
from strategy.indicators import (
    ema, rsi, macd, bollinger_bands, vwap, atr,
    bb_width, bb_percent_b, volume_ratio, ema_slope, adx
)
from utils.logger import Logger
from utils.sizing import dynamic_sl_tp
from utils.regime import regime_preset

try:
    from ml.predictor import MLPredictor
    _ML_AVAILABLE = True
except ImportError:
    _ML_AVAILABLE = False

# Seuils ML (tunable)
ML_BLOCK_THRESHOLD  = 0.38  # En dessous → gate bloqué (signal très peu probable)
ML_PENALTY_THRESHOLD = 0.48  # En dessous → pénalité -1 (signal douteux)


class StrategyEngine:
    def __init__(self, coin="BTC"):
        client = MongoClient(MONGO_URL, serverSelectionTimeoutMS=5000)
        self.mongo = client[MONGO_DB]
        self.coin = coin
        self.logger = Logger(collection="signals")

        # Filtre ML optionnel (chargé si le modèle existe dans ml/models/)
        self.ml_predictor = None
        if _ML_AVAILABLE:
            try:
                pred = MLPredictor(coin=coin)
                if pred.is_available():
                    self.ml_predictor = pred
                    print(f"[STRATEGY] 🤖 Modèle ML activé pour {coin}")
                # Silence si pas encore entraîné
            except Exception as e:
                print(f"[STRATEGY] ML non chargé ({coin}): {e}")

    def reload_ml_model(self) -> bool:
        """Recharge le modèle ML depuis le disque sans redémarrer le bot.

        Appelé par AutoTrainer après un réentraînement réussi.
        Retourne True si le modèle a été rechargé avec succès.
        """
        if not _ML_AVAILABLE:
            return False
        try:
            pred = MLPredictor(coin=self.coin)
            if pred.is_available():
                self.ml_predictor = pred
                print(f"[STRATEGY] 🔄 Modèle ML rechargé à chaud pour {self.coin}")
                return True
            else:
                print(f"[STRATEGY] Rechargement ML échoué — fichier absent ({self.coin})")
                return False
        except Exception as e:
            print(f"[STRATEGY] Erreur reload ML ({self.coin}): {e}")
            return False

    def get_market_context(self):
        """Récupère funding rate, OI et orderbook avec contexte historique.

        Funding  : 6 derniers polls (≈30 min à 300s/poll)
          - funding_rate    : dernière valeur
          - funding_slope   : tendance (last - first) sur les 6 polls
                              > 0 = funding qui monte (accumulation longs)
                              < 0 = funding qui baisse (longs se ferment)

        OI       : 6 derniers polls (≈30 min)
          - oi_change_pct   : variation inter-poll la plus récente (existant)
          - oi_trend_30m    : variation cumulée sur 30 min (last_oi vs oldest_oi)
                              Plus fiable qu'un seul poll

        Orderbook: 10 derniers snapshots (≈5 min à 30s/snapshot)
          - ob_imbalance    : dernier snapshot (réactivité)
          - ob_imbalance_avg: moyenne sur 5 min (filtre les spikes momentanés)
        """
        ctx = {
            "funding_rate": None,
            "funding_slope": None,
            "oi_change_pct": None,
            "oi_trend_30m": None,
            "ob_imbalance": None,
            "ob_imbalance_avg": None,
            "spread_pct": None,
            "ob_depth_ratio": None,
        }

        # --- Funding rate (6 derniers polls ≈ 30 min) ---
        try:
            docs = list(self.mongo[MONGO_COLLECTION_FUNDING].find(
                {"coin": self.coin}, sort=[("timestamp", -1)]
            ).limit(6))
            if docs:
                ctx["funding_rate"] = float(docs[0].get("funding_rate", 0))
                if len(docs) >= 2:
                    rates = [float(d.get("funding_rate", 0)) for d in reversed(docs)]
                    ctx["funding_slope"] = rates[-1] - rates[0]
        except Exception:
            pass

        # --- Open Interest (6 derniers polls ≈ 30 min) ---
        try:
            docs = list(self.mongo[MONGO_COLLECTION_OI].find(
                {"coin": self.coin}, sort=[("timestamp", -1)]
            ).limit(6))
            if docs:
                ctx["oi_change_pct"] = float(docs[0].get("oi_change_pct", 0))
                if len(docs) >= 2:
                    # Variation cumulée : (dernier OI - premier OI) / premier OI
                    oi_last = float(docs[0].get("open_interest", 0))
                    oi_first = float(docs[-1].get("open_interest", 0))
                    if oi_first > 0:
                        ctx["oi_trend_30m"] = (oi_last - oi_first) / oi_first
        except Exception:
            pass

        # --- Orderbook imbalance (10 derniers snapshots ≈ 5 min) ---
        try:
            docs = list(self.mongo[MONGO_COLLECTION_ORDERBOOK].find(
                {"coin": self.coin}, sort=[("timestamp", -1)]
            ).limit(10))
            if docs:
                ctx["ob_imbalance"] = float(docs[0].get("imbalance", 0))
                if len(docs) >= 3:
                    values = [float(d.get("imbalance", 0)) for d in docs]
                    ctx["ob_imbalance_avg"] = round(sum(values) / len(values), 4)
                # Spread (liquidité) — dernier snapshot
                sp = docs[0].get("spread_pct")
                if sp is not None:
                    ctx["spread_pct"] = float(sp)
                # Ratio de profondeur : depth courant / moyenne récente (coin-agnostique)
                depths = [(d.get("bid_depth_5") or 0) + (d.get("ask_depth_5") or 0) for d in docs]
                depths = [d for d in depths if d > 0]
                if len(depths) >= 3 and (avg := sum(depths) / len(depths)) > 0:
                    ctx["ob_depth_ratio"] = round(depths[0] / avg, 3)
        except Exception:
            pass

        return ctx

    def get_last_n_candles(self, n=100, tf="1m"):
        if tf == "1m":
            col = MONGO_COLLECTION_1M
        elif tf == "15m":
            col = MONGO_COLLECTION_15M
        else:
            col = MONGO_COLLECTION_1H
        cursor = self.mongo[col].find({"coin": self.coin}).sort("timestamp", -1).limit(n)
        data = list(cursor)
        if not data:
            return pd.DataFrame()
        df = pd.DataFrame(reversed(data))
        for c in ["open", "high", "low", "close", "volume"]:
            df[c] = df[c].astype(float)
        return df

    def compute_signals(self, score_threshold=None):
        """Scoring pondere multi-timeframe v8.2 — 15m comme timeframe principal.

        Filtres GATE (bloquent le signal si non remplis) :
          1. ADX >= 25  → tendance confirmee sur 15m (sinon = range/chop)
          2. BB width > 0.004 → volatilite suffisante sur 15m (sinon = squeeze)
          3. Gate 1h    → bloque les trades contre-tendance horaire

        Poids scoring (tous les indicateurs sur 15m sauf #9) :
          EMA trend + slope    x2/x1 (frais=+2, décélère=+1)
          MACD momentum        x2    (force)
          MACD hist zero-cross x2/x1 (frais=±2, continuation=±1)
          RSI zones            x1    (extremes)
          Bollinger %B         x1    (position relative)
          VWAP                 x1    (biais institutionnel)
          Volume spike         x1    (confirmation)
          ADX force            x1    (bonus si tendance forte)
          Momentum 1m          x1    (timing précis — alignement 1m)
          Funding rate         x1/x2 (contrarian — haut+montant=x2, haut seul=x1)
          OI trend 30m         x1    (confirmation tendance sur 30 min)
          Orderbook imbalance  x1    (moyenne 5 min — filtre les spikes)
          Age tendance EMA     x1    (frais<5=+1, vieille>20=-1 sur 15m)
        ------------------------------------------
        Total possible : +/-17, normalise en 5 niveaux [-2, -1, 0, 1, 2]
        Seuil de trade : level ±2 (raw >= 8 ou <= -8)
        """
        # 15m = timeframe principal (tous les indicateurs)
        # 1m  = momentum court-terme uniquement (signal #9)
        df_15m = self.get_last_n_candles(150, "15m")
        df_1m  = self.get_last_n_candles(20, "1m")
        df_1h  = self.get_last_n_candles(30, "1h")
        mkt = self.get_market_context()

        if df_15m.empty or len(df_15m) < 50:
            return self._neutral(f"Pas assez de donnees 15m ({len(df_15m)}/50)")

        # === Indicateurs 15m (PRIMARY) ===
        df_15m["EMA9"] = ema(df_15m["close"], 9)
        df_15m["EMA21"] = ema(df_15m["close"], 21)
        df_15m["RSI"] = rsi(df_15m["close"], 14)
        df_15m["MACD"], df_15m["MACD_signal"], df_15m["MACD_hist"] = macd(df_15m["close"])
        df_15m["BB_upper"], df_15m["BB_mid"], df_15m["BB_lower"] = bollinger_bands(df_15m["close"])
        df_15m["VWAP"] = vwap(df_15m)
        df_15m["ATR"] = atr(df_15m)
        df_15m["BB_pctB"] = bb_percent_b(df_15m["close"], df_15m["BB_upper"], df_15m["BB_lower"])
        df_15m["BB_width"] = bb_width(df_15m["BB_upper"], df_15m["BB_lower"], df_15m["BB_mid"])
        df_15m["vol_ratio"] = volume_ratio(df_15m["volume"])
        df_15m["EMA9_slope"] = ema_slope(df_15m["EMA9"], 3)
        df_15m["ADX"], df_15m["PLUS_DI"], df_15m["MINUS_DI"] = adx(df_15m)

        row = df_15m.iloc[-1]
        prev = df_15m.iloc[-2]

        score = 0
        debug = {}

        # === FILTRES GATE (anti-chop) ===
        adx_val = row["ADX"] if pd.notna(row["ADX"]) else 0.0
        bb_w = row["BB_width"] if pd.notna(row["BB_width"]) else 0.0
        is_squeeze = bb_w < 0.004
        is_trending = adx_val >= 25

        # Régime de marché — déterminé avant les gates (Phase 4 : presets adaptatifs)
        atr_pct_now = (row["ATR"] / row["close"]) if pd.notna(row.get("ATR")) and row["close"] > 0 else None

        if REGIME_ADAPTIVE:
            preset = regime_preset(adx_val, bb_w, atr_pct_now,
                                   high_vol_atr=REGIME_HIGH_VOL_ATR_PCT)
            regime = preset["regime"]
            regime_threshold_adj = preset["threshold_adj"]
            regime_tp_mult = preset["tp_mult"]
            regime_sl_mult = preset["sl_mult"]
            regime_size_mult = preset["size_mult"]
            blocked = preset["blocked"]
        else:
            # Comportement legacy v8.4 (presets neutres)
            if adx_val >= 30:
                regime, regime_threshold_adj = "STRONG", 0
            elif adx_val >= 25:
                regime, regime_threshold_adj = "WEAK", 1
            else:
                regime, regime_threshold_adj = "RANGE", 0
            regime_tp_mult = regime_sl_mult = regime_size_mult = 1.0
            blocked = (adx_val < 25) or is_squeeze

        debug["adx"] = f"{adx_val:.1f} ({regime})"
        debug["bb_width_filter"] = f"{bb_w:.4f} ({'OK' if not is_squeeze else 'SQUEEZE — BLOCKED'})"

        if blocked:
            debug["gate"] = f"BLOCKED — {regime} (ADX={adx_val:.1f}, BBw={bb_w:.4f})"
            return self._gate_blocked(debug, row)

        debug["gate"] = f"PASSED ({regime})"

        # === Pré-calcul tendances multi-TF ===
        # 1m — momentum court-terme (entrée précise)
        confirms_bull_1m = confirms_bear_1m = False
        close_1m = None
        if not df_1m.empty and len(df_1m) >= 5:
            df_1m["EMA9"] = ema(df_1m["close"], 9)
            df_1m["EMA21"] = ema(df_1m["close"], 21)
            row_1m = df_1m.iloc[-1]
            close_1m = float(row_1m["close"])
            if pd.notna(row_1m["EMA9"]) and pd.notna(row_1m["EMA21"]):
                confirms_bull_1m = row_1m["EMA9"] > row_1m["EMA21"]
                confirms_bear_1m = row_1m["EMA9"] < row_1m["EMA21"]

        # 1h — trend filter (gate post-normalisation)
        trend_1h = "neutral"
        if not df_1h.empty and len(df_1h) >= 10:
            df_1h["EMA9"] = ema(df_1h["close"], 9)
            df_1h["EMA21"] = ema(df_1h["close"], 21)
            row_1h = df_1h.iloc[-1]
            if pd.notna(row_1h["EMA9"]) and pd.notna(row_1h["EMA21"]):
                trend_1h = "bull" if row_1h["EMA9"] > row_1h["EMA21"] else "bear"
        debug["trend_1h"] = trend_1h

        # --- 1. EMA Trend + Slope (poids x2 si sain, x1 si décélère) ---
        # Le slope détecte si la tendance est encore fraîche (accélère) ou s'essouffle.
        slope = row["EMA9_slope"] if pd.notna(row["EMA9_slope"]) else 0.0
        ema_bull = row["EMA9"] > row["EMA21"]

        if ema_bull and slope > 0:
            score += 2
            debug["ema_trend"] = f"BULLISH+ACC slope={slope:.4f}% (+2)"
        elif ema_bull:
            score += 1
            debug["ema_trend"] = f"BULLISH+DECL slope={slope:.4f}% (+1)"
        elif not ema_bull and slope < 0:
            score -= 2
            debug["ema_trend"] = f"BEARISH+ACC slope={slope:.4f}% (-2)"
        else:
            score -= 1
            debug["ema_trend"] = f"BEARISH+DECL slope={slope:.4f}% (-1)"

        # --- 2. MACD Momentum (poids x2) ---
        if row["MACD"] > row["MACD_signal"]:
            score += 2
            debug["macd"] = "BULLISH (+2)"
        else:
            score -= 2
            debug["macd"] = "BEARISH (-2)"

        # --- 3. MACD Histogramme — croisement zéro frais (±2) ou continuation (±1) ---
        # Un croisement de zéro = momentum qui vient de changer de camp = signal frais.
        hist_sign_changed = (row["MACD_hist"] > 0) != (prev["MACD_hist"] > 0)
        hist_growing = row["MACD_hist"] > prev["MACD_hist"]

        if hist_sign_changed:
            if row["MACD_hist"] > 0:
                score += 2
                debug["macd_hist"] = f"FRESH BULL CROSS {row['MACD_hist']:.4f} (+2)"
            else:
                score -= 2
                debug["macd_hist"] = f"FRESH BEAR CROSS {row['MACD_hist']:.4f} (-2)"
        elif hist_growing:
            score += 1
            debug["macd_hist"] = f"GROWING {row['MACD_hist']:.4f} (+1)"
        else:
            score -= 1
            debug["macd_hist"] = f"SHRINKING {row['MACD_hist']:.4f} (-1)"

        # --- 4. RSI (poids x1) ---
        rsi_val = row["RSI"]
        if pd.isna(rsi_val):
            rsi_val = 50.0
        if rsi_val > 65:
            score -= 1
            debug["rsi"] = f"OVERBOUGHT {rsi_val:.1f} (-1)"
        elif rsi_val < 35:
            score += 1
            debug["rsi"] = f"OVERSOLD {rsi_val:.1f} (+1)"
        else:
            debug["rsi"] = f"NEUTRAL {rsi_val:.1f} (0)"

        # --- 5. Bollinger %B (poids x1) ---
        bb_pctb = row["BB_pctB"] if pd.notna(row["BB_pctB"]) else 0.5

        if bb_pctb > 0.85 and rsi_val > 55:
            score -= 1
            debug["bb"] = f"OVEREXTENDED %B={bb_pctb:.2f} (-1)"
        elif bb_pctb < 0.15 and rsi_val < 45:
            score += 1
            debug["bb"] = f"OVERSOLD ZONE %B={bb_pctb:.2f} (+1)"
        else:
            debug["bb"] = f"INSIDE %B={bb_pctb:.2f} (0)"

        # --- 6. VWAP (poids x1) ---
        if pd.notna(row["VWAP"]) and row["VWAP"] > 0:
            if row["close"] > row["VWAP"]:
                score += 1
                debug["vwap"] = f"ABOVE {row['VWAP']:.2f} (+1)"
            else:
                score -= 1
                debug["vwap"] = f"BELOW {row['VWAP']:.2f} (-1)"
        else:
            debug["vwap"] = "N/A (0)"

        # --- 7. Volume spike (poids x1) ---
        vol_r = row["vol_ratio"] if pd.notna(row["vol_ratio"]) else 1.0
        if vol_r > 1.8:
            candle_dir = 1 if row["close"] > row["open"] else -1
            score += candle_dir
            debug["volume"] = f"SPIKE x{vol_r:.1f} ({'+' if candle_dir > 0 else ''}{candle_dir})"
        else:
            debug["volume"] = f"NORMAL x{vol_r:.1f} (0)"

        # --- 8. ADX strength bonus (poids x1) ---
        if adx_val >= 30:
            # Tendance forte : bonus dans la direction des DI
            plus_di = row["PLUS_DI"] if pd.notna(row["PLUS_DI"]) else 0
            minus_di = row["MINUS_DI"] if pd.notna(row["MINUS_DI"]) else 0
            if plus_di > minus_di:
                score += 1
                debug["adx_bonus"] = f"STRONG TREND +DI>-DI ({plus_di:.1f}>{minus_di:.1f}) (+1)"
            else:
                score -= 1
                debug["adx_bonus"] = f"STRONG TREND -DI>+DI ({minus_di:.1f}>{plus_di:.1f}) (-1)"
        else:
            debug["adx_bonus"] = f"MODERATE TREND ADX={adx_val:.1f} (0)"

        # --- 9. Momentum 1m (poids x1) — timing précis d'entrée ---
        # La tendance principale est sur 15m ; le 1m précise si le momentum est aligné
        # au moment de l'entrée. Poids réduit (x1) car signal court-terme bruité.
        if confirms_bull_1m:
            score += 1
            debug["momentum_1m"] = "BULLISH (+1)"
        elif confirms_bear_1m:
            score -= 1
            debug["momentum_1m"] = "BEARISH (-1)"
        else:
            debug["momentum_1m"] = "NO DATA (0)"

        # --- 10. Funding rate (poids x1 ou x2, contrarian) ---
        # Logique enrichie avec le slope (tendance sur 30 min) :
        #   - Funding haut ET qui monte  → retournement imminent (-2)
        #   - Funding haut mais qui baisse → surextension qui se corrige (-1)
        #   - Funding bas ET qui baisse  → retournement imminent (+2)
        #   - Funding bas mais qui monte → surextension qui se corrige (+1)
        funding = mkt["funding_rate"]
        funding_slope = mkt["funding_slope"]  # None si < 2 polls
        if funding is not None:
            slope_str = f" slope={funding_slope*100:.5f}%" if funding_slope is not None else ""
            if funding > 0.0002:
                if funding_slope is not None and funding_slope > 0:
                    score -= 2
                    debug["funding"] = f"LONGS SUREXTENDUS+MONTANT {funding*100:.4f}%{slope_str} (-2)"
                else:
                    score -= 1
                    debug["funding"] = f"LONGS SUREXTENDUS {funding*100:.4f}%{slope_str} (-1)"
            elif funding < -0.0002:
                if funding_slope is not None and funding_slope < 0:
                    score += 2
                    debug["funding"] = f"SHORTS SUREXTENDUS+MONTANT {funding*100:.4f}%{slope_str} (+2)"
                else:
                    score += 1
                    debug["funding"] = f"SHORTS SUREXTENDUS {funding*100:.4f}%{slope_str} (+1)"
            else:
                debug["funding"] = f"NEUTRAL {funding*100:.4f}%{slope_str} (0)"
        else:
            debug["funding"] = "NO DATA (0)"

        # --- 11. Open Interest — trend 30 min (poids x1) ---
        # On utilise la variation cumulée 30 min (plus fiable qu'un seul poll).
        # OI croissant dans sens de la tendance = nouveau money entrant → confirmation.
        # OI décroissant = positions se ferment → tendance s'essouffle.
        oi_trend = mkt.get("oi_trend_30m")
        oi_chg = mkt["oi_change_pct"]  # fallback si pas assez de polls
        oi_val = oi_trend if oi_trend is not None else oi_chg
        if oi_val is not None:
            ema_bull = row["EMA9"] > row["EMA21"] if pd.notna(row["EMA9"]) and pd.notna(row["EMA21"]) else None
            src = "30m" if oi_trend is not None else "1poll"
            if abs(oi_val) >= 0.002:   # Variation significative > 0.2%
                if oi_val > 0 and ema_bull is True:
                    score += 1
                    debug["oi"] = f"OI GROWING +{oi_val*100:.3f}% ({src}) BULL (+1)"
                elif oi_val > 0 and ema_bull is False:
                    score -= 1
                    debug["oi"] = f"OI GROWING +{oi_val*100:.3f}% ({src}) BEAR (-1)"
                elif oi_val < 0:
                    # OI décroissant = positions se ferment, tendance s'essouffle
                    if ema_bull is True:
                        score -= 1
                        debug["oi"] = f"OI DECLINING {oi_val*100:.3f}% ({src}) BULL WEAKENING (-1)"
                    elif ema_bull is False:
                        score += 1
                        debug["oi"] = f"OI DECLINING {oi_val*100:.3f}% ({src}) BEAR WEAKENING (+1)"
                    else:
                        debug["oi"] = f"OI DECLINING {oi_val*100:.3f}% ({src}) (0)"
                else:
                    debug["oi"] = f"OI {oi_val*100:.3f}% ({src}) ambigu (0)"
            else:
                debug["oi"] = f"OI STABLE {oi_val*100:.3f}% ({src}) (0)"
        else:
            debug["oi"] = "NO DATA (0)"

        # --- 12. Orderbook imbalance — moyenne 5 min (poids x1) ---
        # On utilise la moyenne des 10 derniers snapshots (5 min) pour filtrer
        # les spikes momentanés. Un mur maintenu 5 min est bien plus significatif.
        imbalance_avg = mkt.get("ob_imbalance_avg")
        imbalance = mkt["ob_imbalance"]
        imb_val = imbalance_avg if imbalance_avg is not None else imbalance
        if imb_val is not None:
            src = "5min_avg" if imbalance_avg is not None else "snapshot"
            if imb_val > 0.20:
                score += 1
                debug["ob_imbalance"] = f"BID WALL {imb_val:.3f} ({src}) (+1)"
            elif imb_val < -0.20:
                score -= 1
                debug["ob_imbalance"] = f"ASK WALL {imb_val:.3f} ({src}) (-1)"
            else:
                debug["ob_imbalance"] = f"BALANCED {imb_val:.3f} ({src}) (0)"
        else:
            debug["ob_imbalance"] = "NO DATA (0)"

        # --- 13. Âge de la tendance EMA sur 15m (anti-entrée tardive) ---
        # Sur 15m : 5 candles = 75 min (trend fraîche), 20 candles = 5h (trend vieille)
        # Ces seuils ont plus de sens que sur 1m (où 20 candles = 20 min seulement).
        ema_dir_series = df_15m["EMA9"] > df_15m["EMA21"]
        current_dir = ema_dir_series.iloc[-1]
        ema_age = 0
        for i in range(len(ema_dir_series) - 1, -1, -1):
            if ema_dir_series.iloc[i] == current_dir:
                ema_age += 1
            else:
                break

        if ema_age > 20:
            age_penalty = -1 if ema_bull else 1   # pénalise dans la direction dominante
            score += age_penalty
            debug["ema_age"] = f"OLD TREND {ema_age}x15m={ema_age*15}min ({age_penalty:+d})"
        elif ema_age <= 5:
            age_bonus = 1 if ema_bull else -1
            score += age_bonus
            debug["ema_age"] = f"FRESH TREND {ema_age}x15m={ema_age*15}min ({age_bonus:+d})"
        else:
            debug["ema_age"] = f"MATURE TREND {ema_age}x15m={ema_age*15}min (0)"

        # === Normalisation [-2, +2] ===
        # score_threshold : dynamique via auto-calibration (défaut SIGNAL_THRESHOLD_DEFAULT)
        # + regime_threshold_adj : +1 si tendance faible (ADX 25-30) → seuil 9 au lieu de 8
        base_threshold = score_threshold if score_threshold is not None else SIGNAL_THRESHOLD_DEFAULT
        threshold = base_threshold + regime_threshold_adj
        debug["regime"] = f"{regime} (ADX={adx_val:.1f}, seuil±2={threshold})"
        if score >= threshold:
            level = 2
        elif score >= 4:
            level = 1
        elif score <= -threshold:
            level = -2
        elif score <= -4:
            level = -1
        else:
            level = 0

        # === Gate 1h : bloquer les trades contre-tendance horaire ===
        if level == 2 and trend_1h == "bear":
            debug["gate_1h"] = "BLOCKED — 1h BEARISH vs signal BULLISH"
            return self._gate_blocked(debug, row)
        elif level == -2 and trend_1h == "bull":
            debug["gate_1h"] = "BLOCKED — 1h BULLISH vs signal BEARISH"
            return self._gate_blocked(debug, row)
        else:
            debug["gate_1h"] = f"OK (trend_1h={trend_1h})"

        # === Gate ML (optionnel — uniquement si modèle entraîné disponible) ===
        # N'intervient que sur les signaux forts (±2) pour éviter de bloquer
        # des signaux modérés qui n'ont pas besoin de validation supplémentaire.
        if self.ml_predictor is not None and level in (2, -2):
            atr_pct_for_ml = (row["ATR"] / row["close"]) if pd.notna(row.get("ATR")) and row["close"] > 0 else 0.005
            ml_features = {
                "rsi_14":        float(rsi_val),
                "adx_14":        float(adx_val),
                "bb_width":      float(bb_w),
                "raw_score":     float(score),
                "signal_level":  float(level),
                "atr_pct":       float(atr_pct_for_ml),
                "funding_rate":  float(funding) if funding is not None else 0.0,
                "ob_imbalance":  float(imbalance) if imbalance is not None else 0.0,
                "oi_change_pct": float(oi_chg) if oi_chg is not None else 0.0,
                "bb_pctB":       float(bb_pctb),
            }
            ml_conf = self.ml_predictor.predict(ml_features)
            if ml_conf < ML_BLOCK_THRESHOLD:
                debug["gate_ml"] = f"BLOCKED — confidence={ml_conf:.3f} < {ML_BLOCK_THRESHOLD}"
                return self._gate_blocked(debug, row)
            elif ml_conf < ML_PENALTY_THRESHOLD:
                score -= 1
                debug["gate_ml"] = f"PENALTY — confidence={ml_conf:.3f} < {ML_PENALTY_THRESHOLD} (-1)"
                # Re-normaliser après pénalité (conserver l'ajustement régime)
                threshold = (score_threshold if score_threshold is not None else SIGNAL_THRESHOLD_DEFAULT) + regime_threshold_adj
                if score >= threshold:
                    level = 2
                elif score >= 4:
                    level = 1
                elif score <= -threshold:
                    level = -2
                elif score <= -4:
                    level = -1
                else:
                    level = 0
            else:
                debug["gate_ml"] = f"OK — confidence={ml_conf:.3f}"
        else:
            debug["gate_ml"] = "N/A (modèle non entraîné)" if self.ml_predictor is None else "N/A (signal ≠ ±2)"

        # === TP/SL dynamiques bases sur ATR ===
        # Ratio risk/reward TOUJOURS >= 1.5 (on gagne plus qu'on perd)
        atr_val = row["ATR"] if pd.notna(row["ATR"]) else None
        # SL = clamp(ATR×1.5, SL/2, SL×2) ; TP = max(SL×2, MIN_TP) — cf. utils.sizing
        dynamic_sl, dynamic_tp = dynamic_sl_tp(atr_val, row["close"], SL_PCT, TP_PCT, MIN_TP_PCT)
        # Phase 4 : adaptation au régime (TP plus large en tendance forte, etc.)
        dynamic_sl *= regime_sl_mult
        dynamic_tp *= regime_tp_mult

        if atr_val and row["close"] > 0:
            atr_pct = atr_val / row["close"]
            raw_sl = atr_pct * 1.5
            debug["atr"] = f"{atr_val:.4f} ({atr_pct*100:.4f}%)"
            debug["dynamic_sl"] = f"{dynamic_sl*100:.3f}% (raw ATR*1.5={raw_sl*100:.4f}%)"
            debug["dynamic_tp"] = f"{dynamic_tp*100:.3f}% (R:R={dynamic_tp/dynamic_sl:.1f}:1)"
        else:
            # Fallback statique avec R:R correct
            debug["atr"] = "N/A"
            debug["dynamic_tp"] = f"{dynamic_tp*100:.3f}% (static fallback)"
            debug["dynamic_sl"] = f"{dynamic_sl*100:.3f}% (static fallback)"

        # === Info supplementaire pour debug ===
        ema9_slp = row["EMA9_slope"] if pd.notna(row["EMA9_slope"]) else 0.0

        # close affiché = 1m si disponible (prix temps-réel), sinon dernière bougie 15m
        display_close = close_1m if close_1m is not None else float(row["close"])

        result = {
            "score": level,
            "raw_score": score,
            "label": LEVELS[level]["label"],
            "color": LEVELS[level]["color"],
            "dynamic_tp": dynamic_tp,
            "dynamic_sl": dynamic_sl,
            "trend_1h": trend_1h,
            "trend_1m": "bull" if confirms_bull_1m else ("bear" if confirms_bear_1m else "neutral"),
            "regime": regime,
            "regime_size_mult": regime_size_mult,
            "ml_confidence": float(debug.get("gate_ml", "N/A").split("=")[-1].split(" ")[0])
                             if "confidence=" in debug.get("gate_ml", "") else None,
            "is_squeeze": is_squeeze,
            "debug": {
                **debug,
                "close": display_close,
                "close_15m": float(row["close"]),
                "EMA9": float(row["EMA9"]) if pd.notna(row["EMA9"]) else None,
                "EMA21": float(row["EMA21"]) if pd.notna(row["EMA21"]) else None,
                "RSI": float(rsi_val),
                "MACD": float(row["MACD"]) if pd.notna(row["MACD"]) else None,
                "MACD_signal": float(row["MACD_signal"]) if pd.notna(row["MACD_signal"]) else None,
                "BB_upper": float(row["BB_upper"]) if pd.notna(row["BB_upper"]) else None,
                "BB_lower": float(row["BB_lower"]) if pd.notna(row["BB_lower"]) else None,
                "BB_pctB": float(bb_pctb),
                "BB_width": float(bb_w),
                "VWAP": float(row["VWAP"]) if pd.notna(row["VWAP"]) else None,
                "ATR": float(atr_val) if atr_val else None,
                "atr_pct": float(atr_pct) if atr_val and row["close"] > 0 else 0.001,
                "candle_range_pct": (
                    float((row["high"] - row["low"]) / row["close"])
                    if pd.notna(row.get("high")) and pd.notna(row.get("low")) and row["close"] else None
                ),
                "vol_ratio": float(vol_r),
                "EMA9_slope": float(ema9_slp),
                "funding_rate": float(funding) if funding is not None else None,
                "oi_change_pct": float(oi_chg) if oi_chg is not None else None,
                "ob_imbalance": float(imbalance) if imbalance is not None else None,
                "spread_pct": mkt.get("spread_pct"),         # circuit breaker (Axe B)
                "ob_depth_ratio": mkt.get("ob_depth_ratio"), # liquidité (Axe B)
            }
        }

        # Log signal
        self.logger.log_signal({
            "timestamp": int(row.get("timestamp", datetime.utcnow().timestamp() * 1000)),
            "minute": row.get("minute", datetime.utcnow().strftime("%Y-%m-%d %H:%M")),
            "coin": self.coin,
            "interval": "15m",
            "score": result["raw_score"],
            "signal_level": result["score"],
            "raw_score": result["raw_score"],
            "label": result["label"],
            "color": result["color"],
            "rsi_14": round(float(rsi_val), 2),
            "adx_14": round(float(adx_val), 2),
            "bb_upper": round(float(row["BB_upper"]), 4) if pd.notna(row.get("BB_upper")) else None,
            "bb_lower": round(float(row["BB_lower"]), 4) if pd.notna(row.get("BB_lower")) else None,
            "bb_width": round(float(bb_w), 6),
            "gate_passed": True,
            "debug": result["debug"]
        })

        return result

    def _gate_blocked(self, debug, row):
        """Retourne un signal neutre quand un filtre gate bloque."""
        rsi_val = row["RSI"] if pd.notna(row["RSI"]) else 50.0
        atr_val = row["ATR"] if pd.notna(row["ATR"]) else None
        adx_v = float(row["ADX"]) if pd.notna(row.get("ADX")) else 0.0
        bb_w_v = float(row["BB_width"]) if pd.notna(row.get("BB_width")) else 0.0
        self.logger.log_signal({
            "timestamp": int(row.get("timestamp", datetime.utcnow().timestamp() * 1000)),
            "minute": row.get("minute", datetime.utcnow().strftime("%Y-%m-%d %H:%M")),
            "coin": self.coin,
            "interval": "15m",
            "score": 0,
            "signal_level": 0,
            "raw_score": 0,
            "label": LEVELS[0]["label"],
            "color": LEVELS[0]["color"],
            "rsi_14": round(float(rsi_val), 2),
            "adx_14": round(adx_v, 2),
            "bb_upper": round(float(row["BB_upper"]), 4) if pd.notna(row.get("BB_upper")) else None,
            "bb_lower": round(float(row["BB_lower"]), 4) if pd.notna(row.get("BB_lower")) else None,
            "bb_width": round(bb_w_v, 6),
            "gate_passed": False,
            "debug": {**debug, "close": float(row["close"]), "RSI": float(rsi_val), "ATR": float(atr_val) if atr_val else None}
        })
        return {
            "score": 0,
            "raw_score": 0,
            "label": LEVELS[0]["label"],
            "color": LEVELS[0]["color"],
            "dynamic_tp": None,
            "dynamic_sl": None,
            "is_squeeze": debug.get("bb_width_filter", "").endswith("BLOCKED"),
            "debug": {
                **debug,
                "close": float(row["close"]),
                "RSI": float(rsi_val),
                "ATR": float(atr_val) if atr_val else None,
            }
        }

    def _neutral(self, reason=""):
        if DEBUG:
            print(f"[STRATEGY] Signal neutre : {reason}")
        return {
            "score": 0,
            "raw_score": 0,
            "label": LEVELS[0]["label"],
            "color": LEVELS[0]["color"],
            "dynamic_tp": None,
            "dynamic_sl": None,
            "is_squeeze": False,
            "debug": {"reason": reason}
        }


if __name__ == "__main__":
    engine = StrategyEngine(coin="BTC")
    result = engine.compute_signals()
    print(f"\nScore: {result['score']} (raw: {result['raw_score']}) | {result['label']} {result['color']}")
    if result.get("dynamic_tp"):
        rr = result["dynamic_tp"] / result["dynamic_sl"] if result["dynamic_sl"] else 0
        print(f"TP: {result['dynamic_tp']*100:.3f}% | SL: {result['dynamic_sl']*100:.3f}% | R:R = {rr:.1f}:1")
    if result.get("is_squeeze"):
        print("⚡ BOLLINGER SQUEEZE DETECTED (15m)")
    print(f"Trend 1h : {result.get('trend_1h', 'N/A')}")
    print(f"Trend 1m : {result.get('trend_1m', 'N/A')}")
    print()
    for k, v in result["debug"].items():
        print(f"  {k}: {v}")
