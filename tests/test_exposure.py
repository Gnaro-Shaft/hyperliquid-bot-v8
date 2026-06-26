"""Tests du garde-fou d'exposition globale (utils.exposure)."""
from utils.exposure import exposure_check

LIMITS = dict(max_positions=2, max_per_direction=1, max_total_exposure_pct=0.6)


def test_allows_first_position():
    ok, _ = exposure_check([], "buy", 200, 1000, **LIMITS)
    assert ok is True


def test_blocks_when_max_positions_reached():
    openp = [{"side": "buy", "notional": 200}, {"side": "sell", "notional": 200}]
    ok, reason = exposure_check(openp, "buy", 100, 1000, **LIMITS)
    assert ok is False
    assert "positions simultanées" in reason


def test_blocks_second_same_direction():
    openp = [{"side": "buy", "notional": 200}]
    ok, reason = exposure_check(openp, "buy", 100, 1000, **LIMITS)
    assert ok is False
    assert "buy" in reason


def test_allows_opposite_direction():
    openp = [{"side": "buy", "notional": 200}]
    ok, _ = exposure_check(openp, "sell", 100, 1000, **LIMITS)
    assert ok is True


def test_blocks_when_total_exposure_exceeds():
    openp = [{"side": "buy", "notional": 400}]
    ok, reason = exposure_check(openp, "sell", 300, 1000, **LIMITS)   # total 700 > 600
    assert ok is False
    assert "exposition totale" in reason


def test_allows_within_total_exposure():
    openp = [{"side": "buy", "notional": 400}]
    ok, _ = exposure_check(openp, "sell", 150, 1000, **LIMITS)        # total 550 < 600
    assert ok is True


def test_zero_balance_skips_notional_check():
    # balance 0 → pas de blocage sur le notionnel (évite un blocage absurde)
    ok, _ = exposure_check([], "buy", 100, 0, **LIMITS)
    assert ok is True
