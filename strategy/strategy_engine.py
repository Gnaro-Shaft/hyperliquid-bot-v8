import pandas as pd
from pymongo import MongoClient
from datetime import datetime

from config import (
    MONGO_URL, MONGO_DB, MONGO_COLLECTION_1M, MONGO_COLLECTION_15M,
    LEVELS, SL_PCT, TP_PCT, MIN_TP_PCT, DEBUG
)
from strategy.indicators import (
    ema, rsi, macd, bollinger_bands, vwap, atr,
    bb_width, bb_percent_b, volume_ratio, ema_slope, adx
)
from utils.logger import Logger


class StrategyEngine:
    def __init__(self, coin="BTC"):
        client = MongoClient(MONGO_URL)
        self.mongo = client[MONGO_DB]
        self.coin = coin
        self.logger = Logger(collection="signals")

    def get_last_n_candles(self, n=100, tf="1m"):
        col = MONGO_COLLECTION_1M if tf == "1m" else MONGO_COLLECTION_15M
        cursor = self.mongo[col].find({"coin": self.coin}).sort("timestamp", -1).limit(n)
        data = list(cursor)
        if not data:
            return pd.DataFrame()
        df = pd.DataFrame(reversed(data))
        for c in ["open", "high", "low", "close", "volume"]:
            df[c] = df[c].astype(float)
        return df

    def compute_signals(self):
        """Scoring pondere multi-timeframe v8.1 — anti-chop ameliore.

        Filtres GATE (bloquent le signal si non remplis) :
          1. ADX > 20  → tendance confirmee (sinon = range/chop)
          2. BB width > 0.003 → volatilite suffisante (sinon = squeeze)

        Poids scoring :
          EMA trend 1m         x2    (direction)
          MACD momentum        x2    (force)
          MACD histogram dir.  x1    (acceleration)
          RSI zones            x1    (extremes)
          Bollinger %B         x1    (position relative)
          VWAP                 x1    (biais institutionnel)
          Volume spike         x1    (confirmation)
          ADX force            x1    (bonus si tendance forte)
          Confirmation 15m     x2    (multi-timeframe)
        ------------------------------------------
        Total possible : +/-12, normalise en 5 niveaux [-2, -1, 0, 1, 2]
        Seuil de trade : level ±2 (raw >= 7 ou <= -7)
        """
        df_1m = self.get_last_n_candles(100, "1m")
        df_15m = self.get_last_n_candles(50, "15m")

        if df_1m.empty or len(df_1m) < 40:
            return self._neutral(f"Pas assez de donnees 1m ({len(df_1m)}/40)")

        # === Indicateurs 1m ===
        df_1m["EMA9"] = ema(df_1m["close"], 9)
        df_1m["EMA21"] = ema(df_1m["close"], 21)
        df_1m["RSI"] = rsi(df_1m["close"], 14)
        df_1m["MACD"], df_1m["MACD_signal"], df_1m["MACD_hist"] = macd(df_1m["close"])
        df_1m["BB_upper"], df_1m["BB_mid"], df_1m["BB_lower"] = bollinger_bands(df_1m["close"])
        df_1m["VWAP"] = vwap(df_1m)
        df_1m["ATR"] = atr(df_1m)
        df_1m["BB_pctB"] = bb_percent_b(df_1m["close"], df_1m["BB_upper"], df_1m["BB_lower"])
        df_1m["BB_width"] = bb_width(df_1m["BB_upper"], df_1m["BB_lower"], df_1m["BB_mid"])
        df_1m["vol_ratio"] = volume_ratio(df_1m["volume"])
        df_1m["EMA9_slope"] = ema_slope(df_1m["EMA9"], 3)
        df_1m["ADX"], df_1m["PLUS_DI"], df_1m["MINUS_DI"] = adx(df_1m)

        row = df_1m.iloc[-1]
        prev = df_1m.iloc[-2]

        score = 0
        debug = {}

        # === FILTRES GATE (anti-chop) ===
        adx_val = row["ADX"] if pd.notna(row["ADX"]) else 0.0
        bb_w = row["BB_width"] if pd.notna(row["BB_width"]) else 0.0
        is_squeeze = bb_w < 0.004
        is_trending = adx_val >= 25

        debug["adx"] = f"{adx_val:.1f} ({'TREND' if is_trending else 'RANGE/CHOP'})"
        debug["bb_width_filter"] = f"{bb_w:.4f} ({'OK' if not is_squeeze else 'SQUEEZE — BLOCKED'})"

        if not is_trending:
            debug["gate"] = f"BLOCKED — ADX={adx_val:.1f} < 20 (range/chop)"
            return self._gate_blocked(debug, row)

        if is_squeeze:
            debug["gate"] = f"BLOCKED — BB width={bb_w:.4f} < 0.003 (squeeze)"
            return self._gate_blocked(debug, row)

        debug["gate"] = "PASSED"

        # --- 1. EMA Trend (poids x2) ---
        if row["EMA9"] > row["EMA21"]:
            score += 2
            debug["ema_trend"] = "BULLISH (+2)"
        else:
            score -= 2
            debug["ema_trend"] = "BEARISH (-2)"

        # --- 2. MACD Momentum (poids x2) ---
        if row["MACD"] > row["MACD_signal"]:
            score += 2
            debug["macd"] = "BULLISH (+2)"
        else:
            score -= 2
            debug["macd"] = "BEARISH (-2)"

        # --- 3. MACD Histogramme direction (poids x1) ---
        if row["MACD_hist"] > prev["MACD_hist"]:
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

        # --- 9. Confirmation 15m (poids x2) ---
        if not df_15m.empty and len(df_15m) >= 21:
            df_15m["EMA9"] = ema(df_15m["close"], 9)
            df_15m["EMA21"] = ema(df_15m["close"], 21)
            df_15m["RSI"] = rsi(df_15m["close"], 14)
            row_15m = df_15m.iloc[-1]
            rsi_15m = row_15m["RSI"] if pd.notna(row_15m["RSI"]) else 50.0

            confirms_bull = row_15m["EMA9"] > row_15m["EMA21"] and rsi_15m > 45
            confirms_bear = row_15m["EMA9"] < row_15m["EMA21"] and rsi_15m < 55

            if confirms_bull:
                score += 2
                debug["confirm_15m"] = f"BULLISH (RSI={rsi_15m:.1f}) (+2)"
            elif confirms_bear:
                score -= 2
                debug["confirm_15m"] = f"BEARISH (RSI={rsi_15m:.1f}) (-2)"
            else:
                debug["confirm_15m"] = f"MIXED (RSI={rsi_15m:.1f}) (0)"
        else:
            debug["confirm_15m"] = f"NO DATA ({len(df_15m)} candles) (0)"

        # === Normalisation [-2, +2] ===
        # Score possible : -12 a +12 — seuils TRES stricts pour reduire le bruit
        if score >= 8:
            level = 2
        elif score >= 4:
            level = 1
        elif score <= -8:
            level = -2
        elif score <= -4:
            level = -1
        else:
            level = 0

        # === TP/SL dynamiques bases sur ATR ===
        # Ratio risk/reward TOUJOURS >= 1.5 (on gagne plus qu'on perd)
        atr_val = row["ATR"] if pd.notna(row["ATR"]) else None
        dynamic_tp = TP_PCT
        dynamic_sl = SL_PCT

        if atr_val and row["close"] > 0:
            atr_pct = atr_val / row["close"]

            # SL = 1.5x ATR (assez de marge pour ne pas se faire sortir par le bruit)
            raw_sl = atr_pct * 1.5
            dynamic_sl = max(raw_sl, SL_PCT * 0.5)  # plancher = demi SL config
            dynamic_sl = min(dynamic_sl, SL_PCT * 2)  # plafond = 2x SL config

            # TP = SL * 2.0 (ratio R:R minimum de 2:1)
            dynamic_tp = dynamic_sl * 2.0
            dynamic_tp = max(dynamic_tp, MIN_TP_PCT)  # plancher = 0.8%

            debug["atr"] = f"{atr_val:.4f} ({atr_pct*100:.4f}%)"
            debug["dynamic_sl"] = f"{dynamic_sl*100:.3f}% (raw ATR*1.5={raw_sl*100:.4f}%)"
            debug["dynamic_tp"] = f"{dynamic_tp*100:.3f}% (R:R={dynamic_tp/dynamic_sl:.1f}:1)"
        else:
            # Fallback statique avec R:R correct
            dynamic_tp = max(TP_PCT, SL_PCT * 1.5)
            debug["atr"] = "N/A"
            debug["dynamic_tp"] = f"{dynamic_tp*100:.3f}% (static fallback)"
            debug["dynamic_sl"] = f"{dynamic_sl*100:.3f}% (static fallback)"

        # === Info supplementaire pour debug ===
        ema9_slp = row["EMA9_slope"] if pd.notna(row["EMA9_slope"]) else 0.0

        result = {
            "score": level,
            "raw_score": score,
            "label": LEVELS[level]["label"],
            "color": LEVELS[level]["color"],
            "dynamic_tp": dynamic_tp,
            "dynamic_sl": dynamic_sl,
            "is_squeeze": is_squeeze,
            "debug": {
                **debug,
                "close": float(row["close"]),
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
                "vol_ratio": float(vol_r),
                "EMA9_slope": float(ema9_slp),
            }
        }

        # Log signal
        self.logger.log_signal({
            "timestamp": int(row.get("timestamp", datetime.utcnow().timestamp() * 1000)),
            "minute": row.get("minute", datetime.utcnow().strftime("%Y-%m-%d %H:%M")),
            "coin": self.coin,
            "interval": "1m",
            "score": result["score"],
            "raw_score": result["raw_score"],
            "label": result["label"],
            "color": result["color"],
            "debug": result["debug"]
        })

        return result

    def _gate_blocked(self, debug, row):
        """Retourne un signal neutre quand un filtre gate bloque."""
        rsi_val = row["RSI"] if pd.notna(row["RSI"]) else 50.0
        atr_val = row["ATR"] if pd.notna(row["ATR"]) else None
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
        print("⚡ BOLLINGER SQUEEZE DETECTED")
    print()
    for k, v in result["debug"].items():
        print(f"  {k}: {v}")
