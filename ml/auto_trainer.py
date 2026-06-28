"""
AutoTrainer — Réentraînement ML automatique avec garde-fous
============================================================

Tourne en daemon thread dans main.py.
Vérifie toutes les CHECK_INTERVAL_H heures si un réentraînement est nécessaire.

Conditions pour réentraîner un coin :
  1. Intervalle minimum de RETRAIN_INTERVAL_H heures depuis le dernier train
  2. Au moins MIN_NEW_SIGNALS nouveaux signaux depuis le dernier train
  3. Total de MIN_TOTAL_SIGNALS signaux disponibles dans MongoDB

GARDE-FOUS DE PROMOTION (champion / challenger) :
  Le nouveau modèle ("challenger") est entraîné dans un dossier STAGING,
  jamais par-dessus le modèle actif. Il n'est promu en prod que s'il passe
  TOUS les contrôles :
    - AUC CV ≥ MIN_AUC (plancher absolu)
    - AUC holdout temporel ≥ MIN_HOLDOUT_AUC (généralisation au régime récent)
    - distribution des labels saine (classe positive dans [MIN/MAX]_POS_RATIO)
    - PAS de régression : AUC ≥ AUC du modèle déployé − AUC_REGRESSION_TOL
  Sinon → challenger jeté, modèle actif conservé intact, alerte Telegram.

CIRCUIT-BREAKER LIVE (P2) :
  À chaque cycle, on évalue le PnL réel des trades passés DEPUIS l'activation
  du filtre. Si la performance se dégrade clairement (win rate trop bas ET PnL
  net négatif sur une fenêtre récente), le filtre ML est DÉSACTIVÉ à chaud
  (retour au comportement sans filtre, sans risque ajouté) + alerte.
  Il est réactivé automatiquement à la prochaine promotion d'un bon modèle.

L'état (AUC déployée, dates, filtre actif/inactif) est persisté dans MongoDB
pour survivre aux redémarrages du conteneur (filesystem éphémère sur Fly).

Utilisation dans main.py :
    trainer = AutoTrainer(engines=self.engines, notifier=self.notifier)
    threading.Thread(target=trainer.run_loop, daemon=True).start()
"""

import os
import json
import time
import shutil
import threading
import traceback
from datetime import datetime, timezone

from pymongo import MongoClient

from config import (
    MONGO_URL, MONGO_DB, MONGO_COLLECTION_SIGNALS, MONGO_COLLECTION_TRADES, PAIRS,
)

# ─── Paramètres AutoTrainer ──────────────────────────────────────────
CHECK_INTERVAL_H    = 6      # Vérification toutes les 6 heures
RETRAIN_INTERVAL_H  = 24     # Minimum 24h entre deux retrains
MIN_NEW_SIGNALS     = 200    # Nouveaux signaux depuis le dernier train
MIN_TOTAL_SIGNALS   = 300    # Total minimum pour entraîner
TRAINING_DAYS       = 60     # Historique utilisé pour l'entraînement
LOOKAHEAD_CANDLES   = 4      # Fenêtre de validation (×15m)
TARGET_MOVE_PCT     = 0.004  # Mouvement cible pour le labelling

# ─── Garde-fous de promotion (P0 + P1) ───────────────────────────────
MIN_AUC             = 0.60   # Plancher AUC CV (0.53 → 0.58 → 0.60, Axe B). Modèles ~0.73-0.75.
MIN_HOLDOUT_AUC     = 0.54   # Plancher AUC holdout temporel (un peu plus bas, plus bruité)
HOLDOUT_DAYS        = 10     # Fenêtre de validation temporelle (jours récents)
AUC_REGRESSION_TOL  = 0.01   # Tolérance anti-régression vs modèle déployé
MIN_POS_RATIO       = 0.15   # Classe positive minimale (sinon dataset dégénéré)
MAX_POS_RATIO       = 0.85   # Classe positive maximale

