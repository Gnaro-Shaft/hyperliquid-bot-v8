#!/usr/bin/env python3
"""
Analyse de la valeur du filtre ML (Axe A — mesurer & valider l'edge).

Vérité terrain : parmi les signaux forts ±2, le filtre ML rejette-t-il vraiment
les perdants ? On labellise chaque signal par ce que le prix a RÉELLEMENT fait
(même règle que train_model), on calcule la confiance ML, puis on compare le taux
de « bons signaux » selon la décision du filtre (bloqué / pénalisé / laissé passer).

Deux mesures :
  - IN-SAMPLE  : avec le modèle déployé (indicatif, optimiste car entraîné dessus)
  - OUT-OF-SAMPLE : modèle ré-entraîné sur l'historique ancien, testé sur les
    derniers jours NON VUS (vérité honnête).

Usage :
  python -m ml.analyze_ml_filter                 # BTC + SOL, 90j, OOS 30j
  python -m ml.analyze_ml_filter --coin BTC --days 120 --oos-days 30
"""
import sys
import os
import argparse
import warnings

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
warnings.filterwarnings("ignore")

import numpy as np
from pymongo import MongoClient

from config import MONGO_URL, MONGO_DB, PAIRS
from ml.train_model import (
    load_signals, load_ohlc_15m, label_signals,
    LOOKAHEAD_CANDLES, TARGET_MOVE_PCT,
)
from ml.predictor import MLPredictor, FEATURE_NAMES

BLOCK_THR = 0.38
PEN_THR = 0.48


def _wr(labels):
    return float(np.mean(labels) * 100) if len(labels) else 0.0


def _report(title, labels, conf):
    """Affiche le taux de bons signaux par groupe de décision du filtre."""
    blk = conf < BLOCK_THR
    pen = (conf >= BLOCK_THR) & (conf < PEN_THR)
    alw = conf >= PEN_THR
    n = len(labels)
    print(f"\n  {title}")
    print(f"    {'Groupe':<26}{'N':>6}{'% bons':>9}{'part':>8}")
    rows = [
        ("TOUS (sans filtre)", np.ones(n, bool)),
        (f"BLOQUÉS (<{BLOCK_THR})", blk),
        (f"PÉNALISÉS ({BLOCK_THR}-{PEN_THR})", pen),
        (f"LAISSÉS PASSER (>={PEN_THR})", alw),
    ]
    for name, mask in rows:
        k = int(mask.sum())
        share = k / n * 100 if n else 0
        print(f"    {name:<26}{k:>6}{_wr(labels[mask]):>8.1f}%{share:>7.0f}%")
    lift = _wr(labels[alw]) - _wr(labels)
    print(f"    → Lift : +{lift:.1f} pts (laissés passer vs tous) | "
          f"{int(blk.sum() + pen.sum())} filtrés/dégradés ({(blk.sum()+pen.sum())/n*100:.0f}%)")


def analyze(coin, days, oos_days, db):
    sig = load_signals(db, coin, days)
    ohlc = load_ohlc_15m(db, coin, days)
    if sig.empty or ohlc.empty:
        print(f"\n=== {coin} : pas de données ===")
        return
    df = label_signals(sig, ohlc, LOOKAHEAD_CANDLES, TARGET_MOVE_PCT).reset_index(drop=True)
    labels = df["label"].astype(int).values
    print(f"\n{'='*58}\n  {coin} — {len(df)} signaux forts ±2 ({days}j) "
          f"| label=1 si prix +{TARGET_MOVE_PCT*100:.1f}% dans le bon sens\n{'='*58}")

    # ── IN-SAMPLE : modèle déployé ──
    pred = MLPredictor(coin=coin)
    if pred.is_available():
        conf_is = np.array([pred.predict({f: r[f] for f in FEATURE_NAMES})
                            for _, r in df.iterrows()])
        _report("IN-SAMPLE (modèle déployé — indicatif/optimiste)", labels, conf_is)
    else:
        print("  (modèle déployé indisponible — in-sample sauté)")

    # ── OUT-OF-SAMPLE : train ancien → test récent non vu ──
    from sklearn.ensemble import GradientBoostingClassifier
    from sklearn.preprocessing import StandardScaler
    cutoff = df["timestamp"].max() - oos_days * 86400 * 1000
    tr, te = df[df["timestamp"] < cutoff], df[df["timestamp"] >= cutoff]
    if len(tr) < 200 or len(te) < 50:
        print(f"\n  OOS sauté — split insuffisant (train={len(tr)}, test={len(te)})")
        return
    sc = StandardScaler()
    Xtr = sc.fit_transform(tr[FEATURE_NAMES].fillna(0).values)
    Xte = sc.transform(te[FEATURE_NAMES].fillna(0).values)
    m = GradientBoostingClassifier(n_estimators=200, max_depth=3, learning_rate=0.05,
                                   subsample=0.8, random_state=42)
    m.fit(Xtr, tr["label"].astype(int).values)
    conf_oos = m.predict_proba(Xte)[:, 1]
    _report(f"OUT-OF-SAMPLE (train {len(tr)} anciens → test {len(te)} derniers {oos_days}j NON VUS)",
            te["label"].astype(int).values, conf_oos)


def main():
    p = argparse.ArgumentParser(description="Analyse de la valeur du filtre ML")
    p.add_argument("--coin", default=None, help="BTC, SOL… (défaut : tous les PAIRS)")
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--oos-days", type=int, default=30)
    args = p.parse_args()

    coins = [args.coin.upper()] if args.coin else [c.split("/")[0] for c in PAIRS]
    db = MongoClient(MONGO_URL, serverSelectionTimeoutMS=8000)[MONGO_DB]
    for coin in coins:
        analyze(coin, args.days, args.oos_days, db)


if __name__ == "__main__":
    main()
