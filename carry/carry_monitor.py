#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
carry/carry_monitor.py — PHASE 1 : monitor read-only du carry delta-neutre HYPE.

Observe en LIVE le funding + les prix (API publique HL), calcule le carry qu'une
position SIMULÉE encaisserait, applique les règles (sortie/rebalance) en dry-run,
et logue. AUCUN ordre, AUCUN risque. Sert à prouver l'edge en réel avant le paper.

Usage:
    python carry/carry_monitor.py            # rapport unique (cron-friendly)
    python carry/carry_monitor.py --watch 60 # boucle toutes les 60 s
"""
import sys, os, time, argparse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import (
    CARRY_COIN, CARRY_NOTIONAL_USDC, CARRY_LEVERAGE, CARRY_MIN_FUNDING_ANNUAL,
    CARRY_FUNDING_LOOKBACK_H, MONGO_URL, MONGO_DB,
    MONGO_COLLECTION_CARRY_STATE, MONGO_COLLECTION_CARRY_LOG,
)
from carry import hl_data
from utils.carry_sim import (
    annualized_funding, funding_accrued, should_exit_funding,
    return_on_capital, net_carry_estimate,
)


def run_once() -> dict:
    coin = CARRY_COIN
    f_now = hl_data.current_funding(coin)
    start = int((time.time() - CARRY_FUNDING_LOOKBACK_H * 3600) * 1000)
    hist = hl_data.funding_history(coin, start)
    f_trail = sum(hist) / len(hist) if hist else f_now

    perp = hl_data.perp_mid(coin)
    try:
        spot = hl_data.spot_mid(coin)
        basis = (perp - spot) / spot
    except Exception:
        spot, basis = None, None

    ann_now = annualized_funding(f_now)
    ann_trail = annualized_funding(f_trail)
    notional = CARRY_NOTIONAL_USDC
    daily = funding_accrued(notional, f_trail, 24)            # carry simulé / jour
    roc = return_on_capital(ann_trail, notional, CARRY_LEVERAGE)
    net = net_carry_estimate(ann_trail, 0.0002, 90)          # net approx (coût A/R 0.02% tenu 90j)
    exit_sig = should_exit_funding(ann_trail, CARRY_MIN_FUNDING_ANNUAL)

    state = {
        "_id": "current", "coin": coin, "ts": int(time.time() * 1000),
        "funding_hourly": f_now, "funding_annual_now": ann_now,
        "funding_annual_trailing": ann_trail, "trailing_points": len(hist),
        "perp_mid": perp, "spot_mid": spot, "basis_pct": basis,
        "sim_notional": notional, "sim_daily_carry_usd": daily,
        "return_on_capital_annual": roc, "net_carry_annual_est": net,
        "exit_signal": exit_sig, "live": False,
    }
    return state


def _print(s: dict):
    print(f"\n=== CARRY MONITOR [{s['coin']}] (read-only, simulé) ===")
    print(f"  Funding annualisé : instant {s['funding_annual_now']*100:+.1f}%  |  "
          f"glissant {s['funding_annual_trailing']*100:+.1f}% ({s['trailing_points']}h)")
    b = f"{s['basis_pct']*100:+.3f}%" if s['basis_pct'] is not None else "n/a"
    print(f"  Prix : perp {s['perp_mid']}  spot {s['spot_mid']}  | basis {b}")
    print(f"  Position simulée : {s['sim_notional']} USDC/jambe → carry ~{s['sim_daily_carry_usd']:+.4f} $/jour")
    print(f"  Rendement /capital total ≈ {s['return_on_capital_annual']*100:+.1f}%  |  net estimé ≈ {s['net_carry_annual_est']*100:+.1f}%")
    print(f"  Signal sortie (funding < {CARRY_MIN_FUNDING_ANNUAL*100:.0f}%) : "
          f"{'🔴 OUI — carry trop faible' if s['exit_signal'] else '🟢 non, carry OK'}")


def _log_mongo(s: dict):
    try:
        from pymongo import MongoClient
        db = MongoClient(MONGO_URL, serverSelectionTimeoutMS=5000)[MONGO_DB]
        db[MONGO_COLLECTION_CARRY_STATE].replace_one({"_id": "current"}, s, upsert=True)
        log = {k: v for k, v in s.items() if k != "_id"}
        db[MONGO_COLLECTION_CARRY_LOG].insert_one(log)
    except Exception as e:
        print(f"  [warn] log Mongo ignoré: {str(e)[:60]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", type=int, default=0, help="boucle toutes les N secondes")
    ap.add_argument("--no-mongo", action="store_true", help="ne pas logger en base")
    args = ap.parse_args()
    while True:
        s = run_once()
        _print(s)
        if not args.no_mongo:
            _log_mongo(s)
        if args.watch <= 0:
            break
        time.sleep(args.watch)


if __name__ == "__main__":
    main()
