# Bot de Trading Hyperliquid — v8

Bot de trading automatisé sur **Hyperliquid** (perpetual futures), avec scoring
multi-timeframe, filtre ML, gestion du risque et backtests automatiques.

> ⚠️ **Trading réel.** Ce bot passe de vrais ordres avec de vrais fonds. Il n'y a
> **pas de mode paper-trading** — le seul mode « à blanc » est le backtest sur
> données historiques (voir plus bas). Utilisez-le en connaissance de cause.

---

## Stack

- **Python 3.11**
- Exchange : Hyperliquid via `ccxt`
- Données : MongoDB Atlas (backbone — collecte, signaux, trades, état risque)
- Notifications : Telegram
- Déploiement : Fly.io (Docker), région `cdg` (Paris)

Paires tradées (`config.py`) : `SOL/USDC:USDC`, `BTC/USDC:USDC`.

---

## Installation

```bash
git clone <repo>
cd v8
python3.11 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

## Configuration (variables d'environnement)

Créer un fichier `.env` à la racine (jamais commité — voir `.gitignore`) :

```env
# Hyperliquid (clés API du wallet de trading)
HYPERLIQUID_API_KEY=0x...
HYPERLIQUID_API_SECRET=0x...

# MongoDB (OBLIGATOIRE — le bot ne génère aucun signal sans Mongo)
MONGO_URL=mongodb+srv://user:pass@cluster.mongodb.net/...

# Telegram (alertes et rapports)
TELEGRAM_TOKEN=123456:ABC...
TELEGRAM_CHAT_ID=123456789
```

> 🔴 **MongoDB est une dépendance dure.** Toute la pipeline live passe par Mongo
> (collector → MongoDB → stratégie). Si `MONGO_URL` est absent ou injoignable, le
> bot tourne mais ne produit aucun signal exploitable.

---

## Lancer le bot (live)

```bash
python main.py
```

Le bot démarre les collectors (WebSocket + REST), la stratégie, le risk manager,
l'AutoTrainer ML et le BacktestScheduler, puis trade en continu.

### Arrêter / mettre en pause

- **Pause trading** (process maintenu) : créer un fichier nommé `KILL` à la racine.
  Le bot cesse d'ouvrir des positions tant que le fichier existe (`config.py:KILL_SWITCH_FILE`).
  Le supprimer reprend le trading.
- **Arrêt complet en local** : `Ctrl-C` (arrêt propre, code 0).
- **En production (Fly)** : `fly scale count 0 -a hyperliquid-bot-v8` (le `[[restart]]
  policy = 'always'` redémarre sinon le process à chaque sortie).

---

## Backtest (mode « à blanc »)

```bash
# Backtest 90 jours sur BTC
python backtest/backtest.py --coin BTC --days 90

# Fenêtre de dates explicite + export CSV des trades
python backtest/backtest.py --coin SOL --from 2026-04-01 --to 2026-06-01 --export

# Solde initial personnalisé
python backtest/backtest.py --coin BTC --days 30 --equity 500
```

Le backtest simule frais (0.1% aller-retour), trailing stop, breakeven et sizing
ATR. Données lues depuis MongoDB.

---

## Modèle ML (filtre de signaux)

```bash
# Entraîner les modèles (BTC + SOL) sur 90 jours
python ml/train_model.py --days 90
```

Les modèles (`ml/models/*.pkl`) filtrent les signaux ±2 : bloque sous 0.38 de
confiance, pénalise sous 0.48. L'`AutoTrainer` réentraîne automatiquement toutes
les 6h avec garde-fous (champion/challenger, holdout, anti-régression, circuit-breaker).

> ⚠️ Les `.pkl` doivent être entraînés avec la **même version de scikit-learn** que
> le conteneur (`scikit-learn==1.9.0`, épinglé dans `requirements.txt`), sinon le
> chargement échoue (`No module named '_loss'`).

---

## Déploiement (Fly.io)

```bash
fly deploy            # build + push + rolling restart
fly logs              # logs en direct
fly status            # état de la machine
```

L'état (PnL, risk, modèles ML) est persisté dans MongoDB → survit aux redémarrages.

---

## Architecture

```
main.py (TradingBot)
 ├── collector/   WebSocket + REST  → MongoDB (ohlc, orderbook, funding, OI, trades)
 ├── strategy/    StrategyEngine     → score multi-timeframe ±2 (15m + 1m + 1h + gate ML)
 ├── risk/        RiskManager        → sizing, pertes consécutives, drawdown, cooldown
 ├── trader/      HyperliquidTrader  → ccxt → exchange
 ├── ml/          predictor + train_model + auto_trainer
 ├── backtest/    backtest + scheduler (rapport hebdo Telegram)
 └── utils/       logger, notifier
```

### Garde-fous risque (`config.py`)

| Paramètre | Valeur |
|---|---|
| Taille position | 30 % du solde utilisable |
| Réserve de solde | 20 % |
| Stop après N pertes consécutives | 3 |
| Drawdown journalier max | −5 % |

---

## Tests

```bash
pytest tests/
```

Couvre le risk manager, le calcul TP/SL, le sizing et la fenêtre du rapport
journalier. (Voir `tests/` — suite en cours d'extension.)
