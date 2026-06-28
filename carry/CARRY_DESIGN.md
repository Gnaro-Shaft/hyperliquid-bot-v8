# Bot de Carry Delta-Neutre — Hyperliquid (HYPE)

> Stratégie market-neutral : **long HYPE spot + short HYPE perp** → encaisser le funding,
> sans pari directionnel. Edge validé (28/06/2026) : funding HYPE +8,7%/an, positif 88%
> du temps, 2 jambes ultra-liquides (coût A/R ~0,01%). Net visé ~7-8%.

## 1. Principe

| Jambe | Position | Rôle |
|---|---|---|
| Spot HYPE | **LONG** (acheté en USDC) | exposition prix +1 |
| Perp HYPE | **SHORT** (notional égal) | exposition prix −1 + **encaisse le funding** |

Somme des expositions prix ≈ **0** (delta-neutre). Le P&L ≈ **funding encaissé** −
frais − dérive de basis. Quand le perp a un funding positif, le short le **reçoit**.

## 2. Risques spécifiques (et parades)

| Risque | Détail | Parade |
|---|---|---|
| **Liquidation du short perp** | si HYPE pompe fort, le short perd de la marge USDC AVANT qu'on rééquilibre (le gain spot est en HYPE, pas en marge) | **levier bas** (≤2-3×), buffer de marge large, surveillance + rebalance |
| **Funding qui s'inverse** | si le funding passe durablement négatif, le carry disparaît (le delta-neutre protège le PRIX, pas le funding) | **règle de sortie** : trailing funding < seuil → unwind |
| **Dérive du delta** | les notionnels spot/perp divergent quand le prix bouge | rebalance si \|delta\|/notional > seuil |
| **Basis / dislocation** | écart spot-perp anormal | monitor + alerte |
| **Concentration** | 1 seul token (HYPE) | accepté au début ; diversifier quand HL liste + de spot |

## 3. Architecture (réutilise l'infra existante)

```
carry/
  CARRY_DESIGN.md      ← ce document
  hl_data.py           ← accès API HL publique (funding, mids, books) — pas de clé requise
  carry_monitor.py     ← PHASE 1 : observation read-only (zéro risque)
  carry_executor.py    ← PHASE 3 : ordres réels spot+perp (à venir)
utils/
  carry_sim.py         ← maths pures, testées (funding, delta, pnl, règles)
```
- Config : constantes dans `config.py` (préfixe `CARRY_`).
- État/logs : collections Mongo `carry_state` (doc `_id="current"`) + `carry_log`.
- Connexion live (Phase 3) : `ccxt.hyperliquid` (mêmes clés que le bot directionnel).

## 4. Plan par phases (paper-first, comme le bot directionnel)

- **Phase 1 — Monitor read-only** ✅ (ce livrable) : suit en live le funding HYPE +
  une position SIMULÉE, accumule le carry théorique, applique les règles (exit/rebalance)
  en dry-run, logue. **Aucun ordre, aucun risque.** Prouve l'edge en réel.
- **Phase 2 — Paper exécution** : simule les fills des 2 jambes (réutilise le pattern
  `PaperTrader`), suit delta/marge/pnl net avec frais réalistes.
- **Phase 3 — Live petit capital** : ordres réels delta-neutre, levier bas, garde-fous
  (max notional, liquidation buffer, funding-exit), `CARRY_LIVE=false` par défaut.
- **Phase 4 — Diversification** : rescreen mensuel (`/tmp/hl_carry.py`), ajouter les
  coins HL avec perp+spot liquides à mesure que l'univers spot grandit.

## 5. Maths capital (Phase 3)

Pour un notional de carry `N` (par jambe) :
- Spot : `N` USDC achetés en HYPE
- Perp short : marge `N / levier` (levier bas → ex. `N/2`)
- **Capital total ≈ N + N/levier** (ex. levier 2 → ~1,5·N pour N de carry)
- Rendement sur capital total ≈ carry_annuel · N / (1,5·N) ≈ **carry × 0,67**

→ ~8% de carry sur le notional ≈ **~5-6% sur le capital total**. Market-neutral, faible DD.
