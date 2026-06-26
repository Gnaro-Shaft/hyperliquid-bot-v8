"""Tests de l'adaptation par régime (utils.regime)."""
from utils.regime import detect_regime, regime_preset


def test_squeeze_has_priority():
    assert detect_regime(40, 0.002, 0.005) == "SQUEEZE"   # bb_width < seuil


def test_range_low_adx():
    assert detect_regime(20, 0.01, 0.005) == "RANGE"


def test_high_vol():
    assert detect_regime(35, 0.01, 0.02, high_vol_atr=0.015) == "HIGH_VOL"


def test_strong_trend():
    assert detect_regime(32, 0.01, 0.005, high_vol_atr=0.015) == "STRONG"


def test_weak_trend():
    assert detect_regime(27, 0.01, 0.005, high_vol_atr=0.015) == "WEAK"


def test_atr_none_not_high_vol():
    assert detect_regime(32, 0.01, None) == "STRONG"


def test_preset_strong_fields():
    p = regime_preset(32, 0.01, 0.005, high_vol_atr=0.015)
    assert p["regime"] == "STRONG"
    assert p["blocked"] is False
    assert p["tp_mult"] == 1.25
    assert p["size_mult"] == 1.0


def test_blocked_regimes():
    assert regime_preset(20, 0.01, 0.005)["blocked"] is True    # RANGE
    assert regime_preset(40, 0.002, 0.005)["blocked"] is True   # SQUEEZE


def test_high_vol_reduces_size():
    p = regime_preset(35, 0.01, 0.02, high_vol_atr=0.015)
    assert p["regime"] == "HIGH_VOL"
    assert p["size_mult"] < 1.0
    assert p["sl_mult"] > 1.0      # SL plus large en forte volatilité
