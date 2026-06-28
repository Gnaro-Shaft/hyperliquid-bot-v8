#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
carry/carry_paper.py — PHASE 2 : exécution PAPER du carry delta-neutre HYPE.

Simule les 2 jambes (long spot + short perp), accumule le funding réel, marque
les prix, calcule le P&L NET (funding − frais ± basis), suit le delta, applique
les règles (rebalance/exit). Persiste l'état (Mongo). AUCUN ordre réel.

État : collection carry_paper_state (_id="current"). Trades : carry_paper_trades.

Usage:
    python carry/carry_paper.py             # 1 tick (cron-friendly)
    python carry/carry_paper.py --watch 300 # boucle 5 min
    python carry/carry_paper.py --reset     # repart à zéro
"""
import sys, os, time, argparse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import (
    CARRY_COIN, CARRY_NOTIONAL_USDC, CARRY_LEVERAGE, CARRY_MIN_FUNDING_ANNUAL,
    CARRY_REBALANCE_DELTA_PCT, CARRY_FEE_RATE, MONGO_URL, MONGO_DB,
    MONGO_COLLECTION_CARRY_PAPER_STATE, MONGO_COLLECTION_CARRY_PAPER_TRADES,
)
from carry import hl_data
from utils.carry_sim import (
    annualized_funding, funding_accrued, legs_from_notional, price_pnl,
    position_delta, needs_rebalance, should_exit_funding, trade_fees,
)


def _db():
    from pymongo import MongoClient
    return MongoClient(MONGO_URL, serverSelectionTimeoutMS=5000)[MONGO_DB]


def _load():
    try:
        return _db()[MONGO_COLLECTION_CARRY_PAPER_STATE].find_one({"_id": "current"})
    except Exception:
        return None


def _save(state, trade=None):
    try:
        db = _db()
        db[MONGO_COLLECTION_CARRY_PAPER_STATE].replace_one({"_id": "current"}, state, upsert=True)
        if trade:
            db[MONGO_COLLECTION_CARRY_PAPER_TRADES].insert_one(trade)
    except Exception as e:
        print(f"  [warn] persistance Mongo ignorée: {str(e)[:60]}")


def _open(coin):
    spot = hl_data.spot_mid(coin)
    perp = hl_data.perp_mid(coin)
    sq, pq = legs_from_notional(CARRY_NOTIONAL_USDC, spot, perp)
    fees = trade_fees(CARRY_NOTIONAL_USDC, CARRY_FEE_RATE, 2)   # 2 jambes à l'ouverture
    now = int(time.time() * 1000)
    return {
        "_id": "current", "status": "open", "coin": coin,
        "open_ts": now, "last_ts": now,
        "spot_entry": spot, "perp_entry": perp, "spot_qty": sq, "perp_qty": pq,
        "notional": CARRY_NOTIONAL_USDC,
        "funding_cum": 0.0, "fees_cum": fees, "ticks": 0,
    }


def tick(reset=False):
    coin = CARRY_COIN
    st = None if reset else _load()
    trade = None
    if not st or st.get("status") != "open":
        st = _open(coin)
        trade = {"ts": st["open_ts"], "event": "OPEN", "coin": coin,
                 "spot": st["spot_entry"], "perp": st["perp_entry"],
                 "notional": st["notional"], "fees": st["fees_cum"]}
        print(f"  [PAPER] OUVERTURE {coin} : long {st['spot_qty']:.4f} spot @ {st['spot_entry']} "
              f"+ short {st['perp_qty']:.4f} perp @ {st['perp_entry']} | frais {st['fees_cum']:.4f}$")

    # marché courant
    f_now = hl_data.current_funding(coin)
    spot = hl_data.spot_mid(coin)
    perp = hl_data.perp_mid(coin)
    now = int(time.time() * 1000)

    # accumulation du funding depuis le dernier tick
    hours = max(0.0, (now - st["last_ts"]) / 3_600_000)
    st["funding_cum"] += funding_accrued(st["notional"], f_now, hours)

    # marquage P&L
    ppnl = price_pnl(st["spot_qty"], st["spot_entry"], spot,
                     st["perp_qty"], st["perp_entry"], perp)
    net = st["funding_cum"] + ppnl - st["fees_cum"]
    delta = position_delta(st["spot_qty"], spot, st["perp_qty"], perp)
    ann_trail = annualized_funding(f_now)

    st.update(last_ts=now, ticks=st["ticks"] + 1,
              spot_now=spot, perp_now=perp, price_pnl=ppnl, net_pnl=net,
              delta_usd=delta, funding_annual=ann_trail)

    # règles (dry-run en paper : on signale, et on ferme si exit)
    rebalance = needs_rebalance(delta, st["notional"], CARRY_REBALANCE_DELTA_PCT)
    exit_sig = should_exit_funding(ann_trail, CARRY_MIN_FUNDING_ANNUAL)
    st["rebalance_needed"] = rebalance
    st["exit_signal"] = exit_sig

    if exit_sig:
        st["fees_cum"] += trade_fees(st["notional"], CARRY_FEE_RATE, 2)  # clôture 2 jambes
        st["net_pnl"] = st["funding_cum"] + ppnl - st["fees_cum"]
        st["status"] = "closed"
        trade = {"ts": now, "event": "CLOSE", "coin": coin, "reason": "funding<seuil",
                 "net_pnl": st["net_pnl"], "funding_cum": st["funding_cum"], "fees_cum": st["fees_cum"]}
        print(f"  [PAPER] CLÔTURE (funding {ann_trail*100:.1f}% < seuil) | net {st['net_pnl']:+.4f}$")

    _save(st, trade)
    return st


def _print(s):
    print(f"\n=== CARRY PAPER [{s['coin']}] (simulé, aucun ordre réel) ===")
    print(f"  Statut : {s['status']} | ticks {s['ticks']} | notional {s['notional']}$/jambe")
    print(f"  Funding cumulé : {s['funding_cum']:+.4f}$  (annualisé {s.get('funding_annual',0)*100:+.1f}%)")
    print(f"  P&L prix (≈0 si neutre) : {s.get('price_pnl',0):+.4f}$  | frais {s['fees_cum']:.4f}$")
    print(f"  >>> P&L NET : {s.get('net_pnl',0):+.4f}$")
    print(f"  Delta : {s.get('delta_usd',0):+.2f}$ {'⚠️ rebalance' if s.get('rebalance_needed') else 'OK'} | "
          f"exit {'🔴' if s.get('exit_signal') else '🟢'}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", type=int, default=0)
    ap.add_argument("--reset", action="store_true")
    args = ap.parse_args()
    first = True
    while True:
        s = tick(reset=args.reset and first)
        _print(s)
        first = False
        if args.watch <= 0:
            break
        time.sleep(args.watch)


if __name__ == "__main__":
    main()
