import os
from dotenv import load_dotenv

load_dotenv()

# === MODE PAPER TRADING (v8.11) ===
# Si PAPER_MODE=true (env), le bot reçoit les vrais prix mais N'ENVOIE AUCUN ordre :
# positions, TP/SL et PnL sont simulés. Défaut = false (= trading réel).
PAPER_MODE = os.getenv("PAPER_MODE", "false").strip().lower() in ("1", "true", "yes")
PAPER_START_BALANCE = float(os.getenv("PAPER_START_BALANCE", "1000"))

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
TRAIL_PCT = 0.006               # Trailing Stop 0.6% — assez large pour capturer vrai mouvement
TRAILING_TRIGGER_PCT = 0.010    # Active le trailing apres +1.0% (était 1.2%)
TRAILING_STEP_PCT = 0.003       # Rehausse le stop tous les +0.3%

# === BREAKEVEN STOP ===
# Seuil minimum pour couvrir les frais Hyperliquid (~0.1% aller-retour sur 30% de position)
# Monté à 1.0% (était 0.5%) — évite les sorties breakeven prématurées, améliore R:R 0.6→1.4
BREAKEVEN_TRIGGER_PCT = 0.010   # Protéger seulement après +1.0% (était 0.5%)
BREAKEVEN_OFFSET_PCT = 0.002    # SL placé à entry + 0.2% (buffer net positif garanti)

# === ANTI-OVERTRADING ===
MAX_CONSECUTIVE_LOSSES = 3
PAUSE_DURATION_MINUTES = 15
MAX_DAILY_DRAWDOWN_PCT = 0.05   # Arret si -5% du solde initial du jour

# === LIMITES D'EXPOSITION GLOBALE (v8.9) ===
MAX_OPEN_POSITIONS       = 2     # Nb max de positions simultanees (toutes paires)
MAX_POSITIONS_PER_DIR    = 1     # Nb max de positions dans la meme direction (long OU short)
MAX_TOTAL_EXPOSURE_PCT   = 0.60  # Exposition notionnelle totale max (% du solde)

# === HEALTHCHECK AUTONOME (v8.9) ===
HEALTH_CHECK_INTERVAL_SEC = 300    # Verification toutes les 5 min
HEALTH_MAX_1M_AGE_SEC     = 300    # Bougie 1m la plus recente doit dater de < 5 min
HEALTH_MAX_15M_AGE_SEC    = 2400   # Bougie 15m la plus recente doit dater de < 40 min
HEALTH_MAX_CONSEC_ERRORS  = 5      # Alerte au-dela de N erreurs consecutives

# === CIRCUIT BREAKER MARCHE (v8.9, recalibre Axe A le 28/06/2026) ===
# Bloque les ENTREES pendant des conditions de marche extremes.
# Seuils recales sur les queues reelles (ATR median 0.12% / max 1.6% ; funding
# max 0.01%) : avant, les seuils n'etaient JAMAIS atteints (circuit breaker mort).
CB_MAX_ATR_PCT          = 0.02     # Volatilite anormale si ATR > 2% (~1.25x le max observe)
CB_MAX_ABS_FUNDING      = 0.0002   # Funding extreme si |funding| > 0.02% (~2x le max observe)
CB_MAX_CANDLE_RANGE_PCT = 0.04     # Bougie enorme si range 15m (high-low)/close > 4%
CB_MAX_SPREAD_PCT       = 0.0005   # Spread trop large > 0.05% (branche Axe B ; median ~0.0015%)
CB_MIN_OB_DEPTH_RATIO   = 0.25     # Liquidite : bloque si depth courant < 25% de la moyenne recente

# === COOLDOWN DYNAMIQUE ===
COOLDOWN_BASE_SEC  = 600        # 10 min de base entre deux trades
COOLDOWN_MIN_SEC   = 300        # 5 min minimum (après gains consécutifs)
COOLDOWN_MAX_SEC   = 3600       # 60 min maximum (après pertes consécutives)
COOLDOWN_LOSS_MULT = 1.5        # ×1.5 après chaque perte  (10→15→22→34→51→60 min)
COOLDOWN_WIN_MULT  = 0.75       # ×0.75 après chaque gain  (10→7.5→5 min)
COOLDOWN_BETWEEN_TRADES_SEC = COOLDOWN_BASE_SEC  # alias rétrocompat (risk_manager, backtest)

# === SIGNAL CONFIRMATION ===
SIGNAL_CONFIRM_COUNT = 3        # Nombre de scores forts consecutifs requis

# === LOOP TIMING ===
LOOP_INTERVAL = 15              # Boucle principale (secondes)
TRAILING_CHECK_INTERVAL = 3     # Check trailing quand position active (secondes)

# === PULLBACK ENTRY ===
PULLBACK_PCT = 0.0015           # Recul attendu avant entrée (0.15%)
PULLBACK_EXPIRY_SEC = 45        # Délai max avant entrée au marché (3 candles × 15s)

# === AUTO-CALIBRATION SEUIL ===
AUTOCAL_LOOKBACK_TRADES = 20    # Nb de trades récents pour calibrer
SIGNAL_THRESHOLD_DEFAULT = 9    # Seuil de score par défaut
SIGNAL_THRESHOLD_MIN = 7        # Plancher (plus permissif = plus de trades)
SIGNAL_THRESHOLD_MAX = 10       # Plafond (plus sélectif = moins de trades)

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
MONGO_COLLECTION_1H = "ohlc_1h"

# === DEEP LEARNING DATA COLLECTIONS ===
MONGO_COLLECTION_ORDERBOOK = "orderbook_snapshots"
MONGO_COLLECTION_FUNDING = "funding_rates"
MONGO_COLLECTION_OI = "open_interest"
MONGO_COLLECTION_TRADES_MARKET = "market_trades"

# Observabilite (v8.10 — consomme par le GCN Dashboard)
MONGO_COLLECTION_DECISIONS = "decisions"      # journal de decision (ouvert/refuse)
MONGO_COLLECTION_BOT_STATUS = "bot_status"    # heartbeat etat du bot (doc _id="current")

# Paper trading (v8.11)
MONGO_COLLECTION_PAPER_TRADES = "paper_trades"  # trades simules
MONGO_COLLECTION_PAPER_STATE = "paper_state"    # etat paper persiste (doc _id="current")

# === ADAPTATION PAR RÉGIME (Phase 4 / v8.12) ===
# Si actif, TP/SL/taille/seuil s'adaptent au régime de marché (STRONG/WEAK/
# HIGH_VOL/RANGE/SQUEEZE). Désactivable pour comparer (A/B). Défaut true.
REGIME_ADAPTIVE = os.getenv("REGIME_ADAPTIVE", "true").strip().lower() in ("1", "true", "yes")
REGIME_HIGH_VOL_ATR_PCT = 0.010   # ATR% au-delà → régime HIGH_VOL (abaissé 0.015→0.010 le 28/06, sinon dormant)

# === BACKTEST RÉALISTE (v8.6) ===
# Coûts d'exécution adverses appliqués à l'entrée et à la sortie du backtest,
# en plus des frais 0.1% A/R. Rend les résultats moins optimistes.
BT_SLIPPAGE_PCT = 0.0003   # slippage adverse (0.03% par exécution)
BT_SPREAD_PCT   = 0.0002   # spread moyen (0.02%) → demi-spread payé par leg
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
