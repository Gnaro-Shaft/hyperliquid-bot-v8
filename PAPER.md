# Paper trading sur serveur (Docker)

Faire tourner le bot en **mode simulation** (vrais prix Hyperliquid, **aucun ordre
réel**) sur le homelab, dans un environnement **100% isolé** : sa propre base
MongoDB locale, séparée de la prod. Aucune pollution des données live.

## Pourquoi isolé ?
Le bot live (sur Fly) écrit ses signaux/décisions/trades dans MongoDB Atlas. Le
paper tourne sur une **base Mongo locale dédiée** (conteneur `mongo`) → les deux
n'entrent jamais en collision. Le paper refait sa propre collecte de prix de son côté.

## Prérequis
- Docker + Docker Compose sur le serveur.
- Le repo cloné (`git clone … && cd v8`).
- Internet (données marché Hyperliquid publiques).
- ❌ Pas besoin des clés Hyperliquid (le PaperTrader ne trade pas).

## Lancement
```bash
cp .env.paper.example .env.paper     # remplir Telegram si voulu (optionnel)
docker compose -f docker-compose.paper.yml up -d --build
```
C'est tout. Deux conteneurs démarrent : `hl-paper-mongo` (base isolée) et
`hl-bot-paper` (le bot en PAPER_MODE).

## Suivi / exploitation
```bash
docker compose -f docker-compose.paper.yml logs -f bot-paper   # logs en direct
docker compose -f docker-compose.paper.yml ps                  # état
docker compose -f docker-compose.paper.yml down                # arrêter
docker compose -f docker-compose.paper.yml up -d --build       # mettre à jour (après git pull)
```
Trades simulés dans la base paper : collections `paper_trades` / `paper_state`
(+ `signals`, `decisions`, `bot_status` propres à cette base).

## ⏳ Warm-up (important)
La base paper démarre **vide** → la stratégie a besoin d'environ **37 h de bougies
15m** (150 bougies) avant de produire des signaux. Deux choix :

- **Patienter** 1-2 jours que l'historique s'accumule (le plus simple).
- **Seed depuis la prod** (départ immédiat avec l'historique) :
  ```bash
  # 1. dump de la base prod (Atlas) — adapter l'URI
  mongodump --uri "$BOT_MONGODB_URI" --db bot_hyperliquid -o /tmp/dump
  # 2. exposer le mongo paper : décommenter le bloc ports (27018) dans le compose, puis up -d
  # 3. restore dans le mongo paper
  mongorestore --uri "mongodb://localhost:27018" --db bot_hyperliquid /tmp/dump/bot_hyperliquid
  ```
  (Optionnel : ne restaurer que les collections de marché ohlc_*/orderbook_*/funding_*/open_interest
  pour ne pas importer les trades/décisions de prod.)

## Mode LIVE = inchangé
La prod (Fly) n'est pas affectée : `PAPER_MODE` n'est défini qu'ici. Sans ce flag,
le bot reste en LIVE. Voir aussi `README.md`.
