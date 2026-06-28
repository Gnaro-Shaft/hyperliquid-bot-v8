#!/usr/bin/env python3
"""
Entraînement du filtre ML de qualité des signaux — Bot Hyperliquid v8
=======================================================================

Principe :
  1. Charge les signaux MongoDB (gate_passed=True, signal_level ≠ 0)
  2. Pour chaque signal, regarde les N prochaines bougies 15m
  3. Label = 1 si le prix a bougé > TARGET_MOVE_PCT dans le sens du signal
  4. Entraîne un RandomForestClassifier avec validation croisée
  5. Sauvegarde modèle + scaler dans ml/models/

Features utilisées :
  rsi_14, adx_14, bb_width, raw_score, signal_level,
  atr_pct, funding_rate, ob_imbalance, oi_change_pct, bb_pctB

Usage :
  python ml/train_model.py                  (BTC + SOL, 60 jours)
  python ml/train_model.py --coin BTC
  python ml/train_model.py --coin SOL --days 90 --lookahead 4 --target 0.005
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import argparse
import warnings
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd
from datetime import datetime, timezone
from collections import Counter

from pymongo import MongoClient

from config import (
    MONGO_URL, MONGO_DB,
    MONGO_COLLECTION_SIGNALS, MONGO_COLLECTION_15M,
    PAIRS,
)
from ml.predictor import FEATURE_NAMES

# ─── Paramètres par défaut ───────────────────────────────────────────
LOOKAHEAD_CANDLES  = 4      # 4 × 15m = 1h de fenêtre pour valider
TARGET_MOVE_PCT    = 0.004  # défaut / fallback (+0.4% dans le bon sens → signal "bon")

# Cibles par coin (Axe C, 28/06/2026) — calibrées et validées OOS sur 3 fenêtres :
# BTC favorise un seuil bas, SOL un seuil plus haut (lookahead 4 = 1h, optimal pour
# les deux). Gain ~+0.04-0.05 AUC out-of-sample vs le 0.4% uniforme.
TARGET_MOVE_PCT_BY_COIN = {"BTC": 0.003, "SOL": 0.006}


def target_for_coin(coin: str) -> float:
    """Cible de mouvement pour le labelling, spécifique au coin (fallback défaut)."""
    return TARGET_MOVE_PCT_BY_COIN.get(coin.upper(), TARGET_MOVE_PCT)
MIN_SAMPLES        = 80     # Refuse d'entraîner si trop peu de données


def load_signals(db, coin: str, days: int) -> pd.DataFrame:
    """Charge les signaux valides depuis MongoDB."""
    now_ms   = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = now_ms - days * 86400 * 1000

    docs = list(db[MONGO_COLLECTION_SIGNALS].find({
        "coin": coin,
        "gate_passed": True,
        "signal_level": {"$in": [-2, -1, 1, 2]},
        "timestamp": {"$gte": start_ms},
    }, {
        "timestamp": 1, "signal_level": 1, "raw_score": 1,
        "rsi_14": 1, "adx_14": 1, "bb_width": 1,
        "debug": 1,
    }).sort("timestamp", 1))

    if not docs:
        return pd.DataFrame()

    rows = []
    for d in docs:
        dbg = d.get("debug", {})
        if isinstance(dbg, str):
            # Fallback si debug stocké en string (ancienne version)
            try:
                dbg = eval(dbg)  # noqa: S307
            except Exception:
                dbg = {}

        rows.append({
            "timestamp":    int(d.get("timestamp", 0)),
            "signal_level": int(d.get("signal_level", 0)),
            "raw_score":    float(d.get("raw_score", 0)),
            "rsi_14":       float(d.get("rsi_14") or dbg.get("RSI", 50)),
            "adx_14":       float(d.get("adx_14") or dbg.get("ADX", 20)),
            "bb_width":     float(d.get("bb_width") or dbg.get("BB_width", 0.005)),
            "atr_pct":      float(dbg.get("atr_pct", 0.005)),
            "funding_rate": float(dbg.get("funding_rate") or 0),
            "ob_imbalance": float(dbg.get("ob_imbalance") or 0),
            "oi_change_pct":float(dbg.get("oi_change_pct") or 0),
            "bb_pctB":      float(dbg.get("BB_pctB") or 0.5),
        })

    return pd.DataFrame(rows)


def load_ohlc_15m(db, coin: str, days: int) -> pd.DataFrame:
    """Charge les bougies 15m pour le labelling."""
    now_ms   = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = now_ms - (days + 1) * 86400 * 1000  # +1j de marge

    docs = list(db[MONGO_COLLECTION_15M].find(
        {"coin": coin, "timestamp": {"$gte": start_ms}},
        {"timestamp": 1, "open": 1, "high": 1, "low": 1, "close": 1}
    ).sort("timestamp", 1))

    if not docs:
        return pd.DataFrame()

    df = pd.DataFrame(docs)
    for col in ["open", "high", "low", "close"]:
        df[col] = df[col].astype(float)
    return df.reset_index(drop=True)


def label_signals(signals: pd.DataFrame, ohlc: pd.DataFrame,
                  lookahead: int, target_pct: float) -> pd.DataFrame:
    """
    Attribue un label à chaque signal selon le mouvement des bougies suivantes.

    Label = 1 :
      - Signal haussier (+1/+2) et max(high[t+1..t+N]) > entry * (1 + target_pct)
      - Signal baissier (-1/-2) et min(low[t+1..t+N]) < entry * (1 - target_pct)
    Label = 0 sinon
    """
    ohlc_ts = ohlc["timestamp"].values

    labels = []
    for _, row in signals.iterrows():
        sig_ts   = row["timestamp"]
        sig_dir  = 1 if row["signal_level"] > 0 else -1

        # Indice de la bougie 15m >= timestamp du signal
        idx = np.searchsorted(ohlc_ts, sig_ts)
        future_slice = ohlc.iloc[idx : idx + lookahead]

        if future_slice.empty:
            labels.append(np.nan)
            continue

        entry_close = ohlc.iloc[idx]["open"] if idx < len(ohlc) else None
        if entry_close is None or entry_close == 0:
            labels.append(np.nan)
            continue

        if sig_dir == 1:
            best_move = (future_slice["high"].max() - entry_close) / entry_close
        else:
            best_move = (entry_close - future_slice["low"].min()) / entry_close

        labels.append(1 if best_move >= target_pct else 0)

    signals = signals.copy()
    signals["label"] = labels
    return signals.dropna(subset=["label"])


def train(coin: str, days: int, lookahead: int, target_pct: float,
          model_dir: str, holdout_days: int = 0) -> dict:
    """Entraîne et sauvegarde le modèle pour un coin.

    Si holdout_days > 0 : calcule en plus une AUC de validation temporelle
    (entraînement sur l'historique ancien, test sur les holdout_days récents)
    pour détecter le surapprentissage du régime récent. Retournée dans
    result["holdout_auc"] (None si non calculable).
    """
    from sklearn.ensemble import GradientBoostingClassifier
    from sklearn.preprocessing import StandardScaler
    from sklearn.model_selection import cross_val_score, StratifiedKFold
    from sklearn.metrics import classification_report, roc_auc_score
    import joblib

    print(f"\n{'═' * 55}")
    print(f"  ENTRAÎNEMENT ML — {coin}  ({days} jours)")
    print(f"  Lookahead: {lookahead}×15m = {lookahead*15}min | Cible: {target_pct*100:.2f}%")
    print(f"{'═' * 55}")

    client = MongoClient(MONGO_URL, serverSelectionTimeoutMS=8000)
    db = client[MONGO_DB]

    print(f"[ML] Chargement des signaux…")
    signals = load_signals(db, coin, days)
    if signals.empty:
        print(f"[ML] ❌ Aucun signal trouvé pour {coin} ({days}j).")
        return {}

    print(f"[ML] Chargement OHLC 15m…")
    ohlc = load_ohlc_15m(db, coin, days)
    if ohlc.empty:
        print(f"[ML] ❌ Aucune donnée 15m pour {coin}.")
        return {}

    print(f"[ML] Labelling {len(signals)} signaux…")
    df = label_signals(signals, ohlc, lookahead, target_pct)
    print(f"[ML] Dataset final : {len(df)} échantillons")

    if len(df) < MIN_SAMPLES:
        print(f"[ML] ⚠️  Trop peu d'échantillons ({len(df)} < {MIN_SAMPLES}). "
              f"Accumulez plus de données et relancez.")
        return {}

    label_counts = Counter(df["label"].astype(int))
    print(f"[ML] Distribution labels : {dict(label_counts)}"
          f"  (1=bon signal, 0=mauvais signal)")

    X = df[FEATURE_NAMES].fillna(0).values
    y = df["label"].astype(int).values

    # Normalisation
    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)

    # Modèle — GradientBoosting est plus robuste que RF sur petits datasets
    model = GradientBoostingClassifier(
        n_estimators=200,
        max_depth=3,
        learning_rate=0.05,
        subsample=0.8,
        random_state=42,
    )

    # Validation croisée stratifiée (5-fold)
    cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
    cv_scores = cross_val_score(model, X_scaled, y, cv=cv, scoring="roc_auc")
    print(f"\n[ML] Cross-validation AUC : {cv_scores.mean():.3f} ± {cv_scores.std():.3f}")
    print(f"      Scores par fold      : {' | '.join(f'{s:.3f}' for s in cv_scores)}")

    # ── Validation holdout temporelle (anti-surapprentissage du régime récent) ──
    holdout_auc = None
    if holdout_days and holdout_days > 0:
        ts = df["timestamp"].values
        cutoff = ts.max() - holdout_days * 86400 * 1000
        train_mask = ts < cutoff
        test_mask  = ts >= cutoff
        n_tr, n_te = int(train_mask.sum()), int(test_mask.sum())
        if n_tr >= MIN_SAMPLES and n_te >= 30:
            y_tr, y_te = y[train_mask], y[test_mask]
            if len(set(y_tr)) > 1 and len(set(y_te)) > 1:
                sc_h = StandardScaler()
                X_tr = sc_h.fit_transform(X[train_mask])
                X_te = sc_h.transform(X[test_mask])
                m_h = GradientBoostingClassifier(
                    n_estimators=200, max_depth=3, learning_rate=0.05,
                    subsample=0.8, random_state=42,
                )
                m_h.fit(X_tr, y_tr)
                holdout_auc = float(roc_auc_score(y_te, m_h.predict_proba(X_te)[:, 1]))
                print(f"[ML] Holdout AUC ({holdout_days}j récents, "
                      f"train={n_tr}/test={n_te}) : {holdout_auc:.3f}")
            else:
                print(f"[ML] Holdout impossible : une seule classe dans train/test")
        else:
            print(f"[ML] Holdout impossible : pas assez d'échantillons "
                  f"(train={n_tr}, test={n_te})")

    # Entraînement final sur tout le dataset
    model.fit(X_scaled, y)

    # Report sur tout le dataset (indicatif)
    y_pred = model.predict(X_scaled)
    print(f"\n[ML] Rapport sur dataset complet (train — indicatif) :")
    print(classification_report(y, y_pred, target_names=["Mauvais", "Bon"], digits=3))

    # Importance des features
    print(f"[ML] Importance des features (Top 5) :")
    importances = list(zip(FEATURE_NAMES, model.feature_importances_))
    importances.sort(key=lambda x: -x[1])
    for feat, imp in importances[:5]:
        bar = "█" * int(imp * 40)
        print(f"  {feat:<18} {bar} {imp:.4f}")

    # Sauvegarde
    os.makedirs(model_dir, exist_ok=True)
    model_path  = os.path.join(model_dir, f"signal_filter_{coin}.pkl")
    scaler_path = os.path.join(model_dir, f"scaler_{coin}.pkl")
    joblib.dump(model,  model_path)
    joblib.dump(scaler, scaler_path)
    print(f"\n[ML] ✅ Modèle sauvegardé : {model_path}")
    print(f"[ML] ✅ Scaler sauvegardé  : {scaler_path}")

    return {
        "coin": coin,
        "samples": len(df),
        "label_1_pct": round(label_counts.get(1, 0) / len(df) * 100, 1),
        "cv_auc_mean": round(cv_scores.mean(), 3),
        "cv_auc_std":  round(cv_scores.std(), 3),
        "holdout_auc": round(holdout_auc, 3) if holdout_auc is not None else None,
    }


def main():
    parser = argparse.ArgumentParser(description="Entraîne le filtre ML de signaux")
    parser.add_argument("--coin",      default=None,         help="Coin (BTC, SOL). Défaut = tous les PAIRS.")
    parser.add_argument("--days",      type=int, default=60, help="Historique en jours (défaut 60)")
    parser.add_argument("--lookahead", type=int, default=LOOKAHEAD_CANDLES,
                        help=f"Candles 15m à regarder en avant (défaut {LOOKAHEAD_CANDLES})")
    parser.add_argument("--target",    type=float, default=None,
                        help="Mouvement cible en pct (défaut : par coin — BTC 0.003 / SOL 0.006)")
    parser.add_argument("--model-dir", default=None, help="Répertoire de sortie des modèles")
    args = parser.parse_args()

    model_dir = args.model_dir or os.path.join(os.path.dirname(__file__), "models")

    # Détermination des coins à entraîner
    if args.coin:
        coins = [args.coin.upper()]
    else:
        # Extraire le coin (ex: "BTC" depuis "BTC/USDC:USDC")
        coins = [p.split("/")[0] for p in PAIRS]

    results = []
    for coin in coins:
        try:
            tgt = args.target if args.target is not None else target_for_coin(coin)
            r = train(coin, args.days, args.lookahead, tgt, model_dir)
            if r:
                results.append(r)
        except Exception as e:
            print(f"[ML] ❌ Erreur pour {coin}: {e}")

    if results:
        print(f"\n{'═' * 55}")
        print(f"  RÉSUMÉ ENTRAÎNEMENT")
        print(f"{'═' * 55}")
        for r in results:
            print(f"  {r['coin']:<4} | {r['samples']:>4} échantillons | "
                  f"Bons signaux: {r['label_1_pct']:>5.1f}% | "
                  f"AUC CV: {r['cv_auc_mean']:.3f} ± {r['cv_auc_std']:.3f}")
        print()
        if any(r["cv_auc_mean"] < 0.52 for r in results):
            print("  ⚠️  AUC proche de 0.5 = modèle quasi-aléatoire."
                  "\n     Accumulez plus de données (>500 signaux) avant utilisation en prod.")
        elif any(r["cv_auc_mean"] >= 0.58 for r in results):
            print("  ✅ AUC satisfaisante — le modèle apporte une valeur ajoutée.")
    else:
        print("\n[ML] Aucun modèle entraîné (données insuffisantes ou erreur).")


if __name__ == "__main__":
    main()
