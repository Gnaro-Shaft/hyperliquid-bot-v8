"""Tests de la fenêtre du rapport journalier (utils.reporting).

Régression du bug : la requête comparait un objet datetime à des timestamps
stockés en Int64 → 0 trade retourné en permanence, et la fenêtre visait le jour
courant au lieu de la veille.
"""
from datetime import datetime, timezone
from utils.reporting import daily_report_window


def test_window_is_previous_full_day():
    now = datetime(2026, 6, 20, 3, 0, tzinfo=timezone.utc)
    day_start_ms, today_start_ms, label = daily_report_window(now)
    assert today_start_ms - day_start_ms == 86_400_000   # exactement 24h
    assert label == "2026-06-19"


def test_window_returns_int_ms_not_datetime():
    # Le cœur du bug : il FAUT des int-ms, pas un datetime
    now = datetime(2026, 6, 20, 12, 0, tzinfo=timezone.utc)
    day_start_ms, today_start_ms, _ = daily_report_window(now)
    assert isinstance(day_start_ms, int)
    assert isinstance(today_start_ms, int)


def test_yesterday_trade_in_window_today_excluded():
    now = datetime(2026, 6, 20, 8, 0, tzinfo=timezone.utc)
    day_start_ms, today_start_ms, _ = daily_report_window(now)

    yesterday_noon = int(datetime(2026, 6, 19, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)
    today_one_am   = int(datetime(2026, 6, 20, 1, 0, tzinfo=timezone.utc).timestamp() * 1000)

    assert day_start_ms <= yesterday_noon < today_start_ms       # hier → inclus
    assert not (day_start_ms <= today_one_am < today_start_ms)    # aujourd'hui → exclu
