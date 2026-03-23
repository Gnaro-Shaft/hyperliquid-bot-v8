import os
import csv
from pymongo import MongoClient
from datetime import datetime, timezone

from config import (
    MONGO_URL, MONGO_DB, DATA_DIR,
    MONGO_COLLECTION_TRADES, MONGO_COLLECTION_SIGNALS,
    DEBUG,
)


class Logger:
    def __init__(self, collection="signals"):
        self.mongo_ready = False
        self.csv_ready = False
        self.collection_name = collection

        # Init Mongo
        if MONGO_URL:
            try:
                client = MongoClient(MONGO_URL, serverSelectionTimeoutMS=5000)
                client.admin.command("ping")
                self.db = client[MONGO_DB]
                self.col = self.db[collection]
                self.mongo_ready = True
                if DEBUG:
                    print(f"[LOGGER] MongoDB connecte (collection: {collection})")
            except Exception as e:
                print(f"[LOGGER][ERREUR] MongoDB: {e}")
        else:
            print("[LOGGER] MONGO_URL non configure, logs MongoDB desactives")

        # Init CSV
        os.makedirs(DATA_DIR, exist_ok=True)
        self.csv_signals = os.path.join(DATA_DIR, "signals.csv")
        self.csv_trades = os.path.join(DATA_DIR, "trades.csv")
        self.csv_ready = True

    def log_signal(self, info):
        """Log un signal. Dedup via upsert sur timestamp+coin."""
        info = info.copy()
        info["timestamp"] = int(info.get("timestamp") or datetime.now(timezone.utc).timestamp() * 1000)
        info["minute"] = info.get("minute") or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        info["details"] = str(info.get("debug", {}))

        # Mongo — upsert pour eviter les doublons
        if self.mongo_ready:
            try:
                self.col.update_one(
                    {"timestamp": info["timestamp"], "coin": info.get("coin", "")},
                    {"$set": info},
                    upsert=True
                )
            except Exception as e:
                print(f"[LOGGER][ERREUR] Mongo insert signal: {e}")

        # CSV
        if self.csv_ready:
            self._append_csv(self.csv_signals, [
                "timestamp", "minute", "coin", "interval",
                "score", "raw_score", "label", "color", "details"
            ], info)

    def log_trade(self, trade_info):
        """Log un trade (ouverture ou fermeture).

        trade_info attendu :
            pair, side, action (open/close), entry_price, exit_price,
            size, pnl, reason, duration_sec, signal_score, timestamp
        """
        trade = trade_info.copy()
        trade["timestamp"] = trade.get("timestamp") or int(datetime.now(timezone.utc).timestamp() * 1000)
        trade["datetime"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        # Mongo
        if self.mongo_ready:
            try:
                trades_col = self.db[MONGO_COLLECTION_TRADES]
                trades_col.insert_one(trade)
            except Exception as e:
                print(f"[LOGGER][ERREUR] Mongo insert trade: {e}")

        # CSV
        if self.csv_ready:
            self._append_csv(self.csv_trades, [
                "datetime", "pair", "side", "action",
                "entry_price", "exit_price", "size",
                "pnl", "reason", "duration_sec", "signal_score"
            ], trade)

    def _append_csv(self, filepath, fieldnames, data):
        file_exists = os.path.isfile(filepath)
        try:
            with open(filepath, "a", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
                if not file_exists:
                    writer.writeheader()
                writer.writerow(data)
        except Exception as e:
            print(f"[LOGGER][ERREUR] CSV write {filepath}: {e}")


if __name__ == "__main__":
    logger = Logger(collection="signals")
    logger.log_signal({
        "coin": "BTC", "interval": "1m", "score": 2, "raw_score": 7,
        "label": "Achat fort", "color": "green", "debug": {"test": True}
    })
    logger.log_trade({
        "pair": "BTC/USDC:USDC", "side": "buy", "action": "close",
        "entry_price": 100000, "exit_price": 101000, "size": 0.001,
        "pnl": 1.0, "reason": "TP hit", "duration_sec": 300, "signal_score": 2
    })
    print("Logs ecrits.")
