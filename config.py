import os
from dotenv import load_dotenv

load_dotenv()

# === EXCHANGE & PAIRS ===
PAIRS = ["SOL/USDC:USDC", "BTC/USDC:USDC"]

MIN_COLLATERAL = {
    "SOL/USDC:USDC": 10,
    "BTC/USDC:USDC": 10,
}

# === MONEY MANAGEMENT ===
POSITION_SIZE_PCT = 0.30        # 30% du solde par trade
RESERVE_BALANCE_PCT = 0.20      # 20% toujours en reserve

# === TIMEFRAMES ===
TIMEFRAMES = {
    "main": "1m",
    "confirm": "15m"
}

# === SIGNALS (SCORING 5 NIVEAUX) ===
LEVELS = {
    -2: {"label": "Vente forte",  "color": "\U0001f534"},
    -1: {"label": "Vente legere", "color": "\U0001f7e0"},
     0: {"label": "Neutre",       "color": "\u26aa\ufe0f"},
     1: {"label": "Achat leger",  "color": "\U0001f7e2"},
     2: {"label": "Achat fort",   "color": "\U0001f7e9"},
}

# === SL / TP / TRAILING ===
SL_PCT = 0.012                  # Stop Loss 1.2%
TP_PCT = 0.03                   # Take Profit 3% (R:R = 2.5:1)
MIN_TP_PCT = 0.02               # TP minimum 2%
TRAIL_PCT = 0.006               # Trailing Stop 0.6% (fallback)
TRAILING_TRIGGER_PCT = 0.008    # Active le trailing apres +0.8% de gain
TRAILING_STEP_PCT = 0.003       # Rehausse le stop tous les +0.3%

# === ANTI-OVERTRADING ===
MAX_CONSECUTIVE_LOSSES = 3
PAUSE_DURATION_MINUTES = 30
MAX_DAILY_DRAWDOWN_PCT = 0.05   # Arret si -5% du solde initial du jour
COOLDOWN_BETWEEN_TRADES_SEC = 2400  # 40 min entre deux trades

# === SIGNAL CONFIRMATION ===
SIGNAL_CONFIRM_COUNT = 2        # Nombre de scores forts consecutifs requis

# === NOTIFICATIONS TELEGRAM ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# === MONGODB ===
MONGO_URL = os.getenv("MONGO_URL", "")
MONGO_DB = "bot_hyperliquid"
MONGO_COLLECTION_TRADES = "trades"
MONGO_COLLECTION_SIGNALS = "signals"
MONGO_COLLECTION_1M = "ohlc_1m"
MONGO_COLLECTION_15M = "ohlc_15m"

# === DEEP LEARNING DATA COLLECTIONS ===
MONGO_COLLECTION_ORDERBOOK = "orderbook_snapshots"
MONGO_COLLECTION_FUNDING = "funding_rates"
MONGO_COLLECTION_OI = "open_interest"
MONGO_COLLECTION_TRADES_MARKET = "market_trades"
DL_SNAPSHOT_INTERVAL = 30       # Secondes entre snapshots orderbook
DL_REST_INTERVAL = 300          # Secondes entre polls REST (funding/OI)

# === CSV ===
DATA_DIR = "data"
CSV_TRADES = os.path.join(DATA_DIR, "trades.csv")
CSV_SIGNALS = os.path.join(DATA_DIR, "signals.csv")

# === API KEYS ===
HYPERLIQUID_API_KEY = os.getenv("HYPERLIQUID_API_KEY", "")
HYPERLIQUID_API_SECRET = os.getenv("HYPERLIQUID_API_SECRET", "")

# === DEBUG ===
DEBUG = os.getenv("DEBUG", "true").lower() == "true"

# === KILL SWITCH ===
KILL_SWITCH_FILE = "KILL"  # Creer ce fichier pour arreter le bot
