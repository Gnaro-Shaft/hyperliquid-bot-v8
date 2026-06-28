# -*- coding: utf-8 -*-
"""Tests des maths pures du carry delta-neutre (utils/carry_sim.py)."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.carry_sim import (
    annualized_funding, funding_accrued, position_delta, needs_rebalance,
    should_exit_funding, margin_ratio, liquidation_buffer_pct,
    net_carry_estimate, capital_required, return_on_capital,
)


def test_annualized_funding():
    assert abs(annualized_funding(0.0000125) - 0.1095) < 1e-4   # taux plancher HL ~+10.95%
    assert annualized_funding(0) == 0


def test_funding_accrued_short_recoit_si_positif():
    # 1000 USD notional, +0.001%/h, 24h → +0.24 USD
    assert abs(funding_accrued(1000, 0.00001, 24) - 0.24) < 1e-9
    # funding négatif → le short PAIE
    assert funding_accrued(1000, -0.00001, 24) < 0


def test_position_delta_neutre():
    # 100 HYPE @10 long spot, 100 short perp @10 → delta ~0
    assert position_delta(100, 10, 100, 10) == 0
    # le prix monte à 12 des deux côtés → toujours neutre (mêmes quantités)
    assert position_delta(100, 12, 100, 12) == 0
    # quantités déséquilibrées → delta non nul
    assert position_delta(110, 10, 100, 10) == 100


def test_needs_rebalance():
    assert needs_rebalance(60, 1000, 0.05) is True    # 6% > 5%
    assert needs_rebalance(40, 1000, 0.05) is False   # 4% < 5%
    assert needs_rebalance(10, 0, 0.05) is False       # garde-fou division


def test_should_exit_funding():
    assert should_exit_funding(0.01, 0.02) is True     # 1% < seuil 2% → sortir
    assert should_exit_funding(0.08, 0.02) is False
    assert should_exit_funding(-0.05, 0.0) is True     # funding négatif


def test_margin_ratio():
    assert margin_ratio(1000, 500) == 2.0              # levier 2×
    assert margin_ratio(1000, 0) == float("inf")


def test_liquidation_buffer():
    # notional 1000 (px100 × 10), marge 500, maint 2% → (500-20)/1000 = 48%
    assert abs(liquidation_buffer_pct(100, 500, 10, 0.02) - 0.48) < 1e-9
    # plus de marge = buffer plus large
    assert liquidation_buffer_pct(100, 800, 10) > liquidation_buffer_pct(100, 500, 10)


def test_net_carry_estimate():
    # 8% brut, 0,6% coût A/R, tenu 90j → coût annualisé 0,6%×(365/90)=2,43% → net ~5,57%
    net = net_carry_estimate(0.08, 0.006, 90)
    assert abs(net - (0.08 - 0.006 * 365 / 90)) < 1e-9
    # tenu plus longtemps = coût mieux amorti = net plus haut
    assert net_carry_estimate(0.08, 0.006, 180) > net


def test_capital_et_rendement():
    # notional 1000, levier 2 → capital = 1000 + 500 = 1500
    assert capital_required(1000, 2) == 1500
    # carry 8% sur notional → sur capital total = 8% × 1000/1500 ≈ 5,33%
    assert abs(return_on_capital(0.08, 1000, 2) - 0.08 * 1000 / 1500) < 1e-9
