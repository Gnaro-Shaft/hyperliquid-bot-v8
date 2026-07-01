# Carry manager sur homeServ01 (surveillance 24/7)

Faire tourner `carry_manager.py` en cron horaire sur le serveur toujours allumé
(le Mac dort → le cron ne tourne pas ; le serveur, si). Isolé du paper Docker.

## 🔒 Sécurité d'abord
Sur le serveur, utilise une **API wallet Hyperliquid** (wallet "agent" : peut trader,
**pas** retirer). Génère-la sur l'app HL → Settings → API. Si le serveur est
compromis, l'attaquant ne peut pas vider le compte.

## Étapes (sur homeServ01)

```bash
# 1. Cloner le repo dans un dossier dédié (isolé du paper Docker) + branche carry
cd ~
git clone https://github.com/Gnaro-Shaft/hyperliquid-bot-v8.git carry-bot
cd carry-bot
git checkout carry-strategy

# 2. Environnement Python isolé + dépendances
python3 -m venv venv
source venv/bin/activate
pip install ccxt pymongo python-dotenv

# 3. Fichier .env (clés API-wallet HL + Mongo)
cat > .env <<'EOF'
HYPERLIQUID_API_KEY=<adresse_wallet_agent>
HYPERLIQUID_API_SECRET=<cle_privee_agent>
MONGO_URL=<ton_uri_atlas>
EOF
chmod 600 .env

# 4. Test en DRY-RUN (lit le compte, ne ferme rien)
python carry/carry_manager.py
#   → doit afficher "=== CARRY MANAGER ===" avec funding + position

# 5. Chemin absolu du python du venv (pour le cron)
echo "$(pwd)/venv/bin/python"

# 6. Cron horaire (auto-close actif). crontab -e puis :
#    0 * * * * cd ~/carry-bot && CARRY_LIVE=true ~/carry-bot/venv/bin/python carry/carry_manager.py >> ~/carry-bot/carry_mgr.log 2>&1

# 7. Vérifier après le passage de l'heure pleine
cat ~/carry-bot/carry_mgr.log
```

## Rappels
- `crontab -l` pour confirmer que la ligne est enregistrée.
- Le manager ferme auto si : funding 7j < 2% sur 2 checks, OU buffer liquidation < 10%.
- Pour mettre à jour le code plus tard : `cd ~/carry-bot && git pull`.
- L'OUVERTURE reste manuelle (depuis ton Mac ou le serveur) : `CARRY_LIVE=true python carry/carry_live.py --open`.
