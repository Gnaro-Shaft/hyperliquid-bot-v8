"""Tests du modèle d'exécution réaliste (utils.execution)."""
from utils.execution import execution_price

SLIP, SPREAD = 0.0003, 0.0002      # adverse = 0.0003 + 0.0001 = 0.0004


def test_long_entry_pays_more():
    assert round(execution_price(100, "buy", True, SLIP, SPREAD), 4) == 100.04


def test_long_exit_gets_less():
    assert round(execution_price(100, "buy", False, SLIP, SPREAD), 4) == 99.96


def test_short_entry_gets_less():
    assert round(execution_price(100, "sell", True, SLIP, SPREAD), 4) == 99.96


def test_short_exit_pays_more():
    assert round(execution_price(100, "sell", False, SLIP, SPREAD), 4) == 100.04


def test_always_adverse():
    # entrée long > mid, sortie long < mid ; entrée short < mid, sortie short > mid
    assert execution_price(100, "buy", True, SLIP, SPREAD) > 100
    assert execution_price(100, "buy", False, SLIP, SPREAD) < 100
    assert execution_price(100, "sell", True, SLIP, SPREAD) < 100
    assert execution_price(100, "sell", False, SLIP, SPREAD) > 100


def test_zero_cost_is_identity():
    assert execution_price(100, "buy", True, 0, 0) == 100
