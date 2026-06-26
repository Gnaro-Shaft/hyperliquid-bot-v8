"""
Builders purs pour l'observabilité (v8.10) — testables sans I/O.

- build_decision_doc : un enregistrement du journal de décision (collection `decisions`)
- build_bot_status   : le doc heartbeat de l'état du bot (collection `bot_status`)

Ces documents sont écrits en MongoDB par le bot et lus par le GCN Dashboard.
"""


# Motifs de refus reconnus par le GCN Dashboard (ALLOWED_DECISION_MOTIFS).
_DASHBOARD_MOTIFS = {"risk", "circuit_breaker", "correlation", "exposure"}


def build_decision_doc(coin, sig, side, action, reason, price,
                       size_factor, now_ms, now_str):
    """Construit un enregistrement de décision (ouverture acceptée / refusée).

    action : "accepted" | "refused"
    reason : "ok" ou le motif du refus (gate concerné, ex "exposure: max positions")

    Le doc porte deux jeux de champs :
      - action / reason / timestamp : usage interne (audit, aggregate_decisions)
      - status / motif / created_at : contrat attendu par le GCN Dashboard
        (status ∈ accepted|refused, motif ∈ risk|circuit_breaker|correlation|exposure)
    """
    dbg = sig.get("debug", {}) or {}
    motif = (reason or "").split(":")[0].strip()
    motif = motif if motif in _DASHBOARD_MOTIFS else None
    return {
        "timestamp":    int(now_ms),
        "created_at":   int(now_ms),    # contrat dashboard (champ de tri)
        "datetime":     now_str,
        "coin":         coin,
        "action":       action,
        "status":       action,         # contrat dashboard
        "reason":       reason,
        "motif":        motif,          # contrat dashboard (catégorie du refus)
        "side":         side,
        "score":        sig.get("score"),
        "raw_score":    sig.get("raw_score"),
        "price":        float(price) if price is not None else None,
        "size_factor":  round(size_factor, 3) if size_factor is not None else None,
        "tp_pct":       sig.get("dynamic_tp"),
        "sl_pct":       sig.get("dynamic_sl"),
        "atr_pct":      dbg.get("atr_pct"),
        "funding_rate": dbg.get("funding_rate"),
        "ml_gate":      dbg.get("gate_ml"),
    }


def build_bot_status(metrics, risk_status, positions, kill_switch, now_ms, now_str):
    """Construit le doc heartbeat de l'état du bot (upsert _id='current').

    metrics : sortie de HealthMonitor.collect_metrics()
    risk_status : sortie de RiskManager.status()
    positions : dict {coin: position}
    kill_switch : bool
    """
    open_positions = []
    for coin, p in (positions or {}).items():
        if p.get("active"):
            open_positions.append({
                "coin":  coin,
                "side":  p.get("side"),
                "entry": p.get("entry"),
                "size":  p.get("size"),
            })

    return {
        "_id":                "current",
        "timestamp":          int(now_ms),
        "datetime":           now_str,
        "running":            True,
        "ws_alive":           metrics.get("ws_alive"),
        "mongo_ok":           metrics.get("mongo_ok"),
        "last_1m_age_s":      metrics.get("last_1m_age_s"),
        "last_15m_age_s":     metrics.get("last_15m_age_s"),
        "balance":            metrics.get("balance"),
        "pnl_today":          risk_status.get("pnl_today"),
        "consecutive_losses": risk_status.get("consecutive_losses"),
        "paused":             risk_status.get("paused"),
        "kill_switch":        kill_switch,
        "open_positions":     open_positions,
        "n_open_positions":   len(open_positions),
    }


def aggregate_trades(trades):
    """Agrège des trades clôturés réels (v8.10 — audit de performance).

    trades : liste de dicts {pnl, side, reason, timestamp(ms)}.
    Retourne totaux + ventilation par direction / raison de sortie / heure UTC.
    """
    closed = [t for t in trades if t.get("pnl") is not None]
    out = {"n": len(closed), "wins": 0, "losses": 0, "pnl": 0.0, "win_rate": 0.0,
           "by_direction": {}, "by_reason": {}, "by_hour": {}}
    if not closed:
        return out

    out["wins"] = sum(1 for t in closed if t["pnl"] > 0)
    out["losses"] = out["n"] - out["wins"]
    out["pnl"] = round(sum(t["pnl"] for t in closed), 4)
    out["win_rate"] = round(out["wins"] / out["n"] * 100, 1)

    for t in closed:
        pnl = t["pnl"]
        d = out["by_direction"].setdefault(t.get("side", "?"),
                                           {"n": 0, "pnl": 0.0, "wins": 0})
        d["n"] += 1; d["pnl"] += pnl; d["wins"] += 1 if pnl > 0 else 0

        r = out["by_reason"].setdefault(t.get("reason", "?"), {"n": 0, "pnl": 0.0})
        r["n"] += 1; r["pnl"] += pnl

        ts = t.get("timestamp")
        if ts:
            hour = int((int(ts) // 1000 // 3600) % 24)   # heure UTC
            h = out["by_hour"].setdefault(hour, {"n": 0, "pnl": 0.0})
            h["n"] += 1; h["pnl"] += pnl

    for grp in ("by_direction", "by_reason", "by_hour"):
        for k in out[grp]:
            out[grp][k]["pnl"] = round(out[grp][k]["pnl"], 4)
    return out


def aggregate_decisions(decisions):
    """Agrège le journal de décision : acceptées vs refusées, refus par motif."""
    refused_by_reason = {}
    for d in decisions:
        if d.get("action") == "refused":
            key = (d.get("reason") or "?").split(":")[0].strip()
            refused_by_reason[key] = refused_by_reason.get(key, 0) + 1
    return {
        "n":        len(decisions),
        "accepted": sum(1 for d in decisions if d.get("action") == "accepted"),
        "refused":  sum(1 for d in decisions if d.get("action") == "refused"),
        "refused_by_reason": refused_by_reason,
    }
