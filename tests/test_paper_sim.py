"""Tests de la simulation paper trading (utils.paper_sim)."""
from utils.paper_sim import compute_tp_sl, simulate_candle_fill, compute_pnl


def test_compute_tp_sl_long():
    tp, sl = compute_tp_sl("buy", 100, 0.02, 0.01)
    assert tp == 102.0
    assert sl == 99.0


def test_compute_tp_sl_short():
    tp, sl = compute_tp_sl("sell", 100, 0.02, 0.01)
    assert tp == 98.0
    assert sl == 101.0


def _long():
    return {"side": "buy", "tp_price": 102, "sl_price": 99}


def _short():
    return {"side": "sell", "tp_price": 98, "sl_price": 101}


def test_fill_long_tp_hit():
    assert simulate_candle_fill(_long(), {"high": 103, "low": 100}) == (True, 102, "tp")


def test_fill_long_sl_hit():
    assert simulate_candle_fill(_long(), {"high": 101, "low": 98}) == (True, 99, "sl")


def test_fill_long_neither():
    assert simulate_candle_fill(_long(), {"high": 101, "low": 100}) == (False, None, None)


def test_fill_long_both_sl_priority():
    # TP et SL touchés dans la même bougie → SL d'abord (pessimiste)
    assert simulate_candle_fill(_long(), {"high": 103, "low": 98}) == (True, 99, "sl")


def test_fill_short_tp_hit():
    assert simulate_candle_fill(_short(), {"high": 100, "low": 97}) == (True, 98, "tp")


def test_fill_short_sl_hit():
    assert simulate_candle_fill(_short(), {"high": 102, "low": 99}) == (True, 101, "sl")


def test_compute_pnl_gross():
    assert compute_pnl("buy", 100, 110, 1, fee_rate=0) == 10.0
    assert compute_pnl("sell", 100, 90, 1, fee_rate=0) == 10.0


def test_compute_pnl_with_fees():
    # 10 brut − (100+110)*0.0005 = 10 − 0.105
    assert round(compute_pnl("buy", 100, 110, 1, fee_rate=0.0005), 4) == 9.895
