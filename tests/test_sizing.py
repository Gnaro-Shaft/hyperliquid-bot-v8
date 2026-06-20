"""Tests du dimensionnement de position et des TP/SL dynamiques (utils.sizing)."""
from utils.sizing import size_factor, dynamic_sl_tp


# ─── size_factor ──────────────────────────────────────────────────────

def test_size_factor_full_strength():
    f = size_factor(raw_score=15, dynamic_sl=0.01, sl_pct=0.01,
                    pnl_today=0, daily_start_balance=1000, max_daily_drawdown_pct=0.05)
    assert f == 1.0


def test_size_factor_high_volatility_halves():
    # SL 2× plus large (volatilité ↑) → vol_factor = 0.5 → risque dollar constant
    f = size_factor(raw_score=15, dynamic_sl=0.02, sl_pct=0.01,
                    pnl_today=0, daily_start_balance=1000, max_daily_drawdown_pct=0.05)
    assert f == 0.5


def test_size_factor_floor_at_0_3():
    # SL 5× → vol_factor planché à 0.3
    f = size_factor(raw_score=15, dynamic_sl=0.05, sl_pct=0.01,
                    pnl_today=0, daily_start_balance=1000, max_daily_drawdown_pct=0.05)
    assert f == 0.3


def test_size_factor_reduced_when_losing():
    # PnL = -25, limite quotidienne = 1000×0.05 = 50 → risk = 1 - (25/50)×0.6 = 0.7
    f = size_factor(raw_score=15, dynamic_sl=0.01, sl_pct=0.01,
                    pnl_today=-25, daily_start_balance=1000, max_daily_drawdown_pct=0.05)
    assert f == 0.7


def test_size_factor_always_within_bounds():
    f = size_factor(raw_score=0, dynamic_sl=0.10, sl_pct=0.01,
                    pnl_today=-9999, daily_start_balance=1000, max_daily_drawdown_pct=0.05)
    assert 0.3 <= f <= 1.0


# ─── dynamic_sl_tp ────────────────────────────────────────────────────

def test_dynamic_sl_tp_no_atr_fallback():
    sl, tp = dynamic_sl_tp(atr_val=None, close=100, sl_pct=0.01, tp_pct=0.008, min_tp_pct=0.008)
    assert sl == 0.01
    assert tp == 0.015            # max(tp_pct, sl_pct×1.5)


def test_dynamic_sl_tp_with_atr_capped():
    # atr_pct=0.02 → raw_sl=0.03, plafonné à sl×2=0.02 ; tp=sl×2=0.04
    sl, tp = dynamic_sl_tp(atr_val=2.0, close=100, sl_pct=0.01, tp_pct=0.008, min_tp_pct=0.008)
    assert sl == 0.02
    assert tp == 0.04


def test_dynamic_sl_tp_floored():
    # atr_pct=0.001 → raw_sl=0.0015, planché à sl/2=0.005 ; tp=max(0.01, min_tp)
    sl, tp = dynamic_sl_tp(atr_val=0.1, close=100, sl_pct=0.01, tp_pct=0.008, min_tp_pct=0.008)
    assert sl == 0.005
    assert round(tp, 6) == 0.01


def test_dynamic_sl_tp_respects_rr_2to1():
    sl, tp = dynamic_sl_tp(atr_val=1.0, close=100, sl_pct=0.01, tp_pct=0.008, min_tp_pct=0.008)
    assert tp >= 2 * sl - 1e-9
