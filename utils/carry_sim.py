# -*- coding: utf-8 -*-
"""
utils/carry_sim.py — Maths pures pour le carry delta-neutre (testables, sans I/O).

Convention funding Hyperliquid : taux HORAIRE. Un short perp REÇOIT le funding
quand le taux est positif. Le carry delta-neutre ≈ funding encaissé − frais − basis.
"""

HOURS_PER_YEAR = 24 * 365


def annualized_funding(hourly_rate: float) -> float:
    """Taux de funding horaire → rendement annualisé (fraction). 0.0000125/h → ~0.1095."""
    return hourly_rate * HOURS_PER_YEAR


def funding_accrued(notional: float, hourly_rate: float, hours: float) -> float:
    """Funding encaissé par un SHORT perp sur `hours` heures (positif si taux>0)."""
    return notional * hourly_rate * hours


def position_delta(spot_qty: float, spot_px: float,
                   perp_qty: float, perp_px: float) -> float:
    """Exposition prix nette en USD : long spot − short perp. ~0 = delta-neutre.

    perp_qty est la TAILLE du short (valeur absolue). Le short retire perp_qty*perp_px.
    """
    return spot_qty * spot_px - perp_qty * perp_px


def needs_rebalance(delta_usd: float, total_notional: float, threshold: float) -> bool:
    """True si la dérive de delta dépasse le seuil (fraction du notional)."""
    if total_notional <= 0:
        return False
    return abs(delta_usd) / total_notional > threshold


def should_exit_funding(trailing_annual: float, min_annual: float) -> bool:
    """True si le funding annualisé (moyenne glissante) tombe sous le seuil → sortir."""
    return trailing_annual < min_annual


def margin_ratio(perp_notional: float, usdc_margin: float) -> float:
    """Levier effectif du short perp = notional / marge. Bas = sûr."""
    if usdc_margin <= 0:
        return float("inf")
    return perp_notional / usdc_margin


def liquidation_buffer_pct(entry_px: float, usdc_margin: float, perp_qty: float,
                           maint_margin_frac: float = 0.02) -> float:
    """Distance approx (fraction) avant liquidation d'un SHORT perp à la hausse.

    Le short est liquidé quand la perte ≈ marge dispo au-delà de la maintenance.
    Hausse liquidante ≈ (marge − maint·notional) / notional. Retour en fraction du prix.
    """
    notional = entry_px * perp_qty
    if notional <= 0:
        return float("inf")
    return max(0.0, (usdc_margin - maint_margin_frac * notional) / notional)


def net_carry_estimate(gross_annual: float, roundtrip_cost: float,
                       holding_days: float) -> float:
    """Carry net annualisé = brut − coût d'entrée/sortie amorti sur la durée de détention."""
    if holding_days <= 0:
        return gross_annual - roundtrip_cost
    cost_annualized = roundtrip_cost * (365.0 / holding_days)
    return gross_annual - cost_annualized


def capital_required(notional: float, leverage: float) -> float:
    """Capital total ≈ jambe spot (notional) + marge perp (notional/levier)."""
    if leverage <= 0:
        return notional
    return notional + notional / leverage


def return_on_capital(carry_annual: float, notional: float, leverage: float) -> float:
    """Rendement annualisé sur le CAPITAL TOTAL (pas juste le notional)."""
    cap = capital_required(notional, leverage)
    if cap <= 0:
        return 0.0
    return carry_annual * notional / cap
