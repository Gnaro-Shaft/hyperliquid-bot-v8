import time
import ccxt
from config import (
    HYPERLIQUID_API_KEY,
    HYPERLIQUID_API_SECRET,
    PAIRS,
    MIN_COLLATERAL,
    POSITION_SIZE_PCT,
    RESERVE_BALANCE_PCT,
    DEBUG,
    TP_PCT,
    SL_PCT,
)
from utils.logger import Logger
from utils.notifier import Notifier


class HyperliquidTrader:
    def __init__(self):
        self.exchange = ccxt.hyperliquid({
            "walletAddress": HYPERLIQUID_API_KEY,
            "privateKey": HYPERLIQUID_API_SECRET,
            "enableRateLimit": True,
        })
        self.logger = Logger(collection="trades")
        self.notifier = Notifier()
        self.pair = None  # Determine dynamiquement

    def select_pair(self):
        """Choisit la premiere paire pour laquelle on a assez de collateral."""
        balance = self._get_total_balance()
        usable = balance * (1 - RESERVE_BALANCE_PCT)

        for pair in PAIRS:
            min_col = MIN_COLLATERAL.get(pair, 10)
            if usable >= min_col:
                if self.pair != pair and DEBUG:
                    print(f"[TRADER] Paire selectionnee : {pair} (solde utilisable: {usable:.2f})")
                self.pair = pair
                return pair

        print(f"[TRADER] Solde insuffisant ({usable:.2f}) pour toutes les paires")
        self.pair = None
        return None

    def _get_total_balance(self, currency="USDC"):
        try:
            balance = self.exchange.fetch_balance()
            return float(balance["total"].get(currency, 0))
        except Exception as e:
            print(f"[TRADER][ERREUR] fetch_balance: {e}")
            return 0

    def get_usable_balance(self, currency="USDC"):
        total = self._get_total_balance(currency)
        reserve = total * RESERVE_BALANCE_PCT
        usable = total - reserve
        if DEBUG:
            print(f"[TRADER] Solde total={total:.2f}, reserve={reserve:.2f}, utilisable={usable:.2f}")
        return max(usable, 0)

    def get_position_size(self, price):
        balance = self.get_usable_balance()
        amount = (balance * POSITION_SIZE_PCT) / price
        if DEBUG:
            print(f"[TRADER] Position size: {amount:.6f} ({self.pair})")
        return round(amount, 6)

    def place_order_with_tp_sl(self, side, price, tp_pct=None, sl_pct=None):
        """Ouvre une position + TP/SL. Retourne dict avec les infos ou None."""
        if not self.pair:
            print("[TRADER] Aucune paire selectionnee")
            return None

        size = self.get_position_size(price)
        if size <= 0:
            print("[TRADER] Pas assez de solde pour trader")
            return None

        tp_pct = tp_pct or TP_PCT
        sl_pct = sl_pct or SL_PCT

        # Ordre principal
        try:
            main_order = self.exchange.create_order(
                symbol=self.pair,
                type="market",
                side=side,
                amount=size,
                price=price,
                params={"maxSlippagePcnt": 0.01}
            )
            print(f"[TRADER] {side.upper()} {size} {self.pair} @ {price} (order: {main_order.get('id')})")
        except Exception as e:
            print(f"[TRADER][ERREUR] Ordre principal: {e}")
            self.notifier.error(f"Ordre {side} echoue: {e}")
            return None

        # Calcul TP/SL
        if side == "buy":
            tp_price = round(price * (1 + tp_pct), 2)
            sl_price = round(price * (1 - sl_pct), 2)
            closing_side = "sell"
        else:
            tp_price = round(price * (1 - tp_pct), 2)
            sl_price = round(price * (1 + sl_pct), 2)
            closing_side = "buy"

        # Take Profit
        tp_order_id = None
        try:
            tp_order = self.exchange.create_order(
                symbol=self.pair,
                type="market",
                side=closing_side,
                amount=size,
                price=price,
                params={"takeProfitPrice": tp_price, "reduceOnly": True}
            )
            tp_order_id = tp_order.get("id")
            print(f"[TRADER] TP place @ {tp_price} (order: {tp_order_id})")
        except Exception as e:
            print(f"[TRADER][ERREUR] TP: {e}")

        # Stop Loss
        sl_order_id = None
        try:
            sl_order = self.exchange.create_order(
                symbol=self.pair,
                type="market",
                side=closing_side,
                amount=size,
                price=price,
                params={"stopLossPrice": sl_price, "reduceOnly": True}
            )
            sl_order_id = sl_order.get("id")
            print(f"[TRADER] SL place @ {sl_price} (order: {sl_order_id})")
        except Exception as e:
            print(f"[TRADER][ERREUR] SL: {e}")

        # Notification
        self.notifier.trade_opened(self.pair, side, price, size, tp_price, sl_price)

        # Log
        self.logger.log_trade({
            "pair": self.pair,
            "side": side,
            "action": "open",
            "entry_price": price,
            "exit_price": None,
            "size": size,
            "pnl": None,
            "reason": "signal",
            "duration_sec": None,
            "signal_score": None,
            "tp_price": tp_price,
            "sl_price": sl_price,
        })

        return {
            "order": main_order,
            "size": size,
            "entry_price": price,
            "side": side,
            "tp_price": tp_price,
            "sl_price": sl_price,
            "tp_order_id": tp_order_id,
            "sl_order_id": sl_order_id,
        }

    def close_position(self, reason="manual"):
        """Ferme la position ET annule les ordres TP/SL orphelins."""
        if not self.pair:
            return None

        # 1. Annuler tous les ordres ouverts sur cette paire (TP/SL)
        self.cancel_open_orders()

        # 2. Fetcher le prix actuel AVANT de fermer (plus fiable que markPrice)
        try:
            ticker = self.exchange.fetch_ticker(self.pair)
            current_price = float(ticker.get("last", 0))
        except Exception:
            current_price = 0

        # 3. Fermer la position
        positions = self.fetch_positions()
        for pos in positions:
            if pos.get("symbol") == self.pair and float(pos.get("contracts", 0)) > 0:
                amt = abs(float(pos["contracts"]))
                side = "sell" if pos.get("side") == "long" else "buy"
                entry_price = float(pos.get("entryPrice", 0))

                try:
                    order = self.exchange.create_order(
                        symbol=self.pair,
                        type="market",
                        side=side,
                        amount=amt,
                        price=current_price,
                        params={"maxSlippagePcnt": 0.01}
                    )

                    # Récupérer le vrai prix d'exécution depuis l'ordre
                    fill_price = float(order.get("average", 0) or order.get("price", 0) or current_price)
                    if fill_price == 0:
                        fill_price = current_price

                    # Calcul PnL avec le vrai prix d'exécution
                    if pos.get("side") == "long":
                        pnl = (fill_price - entry_price) * amt
                    else:
                        pnl = (entry_price - fill_price) * amt

                    print(f"[TRADER] Position fermee {side} {amt} @ {fill_price:.2f} (entry: {entry_price:.2f}) | PnL: {pnl:+.4f} | Raison: {reason}")

                    self.notifier.trade_closed(self.pair, pos.get("side", side), entry_price, fill_price, pnl, reason)

                    self.logger.log_trade({
                        "pair": self.pair,
                        "side": pos.get("side", side),
                        "action": "close",
                        "entry_price": entry_price,
                        "exit_price": fill_price,
                        "size": amt,
                        "pnl": pnl,
                        "reason": reason,
                        "duration_sec": None,
                        "signal_score": None,
                    })

                    return {"pnl": pnl, "order": order}
                except Exception as e:
                    print(f"[TRADER][ERREUR] Close: {e}")
                    self.notifier.error(f"Fermeture echouee: {e}")
                    return None
        return None

    def cancel_open_orders(self):
        """Annule tous les ordres ouverts (TP/SL) sur la paire."""
        if not self.pair:
            return
        try:
            open_orders = self.exchange.fetch_open_orders(self.pair)
            for order in open_orders:
                try:
                    self.exchange.cancel_order(order["id"], self.pair)
                    if DEBUG:
                        print(f"[TRADER] Ordre annule: {order['id']} ({order.get('type', '?')})")
                except Exception as e:
                    print(f"[TRADER][ERREUR] Cancel order {order['id']}: {e}")
            if open_orders and DEBUG:
                print(f"[TRADER] {len(open_orders)} ordres annules sur {self.pair}")
        except Exception as e:
            print(f"[TRADER][ERREUR] fetch_open_orders: {e}")

    def fetch_positions(self):
        try:
            return self.exchange.fetch_positions([self.pair]) if self.pair else []
        except Exception as e:
            print(f"[TRADER][ERREUR] fetch_positions: {e}")
            return []

    def has_open_position(self):
        """Retourne (bool, position_info) pour la paire courante."""
        positions = self.fetch_positions()
        for pos in positions:
            contracts = float(pos.get("contracts") or 0)
            if pos.get("symbol") == self.pair and contracts > 0:
                return True, {
                    "side": pos.get("side"),
                    "entry_price": float(pos.get("entryPrice") or 0),
                    "contracts": contracts,
                    "mark_price": float(pos.get("markPrice") or 0),
                    "unrealized_pnl": float(pos.get("unrealizedPnl") or 0),
                }
        return False, None


if __name__ == "__main__":
    trader = HyperliquidTrader()
    pair = trader.select_pair()
    print(f"Paire: {pair}")
    print(f"Solde: {trader.get_usable_balance()}")
    print(f"Positions: {trader.fetch_positions()}")
