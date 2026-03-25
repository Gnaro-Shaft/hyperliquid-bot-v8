import asyncio
import os
import json
import time
import csv
from datetime import datetime, timezone
from collections import defaultdict

import websockets
from pymongo import MongoClient, ASCENDING

from config import (
    PAIRS, MONGO_URL, MONGO_DB,
    MONGO_COLLECTION_1M, MONGO_COLLECTION_15M,
    MONGO_COLLECTION_ORDERBOOK, MONGO_COLLECTION_TRADES_MARKET,
    DATA_DIR, DEBUG, DL_SNAPSHOT_INTERVAL,
)

COINS = [pair.split("/")[0] for pair in PAIRS]
PING_INTERVAL = 30
WS_URL = "wss://api.hyperliquid.xyz/ws"

# Seuil "gros trade" en unites du coin
LARGE_TRADE_THRESHOLD = {
    "BTC": 0.5,
    "ADA": 50000,
}


class WebSocketCollector:
    def __init__(self):
        self.mongo = None
        if MONGO_URL:
            try:
                client = MongoClient(MONGO_URL)
                self.mongo = client[MONGO_DB]
                self._mongo_connected = True
                self._ensure_indexes()
            except Exception as e:
                self._mongo_connected = False
                print(f"[COLLECTOR][ERREUR] MongoDB: {e}")
        else:
            self._mongo_connected = False

        os.makedirs(DATA_DIR, exist_ok=True)
        self.csv_files = {
            "1m": os.path.join(DATA_DIR, "ohlc_1m.csv"),
            "15m": os.path.join(DATA_DIR, "ohlc_15m.csv"),
        }
        self.last_candle_time = 0
        self._running = True

        # --- Deep Learning data buffers ---
        self._last_ob_snapshot = defaultdict(float)  # {coin: timestamp}
        self._trade_buffer = defaultdict(lambda: {
            "buy_volume": 0.0, "sell_volume": 0.0,
            "trade_count": 0, "buy_count": 0, "sell_count": 0,
            "large_trades": 0, "minute_ts": 0,
        })

    def _ensure_indexes(self):
        """Cree les index pour les collections DL."""
        try:
            # Orderbook — TTL 60 jours
            self.mongo[MONGO_COLLECTION_ORDERBOOK].create_index(
                [("coin", ASCENDING), ("timestamp", ASCENDING)]
            )
            self.mongo[MONGO_COLLECTION_ORDERBOOK].create_index(
                "created_at", expireAfterSeconds=60 * 86400
            )
            # Market trades
            self.mongo[MONGO_COLLECTION_TRADES_MARKET].create_index(
                [("coin", ASCENDING), ("timestamp", ASCENDING)], unique=True
            )
        except Exception as e:
            print(f"[COLLECTOR] Index creation: {e}")

    @property
    def is_alive(self):
        if self.last_candle_time == 0:
            return False
        return (time.time() - self.last_candle_time) < 300

    def stop(self):
        self._running = False

    async def subscribe(self, ws):
        """Subscribe a tous les channels : candles + l2Book + trades."""
        for coin in COINS:
            # Candles 1m + 15m (existant)
            for tf in ["1m", "15m"]:
                sub = {
                    "method": "subscribe",
                    "subscription": {"type": "candle", "coin": coin, "interval": tf}
                }
                await ws.send(json.dumps(sub))

            # L2 Orderbook (top 5 niveaux)
            sub_ob = {
                "method": "subscribe",
                "subscription": {"type": "l2Book", "coin": coin, "nSigFigs": 5}
            }
            await ws.send(json.dumps(sub_ob))

            # Market trades
            sub_trades = {
                "method": "subscribe",
                "subscription": {"type": "trades", "coin": coin}
            }
            await ws.send(json.dumps(sub_trades))

        print(f"[COLLECTOR] Abonne: {', '.join(COINS)} (candles + orderbook + trades)")

    async def process_message(self, message):
        try:
            msg = json.loads(message)
            channel = msg.get("channel", "")
            data = msg.get("data", {})

            if channel == "candle" and isinstance(data, dict):
                self.handle_candle(data)
            elif channel == "l2Book" and isinstance(data, dict):
                self.handle_orderbook(data)
            elif channel == "trades" and isinstance(data, list):
                for trade in data:
                    self.handle_market_trade(trade)
        except Exception as e:
            print(f"[COLLECTOR][ERREUR] process_message: {e}")

    # ────────────────────── CANDLES (existant) ──────────────────────

    def handle_candle(self, candle):
        tf = candle["i"]
        minute = datetime.fromtimestamp(candle["t"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        bougie = {
            "timestamp": candle["t"],
            "timestamp_end": candle["T"],
            "minute": minute,
            "coin": candle["s"],
            "interval": tf,
            "open": float(candle["o"]),
            "high": float(candle["h"]),
            "low": float(candle["l"]),
            "close": float(candle["c"]),
            "volume": float(candle["v"]),
            "n": int(candle["n"]),
        }

        self.last_candle_time = time.time()

        if self._mongo_connected:
            col = MONGO_COLLECTION_1M if tf == "1m" else MONGO_COLLECTION_15M
            try:
                self.mongo[col].update_one(
                    {"timestamp": bougie["timestamp"], "coin": bougie["coin"]},
                    {"$set": bougie},
                    upsert=True
                )
            except Exception as e:
                print(f"[COLLECTOR][ERREUR][MongoDB] {e}")

        self._save_csv(tf, bougie)

    # ────────────────────── ORDERBOOK (nouveau) ──────────────────────

    def handle_orderbook(self, data):
        """Traite l'orderbook et sauvegarde un snapshot toutes les DL_SNAPSHOT_INTERVAL sec."""
        coin = data.get("coin", "")
        if not coin:
            return

        now = time.time()
        if now - self._last_ob_snapshot[coin] < DL_SNAPSHOT_INTERVAL:
            return  # Pas encore le temps de snapshot

        self._last_ob_snapshot[coin] = now

        levels = data.get("levels", [[], []])
        bids = levels[0] if len(levels) > 0 else []  # [[prix, taille, nb_orders], ...]
        asks = levels[1] if len(levels) > 1 else []

        if not bids or not asks:
            return

        best_bid = float(bids[0].get("px", 0))
        best_ask = float(asks[0].get("px", 0))
        spread = best_ask - best_bid
        mid = (best_ask + best_bid) / 2

        # Depth top 5
        bid_depth = sum(float(b.get("sz", 0)) * float(b.get("px", 0)) for b in bids[:5])
        ask_depth = sum(float(a.get("sz", 0)) * float(a.get("px", 0)) for a in asks[:5])
        total_depth = bid_depth + ask_depth
        imbalance = (bid_depth - ask_depth) / total_depth if total_depth > 0 else 0

        snapshot = {
            "timestamp": int(now * 1000),
            "coin": coin,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread": spread,
            "spread_pct": spread / mid if mid > 0 else 0,
            "bid_depth_5": round(bid_depth, 2),
            "ask_depth_5": round(ask_depth, 2),
            "imbalance": round(imbalance, 4),
            "created_at": datetime.utcnow(),
        }

        if self._mongo_connected:
            try:
                self.mongo[MONGO_COLLECTION_ORDERBOOK].insert_one(snapshot)
            except Exception as e:
                print(f"[COLLECTOR][ERREUR][OB] {e}")

    # ────────────────────── MARKET TRADES (nouveau) ──────────────────────

    def handle_market_trade(self, trade):
        """Agrege les trades par minute et flush dans MongoDB."""
        coin = trade.get("coin", "")
        if not coin:
            return

        size = float(trade.get("sz", 0))
        side = trade.get("side", "").upper()
        ts = int(trade.get("time", time.time() * 1000))
        minute_ts = (ts // 60000) * 60000  # Arrondi a la minute

        buf = self._trade_buffer[coin]

        # Nouvelle minute → flush l'ancienne
        if buf["minute_ts"] != 0 and buf["minute_ts"] != minute_ts:
            self._flush_trade_buffer(coin)

        buf["minute_ts"] = minute_ts
        buf["trade_count"] += 1

        if side == "B" or side == "BUY":
            buf["buy_volume"] += size
            buf["buy_count"] += 1
        else:
            buf["sell_volume"] += size
            buf["sell_count"] += 1

        threshold = LARGE_TRADE_THRESHOLD.get(coin, 1.0)
        if size >= threshold:
            buf["large_trades"] += 1

    def _flush_trade_buffer(self, coin):
        """Ecrit le buffer de trades agregés dans MongoDB."""
        buf = self._trade_buffer[coin]
        if buf["trade_count"] == 0:
            return

        doc = {
            "timestamp": buf["minute_ts"],
            "coin": coin,
            "buy_volume": round(buf["buy_volume"], 6),
            "sell_volume": round(buf["sell_volume"], 6),
            "trade_count": buf["trade_count"],
            "buy_count": buf["buy_count"],
            "sell_count": buf["sell_count"],
            "large_trades": buf["large_trades"],
        }

        if self._mongo_connected:
            try:
                self.mongo[MONGO_COLLECTION_TRADES_MARKET].update_one(
                    {"timestamp": doc["timestamp"], "coin": coin},
                    {"$set": doc},
                    upsert=True
                )
            except Exception as e:
                print(f"[COLLECTOR][ERREUR][TRADES] {e}")

        # Reset buffer
        self._trade_buffer[coin] = {
            "buy_volume": 0.0, "sell_volume": 0.0,
            "trade_count": 0, "buy_count": 0, "sell_count": 0,
            "large_trades": 0, "minute_ts": 0,
        }

    # ────────────────────── CSV (existant) ──────────────────────

    def _save_csv(self, tf, bougie):
        csv_file = self.csv_files.get(tf)
        if not csv_file:
            return
        file_exists = os.path.isfile(csv_file)
        try:
            with open(csv_file, "a", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=list(bougie.keys()))
                if not file_exists:
                    writer.writeheader()
                writer.writerow(bougie)
        except Exception as e:
            print(f"[COLLECTOR][ERREUR][CSV] {e}")

    # ────────────────────── LIFECYCLE ──────────────────────

    async def heartbeat(self, ws):
        while self._running:
            try:
                await ws.ping()
            except Exception:
                break
            await asyncio.sleep(PING_INTERVAL)

    async def periodic_flush(self):
        """Flush les trade buffers periodiquement (au cas ou pas de nouveau trade)."""
        while self._running:
            await asyncio.sleep(60)
            for coin in COINS:
                self._flush_trade_buffer(coin)

    async def collect(self):
        while self._running:
            try:
                async with websockets.connect(WS_URL, ping_interval=None) as ws:
                    print("[COLLECTOR] WebSocket connecte.")
                    await self.subscribe(ws)
                    heartbeat_task = asyncio.create_task(self.heartbeat(ws))
                    flush_task = asyncio.create_task(self.periodic_flush())
                    try:
                        async for message in ws:
                            if not self._running:
                                break
                            await self.process_message(message)
                    finally:
                        heartbeat_task.cancel()
                        flush_task.cancel()
            except Exception as e:
                print(f"[COLLECTOR][ERREUR] Deconnexion WebSocket: {e}")
                if self._running:
                    await asyncio.sleep(5)


if __name__ == "__main__":
    collector = WebSocketCollector()
    try:
        asyncio.run(collector.collect())
    except KeyboardInterrupt:
        collector.stop()
        print("[COLLECTOR] Arret propre.")
