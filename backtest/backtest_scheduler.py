"""
BacktestScheduler — Backtest hebdomadaire automatique
=====================================================
Daemon thread qui lance un backtest rolling toutes les N jours
et envoie les résultats + alertes sur Telegram.

Intégration dans main.py :
    from backtest.backtest_scheduler import BacktestScheduler
    scheduler = BacktestScheduler(notifier=self.notifier)
    threading.Thread(target=scheduler.run_loop, daemon=True, name="BacktestScheduler").start()
"""

import io
import json
import os
import sys
import time
import threading
from datetime import datetime, timezone

# ── Paramètres ─────────────────────────────────────────────────
CHECK_INTERVAL_H   = 6      # Vérifie toutes les 6h si c'est l'heure de backtester
BACKTEST_INTERVAL_DAYS = 7  # Lance le backtest toutes les 7 jours
LOOKBACK_DAYS      = 30     # Fenêtre d'analyse (30 derniers jours)
STATE_FILE         = "backtest/scheduler_state.json"

# Seuils d'alerte
ALERT_PF_CRITICAL  = 1.0    # 🚨 en dessous → stratégie perdante
ALERT_PF_WARN      = 1.2    # ⚠️ en dessous → surveiller
ALERT_DD_CRITICAL  = 3.0    # 🚨 drawdown max critique (%)
ALERT_WINRATE_WARN = 40.0   # ⚠️ win rate faible (%)


