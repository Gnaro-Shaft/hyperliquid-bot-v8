"""Tests des aides au minimum d'ordre (utils.min_order)."""
from utils.min_order import min_target_size, meets_minimum


def test_target_size_sol():
    # 10 USDC × 1.20 marge / 150 = 0.08 SOL
    assert round(min_target_size(10, 150, 1.20), 6) == 0.08


def test_target_size_btc():
    assert round(min_target_size(10, 64000, 1.20), 8) == round(12 / 64000, 8)


def test_target_size_zero_price():
    assert min_target_size(10, 0) == 0.0


def test_meets_minimum_true():
    assert meets_minimum(0.08, 150, 10) is True       # 12 USDC


def test_meets_minimum_exact():
    assert meets_minimum(10 / 150, 150, 10) is True    # exactement 10


def test_meets_minimum_false():
    assert meets_minimum(0.05, 150, 10) is False       # 7.5 USDC < 10
