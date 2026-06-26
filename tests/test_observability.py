"""Tests des builders et agrégateurs d'observabilité (utils.observability)."""
from utils.observability import (
    build_decision_doc, build_bot_status, aggregate_trades, aggregate_decisions,
)


def make_sig():
    return {
        "score": 2, "raw_score": 11,
        "dynamic_tp": 0.02, "dynamic_sl": 0.01,
        "debug": {"atr_pct": 0.006, "funding_rate": 0.0001,
                  "gate_ml": "OK — confidence=0.87"},
    }


def test_decision_doc_accepted():
    d = build_decision_doc("BTC", make_sig(), "buy", "accepted", "ok",
                           64000, 0.7, 1000, "2026-06-26 10:00:00")
    assert d["coin"] == "BTC"
    assert d["action"] == "accepted"
    assert d["reason"] == "ok"
    assert d["side"] == "buy"
    assert d["score"] == 2
    assert d["raw_score"] == 11
    assert d["price"] == 64000.0
    assert d["size_factor"] == 0.7
    assert d["tp_pct"] == 0.02
    assert d["sl_pct"] == 0.01
    assert d["atr_pct"] == 0.006
    assert "confidence=0.87" in d["ml_gate"]
    assert d["timestamp"] == 1000
    # Contrat dashboard
    assert d["status"] == "accepted"
    assert d["created_at"] == 1000
    assert d["motif"] is None          # "ok" → pas un motif de refus


def test_decision_doc_refused_without_size():
    d = build_decision_doc("SOL", make_sig(), "sell", "refused",
                           "exposure: max positions", 150, None, 2000, "x")
    assert d["action"] == "refused"
    assert d["reason"] == "exposure: max positions"
    assert d["size_factor"] is None
    # Contrat dashboard : status + motif (catégorie extraite avant le ':')
    assert d["status"] == "refused"
    assert d["motif"] == "exposure"


def test_decision_doc_motif_categories():
    for reason, expected in [
        ("circuit_breaker: funding", "circuit_breaker"),
        ("correlation", "correlation"),
        ("risk: drawdown", "risk"),
        ("ok", None),
        ("inconnu: x", None),
    ]:
        d = build_decision_doc("BTC", make_sig(), "buy", "refused", reason, 1, None, 1, "x")
        assert d["motif"] == expected


def test_decision_doc_handles_missing_debug():
    sig = {"score": -2, "raw_score": -10}   # pas de clé "debug"
    d = build_decision_doc("BTC", sig, "sell", "refused", "risk", 64000, None, 1, "x")
    assert d["atr_pct"] is None
    assert d["ml_gate"] is None


def test_bot_status_summarizes_open_positions():
    metrics = {"ws_alive": True, "mongo_ok": True, "last_1m_age_s": 30,
               "last_15m_age_s": 100, "balance": 82.5}
    risk = {"pnl_today": -0.06, "consecutive_losses": 0, "paused": False}
    positions = {
        "BTC": {"active": True, "side": "buy", "entry": 64000, "size": 0.001},
        "SOL": {"active": False, "side": None},
    }
    s = build_bot_status(metrics, risk, positions, False, 1234, "2026-06-26 10:00:00")
    assert s["_id"] == "current"
    assert s["running"] is True
    assert s["balance"] == 82.5
    assert s["pnl_today"] == -0.06
    assert s["kill_switch"] is False
    assert s["n_open_positions"] == 1
    assert s["open_positions"][0]["coin"] == "BTC"


def test_bot_status_no_positions():
    s = build_bot_status({}, {}, {}, True, 1, "x")
    assert s["n_open_positions"] == 0
    assert s["open_positions"] == []
    assert s["kill_switch"] is True


# ─── aggregate_trades / aggregate_decisions ───────────────────────────

def test_aggregate_trades_empty():
    out = aggregate_trades([])
    assert out["n"] == 0
    assert out["pnl"] == 0.0


def test_aggregate_trades_breakdown():
    trades = [
        {"pnl": 10.0, "side": "buy",  "reason": "tp",       "timestamp": 1_700_000_000_000},
        {"pnl": -5.0, "side": "buy",  "reason": "sl",       "timestamp": 1_700_003_600_000},
        {"pnl": 3.0,  "side": "sell", "reason": "tp",       "timestamp": 1_700_000_000_000},
        {"pnl": 2.0,  "side": "sell", "reason": "trailing", "timestamp": 1_700_000_000_000},
        {"action": "close", "timestamp": 1},   # pas de pnl → ignoré
    ]
    out = aggregate_trades(trades)
    assert out["n"] == 4
    assert out["wins"] == 3
    assert out["losses"] == 1
    assert out["pnl"] == 10.0
    assert out["win_rate"] == 75.0
    assert out["by_direction"]["buy"]["n"] == 2
    assert out["by_direction"]["sell"]["wins"] == 2
    assert out["by_reason"]["tp"]["n"] == 2
    assert len(out["by_hour"]) >= 1


def test_aggregate_decisions():
    decs = [
        {"action": "accepted", "reason": "ok"},
        {"action": "refused",  "reason": "exposure: max positions"},
        {"action": "refused",  "reason": "circuit_breaker: funding"},
        {"action": "refused",  "reason": "exposure: total"},
    ]
    out = aggregate_decisions(decs)
    assert out["n"] == 4
    assert out["accepted"] == 1
    assert out["refused"] == 3
    assert out["refused_by_reason"]["exposure"] == 2
    assert out["refused_by_reason"]["circuit_breaker"] == 1
