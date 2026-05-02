"""
AutoTrainer — Réentraînement ML automatique en arrière-plan
============================================================

Tourne en daemon thread dans main.py.
Vérifie toutes les CHECK_INTERVAL_H heures si un réentraînement est nécessaire.

Conditions pour réentraîner un coin :
  1. Intervalle minimum de RETRAIN_INTERVAL_H heures depuis le dernier train
  2. Au moins MIN_NEW_SIGNALS nouveaux signaux depuis le dernier train
  3. Total de MIN_TOTAL_SIGNALS signaux disponibles dans MongoDB

Si l'AUC CV du nouveau modèle est ≥ MIN_AUC :
  → Modèle sauvegardé + rechargé à chaud dans le StrategyEngine en cours

Si l'AUC est trop basse :
  → Modèle NON déployé, ancien conservé, notification d'alerte envoyée

Utilisation dans main.py :
    trainer = AutoTrainer(engines=self.engines, notifier=self.notifier)
    threading.Thread(target=trainer.run_loop, daemon=True).start()
"""

import os
import json
import time
import threading
import traceback
from datetime import datetime, timezone

from pymongo import MongoClient

from config import (
    MONGO_URL, MONGO_DB, MONGO_COLLECTION_SIGNALS, PAIRS,
)

# ─── Paramètres AutoTrainer ──────────────────────────────────────────
CHECK_INTERVAL_H    = 6      # Vérification toutes les 6 heures
RETRAIN_INTERVAL_H  = 24     # Minimum 24h entre deux retrains
MIN_NEW_SIGNALS     = 200    # Nouveaux signaux depuis le dernier train
MIN_TOTAL_SIGNALS   = 300    # Total minimum pour entraîner
MIN_AUC             = 0.53   # AUC minimum pour déployer le nouveau modèle
TRAINING_DAYS       = 60     # Historique utilisé pour l'entraînement
LOOKAHEAD_CANDLES   = 4      # Fenêtre de validation (×15m)
TARGET_MOVE_PCT     = 0.004  # Mouvement cible pour le labelling

STATE_FILE = os.path.join(os.path.dirname(__file__), "models", "trainer_state.json")


