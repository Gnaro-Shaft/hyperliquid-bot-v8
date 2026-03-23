import asyncio
import os
import json
import time
import csv
from datetime import datetime, timezone

import websockets
from pymongo import MongoClient

from config import (
    PAIRS, MONGO_URL, MONGO_DB,
    MONGO_COLLECTION_1M, MONGO_COLLECTION_15M,
    DATA_DIR, DEBUG,
)

COINS = [pair.split("/")[0] for pair in PAIRS]
PING_INTERVAL = 30
WS_URL = "wss://api.hyperliquid.xyz/ws"


class WebSocketCollector:
    def __init__(self):
        self.mongo = None
        if MONGO_URL:
            try:
                client = MongoClient(MONGO_URL)
                self.mongo = client[MONGO_DB]
                self._mongo_connected = True
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
        self.last_candle_time = 0  # Timestamp de la derniere bougie recue
        self._running = True

    @property
    def is_alive(self):
        """Retourne True si une bougie a ete recue dans les 5 dernières minutes."""
        if self.last_candle_time == 0:
            return False
        return (time.time() - self.last_candle_time) < 300

    def stop(self):
        self._running = False

    async def subscribe(self, ws):
        for coin in COINS:
            for tf in ["1m", "15m"]:
                sub = {
                    "method": "subscribe",
                    "subscription": {
                        "type": "candle",
                        "coin": coin,
                        "interval": tf
                    }
                }
                await ws.send(json.dumps(sub))
                if DEBUG:
                    print(f"[COLLECTOR] Abonne: {coin} {tf}")

    async def process_message(self, message):
        try:
            msg = json.loads(message)
            if msg.get("channel") == "candle" and isinstance(msg.get("data"), dict):
                self.handle_candle(msg["data"])
        except Exception as e:
            print(f"[COLLECTOR][ERREUR] process_message: {e}")

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

        # MongoDB upsert
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

        # CSV
        self._save_csv(tf, bougie)

        if DEBUG:
            print(f"[Bougie {tf}] {minute} {bougie['coin']} O:{bougie['open']} C:{bougie['close']} V:{bougie['volume']}")

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

    async def heartbeat(self, ws):
        while self._running:
            try:
                await ws.ping()
            except Exception:
                break
            await asyncio.sleep(PING_INTERVAL)

    async def collect(self):
        while self._running:
            try:
                async with websockets.connect(WS_URL, ping_interval=None) as ws:
                    print("[COLLECTOR] WebSocket connecte.")
                    await self.subscribe(ws)
                    heartbeat_task = asyncio.create_task(self.heartbeat(ws))
                    try:
                        async for message in ws:
                            if not self._running:
                                break
                            await self.process_message(message)
                    finally:
                        heartbeat_task.cancel()
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
