"""Tests du RiskManager (sans MongoDB — persistance désactivée)."""
import pytest
import risk.risk_manager as rm_mod


@pytest.fixture
def rm(monkeypatch):
    # Désactive Mongo : __init__ saute la connexion si MONGO_URL est vide
    monkeypatch.setattr(rm_mod, "MONGO_URL", "")
    r = rm_mod.RiskManager()
    r.reset_daily(1000.0)
    return r


def test_pause_after_consecutive_losses(rm):
    for _ in range(rm_mod.MAX_CONSECUTIVE_LOSSES):
        rm.register_trade_result(-10)
    ok, reason = rm.can_trade(current_balance=1000)
    assert ok is False
    assert "Pause" in reason


def test_win_resets_loss_streak(rm):
    rm.register_trade_result(-10)
    rm.register_trade_result(-10)
    assert rm.consecutive_losses == 2
    rm.register_trade_result(+5)
    assert rm.consecutive_losses == 0


def test_daily_drawdown_blocks_trading(rm):
    # -6% > seuil 5% → bloqué
    ok, reason = rm.can_trade(current_balance=940)
    assert ok is False
    assert "Drawdown" in reason


def test_within_drawdown_allows_trading(rm):
    # -3% < seuil 5% → autorisé
    ok, reason = rm.can_trade(current_balance=970)
    assert ok is True


def test_kill_switch_blocks(monkeypatch, tmp_path, rm):
    kill = tmp_path / "KILL"
    monkeypatch.setattr(rm_mod, "KILL_SWITCH_FILE", str(kill))
    ok, _ = rm.can_trade(current_balance=1000)
    assert ok is True
    kill.write_text("")                       # active le kill switch
    ok, reason = rm.can_trade(current_balance=1000)
    assert ok is False
    assert "KILL" in reason


def test_pnl_accumulates(rm):
    rm.register_trade_result(+10)
    rm.register_trade_result(-4)
    assert rm.total_pnl_today == pytest.approx(6.0)
