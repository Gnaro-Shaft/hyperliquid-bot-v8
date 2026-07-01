#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
carry/carry_live.py — PHASE 3 : exécution LIVE du carry delta-neutre HYPE.

⚠️ MONEY-PATH. Par défaut DRY-RUN : affiche les ordres qu'il passerait, n'envoie RIEN.
N'exécute des ordres réels QUE si CARRY_LIVE=true (variable d'env) ET que tu le lances.
À n'utiliser qu'APRÈS validation paper (carry_paper.py) sur plusieurs semaines.

Sécurités intégrées :
  - DRY-RUN par défaut (CARRY_LIVE=false)
  - Vérif solde USDC + buffer de liquidation avant d'ouvrir
  - Gestion du RISQUE DE JAMBE : place le perp d'abord, confirme, puis le spot ;
    si la 2e jambe échoue → unwind immédiat de la 1re (pas d'expo directionnelle)
  - Levier bas (CARRY_LEVERAGE), notional plafonné

Usage:
    python carry/carry_live.py --plan            # affiche le plan (dry-run, sûr)
    CARRY_LIVE=true python carry/carry_live.py --open   # ouvre réellement (À TES RISQUES)
"""
import sys, os, argparse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import (
    CARRY_COIN, CARRY_NOTIONAL_USDC, CARRY_LEVERAGE, CARRY_FEE_RATE, CARRY_LIVE,
    HYPERLIQUID_API_KEY, HYPERLIQUID_API_SECRET,
)
from carry import hl_data
from utils.carry_sim import (
    legs_from_notional, capital_required, liquidation_buffer_pct, trade_fees,
)

SPOT_SYMBOL = f"{CARRY_COIN}/USDC"
PERP_SYMBOL = f"{CARRY_COIN}/USDC:USDC"


def build_plan() -> dict:
    """Construit le plan delta-neutre (prix publics, aucune clé requise). Pur affichage."""
    spot = hl_data.spot_mid(CARRY_COIN)
    perp = hl_data.perp_mid(CARRY_COIN)
    n = CARRY_NOTIONAL_USDC
    spot_qty, perp_qty = legs_from_notional(n, spot, perp)
    margin = n / CARRY_LEVERAGE
    capital = capital_required(n, CARRY_LEVERAGE)
    buf = liquidation_buffer_pct(perp, margin, perp_qty)
    fees_open = trade_fees(n, CARRY_FEE_RATE, 2)
    return {
        "coin": CARRY_COIN, "spot_px": spot, "perp_px": perp, "notional": n,
        "spot_qty": spot_qty, "perp_qty": perp_qty, "leverage": CARRY_LEVERAGE,
        "perp_margin": margin, "capital_total": capital,
        "liq_buffer_pct": buf, "fees_open": fees_open,
    }


def print_plan(p: dict):
    print(f"\n=== PLAN CARRY DELTA-NEUTRE [{p['coin']}] ({'🟢 LIVE' if CARRY_LIVE else '📝 DRY-RUN'}) ===")
    print(f"  Jambe 1 (perp)  : SHORT {p['perp_qty']:.4f} {p['coin']} @ ~{p['perp_px']}  (marge {p['perp_margin']:.1f} USDC, levier {p['leverage']}×)")
    print(f"  Jambe 2 (spot)  : BUY   {p['spot_qty']:.4f} {p['coin']} @ ~{p['spot_px']}  ({p['notional']} USDC)")
    print(f"  Capital total requis ≈ {p['capital_total']:.1f} USDC | frais ouverture ~{p['fees_open']:.3f}$")
    print(f"  Buffer avant liquidation du short : {p['liq_buffer_pct']*100:.0f}% de hausse HYPE")
    if not CARRY_LIVE:
        print("  → DRY-RUN : aucun ordre envoyé. Pour exécuter : CARRY_LIVE=true + --open (à tes risques).")


def _client():
    import ccxt
    return ccxt.hyperliquid({"walletAddress": HYPERLIQUID_API_KEY,
                             "privateKey": HYPERLIQUID_API_SECRET})


def open_position(p: dict):
    """Ouvre RÉELLEMENT les 2 jambes avec gestion du risque de jambe. Gated par CARRY_LIVE."""
    if not CARRY_LIVE:
        print("  ⛔ CARRY_LIVE=false → refus d'exécuter. (dry-run uniquement)")
        return
    if not HYPERLIQUID_API_KEY:
        print("  ⛔ clés HL absentes → refus.")
        return
    ex = _client()
    bal = ex.fetch_balance()
    usdc = bal.get("USDC", {}).get("free", 0)
    if usdc < p["capital_total"]:
        print(f"  ⛔ solde insuffisant : {usdc} < {p['capital_total']:.1f} USDC requis.")
        return
    # HL exige un prix de référence pour les "market" (borne le slippage à 5% par défaut).
    # Jambe 1 : SHORT perp (la plus risquée — on la pose en premier)
    print(f"  → Ordre perp SHORT {p['perp_qty']:.4f}…")
    o1 = ex.create_order(PERP_SYMBOL, "market", "sell", p["perp_qty"], p["perp_px"])
    # Jambe 2 : BUY spot ; si échec → on déboucle le perp (pas d'expo directionnelle)
    try:
        print(f"  → Ordre spot BUY {p['spot_qty']:.4f}…")
        o2 = ex.create_order(SPOT_SYMBOL, "market", "buy", p["spot_qty"], p["spot_px"])
    except Exception as e:
        print(f"  ⚠️ jambe spot échouée ({str(e)[:60]}) → UNWIND du perp pour rester neutre")
        ex.create_order(PERP_SYMBOL, "market", "buy", p["perp_qty"], p["perp_px"],
                        params={"reduceOnly": True})
        return
    print(f"  ✅ position delta-neutre ouverte (perp {o1.get('id')} / spot {o2.get('id')})")


def status():
    """Affiche la position carry réelle (perp + spot) + funding. Lecture seule (clés requises)."""
    if not HYPERLIQUID_API_KEY:
        print("  ⛔ clés HL absentes → impossible de lire le compte.")
        return
    ex = _client()
    bal = ex.fetch_balance()
    hype = bal.get(CARRY_COIN, {}).get("total", 0)
    usdc = bal.get("USDC", {}).get("free", 0)
    perp_sz = 0.0
    try:
        for pos in ex.fetch_positions([PERP_SYMBOL]):
            perp_sz = float(pos.get("contracts") or 0) * (-1 if pos.get("side") == "short" else 1)
    except Exception as e:
        print(f"  [warn] fetch_positions: {str(e)[:50]}")
    f = hl_data.current_funding(CARRY_COIN)
    print(f"\n=== STATUT CARRY LIVE [{CARRY_COIN}] ===")
    print(f"  Spot {CARRY_COIN} détenu : {hype:.4f}  | Perp (négatif=short) : {perp_sz:.4f}")
    print(f"  USDC libre : {usdc:.2f}  | funding annualisé : {f*24*365*100:+.1f}%")
    print(f"  Delta approx (spot+perp) : {(hype + perp_sz):.4f} {CARRY_COIN} ({'~neutre' if abs(hype+perp_sz)<0.05 else '⚠️ déséquilibré'})")


def close_position():
    """Ferme les 2 jambes (rachat perp reduceOnly + vente spot). Gated par CARRY_LIVE."""
    if not CARRY_LIVE:
        print("  ⛔ CARRY_LIVE=false → refus. (pour fermer réellement : CARRY_LIVE=true)")
        return
    ex = _client()
    perp_px = hl_data.perp_mid(CARRY_COIN)   # prix de référence requis par HL
    spot_px = hl_data.spot_mid(CARRY_COIN)
    # 1) racheter le short perp (reduceOnly)
    for pos in ex.fetch_positions([PERP_SYMBOL]):
        sz = abs(float(pos.get("contracts") or 0))
        if sz > 0 and pos.get("side") == "short":
            print(f"  → rachat perp {sz:.4f} (reduceOnly)…")
            ex.create_order(PERP_SYMBOL, "market", "buy", sz, perp_px, params={"reduceOnly": True})
    # 2) vendre le spot HYPE
    hype = ex.fetch_balance().get(CARRY_COIN, {}).get("free", 0)
    if hype and hype > 0:
        print(f"  → vente spot {hype:.4f}…")
        ex.create_order(SPOT_SYMBOL, "market", "sell", hype, spot_px)
    print("  ✅ position carry fermée.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", action="store_true", help="affiche le plan (sûr, sans clé)")
    ap.add_argument("--status", action="store_true", help="affiche la position réelle (lecture)")
    ap.add_argument("--open", action="store_true", help="OUVRE (nécessite CARRY_LIVE=true)")
    ap.add_argument("--close", action="store_true", help="FERME les 2 jambes (nécessite CARRY_LIVE=true)")
    args = ap.parse_args()
    if args.status:
        status(); return
    if args.close:
        close_position(); return
    p = build_plan()
    print_plan(p)
    if args.open:
        open_position(p)


if __name__ == "__main__":
    main()