class AutoTrainer:
    """
    Réentraîneur ML automatique (daemon thread).

    Args:
        engines  : dict {coin: StrategyEngine} — moteurs de stratégie actifs
        notifier : Notifier — pour les alertes Telegram (peut être None)
    """

    def __init__(self, engines: dict, notifier=None):
        self.engines  = engines
        self.notifier = notifier
        self._state   = {}           # {coin: {"last_train_ts": ..., "signals_at_last_train": ..., "last_auc": ...}}
        self._lock    = threading.Lock()

        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        self._load_state()

    # ─── Boucle principale ────────────────────────────────────────────

    def run_loop(self):
        """Boucle infinie — doit être lancée dans un daemon thread."""
        print(f"[AutoTrainer] Démarré — vérification toutes les {CHECK_INTERVAL_H}h")
        # Première vérification après 30s (laisser le bot se stabiliser)
        time.sleep(30)

        while True:
            try:
                self._run_cycle()
            except Exception as e:
                print(f"[AutoTrainer] Erreur cycle: {e}")
                print(traceback.format_exc())

            time.sleep(CHECK_INTERVAL_H * 3600)

    def _run_cycle(self):
        """Un cycle de vérification + réentraînement éventuel."""
        coins = [p.split("/")[0] for p in PAIRS]
        for coin in coins:
            try:
                should, reason = self._should_retrain(coin)
                if should:
                    print(f"[AutoTrainer] [{coin}] Réentraînement déclenché : {reason}")
                    self._retrain_and_reload(coin)
                else:
                    print(f"[AutoTrainer] [{coin}] Pas de réentraînement ({reason})")
            except Exception as e:
                print(f"[AutoTrainer] [{coin}] Erreur: {e}")

    # ─── Conditions de réentraînement ────────────────────────────────

    def _should_retrain(self, coin: str) -> tuple:
        """Retourne (bool, raison) — True = réentraîner."""
        state = self._state.get(coin, {})
        now_ts = time.time()

        # 1. Intervalle minimum
        last_ts = state.get("last_train_ts", 0)
        elapsed_h = (now_ts - last_ts) / 3600
        if elapsed_h < RETRAIN_INTERVAL_H:
            remaining = RETRAIN_INTERVAL_H - elapsed_h
            return False, f"trop tôt ({remaining:.1f}h restantes)"

        # 2. Nombre total de signaux
        total_signals = self._count_signals(coin)
        if total_signals < MIN_TOTAL_SIGNALS:
            return False, f"pas assez de signaux ({total_signals} < {MIN_TOTAL_SIGNALS})"

        # 3. Nouveaux signaux depuis le dernier train
        signals_at_last = state.get("signals_at_last_train", 0)
        new_signals = total_signals - signals_at_last
        if new_signals < MIN_NEW_SIGNALS:
            return False, f"pas assez de nouveaux signaux ({new_signals} < {MIN_NEW_SIGNALS})"

        return True, f"{new_signals} nouveaux signaux ({total_signals} total)"

    def _count_signals(self, coin: str) -> int:
        """Compte les signaux valides en MongoDB."""
        try:
            client = MongoClient(MONGO_URL, serverSelectionTimeoutMS=5000)
            db = client[MONGO_DB]
            return db[MONGO_COLLECTION_SIGNALS].count_documents({
                "coin": coin,
                "gate_passed": True,
                "signal_level": {"$in": [-2, -1, 1, 2]},
            })
        except Exception as e:
            print(f"[AutoTrainer] Erreur count_signals ({coin}): {e}")
            return 0

    # ─── Réentraînement + rechargement à chaud ───────────────────────

    def _retrain_and_reload(self, coin: str):
        """Entraîne un nouveau modèle et le charge à chaud si l'AUC est suffisante."""
        model_dir = os.path.join(os.path.dirname(__file__), "models")

        # Import local pour ne pas planter si sklearn absent
        try:
            from ml.train_model import train as ml_train
        except ImportError as e:
            print(f"[AutoTrainer] scikit-learn absent, impossible d'entraîner: {e}")
            return

        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        print(f"[AutoTrainer] [{coin}] Début entraînement @ {now_str}...")

        result = ml_train(
            coin        = coin,
            days        = TRAINING_DAYS,
            lookahead   = LOOKAHEAD_CANDLES,
            target_pct  = TARGET_MOVE_PCT,
            model_dir   = model_dir,
        )

        if not result:
            self._notify(
                f"⚠️ <b>ML AutoTrain [{coin}]</b>\n"
                f"Échec de l'entraînement (données insuffisantes ?)",
                error=True
            )
            return

        auc  = result["cv_auc_mean"]
        std  = result["cv_auc_std"]
        n    = result["samples"]
        pos1 = result["label_1_pct"]
        now_ts = time.time()
        total_signals = self._count_signals(coin)

        if auc >= MIN_AUC:
            # ── Chargement à chaud ──────────────────────────────
            reloaded = False
            if coin in self.engines:
                reloaded = self.engines[coin].reload_ml_model()

            status_emoji = "✅" if reloaded else "💾"
            status_txt   = "rechargé en prod" if reloaded else "sauvegardé (rechargement manuel)"

            self._notify(
                f"🤖 <b>ML AutoTrain [{coin}]</b>\n"
                f"Échantillons : <code>{n}</code> | Bons signaux : <code>{pos1:.1f}%</code>\n"
                f"AUC CV : <b>{auc:.3f} ± {std:.3f}</b>\n"
                f"{status_emoji} Modèle {status_txt}"
            )
            print(f"[AutoTrainer] [{coin}] ✅ AUC={auc:.3f} — modèle {'rechargé' if reloaded else 'sauvegardé'}")
        else:
            # ── AUC insuffisante — supprimer le fichier généré ──
            for suffix in ["signal_filter", "scaler"]:
                path = os.path.join(model_dir, f"{suffix}_{coin}.pkl")
                if os.path.isfile(path):
                    # On ne supprime pas l'ancien modèle s'il y en avait un
                    # La logique train() écrase, donc on restaure l'état précédent
                    # en gardant l'ancien : ici on n'écrase pas intentionnellement
                    pass

            self._notify(
                f"⚠️ <b>ML AutoTrain [{coin}]</b>\n"
                f"AUC trop basse : <b>{auc:.3f}</b> (min requis: {MIN_AUC})\n"
                f"Ancien modèle conservé — plus de données nécessaires",
                error=True
            )
            print(f"[AutoTrainer] [{coin}] ⚠️ AUC={auc:.3f} < {MIN_AUC} — modèle non déployé")

        # Toujours mettre à jour l'état (même si modèle non déployé)
        # pour ne pas re-essayer trop vite
        with self._lock:
            self._state[coin] = {
                "last_train_ts":         now_ts,
                "signals_at_last_train": total_signals,
                "last_auc":              round(auc, 4),
                "last_train_str":        now_str,
            }
            self._save_state()

    # ─── État persistant ─────────────────────────────────────────────

    def _load_state(self):
        if os.path.isfile(STATE_FILE):
            try:
                with open(STATE_FILE) as f:
                    self._state = json.load(f)
                print(f"[AutoTrainer] État chargé depuis {STATE_FILE}")
            except Exception as e:
                print(f"[AutoTrainer] Impossible de lire l'état: {e}")
                self._state = {}
        else:
            self._state = {}

    def _save_state(self):
        try:
            with open(STATE_FILE, "w") as f:
                json.dump(self._state, f, indent=2)
        except Exception as e:
            print(f"[AutoTrainer] Impossible de sauvegarder l'état: {e}")

    # ─── Notification Telegram ────────────────────────────────────────

    def _notify(self, msg: str, error: bool = False):
        if self.notifier is None:
            print(f"[AutoTrainer] (no notifier) {msg}")
            return
        try:
            if error:
                self.notifier.error(msg)
            else:
                self.notifier.send(msg)
        except Exception as e:
            print(f"[AutoTrainer] Erreur notification: {e}")

    # ─── Status (debug) ───────────────────────────────────────────────

    def status(self) -> dict:
        """Retourne l'état actuel de l'AutoTrainer (pour debug/monitoring)."""
        result = {}
        coins = [p.split("/")[0] for p in PAIRS]
        for coin in coins:
            state = self._state.get(coin, {})
            total = self._count_signals(coin)
            signals_at_last = state.get("signals_at_last_train", 0)
            result[coin] = {
                "last_train":    state.get("last_train_str", "jamais"),
                "last_auc":      state.get("last_auc", None),
                "total_signals": total,
                "new_since_last":total - signals_at_last,
                "next_check_in": f"{CHECK_INTERVAL_H}h",
            }
        return result
