#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
carry/carry_manager.py — PHASE 3b : gestionnaire automatique du carry live.

Surveille la position carry et la FERME automatiquement si :
  - le funding moyen 7j tombe sous le seuil (2%) pendant N checks consécutifs, ou
  - le prix approche dangereusement la liquidation du short perp.

Sécurité : DRY-RUN par défaut (CARRY_LIVE=false → signale seulement, ne ferme pas).
Ferme réellement seulement si CARRY_LIVE=true. Conçu pour tourner en cron (1×/h).

Usage:
    python carry/carry_manager.py                 # 1 check (dry-run si CARRY_LIVE=false)
    CARRY_LIVE=true python carry/carry_manager.py # 1 check + fermeture auto si besoin
    # cron horaire recommandé :
    #   0 * * * * cd ~/…/v8 && CARRY_LIVE=true python carry/carry_manager.py >> carry_mgr.log 2>&1
"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import (
    CARRY_COIN, CARRY_MIN_FUNDING_ANNUAL, CARRY_FUNDING_LOOKBACK_H,
    CARRY_REBALANCE_DELTA_PCT, CARRY_LIVE, HYPERLIQUID_API_KEY,
    MONGO_URL, MONGO_DB,
)
from carry import hl_data
from carry.carry_live import _client, close_position, PERP_SYMBOL
from utils.carry_sim import should_exit_funding, needs_rebalance, annualized_funding

EXIT_CONFIRM   = 2      # nb de checks 🔴 consécutifs avant fermeture auto (anti-creux ponctuel)
LIQ_MIN_BUFFER = 0.10   # ferme si le prix est à moins de 10% du prix de liquidation du short
STATE_COL = "carry_manager_state"


def _db():
    from pymongo import MongoClient
    return MongoClient(MONGO_URL, serverSelectionTimeoutMS=5000)[MONGO_DB]


def _load_count():
    try:
        d = _db()[STATE_COL].find_one({"_id": "current"})
        return int(d.get("exit_count", 0)) if d else 0
    except Exception:
        return 0


def _save(doc):
    try:
        _db()[STATE_COL].replace_one({"_id": "current"}, {"_id": "current", **doc}, upsert=True)
    except Exception as e:
        print(f"  [warn] état Mongo ignoré: {str(e)[:50]}")


def read_position(ex):
    bal = ex.fetch_balance()
    hype = float(bal.get(CARRY_COIN, {}).get("total", 0) or 0)
    usdc = float(bal.get("USDC", {}).get("free", 0) or 0)
    perp_sz, liq = 0.0, None
    for p in ex.fetch_positions([PERP_SYMBOL]):
        c = float(p.get("contracts") or 0)
        if c > 0:
            perp_sz = -c if p.get("side") == "short" else c
            liq = p.get("liquidationPrice")
    return hype, perp_sz, usdc, liq


def manage():
    if not HYPERLIQUID_API_KEY:
        print("  ⛔ clés HL absentes → impossible de gérer.")
        return
    ex = _client()
    hype, perp_sz, usdc, liq = read_position(ex)
    if abs(perp_sz) < 1e-6 and hype < 1e-6:
        print("  ℹ️ aucune position carry ouverte — rien à gérer.")
        _save({"exit_count": 0, "status": "flat"})
        return

    # 1) funding moyen 7j
    start = int((time.time() - CARRY_FUNDING_LOOKBACK_H * 3600) * 1000)
    hist = hl_data.funding_history(CARRY_COIN, start)
    ann = annualized_funding(sum(hist) / len(hist)) if hist else 0.0
    exit_sig = should_exit_funding(ann, CARRY_MIN_FUNDING_ANNUAL)
    cnt = _load_count() + 1 if exit_sig else 0

    # 2) risque de liquidation (short → liquidé à la hausse)
    perp_px = hl_data.perp_mid(CARRY_COIN)
    liq_buf = ((float(liq) - perp_px) / perp_px) if (liq and perp_px > 0) else 1.0
    liq_risk = 0 < liq_buf < LIQ_MIN_BUFFER

    # 3) delta (info)
    delta_units = hype + perp_sz
    delta_off = needs_rebalance(delta_units * perp_px, abs(perp_sz) * perp_px, CARRY_REBALANCE_DELTA_PCT)

    reason = None
    if liq_risk:
        reason = f"liquidation proche (buffer {liq_buf*100:.0f}% < {LIQ_MIN_BUFFER*100:.0f}%)"
    elif cnt >= EXIT_CONFIRM:
        reason = f"funding {ann*100:.1f}% < {CARRY_MIN_FUNDING_ANNUAL*100:.0f}% sur {cnt} checks"

    print(f"\n=== CARRY MANAGER [{CARRY_COIN}] ({'🟢 LIVE' if CARRY_LIVE else '📝 DRY-RUN'}) ===")
    print(f"  Position : spot {hype:.4f} / perp {perp_sz:.4f} | USDC libre {usdc:.2f}")
    print(f"  Funding 7j : {ann*100:+.1f}%  (exit {'🔴' if exit_sig else '🟢'}, {cnt}/{EXIT_CONFIRM}) | "
          f"buffer liq {liq_buf*100:.0f}% | delta {'⚠️' if delta_off else 'OK'}")

    if reason:
        if CARRY_LIVE:
            print(f"  🔴 FERMETURE AUTO → {reason}")
            close_position()
            _save({"exit_count": 0, "status": "closed", "reason": reason})
        else:
            print(f"  🔴 Fermeture RECOMMANDÉE ({reason}) — dry-run, rien fait. "
                  f"CARRY_LIVE=true pour auto-close.")
            _save({"exit_count": cnt, "status": "exit_recommended", "reason": reason})
    else:
        print("  🟢 On garde. Rien à faire.")
        _save({"exit_count": cnt, "status": "holding"})


if __name__ == "__main__":
    manage()
