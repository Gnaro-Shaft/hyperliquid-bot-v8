#!/usr/bin/env python3
"""
Backtesting sur données MongoDB — Bot Hyperliquid v8
=====================================================
Simule la stratégie complète sur l'historique OHLC stocké en base.

Usage:
    python backtest/backtest.py --coin BTC
    python backtest/backtest.py --coin SOL --days 30
    python backtest/backtest.py --coin BTC --from 2025-01-01 --to 2025-03-01
    python backtest/backtest.py --coin BTC --days 30 --export
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import argparse
import csv
import math
from datetime import datetime, timezone
from collections import defaultdict

import pandas as pd
from pymongo import MongoClient

from config import (
    MONGO_URL, MONGO_DB,
    MONGO_COLLECTION_1M, MONGO_COLLECTION_15M, MONGO_COLLECTION_1H,
    MONGO_COLLECTION_FUNDING, MONGO_COLLECTION_OI, MONGO_COLLECTION_ORDERBOOK,
    TP_PCT, SL_PCT, MIN_TP_PCT, POSITION_SIZE_PCT, RESERVE_BALANCE_PCT,
    SIGNAL_CONFIRM_COUNT, COOLDOWN_BASE_SEC,
    TRAIL_PCT, TRAILING_TRIGGER_PCT, TRAILING_STEP_PCT,
    BREAKEVEN_TRIGGER_PCT, BREAKEVEN_OFFSET_PCT,
    BT_SLIPPAGE_PCT, BT_SPREAD_PCT,
)
from strategy.strategy_engine import StrategyEngine
from utils.execution import execution_price
from utils.min_order import min_target_size

MIN_ORDER_USDC = 10   # minimum notionnel Hyperliquid (modélisé dans le backtest)


# ──────────────────────────────────────────────────────────────
# MockLogger — évite toute écriture MongoDB pendant le backtest
# ──────────────────────────────────────────────────────────────

class MockLogger:
    def log_signal(self, *args, **kwargs): pass
    def log_trade(self, *args, **kwargs): pass


# ──────────────────────────────────────────────────────────────
# BacktestEngine — injecte les DataFrames dans StrategyEngine
# ──────────────────────────────────────────────────────────────

class BacktestEngine(StrategyEngine):
    """
    Sous-classe de StrategyEngine qui lit des DataFrames en mémoire
    au lieu de requêter MongoDB — rejoue la stratégie bougie par bougie.
    """

    def __init__(self, coin, df_1m, df_15m, df_1h=None, df_funding=None, df_oi=None, df_ob=None):
        self.coin = coin
        self._df_1m_full = df_1m.reset_index(drop=True)
        self._df_15m_full = df_15m.reset_index(drop=True)
        self._df_1h_full = df_1h.sort_values("timestamp").reset_index(drop=True) if df_1h is not None and not df_1h.empty else pd.DataFrame()
        self._current_idx = 750   # Warmup : 750 bougies 1m = 50 bougies 15m minimum
        self.logger = MockLogger()
        self.mongo = None
        self.ml_predictor = None   # Pas de ML en backtest

        # Données marché historiques (triées par timestamp pour bisect)
        self._df_funding = df_funding.sort_values("timestamp").reset_index(drop=True) if df_funding is not None and not df_funding.empty else None
        self._df_oi = df_oi.sort_values("timestamp").reset_index(drop=True) if df_oi is not None and not df_oi.empty else None
        self._df_ob = df_ob.sort_values("timestamp").reset_index(drop=True) if df_ob is not None and not df_ob.empty else None

    def get_last_n_candles(self, n=100, tf="1m"):
        current_ts = self._df_1m_full.iloc[self._current_idx]["timestamp"]
        if tf == "1m":
            end = self._current_idx + 1
            start = max(0, end - n)
            return self._df_1m_full.iloc[start:end].copy()
        elif tf == "15m":
            mask = self._df_15m_full["timestamp"] <= current_ts
            return self._df_15m_full[mask].tail(n).copy()
        else:  # "1h"
            if self._df_1h_full.empty:
                return pd.DataFrame()
            mask = self._df_1h_full["timestamp"] <= current_ts
            return self._df_1h_full[mask].tail(n).copy()

    def get_market_context(self):
        """Récupère le contexte marché enrichi (slope, trend 30m, avg 5m) avant le timestamp courant."""
        current_ts = int(self._df_1m_full.iloc[self._current_idx]["timestamp"])
        ctx = {
            "funding_rate": None, "funding_slope": None,
            "oi_change_pct": None, "oi_trend_30m": None,
            "ob_imbalance": None, "ob_imbalance_avg": None,
        }

        if self._df_funding is not None:
            recent = self._df_funding[self._df_funding["timestamp"] <= current_ts].tail(6)
            if not recent.empty:
                ctx["funding_rate"] = float(recent.iloc[-1].get("funding_rate", 0))
                if len(recent) >= 2:
                    rates = recent["funding_rate"].astype(float).tolist()
                    ctx["funding_slope"] = rates[-1] - rates[0]

        if self._df_oi is not None:
            recent = self._df_oi[self._df_oi["timestamp"] <= current_ts].tail(6)
            if not recent.empty:
                ctx["oi_change_pct"] = float(recent.iloc[-1].get("oi_change_pct", 0))
                if len(recent) >= 2 and "open_interest" in recent.columns:
                    oi_last  = float(recent.iloc[-1].get("open_interest", 0))
                    oi_first = float(recent.iloc[0].get("open_interest", 0))
                    if oi_first > 0:
                        ctx["oi_trend_30m"] = (oi_last - oi_first) / oi_first

        if self._df_ob is not None:
            recent = self._df_ob[self._df_ob["timestamp"] <= current_ts].tail(10)
            if not recent.empty:
                ctx["ob_imbalance"] = float(recent.iloc[-1].get("imbalance", 0))
                if len(recent) >= 3 and "imbalance" in recent.columns:
                    vals = recent["imbalance"].astype(float).tolist()
                    ctx["ob_imbalance_avg"] = round(sum(vals) / len(vals), 4)

        return ctx

    def advance(self):
        self._current_idx += 1
        return self._current_idx < len(self._df_1m_full)

    @property
    def current_candle(self):
        return self._df_1m_full.iloc[self._current_idx]


# ──────────────────────────────────────────────────────────────
# Backtester — moteur de simulation
# ──────────────────────────────────────────────────────────────

class Backtester:
    def __init__(self, coin, df_1m, df_15m, df_1h=None, df_funding=None, df_oi=None, df_ob=None,
                 initial_equity=1000.0):
        self.engine = BacktestEngine(coin, df_1m, df_15m, df_1h, df_funding, df_oi, df_ob)
        self.coin = coin
        self.initial_equity = initial_equity
        self.equity = initial_equity

        self.position = None
        self.trades = []
        self.skipped_min = 0   # trades ignorés car sous le minimum d'ordre (solde insuffisant)
        self.equity_curve = []

        self._signal_streak = 0
        self._last_signal_dir = 0
        self._last_trade_ts = 0
        self._reverse_streak = 0      # Confirmation avant signal_reverse

        # Stats de diagnostic
        self.score_distribution = defaultdict(int)   # {raw_score: count}
        self.gate_blocks = {"adx": 0, "squeeze": 0, "no_data": 0, "1h": 0}
        self.candles_processed = 0

    # ── Boucle principale ──────────────────────────────────────

    def run(self):
        total = len(self.engine._df_1m_full)
        warmup = self.engine._current_idx
        print(f"\n[BACKTEST] {self.coin} | {total - warmup} bougies simulées "
              f"({warmup} warmup) | Solde initial: {self.initial_equity:.2f} USDC")

        while True:
            candle = self.engine.current_candle

            try:
                sig = self.engine.compute_signals()
            except Exception:
                sig = {"score": 0, "raw_score": 0, "dynamic_tp": None, "dynamic_sl": None,
                       "debug": {"close": float(candle["close"]), "atr_pct": 0.002}}

            self.candles_processed += 1

            # Diagnostic : distribution des scores et gates
            raw = sig.get("raw_score", 0)
            self.score_distribution[raw] += 1
            dbg = sig.get("debug", {})
            gate    = dbg.get("gate", "")
            gate_1h = dbg.get("gate_1h", "")
            if "ADX" in gate and "BLOCKED" in gate:
                self.gate_blocks["adx"] += 1
            elif "BB width" in gate and "BLOCKED" in gate:
                self.gate_blocks["squeeze"] += 1
            elif dbg.get("reason", "").startswith("Pas assez"):
                self.gate_blocks["no_data"] += 1
            elif "BLOCKED" in gate_1h:
                self.gate_blocks["1h"] += 1

            close = float(candle["close"])

            # 1. Gérer la position ouverte
            if self.position:
                self._manage_position(candle, sig)

            # 2. Ouvrir une position si aucune
            if not self.position:
                self._try_open(sig, candle)

            self.equity_curve.append({
                "timestamp": int(candle["timestamp"]),
                "equity": round(self.equity, 4),
            })

            if not self.engine.advance():
                break

        # Fermer position résiduelle
        if self.position:
            last = self.engine._df_1m_full.iloc[-1]
            self._close_position(float(last["close"]), "end_of_data", int(last["timestamp"]))

        return self._summary()

    # ── Gestion de position ────────────────────────────────────

    def _try_open(self, sig, candle):
        if sig["score"] not in (2, -2):
            self._signal_streak = 0
            self._last_signal_dir = 0
            return

        if sig["score"] == self._last_signal_dir:
            self._signal_streak += 1
        else:
            self._signal_streak = 1
            self._last_signal_dir = sig["score"]

        if self._signal_streak < SIGNAL_CONFIRM_COUNT:
            return

        ts = int(candle["timestamp"])
        if ts - self._last_trade_ts < COOLDOWN_BASE_SEC * 1000:
            return

        self._signal_streak = 0
        self._reverse_streak = 0

        side = "buy" if sig["score"] == 2 else "sell"
        # Exécution réaliste : entrée à un prix adverse (slippage + demi-spread)
        entry = execution_price(float(candle["close"]), side, True,
                                BT_SLIPPAGE_PCT, BT_SPREAD_PCT)

        sl_pct  = sig.get("dynamic_sl") or SL_PCT
        tp_pct  = sig.get("dynamic_tp") or TP_PCT
        atr_pct = sig.get("debug", {}).get("atr_pct", SL_PCT)

        # Taille ATR-based (risque réduit si volatilité élevée) × régime (Phase 4)
        vol_factor = max(0.3, min(1.0, SL_PCT / sl_pct)) if sl_pct > 0 else 1.0
        regime_mult = sig.get("regime_size_mult", 1.0)
        usable = self.equity * (1 - RESERVE_BALANCE_PCT)
        size   = (usable * POSITION_SIZE_PCT * vol_factor * regime_mult) / entry

        # Minimum d'ordre (comme le trader live) : remonte au min si possible, sinon skip.
        min_target = min_target_size(MIN_ORDER_USDC, entry)        # +20% de marge
        if size < min_target:
            full = (usable * POSITION_SIZE_PCT) / entry            # taille pleine (sans factors)
            if full >= min_target:
                size = min_target                                  # remonté au minimum
            else:
                self.skipped_min += 1                              # solde insuffisant → trade ignoré
                return

        if side == "buy":
            tp_price = round(entry * (1 + tp_pct), 4)
            sl_price = round(entry * (1 - sl_pct), 4)
        else:
            tp_price = round(entry * (1 - tp_pct), 4)
            sl_price = round(entry * (1 + sl_pct), 4)

        # Paramètres trailing stop dynamiques (même logique que main.py)
        trail_distance = max(atr_pct * 1.5, TRAIL_PCT)
        trail_trigger  = max(atr_pct * 2.0, TRAILING_TRIGGER_PCT)

        self.position = {
            "side": side,
            "entry": entry,
            "tp_price": tp_price,
            "sl_price": sl_price,
            "size": size,
            "open_ts": ts,
            "regime": sig.get("regime"),   # régime au moment de l'entrée (analyse Axe A)
            # Trailing stop
            "trail_distance": trail_distance,
            "trail_trigger":  trail_trigger,
            "trail_active":   False,
            "trail_sl":       None,
            # Breakeven
            "breakeven_done": False,
        }

    def _manage_position(self, candle, sig):
        high  = float(candle["high"])
        low   = float(candle["low"])
        close = float(candle["close"])
        ts    = int(candle["timestamp"])

        pos  = self.position
        side = pos["side"]
        entry = pos["entry"]

        # ── Breakeven ────────────────────────────────────────────
        if not pos["breakeven_done"]:
            profit_pct = (close - entry) / entry if side == "buy" else (entry - close) / entry
            if profit_pct >= BREAKEVEN_TRIGGER_PCT:
                if side == "buy":
                    be_sl = entry * (1 + BREAKEVEN_OFFSET_PCT)
                    if be_sl > pos["sl_price"]:
                        pos["sl_price"] = be_sl
                        pos["breakeven_done"] = True
                else:
                    be_sl = entry * (1 - BREAKEVEN_OFFSET_PCT)
                    if be_sl < pos["sl_price"]:
                        pos["sl_price"] = be_sl
                        pos["breakeven_done"] = True

        # ── Trailing stop ─────────────────────────────────────────
        profit_pct = (close - entry) / entry if side == "buy" else (entry - close) / entry
        if not pos["trail_active"] and profit_pct >= pos["trail_trigger"]:
            pos["trail_active"] = True
            if side == "buy":
                pos["trail_sl"] = close * (1 - pos["trail_distance"])
            else:
                pos["trail_sl"] = close * (1 + pos["trail_distance"])

        if pos["trail_active"]:
            if side == "buy":
                new_trail = close * (1 - pos["trail_distance"])
                if new_trail > pos["trail_sl"]:
                    pos["trail_sl"] = new_trail
                # Trailing SL surpasse le SL statique → on prend le meilleur
                if pos["trail_sl"] > pos["sl_price"]:
                    pos["sl_price"] = pos["trail_sl"]
            else:
                new_trail = close * (1 + pos["trail_distance"])
                if new_trail < pos["trail_sl"]:
                    pos["trail_sl"] = new_trail
                if pos["trail_sl"] < pos["sl_price"]:
                    pos["sl_price"] = pos["trail_sl"]

        # ── Signal reverse avec confirmation ─────────────────────
        tp = pos["tp_price"]
        sl = pos["sl_price"]
        is_opposite = (side == "buy" and sig["score"] == -2) or \
                      (side == "sell" and sig["score"] == 2)
        if is_opposite:
            self._reverse_streak += 1
        else:
            self._reverse_streak = 0
        reverse_confirmed = self._reverse_streak >= SIGNAL_CONFIRM_COUNT

        # ── Vérification SL / TP (SL prioritaire dans la même bougie) ──
        if side == "buy":
            if low <= sl:
                self._reverse_streak = 0
                self._close_position(sl, "sl", ts)
            elif high >= tp:
                self._reverse_streak = 0
                self._close_position(tp, "tp", ts)
            elif reverse_confirmed:
                self._reverse_streak = 0
                self._close_position(close, "signal_reverse", ts)
        else:  # sell
            if high >= sl:
                self._reverse_streak = 0
                self._close_position(sl, "sl", ts)
            elif low <= tp:
                self._reverse_streak = 0
                self._close_position(tp, "tp", ts)
            elif reverse_confirmed:
                self._reverse_streak = 0
                self._close_position(close, "signal_reverse", ts)

    def _close_position(self, exit_price, reason, ts=None):
        pos = self.position
        if not pos:
            return

        side = pos["side"]
        entry = pos["entry"]
        size = pos["size"]

        # Exécution réaliste : sortie à un prix adverse (slippage + demi-spread)
        exit_price = execution_price(exit_price, side, False,
                                     BT_SLIPPAGE_PCT, BT_SPREAD_PCT)

        pnl = (exit_price - entry) * size if side == "buy" else (entry - exit_price) * size

        # Frais Hyperliquid : ~0.05% taker par leg = 0.1% aller-retour sur notionnel
        fee = (size * entry + size * exit_price) * 0.0005
        pnl -= fee

        pnl_pct = (pnl / (size * entry)) * 100

        self.equity += pnl
        self._last_trade_ts = ts or 0

        duration_min = ((ts or 0) - pos["open_ts"]) // 60000 if ts else 0

        open_dt = datetime.fromtimestamp(pos["open_ts"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        close_dt = datetime.fromtimestamp((ts or 0) / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M") if ts else "—"

        self.trades.append({
            "open_time": open_dt,
            "close_time": close_dt,
            "duration_min": duration_min,
            "side": side,
            "entry": entry,
            "exit": exit_price,
            "size": round(size, 6),
            "pnl": round(pnl, 4),
            "pnl_pct": round(pnl_pct, 3),
            "reason": reason,
            "regime": pos.get("regime"),
            "equity_after": round(self.equity, 4),
        })
        self.position = None

    # ── Métriques ─────────────────────────────────────────────

    def _summary(self):
        # Diagnostic gates
        total_c = self.candles_processed or 1
        blocked_adx = self.gate_blocks["adx"]
        blocked_sq  = self.gate_blocks["squeeze"]
        blocked_nd  = self.gate_blocks["no_data"]
        blocked_1h  = self.gate_blocks["1h"]
        passed = total_c - blocked_adx - blocked_sq - blocked_nd - blocked_1h
        print(f"\n[DIAGNOSTIC] {total_c} bougies 1m traitées :")
        print(f"  Gate ADX bloqué   : {blocked_adx} ({blocked_adx/total_c*100:.1f}%)")
        print(f"  Gate Squeeze blq  : {blocked_sq} ({blocked_sq/total_c*100:.1f}%)")
        print(f"  Gate 1h bloqué    : {blocked_1h} ({blocked_1h/total_c*100:.1f}%)")
        print(f"  Données manquantes: {blocked_nd} ({blocked_nd/total_c*100:.1f}%)")
        print(f"  Scoring effectué  : {passed} ({passed/total_c*100:.1f}%)")
        print(f"  [Frais simulés: 0.1% aller-retour | Trailing: {TRAIL_PCT*100:.2f}% dist / "
              f"{TRAILING_TRIGGER_PCT*100:.2f}% déclenchement | BE: {BREAKEVEN_TRIGGER_PCT*100:.2f}%]")

        # Distribution des scores (seulement les bougies scorées)
        scored = {k: v for k, v in self.score_distribution.items() if k != 0}
        if scored:
            print(f"\n[DIAGNOSTIC] Distribution raw scores (bougies scorées) :")
            for s in sorted(scored.keys()):
                bar = "█" * min(40, int(scored[s] / max(scored.values()) * 40))
                print(f"  {s:+3d} : {bar} {scored[s]}")
        n2_bull = self.score_distribution.get(10, 0) + self.score_distribution.get(11, 0) + \
                  self.score_distribution.get(12, 0) + self.score_distribution.get(13, 0) + \
                  self.score_distribution.get(14, 0) + self.score_distribution.get(15, 0)
        n2_bear = sum(self.score_distribution.get(k, 0) for k in range(-15, -9))
        print(f"\n  → Signaux FORT HAUSSIER (raw≥10) : {n2_bull}")
        print(f"  → Signaux FORT BAISSIER (raw≤-10): {n2_bear}")

        trades = self.trades
        n = len(trades)
        if n == 0:
            print("\n[BACKTEST] Aucun trade généré avec les paramètres actuels.")
            return {}

        wins = [t for t in trades if t["pnl"] > 0]
        losses = [t for t in trades if t["pnl"] <= 0]
        total_pnl = sum(t["pnl"] for t in trades)
        total_pnl_pct = ((self.equity - self.initial_equity) / self.initial_equity) * 100
        win_rate = len(wins) / n * 100
        gross_profit = sum(t["pnl"] for t in wins)
        gross_loss = abs(sum(t["pnl"] for t in losses))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")
        avg_win = gross_profit / len(wins) if wins else 0
        avg_loss = gross_loss / len(losses) if losses else 0
        rr_ratio = avg_win / avg_loss if avg_loss > 0 else float("inf")

        # Max drawdown
        peak = self.initial_equity
        max_dd = 0.0
        for point in self.equity_curve:
            eq = point["equity"]
            if eq > peak:
                peak = eq
            dd = (peak - eq) / peak * 100
            if dd > max_dd:
                max_dd = dd

        avg_dur = sum(t["duration_min"] for t in trades) / n

        # Sharpe simplifié
        pnls = [t["pnl"] for t in trades]
        mean_pnl = sum(pnls) / len(pnls)
        variance = sum((p - mean_pnl) ** 2 for p in pnls) / len(pnls)
        std_pnl = math.sqrt(variance)
        sharpe = (mean_pnl / std_pnl) * math.sqrt(252) if std_pnl > 0 else 0

        reasons = defaultdict(int)
        for t in trades:
            reasons[t["reason"]] += 1

        return {
            "coin": self.coin,
            "total_trades": n,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": round(win_rate, 1),
            "total_pnl": round(total_pnl, 2),
            "total_pnl_pct": round(total_pnl_pct, 2),
            "final_equity": round(self.equity, 2),
            "gross_profit": round(gross_profit, 2),
            "gross_loss": round(gross_loss, 2),
            "profit_factor": round(profit_factor, 2),
            "avg_win": round(avg_win, 4),
            "avg_loss": round(avg_loss, 4),
            "rr_ratio": round(rr_ratio, 2),
            "max_drawdown_pct": round(max_dd, 2),
            "avg_duration_min": round(avg_dur, 1),
            "sharpe_ratio": round(sharpe, 2),
            "best_trade": round(max(trades, key=lambda t: t["pnl"])["pnl"], 4),
            "worst_trade": round(min(trades, key=lambda t: t["pnl"])["pnl"], 4),
            "exit_reasons": dict(reasons),
        }


# ──────────────────────────────────────────────────────────────
# Chargement des données MongoDB
# ──────────────────────────────────────────────────────────────

def load_data(coin, days=None, from_date=None, to_date=None):
    client = MongoClient(MONGO_URL)
    db = client[MONGO_DB]

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = int(from_date.timestamp() * 1000) if from_date else (
        now_ms - days * 86400 * 1000 if days else 0)
    end_ms = int(to_date.timestamp() * 1000) if to_date else now_ms

    query = {"coin": coin, "timestamp": {"$gte": start_ms, "$lte": end_ms}}
    print(f"[BACKTEST] Chargement données {coin}...")

    def load_col(col, fields):
        cursor = db[col].find(query, {f: 1 for f in fields + ["_id"]}).sort("timestamp", 1)
        docs = list(cursor)
        return pd.DataFrame(docs) if docs else pd.DataFrame()

    df_1m  = load_col(MONGO_COLLECTION_1M,  ["timestamp", "open", "high", "low", "close", "volume"])
    df_15m = load_col(MONGO_COLLECTION_15M, ["timestamp", "open", "high", "low", "close", "volume"])
    df_1h  = load_col(MONGO_COLLECTION_1H,  ["timestamp", "open", "high", "low", "close", "volume"])

    for df in [df_1m, df_15m, df_1h]:
        for col in ["open", "high", "low", "close", "volume"]:
            if col in df.columns:
                df[col] = df[col].astype(float)

    # Données marché (funding, OI, orderbook) — même filtre coin
    df_funding = load_col(MONGO_COLLECTION_FUNDING, ["timestamp", "funding_rate", "premium"])
    df_oi      = load_col(MONGO_COLLECTION_OI,      ["timestamp", "oi_change_pct", "open_interest"])

    ob_cursor = db[MONGO_COLLECTION_ORDERBOOK].find(
        {"coin": coin, "timestamp": {"$gte": start_ms, "$lte": end_ms}},
        {"timestamp": 1, "imbalance": 1}
    ).sort("timestamp", 1)
    ob_docs = list(ob_cursor)
    df_ob = pd.DataFrame(ob_docs) if ob_docs else pd.DataFrame()

    print(f"[BACKTEST] Chargé : {len(df_1m)} bougies 1m | {len(df_15m)} bougies 15m | "
          f"{len(df_1h)} bougies 1h | {len(df_funding)} funding | "
          f"{len(df_oi)} OI | {len(df_ob)} orderbook snapshots")
    return df_1m, df_15m, df_1h, df_funding, df_oi, df_ob


# ──────────────────────────────────────────────────────────────
# Affichage des résultats
# ──────────────────────────────────────────────────────────────

def print_summary(summary, trades):
    if not summary:
        return
    sep = "─" * 52
    print(f"\n{'═' * 52}")
    print(f"  RÉSULTATS BACKTEST — {summary['coin']}")
    print(f"{'═' * 52}")
    print(f"  Trades totaux    : {summary['total_trades']}  "
          f"(✅ {summary['wins']} | ❌ {summary['losses']})")
    print(f"  Taux de réussite : {summary['win_rate_pct']}%")
    print(sep)
    print(f"  PnL total        : {summary['total_pnl']:+.2f} USDC  ({summary['total_pnl_pct']:+.2f}%)")
    print(f"  Équity finale    : {summary['final_equity']:.2f} USDC")
    print(sep)
    print(f"  Profit factor    : {summary['profit_factor']:.2f}  (>1.5 = bon)")
    print(f"  Ratio R:R moyen  : {summary['rr_ratio']:.2f}")
    print(f"  Sharpe ratio     : {summary['sharpe_ratio']:.2f}")
    print(sep)
    print(f"  Max drawdown     : {summary['max_drawdown_pct']:.2f}%")
    print(f"  Durée moy. trade : {summary['avg_duration_min']:.0f} min")
    print(f"  Meilleur trade   : {summary['best_trade']:+.4f} USDC")
    print(f"  Pire trade       : {summary['worst_trade']:+.4f} USDC")
    print(sep)
    print(f"  Raisons de sortie:")
    for reason, count in summary["exit_reasons"].items():
        pct = count / summary["total_trades"] * 100
        print(f"    {reason:<20} : {count} ({pct:.0f}%)")
    print(f"{'═' * 52}\n")

    print("  Derniers trades :")
    print(f"  {'Date ouv.':<17} {'Dir':<5} {'Entry':>9} {'Exit':>9} {'PnL':>9} {'Raison'}")
    print(f"  {sep}")
    for t in trades[-15:]:
        icon = "✅" if t["pnl"] > 0 else "❌"
        print(f"  {t['open_time']:<17} {t['side'].upper():<5} "
              f"{t['entry']:>9.2f} {t['exit']:>9.2f} {t['pnl']:>+9.4f} {icon} {t['reason']}")
    print()


def export_csv(trades, equity_curve, coin, output_dir="backtest/results"):
    os.makedirs(output_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    if trades:
        path = os.path.join(output_dir, f"trades_{coin}_{ts}.csv")
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=trades[0].keys())
            writer.writeheader()
            writer.writerows(trades)
        print(f"[BACKTEST] Trades → {path}")

    if equity_curve:
        path = os.path.join(output_dir, f"equity_{coin}_{ts}.csv")
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["timestamp", "equity"])
            writer.writeheader()
            writer.writerows(equity_curve)
        print(f"[BACKTEST] Equity curve → {path}")


# ──────────────────────────────────────────────────────────────
# Point d'entrée
# ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Backtest Bot Hyperliquid v8")
    parser.add_argument("--coin", default="BTC", help="Coin (BTC, SOL...)")
    parser.add_argument("--days", type=int, default=None, help="Derniers N jours")
    parser.add_argument("--from", dest="from_date", default=None, help="Date début YYYY-MM-DD")
    parser.add_argument("--to", dest="to_date", default=None, help="Date fin YYYY-MM-DD")
    parser.add_argument("--equity", type=float, default=1000.0, help="Solde initial (USDC)")
    parser.add_argument("--export", action="store_true", help="Export CSV des trades")
    args = parser.parse_args()

    from_date = datetime.strptime(args.from_date, "%Y-%m-%d").replace(tzinfo=timezone.utc) if args.from_date else None
    to_date = datetime.strptime(args.to_date, "%Y-%m-%d").replace(tzinfo=timezone.utc) if args.to_date else None

    df_1m, df_15m, df_1h, df_funding, df_oi, df_ob = load_data(
        args.coin, days=args.days, from_date=from_date, to_date=to_date)

    if df_1m.empty or len(df_1m) < 150:
        print(f"[BACKTEST] Données insuffisantes ({len(df_1m)} bougies 1m, minimum 150).")
        sys.exit(1)
    if df_15m.empty or len(df_15m) < 50:
        print(f"[BACKTEST] Données insuffisantes ({len(df_15m)} bougies 15m, minimum 50).")
        sys.exit(1)

    bt = Backtester(args.coin, df_1m, df_15m, df_1h, df_funding, df_oi, df_ob,
                    initial_equity=args.equity)
    summary = bt.run()
    print_summary(summary, bt.trades)

    if args.export and bt.trades:
        export_csv(bt.trades, bt.equity_curve, args.coin)


if __name__ == "__main__":
    main()
