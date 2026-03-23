import time
import os
from datetime import datetime, timezone

from config import (
    MAX_CONSECUTIVE_LOSSES,
    PAUSE_DURATION_MINUTES,
    MAX_DAILY_DRAWDOWN_PCT,
    COOLDOWN_BETWEEN_TRADES_SEC,
    KILL_SWITCH_FILE,
    DEBUG,
)


class RiskManager:
    def __init__(self):
        self.consecutive_losses = 0
        self.pause_until = 0  # timestamp
        self.last_trade_time = 0
        self.daily_start_balance = None
        self.daily_date = None
        self.total_pnl_today = 0.0

    def reset_daily(self, balance):
        """Appeler au debut de chaque journee ou au demarrage."""
        today = datetime.now(timezone.utc).date()
        if self.daily_date != today:
            self.daily_date = today
            self.daily_start_balance = balance
            self.total_pnl_today = 0.0
            self.consecutive_losses = 0
            if DEBUG:
                print(f"[RISK] Reset journalier. Solde initial : {balance:.2f}")

    def register_trade_result(self, pnl):
        """Enregistre le resultat d'un trade pour le suivi."""
        self.total_pnl_today += pnl
        self.last_trade_time = time.time()

        if pnl < 0:
            self.consecutive_losses += 1
            if DEBUG:
                print(f"[RISK] Perte #{self.consecutive_losses} consecutive. PnL jour: {self.total_pnl_today:.2f}")
        else:
            self.consecutive_losses = 0
            if DEBUG:
                print(f"[RISK] Trade gagnant. Serie pertes reset. PnL jour: {self.total_pnl_today:.2f}")

        # Pause automatique apres N pertes consecutives
        if self.consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
            self.pause_until = time.time() + PAUSE_DURATION_MINUTES * 60
            if DEBUG:
                print(f"[RISK] PAUSE {PAUSE_DURATION_MINUTES}min apres {MAX_CONSECUTIVE_LOSSES} pertes consecutives")

    def can_trade(self, current_balance=None):
        """Verifie si le bot est autorise a trader. Retourne (bool, raison)."""

        # Kill switch
        if os.path.exists(KILL_SWITCH_FILE):
            return False, "KILL SWITCH actif (fichier KILL detecte)"

        # Pause apres pertes consecutives
        now = time.time()
        if now < self.pause_until:
            remaining = int((self.pause_until - now) / 60)
            return False, f"Pause anti-overtrading ({remaining}min restantes)"

        # Cooldown entre trades
        elapsed = now - self.last_trade_time
        if self.last_trade_time > 0 and elapsed < COOLDOWN_BETWEEN_TRADES_SEC:
            remaining = int(COOLDOWN_BETWEEN_TRADES_SEC - elapsed)
            return False, f"Cooldown entre trades ({remaining}s restantes)"

        # Drawdown max journalier
        if current_balance and self.daily_start_balance:
            drawdown = (self.daily_start_balance - current_balance) / self.daily_start_balance
            if drawdown >= MAX_DAILY_DRAWDOWN_PCT:
                return False, f"Drawdown journalier max atteint ({drawdown*100:.1f}% >= {MAX_DAILY_DRAWDOWN_PCT*100:.1f}%)"

        return True, "OK"

    def status(self):
        """Retourne un dict avec l'etat courant du risk manager."""
        return {
            "consecutive_losses": self.consecutive_losses,
            "paused": time.time() < self.pause_until,
            "pause_remaining_min": max(0, int((self.pause_until - time.time()) / 60)),
            "pnl_today": round(self.total_pnl_today, 2),
            "daily_start_balance": self.daily_start_balance,
            "kill_switch": os.path.exists(KILL_SWITCH_FILE),
        }


if __name__ == "__main__":
    rm = RiskManager()
    rm.reset_daily(1000.0)

    # Simule 3 pertes
    for i in range(3):
        rm.register_trade_result(-10)
        ok, reason = rm.can_trade(current_balance=1000 - (i + 1) * 10)
        print(f"Trade #{i+1}: can_trade={ok}, reason={reason}")

    print("Status:", rm.status())
