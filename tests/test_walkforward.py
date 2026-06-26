"""Tests des helpers walk-forward (utils.walkforward)."""
from datetime import datetime, timezone
from utils.walkforward import walk_forward_windows, summarize_walkforward


def test_windows_contiguous_and_count():
    end = datetime(2026, 6, 26, tzinfo=timezone.utc)
    w = walk_forward_windows(end, 3, 10)
    assert len(w) == 3
    assert w[0][1] == w[1][0]          # contiguës
    assert w[1][1] == w[2][0]
    assert w[-1][1] == end             # se terminent à end_dt
    assert (w[-1][1] - w[0][0]).days == 30


def test_summary_empty():
    s = summarize_walkforward([])
    assert s["n_windows"] == 0
    assert s["consistency_pct"] == 0.0


def test_summary_mixed():
    pw = [{"total_pnl_pct": 2.0}, {"total_pnl_pct": -1.0},
          {"total_pnl_pct": 3.0}, None]   # None = fenêtre en erreur, ignorée
    s = summarize_walkforward(pw)
    assert s["n_windows"] == 3
    assert s["profitable"] == 2
    assert s["consistency_pct"] == round(2 / 3 * 100, 1)
    assert s["best_pnl_pct"] == 3.0
    assert s["worst_pnl_pct"] == -1.0
    assert s["mean_pnl_pct"] == round((2 - 1 + 3) / 3, 3)
