"""Tests de la logique d'évaluation du healthcheck (monitor.health.evaluate_health)."""
from monitor.health import evaluate_health

THR = {"max_1m_age_s": 300, "max_15m_age_s": 2400, "max_consec_errors": 5}


def healthy():
    return {"ws_alive": True, "mongo_ok": True, "last_1m_age_s": 30,
            "last_15m_age_s": 100, "balance": 1000.0, "consec_errors": 0}


def test_all_healthy_no_problems():
    assert evaluate_health(healthy(), THR) == []


def test_ws_down():
    m = healthy(); m["ws_alive"] = False
    assert any("WebSocket" in p for p in evaluate_health(m, THR))


def test_mongo_down():
    m = healthy(); m["mongo_ok"] = False
    assert any("MongoDB" in p for p in evaluate_health(m, THR))


def test_stale_1m_candle():
    m = healthy(); m["last_1m_age_s"] = 400
    assert any("1m" in p for p in evaluate_health(m, THR))


def test_stale_15m_candle():
    m = healthy(); m["last_15m_age_s"] = 3000
    assert any("15m" in p for p in evaluate_health(m, THR))


def test_balance_inaccessible():
    m = healthy(); m["balance"] = None
    assert any("Solde" in p for p in evaluate_health(m, THR))


def test_too_many_consecutive_errors():
    m = healthy(); m["consec_errors"] = 6
    assert any("erreurs" in p for p in evaluate_health(m, THR))


def test_multiple_problems_reported():
    m = healthy(); m["ws_alive"] = False; m["balance"] = None
    assert len(evaluate_health(m, THR)) >= 2


def test_none_ages_not_a_problem():
    # âges None (pas encore de données) → pas un problème en soi
    m = healthy(); m["last_1m_age_s"] = None; m["last_15m_age_s"] = None
    assert evaluate_health(m, THR) == []
