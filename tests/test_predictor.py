"""Test du fallback gracieux du filtre ML (ml.predictor)."""
from ml.predictor import MLPredictor


def test_predictor_unavailable_returns_neutral():
    # Aucun modèle sur le disque → le filtre doit être neutre (0.5), pas planter
    p = MLPredictor(coin="ZZZ", model_dir="/tmp/nonexistent_model_dir_xyz_v8")
    assert p.is_available() is False
    assert p.predict({"rsi_14": 50, "signal_level": 2}) == 0.5
