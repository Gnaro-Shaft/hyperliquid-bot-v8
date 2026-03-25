import time
import signal
import threading
import asyncio
from datetime import datetime, timezone

from config import (
    PAIRS, DEBUG, TP_PCT, SL_PCT, TRAIL_PCT,
    TRAILING_TRIGGER_PCT, TRAILING_STEP_PCT,
    KILL_SWITCH_FILE, COOLDOWN_BETWEEN_TRADES_SEC,
    SIGNAL_CONFIRM_COUNT,
)
from strategy.strategy_engine import StrategyEngine
from trader.ccxt_trader import HyperliquidTrader
from collector.websocket_collector import WebSocketCollector
from collector.rest_collector import RestCollector
from risk.risk_manager import RiskManager
from utils.notifier import Notifier


class TradingBot:
    def __init__(self):
        self.collector = WebSocketCollector()
        self.rest_collector = RestCollector()
        self.trader = HyperliquidTrader()
        self.risk = RiskManager()
        self.notifier = Notifier()
        self.engine = None
        self._shutdown = False
        self.position = self._empty_position()
        self._last_daily_reset = None
        self._last_trade_time = 0  # Cooldown entre trades
        self._signal_streak = 0    # Compteur de signaux forts consecutifs
        self._last_signal_dir = 0  # Direction du dernier signal fort

    def start(self):
        """Point d'entree principal."""
        # Ignorer les signaux pendant le demarrage (Fly envoie SIGINT/SIGTERM pendant le deploy)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)

        # Lance le collector WS en thread separee
        collector_thread = threading.Thread(target=self._run_collector, daemon=True)
        collector_thread.start()

        # Lance le collector REST (funding/OI) en thread separee
        rest_thread = threading.Thread(target=self.rest_collector.collect_loop, daemon=True)
        rest_thread.start()

        # Attendre que le collector recup des donnees
        print("[BOT] Attente des premieres donnees du collector...")
        for _ in range(60):
            if self.collector.is_alive:
                break
            time.sleep(2)

        if not self.collector.is_alive:
            print("[BOT] Le collector n'a pas recu de donnees en 2 min. Demarrage quand meme...")

        # Selection de paire + init strategy
        pair = self.trader.select_pair()
        if not pair:
            self.notifier.error("Aucune paire disponible (solde insuffisant)")
            return

        coin = pair.split("/")[0]
        self.engine = StrategyEngine(coin=coin)

        # Init risk manager
        balance = self.trader._get_total_balance()
        self.risk.reset_daily(balance)
        self._last_daily_reset = datetime.now(timezone.utc).date()

        # Sync position existante au demarrage
        self._sync_position_on_start()

        self.notifier.bot_started(pair, balance)
        print(f"\n=== Trading Bot v8 LIVE sur {pair} | Solde: {balance:.2f} USDC ===\n")

        # Maintenant que le bot est pret, activer les handlers de shutdown
        signal.signal(signal.SIGINT, self._handle_shutdown)
        signal.signal(signal.SIGTERM, self._handle_shutdown)

        self._trading_loop()

    def _sync_position_on_start(self):
        """Recupere la position ouverte depuis l'exchange au demarrage."""
        has_pos, pos_info = self.trader.has_open_position()
        if has_pos and pos_info:
            self.position = {
                "active": True,
                "entry": pos_info["entry_price"],
                "side": "buy" if pos_info["side"] == "long" else "sell",
                "size": abs(pos_info.get("size", 0)),
                "trail_distance": TRAIL_PCT,
                "trail_trigger": TRAILING_TRIGGER_PCT,
                "trail_step": TRAILING_STEP_PCT,
                "trailing": None,
                "trailing_active": False,
                "open_time": time.time(),
            }
            print(f"[BOT] Position existante detectee: {pos_info['side']} "
                  f"@ {pos_info['entry_price']:.2f} | PnL: {pos_info['unrealized_pnl']:+.2f}")

    def _trading_loop(self):
        while not self._shutdown:
            try:
                # Reset journalier (une seule fois par jour)
                today = datetime.now(timezone.utc).date()
                if today != self._last_daily_reset:
                    balance = self.trader._get_total_balance()
                    self.risk.reset_daily(balance)
                    self._last_daily_reset = today
                    self.notifier.send(f"📅 <b>Nouveau jour</b> — Solde: <code>{balance:.2f} USDC</code>")

                # Kill switch
                if self._check_kill_switch():
                    time.sleep(60)
                    continue

                # Verifier que le collector est vivant
                if not self.collector.is_alive:
                    print("[BOT] ⚠️ Collector inactif — donnees potentiellement stales")

                # Re-verifier la paire
                pair = self.trader.select_pair()
                if not pair:
                    print("[BOT] Solde insuffisant pour toutes les paires. Attente...")
                    time.sleep(60)
                    continue

                # Mise a jour du coin si la paire a change
                coin = pair.split("/")[0]
                if self.engine is None or self.engine.coin != coin:
                    self.engine = StrategyEngine(coin=coin)
                    print(f"[BOT] Switch vers {pair}")
                    self.notifier.send(f"🔄 Switch vers <code>{pair}</code>")

                # 1. Calcul du signal
                sig = self.engine.compute_signals()
                last_price = sig["debug"].get("close", 0)

                ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                print(f"[{ts}] {sig['color']} Score: {sig['score']} (raw: {sig['raw_score']}) | "
                      f"{sig['label']} | {coin}: {last_price:.2f}")

                # 2. Sync avec l'exchange
                has_pos, pos_info = self.trader.has_open_position()

                # Position fermee par l'exchange (TP/SL hit)
                if not has_pos and self.position["active"]:
                    entry = self.position.get("entry", 0)
                    side = self.position.get("side", "buy")
                    size = self.position.get("size", 0)

                    # Récupérer le vrai prix de sortie depuis les fills
                    last_fill = self.trader.get_last_closed_trade()
                    if last_fill and last_fill["price"] > 0:
                        exit_price = last_fill["price"]
                    else:
                        # Fallback: prix actuel
                        exit_price = last_price

                    # Calcul PnL réel
                    if side == "buy":
                        pnl = (exit_price - entry) * size
                    else:
                        pnl = (entry - exit_price) * size

                    reason = "tp_sl_exchange"
                    print(f"[BOT] Position fermee par l'exchange | Entry: {entry:.2f} → Exit: {exit_price:.2f} | PnL: {pnl:+.4f}")

                    self.trader.cancel_open_orders()
                    self.notifier.trade_closed(self.trader.pair, side, entry, exit_price, pnl, reason)
                    self.risk.register_trade_result(pnl)
                    self.trader.logger.log_trade({
                        "pair": self.trader.pair,
                        "side": side,
                        "action": "close",
                        "entry_price": entry,
                        "exit_price": exit_price,
                        "size": size,
                        "pnl": pnl,
                        "reason": reason,
                    })
                    self.position = self._empty_position()

                # 3. Gestion de position existante
                if has_pos and self.position["active"]:
                    # Signal oppose fort → fermer la position
                    if self._should_reverse(sig):
                        print(f"[BOT] Signal oppose fort ({sig['score']}) — fermeture de position")
                        result = self.trader.close_position(reason="signal_reverse")
                        if result:
                            self.risk.register_trade_result(result["pnl"])
                        self._last_trade_time = time.time()
                        self.position = self._empty_position()
                    else:
                        self._manage_trailing(last_price)

                # 4. Ouverture de position si pas de position
                elif not has_pos:
                    self._try_open_position(sig, last_price)

                # Debug status
                if DEBUG:
                    risk_status = self.risk.status()
                    print(f"  [DEBUG] pos={has_pos} | trailing={'ON' if self.position['trailing_active'] else 'OFF'} | "
                          f"losses={risk_status['consecutive_losses']} | pnl_day={risk_status['pnl_today']:+.2f}")

            except Exception as e:
                import traceback
                err_msg = traceback.format_exc()
                print(f"[BOT][ERREUR] {e}")
                print(err_msg)
                # Notifier seulement 1 fois sur 5 pour ne pas spammer
                if not hasattr(self, '_err_count'):
                    self._err_count = 0
                self._err_count += 1
                if self._err_count <= 3 or self._err_count % 10 == 0:
                    self.notifier.error(f"[{self._err_count}] {str(e)[:200]}")

            time.sleep(60)

        self._cleanup()

    def _should_reverse(self, sig):
        """Verifie si le signal est oppose a la position en cours."""
        if not self.position["active"]:
            return False
        if self.position["side"] == "buy" and sig["score"] == -2:
            return True
        if self.position["side"] == "sell" and sig["score"] == 2:
            return True
        return False

    def _try_open_position(self, sig, price):
        """Tente d'ouvrir une position si le signal est fort ET confirme."""
        if sig["score"] not in (2, -2):
            # Reset streak si signal faible
            self._signal_streak = 0
            self._last_signal_dir = 0
            return

        # Compteur de signaux consecutifs dans la meme direction
        if sig["score"] == self._last_signal_dir:
            self._signal_streak += 1
        else:
            self._signal_streak = 1
            self._last_signal_dir = sig["score"]

        if self._signal_streak < SIGNAL_CONFIRM_COUNT:
            if DEBUG:
                print(f"  [CONFIRM] Signal fort {sig['score']} ({self._signal_streak}/{SIGNAL_CONFIRM_COUNT}) — en attente de confirmation")
            return

        # Cooldown entre trades
        elapsed = time.time() - self._last_trade_time
        if elapsed < COOLDOWN_BETWEEN_TRADES_SEC:
            remaining = int(COOLDOWN_BETWEEN_TRADES_SEC - elapsed)
            if DEBUG:
                print(f"  [COOLDOWN] {remaining}s restantes avant prochain trade")
            return

        # Signal confirme ! Reset streak
        self._signal_streak = 0

        # Notifier le signal fort confirme
        self.notifier.signal_alert(
            self.engine.coin, sig["score"], sig["raw_score"],
            sig["label"], sig["color"], price, sig["debug"]
        )

        # Verifier le risk manager
        balance = self.trader._get_total_balance()
        can_trade, reason = self.risk.can_trade(current_balance=balance)
        if not can_trade:
            print(f"[BOT] Trading bloque: {reason}")
            self.notifier.risk_alert(reason)
            return

        side = "buy" if sig["score"] == 2 else "sell"

        # TP/SL dynamiques
        tp = sig.get("dynamic_tp") or TP_PCT
        sl = sig.get("dynamic_sl") or SL_PCT

        result = self.trader.place_order_with_tp_sl(side, price, tp_pct=tp, sl_pct=sl)
        if result:
            self._last_trade_time = time.time()
            # Trailing dynamique basé sur ATR — seuils LARGES pour laisser courir
            atr_pct = sig["debug"].get("atr_pct", 0.001)  # ATR en % du prix
            trail_distance = max(atr_pct * 1.5, 0.005)     # 1.5x ATR, minimum 0.5%
            trail_trigger = max(atr_pct * 2.0, 0.008)      # Activation à 2x ATR (laisser le trade respirer)
            trail_step = max(atr_pct * 0.5, 0.002)         # Rehausse à 0.5x ATR

            self.position = {
                "active": True,
                "entry": price,
                "side": side,
                "size": result.get("size", 0),
                "trailing": None,
                "trailing_active": False,
                "trail_distance": trail_distance,
                "trail_trigger": trail_trigger,
                "trail_step": trail_step,
                "open_time": time.time(),
            }
            print(f"[BOT] Trailing dynamique: distance={trail_distance*100:.2f}% | trigger={trail_trigger*100:.2f}% | step={trail_step*100:.2f}% (ATR={atr_pct*100:.3f}%)")
            rr = tp / sl if sl > 0 else 0
            print(f"[BOT] ✅ {side.upper()} @ {price:.2f} | TP: {result['tp_price']:.2f} | "
                  f"SL: {result['sl_price']:.2f} | R:R={rr:.1f}:1")

    def _manage_trailing(self, last_price):
        """Gere le trailing stop dynamique basé sur ATR."""
        entry = self.position["entry"]
        side = self.position["side"]
        trail_dist = self.position.get("trail_distance", TRAIL_PCT)
        trail_trig = self.position.get("trail_trigger", TRAILING_TRIGGER_PCT)
        trail_step = self.position.get("trail_step", TRAILING_STEP_PCT)

        if side == "buy":
            gain_pct = (last_price - entry) / entry
        else:
            gain_pct = (entry - last_price) / entry

        # Activer le trailing apres le seuil dynamique
        if not self.position["trailing_active"] and gain_pct >= trail_trig:
            if side == "buy":
                self.position["trailing"] = last_price * (1 - trail_dist)
            else:
                self.position["trailing"] = last_price * (1 + trail_dist)
            self.position["trailing_active"] = True
            print(f"[BOT] 📈 Trailing active @ {self.position['trailing']:.2f} (gain: {gain_pct*100:.2f}%, distance: {trail_dist*100:.2f}%)")

        # Gerer le trailing actif
        if self.position["trailing_active"]:
            trailing = self.position["trailing"]

            if side == "buy":
                new_trailing = last_price * (1 - trail_dist)
                if new_trailing > trailing + (entry * trail_step):
                    self.position["trailing"] = new_trailing
                    print(f"[BOT] 📈 Trailing rehausse @ {new_trailing:.2f}")
                elif last_price <= trailing:
                    print(f"[BOT] 🔔 Trailing touche ({last_price:.2f} <= {trailing:.2f})")
                    result = self.trader.close_position(reason="trailing_stop")
                    if result:
                        self.risk.register_trade_result(result["pnl"])
                    self.position = self._empty_position()

            elif side == "sell":
                new_trailing = last_price * (1 + trail_dist)
                if new_trailing < trailing - (entry * trail_step):
                    self.position["trailing"] = new_trailing
                    print(f"[BOT] 📉 Trailing abaisse @ {new_trailing:.2f}")
                elif last_price >= trailing:
                    print(f"[BOT] 🔔 Trailing touche ({last_price:.2f} >= {trailing:.2f})")
                    result = self.trader.close_position(reason="trailing_stop")
                    if result:
                        self.risk.register_trade_result(result["pnl"])
                    self.position = self._empty_position()

    def _check_kill_switch(self):
        """Verifie le kill switch fichier."""
        import os
        if os.path.exists(KILL_SWITCH_FILE):
            print("[BOT] 🛑 KILL SWITCH actif")
            return True
        return False

    def _empty_position(self):
        return {
            "active": False,
            "entry": None,
            "side": None,
            "size": 0,
            "trail_distance": TRAIL_PCT,
            "trail_trigger": TRAILING_TRIGGER_PCT,
            "trail_step": TRAILING_STEP_PCT,
            "trailing": None,
            "trailing_active": False,
            "open_time": None,
        }

    def _run_collector(self):
        """Lance le collector WebSocket dans un event loop dedie."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self.collector.collect())
        except Exception as e:
            print(f"[COLLECTOR][FATAL] {e}")
        finally:
            loop.close()

    def _handle_shutdown(self, signum, frame):
        print(f"\n[BOT] Signal {signum} recu, arret en cours...")
        self._shutdown = True

    def _cleanup(self):
        """Arret propre."""
        self.collector.stop()
        balance = self.trader._get_total_balance()
        risk_status = self.risk.status()
        self.notifier.bot_stopped(
            f"PnL jour: {risk_status['pnl_today']:+.2f} | Solde: {balance:.2f}"
        )
        print("[BOT] Arret complet.")


if __name__ == "__main__":
    import sys
    bot = TradingBot()
    bot.start()
    # Ne JAMAIS quitter avec code 0 sur Fly (sinon pas de restart)
    sys.exit(1)
