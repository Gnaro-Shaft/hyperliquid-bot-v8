"""
MLPredictor — Filtre de qualité des signaux (inférence temps-réel).

Charge le modèle entraîné par train_model.py et retourne une probabilité
que le signal soit de bonne qualité (P(trade gagnant)).

Utilisation dans strategy_engine.py :
    self.ml_predictor = MLPredictor(coin=coin)
    confidence = self.ml_predictor.predict(features_dict)
"""

import os
import numpy as np

# joblib est inclus dans scikit-learn
try:
    import joblib
    _JOBLIB_OK = True
except ImportError:
    _JOBLIB_OK = False


FEATURE_NAMES = [
    "rsi_14",
    "adx_14",
    "bb_width",
    "raw_score",
    "signal_level",
    "atr_pct",
    "funding_rate",
    "ob_imbalance",
    "oi_change_pct",
    "bb_pctB",
]


class MLPredictor:
    """
    Charge et exécute le modèle de classification de signaux.

    Args:
        coin       : Coin (BTC, SOL…) — un modèle par coin
        model_dir  : Répertoire contenant les fichiers .pkl
    """

    def __init__(self, coin: str = "BTC", model_dir: str = None):
        if model_dir is None:
            model_dir = os.path.join(os.path.dirname(__file__), "models")

        self.coin = coin.upper()
        self.model_dir = model_dir
        self._model = None
        self._scaler = None
        self._available = False

        if not _JOBLIB_OK:
            print("[ML] joblib non disponible — modèle ML désactivé")
            return

        model_path  = os.path.join(model_dir, f"signal_filter_{self.coin}.pkl")
        scaler_path = os.path.join(model_dir, f"scaler_{self.coin}.pkl")

        if os.path.isfile(model_path) and os.path.isfile(scaler_path):
            try:
                self._model  = joblib.load(model_path)
                self._scaler = joblib.load(scaler_path)
                self._available = True
                print(f"[ML] Modèle chargé : {model_path}")
            except Exception as e:
                print(f"[ML] Erreur chargement modèle {coin}: {e}")
        else:
            # Silencieux si le modèle n'a pas encore été entraîné
            pass

    def is_available(self) -> bool:
        return self._available

    def predict(self, features: dict) -> float:
        """
        Retourne P(signal de bonne qualité) ∈ [0, 1].

        features : dict avec les clés de FEATURE_NAMES (NaN accepté → 0)

        Retourne 0.5 si modèle non dispo (neutre = pas de filtre).
        """
        if not self._available:
            return 0.5

        try:
            vec = np.array([
                _safe_float(features.get(f, 0))
                for f in FEATURE_NAMES
            ], dtype=float).reshape(1, -1)

            # Remplacement NaN par 0 (médiane approx pour features normalisées)
            vec = np.nan_to_num(vec, nan=0.0)

            vec_scaled = self._scaler.transform(vec)
            proba = self._model.predict_proba(vec_scaled)[0]
            # proba[1] = P(label=1) = P(signal gagnant)
            return float(proba[1])

        except Exception as e:
            print(f"[ML] Erreur prédiction: {e}")
            return 0.5


def _safe_float(val, default=0.0) -> float:
    """Convertit une valeur en float, retourne default si impossible."""
    if val is None:
        return default
    try:
        f = float(val)
        return f if np.isfinite(f) else default
    except (TypeError, ValueError):
        return default