# ─── Circuit-breaker live (P2) ────────────────────────────────────────
LIVE_WINDOW_DAYS    = 14     # Fenêtre d'évaluation de la perf live
LIVE_MIN_TRADES     = 25     # Nb min de trades clôturés pour juger
LIVE_MIN_WINRATE    = 0.38   # En dessous (+ PnL net < 0) → désactivation filtre

STATE_FILE = os.path.join(os.path.dirname(__file__), "models", "trainer_state.json")
STAGING_DIR = os.path.join(os.path.dirname(__file__), "models", "staging")
MODEL_DIR   = os.path.join(os.path.dirname(__file__), "models")
MONGO_COLLECTION_TRAINER_STATE = "ml_trainer_state"


class AutoTrainer:
    """
    Réentraîneur ML automatique avec garde-fous (daemon thread).

    Args:
        engines  : dict {coin: StrategyEngine} — moteurs de stratégie actifs
        notifier : Notifier — pour les alertes Telegram (peut être None)
    """

    def __init__(self, engines: dict, notifier=None):
        self.engines  = engines
        self.notifier = notifier
        self._state   = {}           # {coin: {...}} — miroir mémoire de l'état Mongo
        self._lock    = threading.Lock()
        self._client  = None

        os.makedirs(MODEL_DIR, exist_ok=True)
        os.makedirs(STAGING_DIR, exist_ok=True)
        self._load_state()

    # ─── Connexion Mongo réutilisable ─────────────────────────────────

    def _db(self):
        if self._client is None:
            self._client = MongoClient(MONGO_URL, serverSelectionTimeoutMS=5000)
        return self._client[MONGO_DB]

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
        """Un cycle : circuit-breaker live + réentraînement éventuel."""
        coins = [p.split("/")[0] for p in PAIRS]
        for coin in coins:
            # 1. Circuit-breaker live (toujours vérifié, indépendant du retrain)
            try:
                self._check_live_performance(coin)
            except Exception as e:
                print(f"[AutoTrainer] [{coin}] Erreur circuit-breaker: {e}")

            # 2. Réentraînement si nécessaire
            try:
                should, reason = self._should_retrain(coin)
                if should:
                    print(f"[AutoTrainer] [{coin}] Réentraînement déclenché : {reason}")
                    self._train_challenger_and_maybe_promote(coin)
                else:
                    print(f"[AutoTrainer] [{coin}] Pas de réentraînement ({reason})")
            except Exception as e:
                print(f"[AutoTrainer] [{coin}] Erreur retrain: {e}")
                print(traceback.format_exc())

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
            return self._db()[MONGO_COLLECTION_SIGNALS].count_documents({
                "coin": coin,
                "gate_passed": True,
                "signal_level": {"$in": [-2, -1, 1, 2]},
            })
        except Exception as e:
            print(f"[AutoTrainer] Erreur count_signals ({coin}): {e}")
            return 0

    # ─── Entraînement challenger + promotion sous garde-fous ──────────

    def _train_challenger_and_maybe_promote(self, coin: str):
        """Entraîne un challenger en staging et ne le promeut que s'il est
        meilleur que le modèle déployé et passe tous les contrôles."""
        try:
            from ml.train_model import train as ml_train, target_for_coin
        except ImportError as e:
            print(f"[AutoTrainer] scikit-learn absent, impossible d'entraîner: {e}")
            return

        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        now_ts  = time.time()
        print(f"[AutoTrainer] [{coin}] Entraînement challenger @ {now_str} (staging)...")

        # Nettoyer le staging avant
        self._clear_staging(coin)

        result = ml_train(
            coin         = coin,
            days         = TRAINING_DAYS,
            lookahead    = LOOKAHEAD_CANDLES,
            target_pct   = target_for_coin(coin),
            model_dir    = STAGING_DIR,
            holdout_days = HOLDOUT_DAYS,
        )

        if not result:
            self._clear_staging(coin)
            self._notify(
                f"⚠️ <b>ML AutoTrain [{coin}]</b>\n"
                f"Échec de l'entraînement (données insuffisantes ?)",
                error=True,
            )
            self._record_train_attempt(coin, now_ts, auc=None, reason="échec entraînement")
            return

        auc         = result["cv_auc_mean"]
        std         = result["cv_auc_std"]
        holdout_auc = result.get("holdout_auc")
        n           = result["samples"]
        pos1        = result["label_1_pct"]
        pos_ratio   = pos1 / 100.0

        state        = self._state.get(coin, {})
        deployed_auc = state.get("deployed_auc")

        # ── Contrôles de promotion (garde-fous) ──────────────────────
        ok, reason = self._evaluate_challenger(auc, holdout_auc, pos_ratio, deployed_auc)

        hold_str = f"{holdout_auc:.3f}" if holdout_auc is not None else "N/A"
        if ok:
            promoted = self._promote(coin)
            reloaded = False
            if promoted and coin in self.engines:
                # reload_ml_model() recharge le nouveau modèle ET réactive le
                # filtre s'il avait été coupé par le circuit-breaker.
                reloaded = self.engines[coin].reload_ml_model()

            status_emoji = "✅" if reloaded else "💾"
            status_txt   = "promu + rechargé en prod" if reloaded else "promu (rechargement à faire)"
            self._notify(
                f"🤖 <b>ML AutoTrain [{coin}]</b> — challenger PROMU\n"
                f"Échantillons : <code>{n}</code> | Bons signaux : <code>{pos1:.1f}%</code>\n"
                f"AUC CV : <b>{auc:.3f} ± {std:.3f}</b> | Holdout : <b>{hold_str}</b>\n"
                f"{('(précédent: ' + format(deployed_auc, '.3f') + ')') if deployed_auc else '(premier modèle)'}\n"
                f"{status_emoji} Modèle {status_txt}"
            )
            print(f"[AutoTrainer] [{coin}] ✅ PROMU AUC={auc:.3f} holdout={hold_str}")
            self._record_promotion(coin, now_ts, now_str, auc, holdout_auc)
        else:
            # Challenger rejeté → on jette le staging, le modèle actif reste intact
            self._clear_staging(coin)
            self._notify(
                f"🛡️ <b>ML AutoTrain [{coin}]</b> — challenger REJETÉ\n"
                f"AUC CV : {auc:.3f} | Holdout : {hold_str} | Bons : {pos1:.1f}%\n"
                f"Raison : {reason}\n"
                f"Modèle actuel conservé"
                + (f" (AUC déployée: {deployed_auc:.3f})" if deployed_auc else ""),
                error=True,
            )
            print(f"[AutoTrainer] [{coin}] 🛡️ REJETÉ ({reason}) — modèle actif conservé")
            self._record_train_attempt(coin, now_ts, auc=auc, reason=reason)

    def _evaluate_challenger(self, auc, holdout_auc, pos_ratio, deployed_auc) -> tuple:
        """Retourne (ok: bool, raison: str). Tous les contrôles doivent passer."""
        if auc < MIN_AUC:
            return False, f"AUC CV {auc:.3f} < plancher {MIN_AUC}"
        if not (MIN_POS_RATIO <= pos_ratio <= MAX_POS_RATIO):
            return False, f"distribution labels dégénérée ({pos_ratio*100:.0f}% positifs)"
        if holdout_auc is not None and holdout_auc < MIN_HOLDOUT_AUC:
            return False, f"holdout AUC {holdout_auc:.3f} < plancher {MIN_HOLDOUT_AUC} (surapprentissage probable)"
        if deployed_auc is not None and auc < deployed_auc - AUC_REGRESSION_TOL:
            return False, f"régression vs déployé (AUC {auc:.3f} < {deployed_auc:.3f} − {AUC_REGRESSION_TOL})"
        return True, "tous les contrôles OK"

    # ─── Promotion atomique + backup ──────────────────────────────────

    def _promote(self, coin: str) -> bool:
        """Déplace le modèle staging → actif, en sauvegardant l'ancien en .prev."""
        try:
            for kind in ["signal_filter", "scaler"]:
                staged = os.path.join(STAGING_DIR, f"{kind}_{coin}.pkl")
                active = os.path.join(MODEL_DIR, f"{kind}_{coin}.pkl")
                prev   = os.path.join(MODEL_DIR, f"{kind}_{coin}.prev.pkl")
                if not os.path.isfile(staged):
                    print(f"[AutoTrainer] [{coin}] Fichier staging manquant: {staged}")
                    return False
                # Backup de l'actif courant (pour rollback)
                if os.path.isfile(active):
                    shutil.copy2(active, prev)
                shutil.move(staged, active)
            return True
        except Exception as e:
            print(f"[AutoTrainer] [{coin}] Erreur promotion: {e}")
            return False

    def rollback(self, coin: str) -> bool:
        """Restaure le modèle précédent (.prev) et le recharge à chaud."""
        try:
            for kind in ["signal_filter", "scaler"]:
                prev   = os.path.join(MODEL_DIR, f"{kind}_{coin}.prev.pkl")
                active = os.path.join(MODEL_DIR, f"{kind}_{coin}.pkl")
                if not os.path.isfile(prev):
                    print(f"[AutoTrainer] [{coin}] Pas de backup à restaurer")
                    return False
                shutil.copy2(prev, active)
            if coin in self.engines:
                self.engines[coin].reload_ml_model()
            self._notify(f"↩️ <b>ML [{coin}]</b> — rollback vers le modèle précédent effectué")
            print(f"[AutoTrainer] [{coin}] ↩️ Rollback effectué")
            return True
        except Exception as e:
            print(f"[AutoTrainer] [{coin}] Erreur rollback: {e}")
            return False

    def _clear_staging(self, coin: str):
        for kind in ["signal_filter", "scaler"]:
            p = os.path.join(STAGING_DIR, f"{kind}_{coin}.pkl")
            try:
                if os.path.isfile(p):
                    os.remove(p)
            except Exception:
                pass

    # ─── Circuit-breaker live (P2) ────────────────────────────────────

    def _check_live_performance(self, coin: str):
        """Désactive le filtre ML si la perf live se dégrade clairement.

        N'évalue que les trades clôturés DEPUIS l'activation du filtre
        (deployed_at_ts) et dans la fenêtre LIVE_WINDOW_DAYS.
        Action de sécurité : on coupe le filtre (retour au comportement
        sans filtre), jamais d'action plus risquée.
        """
        # On surveille dès que le filtre est RÉELLEMENT actif dans le moteur
        # (modèle chargé au boot OU promu), indépendamment de l'état stocké.
        eng = self.engines.get(coin)
        if eng is None or getattr(eng, "ml_predictor", None) is None:
            return  # filtre inactif → rien à surveiller

        state       = self._state.get(coin, {})
        now_ms      = int(time.time() * 1000)
        window_ms   = now_ms - LIVE_WINDOW_DAYS * 86400 * 1000
        deployed_ms = state.get("deployed_at_ts", 0) or 0
        start_ms    = max(window_ms, deployed_ms)

        try:
            docs = list(self._db()[MONGO_COLLECTION_TRADES].find({
                "pair": {"$regex": f"^{coin}/"},
                "action": "close",
                "timestamp": {"$gte": start_ms},
            }, {"pnl": 1}))
        except Exception as e:
            print(f"[AutoTrainer] [{coin}] Erreur lecture trades: {e}")
            return

        pnls = [float(d.get("pnl", 0) or 0) for d in docs]
        n = len(pnls)
        if n < LIVE_MIN_TRADES:
            return  # pas assez de recul pour juger

        wins     = sum(1 for p in pnls if p > 0)
        win_rate = wins / n
        net_pnl  = sum(pnls)

        if win_rate < LIVE_MIN_WINRATE and net_pnl < 0:
            # ── Coupe-circuit : désactiver le filtre ML ──
            if coin in self.engines:
                self.engines[coin].ml_predictor = None
            with self._lock:
                self._state.setdefault(coin, {})["filter_enabled"] = False
                self._save_state_coin(coin)
            self._notify(
                f"🚨 <b>CIRCUIT-BREAKER ML [{coin}]</b>\n"
                f"Perf live dégradée : win rate <b>{win_rate*100:.0f}%</b> "
                f"| PnL net <b>{net_pnl:+.2f}</b> sur {n} trades ({LIVE_WINDOW_DAYS}j)\n"
                f"➡️ Filtre ML <b>DÉSACTIVÉ</b> (retour au comportement sans filtre).\n"
                f"Réactivation auto à la prochaine promotion d'un bon modèle.",
                error=True,
            )
            print(f"[AutoTrainer] [{coin}] 🚨 Circuit-breaker: filtre désactivé "
                  f"(WR={win_rate*100:.0f}%, PnL={net_pnl:+.2f}, n={n})")

    # ─── État persistant (MongoDB + miroir JSON) ──────────────────────

    def _load_state(self):
        """Charge l'état depuis MongoDB (source de vérité), fallback JSON puis vide."""
        loaded = {}
        try:
            for doc in self._db()[MONGO_COLLECTION_TRAINER_STATE].find({}):
                coin = doc.get("_id")
                if coin:
                    doc.pop("_id", None)
                    loaded[coin] = doc
            if loaded:
                self._state = loaded
                print(f"[AutoTrainer] État chargé depuis MongoDB ({len(loaded)} coins)")
                return
        except Exception as e:
            print(f"[AutoTrainer] Lecture état Mongo impossible ({e}) — fallback JSON")

        if os.path.isfile(STATE_FILE):
            try:
                with open(STATE_FILE) as f:
                    self._state = json.load(f)
                print(f"[AutoTrainer] État chargé depuis {STATE_FILE}")
                return
            except Exception as e:
                print(f"[AutoTrainer] Lecture JSON impossible: {e}")
        self._state = {}

    def _save_state_coin(self, coin: str):
        """Persiste l'état d'un coin dans Mongo + miroir JSON."""
        data = self._state.get(coin, {})
        try:
            self._db()[MONGO_COLLECTION_TRAINER_STATE].update_one(
                {"_id": coin}, {"$set": data}, upsert=True
            )
        except Exception as e:
            print(f"[AutoTrainer] Sauvegarde état Mongo ({coin}) impossible: {e}")
        # Miroir JSON local (best effort)
        try:
            with open(STATE_FILE, "w") as f:
                json.dump(self._state, f, indent=2)
        except Exception:
            pass

    def _record_promotion(self, coin, now_ts, now_str, auc, holdout_auc):
        """Met à jour l'état après une promotion réussie."""
        with self._lock:
            st = self._state.setdefault(coin, {})
            st.update({
                "last_train_ts":         now_ts,
                "last_train_str":        now_str,
                "signals_at_last_train": self._count_signals(coin),
                "deployed_auc":          round(auc, 4),
                "deployed_holdout_auc":  round(holdout_auc, 4) if holdout_auc is not None else None,
                "deployed_at":           now_str,
                "deployed_at_ts":        int(now_ts * 1000),
                "filter_enabled":        True,
                "last_reject_reason":    None,
            })
            self._save_state_coin(coin)

    def _record_train_attempt(self, coin, now_ts, auc, reason):
        """Met à jour l'état après un train sans promotion (rejet/échec).

        On ne touche PAS à deployed_auc (le modèle actif reste la référence)."""
        with self._lock:
            st = self._state.setdefault(coin, {})
            st.update({
                "last_train_ts":         now_ts,
                "signals_at_last_train": self._count_signals(coin),
                "last_challenger_auc":   round(auc, 4) if auc is not None else None,
                "last_reject_reason":    reason,
            })
            self._save_state_coin(coin)

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
                "last_train":       state.get("last_train_str", "jamais"),
                "deployed_auc":     state.get("deployed_auc"),
                "deployed_holdout": state.get("deployed_holdout_auc"),
                "filter_enabled":   state.get("filter_enabled", False),
                "last_reject":      state.get("last_reject_reason"),
                "total_signals":    total,
                "new_since_last":   total - signals_at_last,
                "next_check_in":    f"{CHECK_INTERVAL_H}h",
            }
        return result
