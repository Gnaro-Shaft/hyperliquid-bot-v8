"""Tests de sanité des indicateurs techniques (strategy.indicators)."""
import numpy as np
import pandas as pd

from strategy.indicators import ema, rsi, atr, bollinger_bands, bb_width


def test_ema_of_constant_is_constant():
    s = pd.Series([5.0] * 30)
    assert abs(ema(s, 9).iloc[-1] - 5.0) < 1e-9


def test_rsi_bounded_and_high_on_uptrend():
    # Tendance haussière AVEC vrais replis (sinon perte=0 → RS indéfini = NaN,
    # comportement normal de l'indicateur ; les vrais prix ont toujours du bruit).
    increments = np.full(60, 1.0)
    increments[::5] = -0.5          # un repli réel tous les 5 pas
    r = rsi(pd.Series(np.cumsum(increments) + 100), 14).iloc[-1]
    assert 0 <= r <= 100
    assert r > 60        # tendance majoritairement haussière → RSI élevé


def test_rsi_low_on_downtrend():
    down = pd.Series(range(60, 1, -1), dtype=float)
    assert rsi(down, 14).iloc[-1] < 10


def test_atr_is_positive():
    df = pd.DataFrame({
        "high":  [10 + i * 0.5 for i in range(30)],
        "low":   [9 + i * 0.5 for i in range(30)],
        "close": [9.5 + i * 0.5 for i in range(30)],
    })
    assert atr(df, 14).iloc[-1] > 0


def test_bb_width_positive_on_varying_series():
    s = pd.Series(np.sin(np.linspace(0, 10, 60)) * 5 + 100)
    upper, middle, lower = bollinger_bands(s, 20, 2)
    assert bb_width(upper, lower, middle).iloc[-1] > 0