class BacktestScheduler:
    """Lance un backtest rolling toutes les semaines et notifie sur Telegram."""

    def __init__(self, notifier, coins=None, lookback_days=LOOKBACK_DAYS,
                 interval_days=BACKTEST_INTERVAL_DAYS):
        self.notifier     = notifier
        self.coins        = coins or ["BTC", "SOL"]
        self.lookback     = lookback_days
        self.interval_sec = interval_days * 86400
        self._lock        = threading.Lock()
        self._state       = self._load_state()
        print(f"[BACKTEST_SCHED] Initialisé — backtest tous les {interval_days}j | "
              f"fenêtre {lookback_days}j | coins: {self.coins}")

    # ── Boucle principale ───────────────────────────────────────

    def run_loop(self):
        """Blocking — à lancer dans un daemon thread."""
        while True:
            try:
                if self._should_run():
                    print("[BACKTEST_SCHED] Lancement backtest hebdomadaire...")
                    results = self._run_all_coins()
                    self._save_state(results)
                    self._notify(results)
                else:
                    next_run = self._state.get("last_run", 0) + self.interval_sec
                    remaining_h = max(0, (next_run - time.time()) / 3600)
                    print(f"[BACKTEST_SCHED] Prochain backtest dans {remaining_h:.1f}h")
            except Exception as e:
                print(f"[BACKTEST_SCHED] Erreur: {e}")
                try:
                    self.notifier.send(f"⚠️ <b>BacktestScheduler erreur</b>\n<code>{e}</code>")
                except Exception:
                    pass

            time.sleep(CHECK_INTERVAL_H * 3600)

    # ── Logique principale ──────────────────────────────────────

    def _should_run(self) -> bool:
        last = self._state.get("last_run", 0)
        return (time.time() - last) >= self.interval_sec

    def _run_all_coins(self) -> dict:
        """Lance le backtest pour chaque coin et retourne les résultats."""
        # Import ici pour éviter les imports circulaires au démarrage
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from backtest.backtest import load_data, Backtester

        results = {}
        for coin in self.coins:
            try:
                print(f"[BACKTEST_SCHED] Backtest {coin} ({self.lookback}j)...")
                df_1m, df_15m, df_1h, df_f, df_oi, df_ob = load_data(coin, days=self.lookback)

                # Supprimer les prints verbeux du backtest
                captured = io.StringIO()
                old_out, sys.stdout = sys.stdout, captured
                bt = Backtester(coin, df_1m, df_15m, df_1h, df_f, df_oi, df_ob)
                res = bt.run()
                sys.stdout = old_out

                results[coin] = res or {}
                n = res.get("total_trades", 0) if res else 0
                print(f"[BACKTEST_SCHED] {coin} terminé — {n} trades")

            except Exception as e:
                print(f"[BACKTEST_SCHED] Erreur backtest {coin}: {e}")
                results[coin] = {"error": str(e)}

        return results

    # ── Notification ────────────────────────────────────────────

    def _notify(self, results: dict):
        msg = self._format_message(results)
        self.notifier.send(msg)

    def _format_message(self, results: dict) -> str:
        now = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
        lines = [
            f"📊 <b>Backtest Hebdomadaire</b>",
            f"🗓 {self.lookback} derniers jours — {now}",
            "",
        ]

        alerts = []
        suggestions = []  # suggestions config par coin

        for coin, res in results.items():
            if not res or "error" in res:
                err = res.get("error", "pas de données") if res else "pas de données"
                lines.append(f"<b>{coin}</b> — ❌ Erreur : {err}")
                lines.append("")
                continue

            n       = res.get("total_trades", 0)
            wins    = res.get("wins", 0)
            losses  = res.get("losses", 0)
            wr      = res.get("win_rate_pct", 0)
            pnl     = res.get("total_pnl_pct", 0)
            pf      = res.get("profit_factor", 0)
            rr      = res.get("rr_ratio", 0)
            dd      = res.get("max_drawdown_pct", 0)
            sharpe  = res.get("sharpe_ratio", 0)
            reasons = res.get("exit_reasons", {})

            # Calcul % sorties par type
            sl_pct  = round(reasons.get("sl", 0) / n * 100) if n else 0
            tp_pct  = round(reasons.get("tp", 0) / n * 100) if n else 0
            rev_pct = round(reasons.get("signal_reverse", 0) / n * 100) if n else 0

            # Icône performance
            icon = "📈" if pnl > 1 else ("📊" if pnl > 0 else "📉")

            lines.append(f"{icon} <b>{coin}</b> — {n} trades (✅{wins} ❌{losses})")
            lines.append(f"  Win: <b>{wr}%</b> | PnL: <b>{pnl:+.2f}%</b>")
            lines.append(f"  PF: <b>{pf}</b> | R:R: <b>{rr}</b> | DD: <b>{dd}%</b>")
            lines.append(f"  Sorties: SL {sl_pct}% | TP {tp_pct}% | Rev {rev_pct}%")
            lines.append("")

            # ── Alertes ────────────────────────────────────────
            if pf < ALERT_PF_CRITICAL:
                alerts.append(f"🚨 {coin} PF critique ({pf}) — stratégie en perte")
            elif pf < ALERT_PF_WARN:
                alerts.append(f"⚠️ {coin} PF faible ({pf}) — surveiller")
            if dd > ALERT_DD_CRITICAL:
                alerts.append(f"🚨 {coin} drawdown critique ({dd}%)")
            if wr < ALERT_WINRATE_WARN and n >= 10:
                alerts.append(f"⚠️ {coin} win rate faible ({wr}%)")

            # ── Diagnostic → suggestions config ────────────────
            diag = self._diagnose(coin, wr, pf, rr, dd, sl_pct, tp_pct, n)
            suggestions.extend(diag)

        # Section alertes
        if alerts:
            lines.append("<b>⚠️ Alertes :</b>")
            for a in alerts:
                lines.append(f"  {a}")
            lines.append("")

        # Section suggestions config
        if suggestions:
            lines.append("<b>🔧 Suggestions config.py :</b>")
            for s in suggestions:
                lines.append(f"  {s}")
            lines.append("")
        elif not alerts:
            lines.append("✅ Tous les indicateurs dans les limites normales")
            lines.append("")

        next_run = datetime.fromtimestamp(
            time.time() + self.interval_sec, tz=timezone.utc
        ).strftime("%d/%m/%Y")
        lines.append(f"🔄 Prochain backtest : {next_run}")

        return "\n".join(lines)

    def _diagnose(self, coin, wr, pf, rr, dd, sl_pct, tp_pct, n) -> list:
        """
        Mappe les symptômes de performance vers des suggestions concrètes de config.py.
        Retourne une liste de strings prêtes à insérer dans le message Telegram.
        """
        tips = []

        # Pas assez de trades pour diagnostiquer
        if n < 10:
            return tips

        # ── R:R trop faible ────────────────────────────────────
        # Cause probable : sorties breakeven trop précoces (BREAKEVEN_TRIGGER trop bas)
        if rr < 0.8 and sl_pct > 75:
            tips.append(
                f"[{coin}] R:R faible ({rr}) + {sl_pct}% SL exits\n"
                f"    → Essayer BREAKEVEN_TRIGGER_PCT ↑ (ex: 0.012→0.015)\n"
                f"    → Ou TRAILING_TRIGGER_PCT ↓ pour activer le trail plus tôt"
            )

        # ── TP jamais atteint ──────────────────────────────────
        # Cause probable : TP trop loin ou trail trop large
        if tp_pct < 5 and rr < 1.0:
            tips.append(
                f"[{coin}] TP atteint seulement {tp_pct}% des trades\n"
                f"    → Essayer MIN_TP_PCT ↓ ou TRAIL_PCT ↓ (trail plus serré)"
            )

        # ── Win rate trop faible ───────────────────────────────
        # Cause probable : signal pas assez sélectif
        if wr < 45 and pf < 1.0:
            tips.append(
                f"[{coin}] Win rate {wr}% + PF {pf} → signaux peu fiables\n"
                f"    → Essayer SIGNAL_THRESHOLD_DEFAULT ↑ (ex: 8→9)\n"
                f"    → Ou SIGNAL_CONFIRM_COUNT ↑ (ex: 2→3)"
            )
        elif wr < 45:
            tips.append(
                f"[{coin}] Win rate bas ({wr}%) mais PF OK — R:R compense\n"
                f"    → Normal si R:R > 1.3, pas d'action requise"
            )

        # ── Drawdown trop élevé ────────────────────────────────
        # Cause probable : taille de position ou pertes consécutives
        if dd > ALERT_DD_CRITICAL:
            tips.append(
                f"[{coin}] Drawdown {dd}% trop élevé\n"
                f"    → Essayer POSITION_SIZE_PCT ↓ (ex: 0.30→0.25)\n"
                f"    → Ou MAX_CONSECUTIVE_LOSSES ↓ (ex: 3→2)"
            )

        # ── PF faible avec bon R:R ─────────────────────────────
        # Cause probable : trop de trades (overtrading) ou signal noisy
        if pf < 1.1 and rr >= 1.0 and wr < 45:
            tips.append(
                f"[{coin}] PF {pf} malgré R:R {rr} — trop de faux signaux\n"
                f"    → Essayer SIGNAL_CONFIRM_COUNT ↑ (ex: 2→3)\n"
                f"    → Ou vérifier Gate ADX (seuil 25 → 28)"
            )

        # ── Tout va bien ───────────────────────────────────────
        if not tips and pf >= ALERT_PF_WARN and rr >= 1.0:
            tips.append(f"[{coin}] ✅ Paramètres optimaux — aucune action")

        return tips

    # ── Persistance état ────────────────────────────────────────

    def _load_state(self) -> dict:
        try:
            if os.path.exists(STATE_FILE):
                with open(STATE_FILE) as f:
                    return json.load(f)
        except Exception:
            pass
        return {}

    def _save_state(self, results: dict):
        state = {
            "last_run": time.time(),
            "last_run_str": datetime.now(timezone.utc).isoformat(),
            "last_results": {
                coin: {
                    "total_trades":   res.get("total_trades"),
                    "win_rate_pct":   res.get("win_rate_pct"),
                    "total_pnl_pct":  res.get("total_pnl_pct"),
                    "profit_factor":  res.get("profit_factor"),
                    "rr_ratio":       res.get("rr_ratio"),
                    "max_drawdown_pct": res.get("max_drawdown_pct"),
                }
                for coin, res in results.items() if res and "error" not in res
            }
        }
        try:
            os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
            with open(STATE_FILE, "w") as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            print(f"[BACKTEST_SCHED] Impossible de sauver l'état: {e}")

    def status(self) -> dict:
        """Retourne l'état courant pour debug/monitoring."""
        last = self._state.get("last_run", 0)
        next_run = last + self.interval_sec
        return {
            "last_run": self._state.get("last_run_str", "jamais"),
            "next_run_in_h": round(max(0, next_run - time.time()) / 3600, 1),
            "last_results": self._state.get("last_results", {}),
        }
