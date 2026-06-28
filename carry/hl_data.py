# -*- coding: utf-8 -*-
"""
carry/hl_data.py — Accès aux données publiques Hyperliquid (funding, prix, carnet).

API publique (pas de clé requise). Utilisé par le monitor read-only (Phase 1).
"""
import json
import urllib.request

INFO_URL = "https://api.hyperliquid.xyz/info"


def _post(body: dict, timeout: int = 20):
    req = urllib.request.Request(
        INFO_URL, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
    )
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


def current_funding(coin: str) -> float:
    """Funding HORAIRE instantané du perp `coin` (fraction)."""
    meta, ctxs = _post({"type": "metaAndAssetCtxs"})
    for u, c in zip(meta["universe"], ctxs):
        if u["name"] == coin and c.get("funding") is not None:
            return float(c["funding"])
    raise ValueError(f"perp introuvable: {coin}")


def funding_history(coin: str, start_ms: int) -> list:
    """Historique de funding horaire depuis start_ms (liste de taux, fraction)."""
    out, cur = [], start_ms
    while True:
        d = _post({"type": "fundingHistory", "coin": coin, "startTime": cur})
        if not d:
            break
        out += d
        if len(d) < 500:
            break
        cur = d[-1]["time"] + 1
    return [float(x["fundingRate"]) for x in out]


def perp_mid(coin: str) -> float:
    """Mid price du perp `coin`."""
    book = _post({"type": "l2Book", "coin": coin})
    lv = book["levels"]
    return (float(lv[0][0]["px"]) + float(lv[1][0]["px"])) / 2


def spot_pair_id(coin: str) -> str:
    """Identifiant l2Book de la paire spot `coin`/USDC (ex '@107' ou 'PURR/USDC')."""
    sm = _post({"type": "spotMetaAndAssetCtxs"})[0]
    tokens = {t["index"]: t["name"] for t in sm["tokens"]}
    for u in sm["universe"]:
        if tokens.get(u["tokens"][0]) == coin and tokens.get(u["tokens"][1]) == "USDC":
            return u["name"]
    raise ValueError(f"paire spot introuvable: {coin}/USDC")


def spot_mid(coin: str) -> float:
    """Mid price du spot `coin`/USDC."""
    book = _post({"type": "l2Book", "coin": spot_pair_id(coin)})
    lv = book["levels"]
    return (float(lv[0][0]["px"]) + float(lv[1][0]["px"])) / 2
