"""Tests du circuit breaker marché (utils.market_guard)."""
from utils.market_guard import market_circuit_breaker

THR = {"max_atr_pct": 0.03, "max_abs_funding": 0.001,
       "max_candle_range_pct": 0.04, "max_spread_pct": 0.002}


def normal():
    return {"atr_pct": 0.006, "funding_rate": 0.0001,
            "candle_range_pct": 0.01, "spread_pct": 0.0005}


def test_normal_market_not_tripped():
    tripped, reasons = market_circuit_breaker(normal(), THR)
    assert tripped is False
    assert reasons == []


def test_abnormal_volatility_trips():
    m = normal(); m["atr_pct"] = 0.05
    tripped, reasons = market_circuit_breaker(m, THR)
    assert tripped is True
    assert any("volatilité" in r for r in reasons)


def test_extreme_funding_trips_both_signs():
    for f in (0.002, -0.002):
        m = normal(); m["funding_rate"] = f
        tripped, reasons = market_circuit_breaker(m, THR)
        assert tripped is True
        assert any("funding" in r for r in reasons)


def test_huge_candle_trips():
    m = normal(); m["candle_range_pct"] = 0.06
    tripped, reasons = market_circuit_breaker(m, THR)
    assert tripped is True
    assert any("bougie" in r for r in reasons)


def test_wide_spread_trips():
    m = normal(); m["spread_pct"] = 0.005
    tripped, reasons = market_circuit_breaker(m, THR)
    assert tripped is True
    assert any("spread" in r for r in reasons)


def test_none_metrics_ignored():
    m = {"atr_pct": None, "funding_rate": None,
         "candle_range_pct": None, "spread_pct": None}
    tripped, reasons = market_circuit_breaker(m, THR)
    assert tripped is False


def test_multiple_reasons():
    m = normal(); m["atr_pct"] = 0.05; m["funding_rate"] = -0.003
    tripped, reasons = market_circuit_breaker(m, THR)
    assert tripped is True
    assert len(reasons) >= 2
