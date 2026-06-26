"""
Modèle d'exécution réaliste (v8.6) — fonction pure et testable.

Applique un coût adverse (slippage + demi-spread) au prix d'exécution :
- à l'ENTRÉE : on achète plus cher (long) / on vend moins cher (short)
- à la SORTIE : on vend moins cher (long) / on rachète plus cher (short)

Toujours défavorable au trader → résultats de backtest moins optimistes.
"""


def execution_price(mid, position_side, is_entry, slippage_pct, spread_pct):
    """Prix d'exécution réaliste à partir du prix milieu `mid`.

    position_side : "buy" (long) ou "sell" (short)
    is_entry      : True = ouverture, False = clôture
    """
    adverse = slippage_pct + spread_pct / 2.0
    if position_side == "buy":
        # long : entrée = achat (plus cher), sortie = vente (moins cher)
        factor = (1 + adverse) if is_entry else (1 - adverse)
    else:
        # short : entrée = vente (moins cher), sortie = rachat (plus cher)
        factor = (1 - adverse) if is_entry else (1 + adverse)
    return mid * factor
