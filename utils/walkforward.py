"""
Walk-forward (v8.6) — helpers purs et testables.

Au lieu d'un unique backtest global (biaisable par une période atypique), on
évalue la stratégie sur des fenêtres consécutives NON chevauchantes et on mesure
la CONSTANCE de la performance (robustesse) d'une fenêtre à l'autre.

NB : l'optimisation par grille de paramètres (vrai walk-forward optimization)
est une extension future ; ici on fait la validation out-of-sample roulante.
"""

from datetime import timedelta


def walk_forward_windows(end_dt, n_windows, window_days):
    """Retourne n_windows fenêtres (from_dt, to_dt) consécutives non chevauchantes
    se terminant à end_dt, chacune de window_days jours."""
    start = end_dt - timedelta(days=n_windows * window_days)
    return [
        (start + timedelta(days=i * window_days),
         start + timedelta(days=(i + 1) * window_days))
        for i in range(n_windows)
    ]


def summarize_walkforward(per_window):
    """Agrège les métriques par fenêtre (chaque élément = dict avec total_pnl_pct).

    Retourne robustesse : nb fenêtres profitables, constance %, moyenne/écart-type,
    meilleure/pire fenêtre.
    """
    pnls = [w["total_pnl_pct"] for w in per_window
            if w and w.get("total_pnl_pct") is not None]
    n = len(pnls)
    if n == 0:
        return {"n_windows": 0, "profitable": 0, "consistency_pct": 0.0,
                "mean_pnl_pct": 0.0, "std_pnl_pct": 0.0,
                "best_pnl_pct": 0.0, "worst_pnl_pct": 0.0}

    profitable = sum(1 for p in pnls if p > 0)
    mean = sum(pnls) / n
    var = sum((p - mean) ** 2 for p in pnls) / n
    return {
        "n_windows": n,
        "profitable": profitable,
        "consistency_pct": round(profitable / n * 100, 1),
        "mean_pnl_pct": round(mean, 3),
        "std_pnl_pct": round(var ** 0.5, 3),
        "best_pnl_pct": round(max(pnls), 3),
        "worst_pnl_pct": round(min(pnls), 3),
    }
