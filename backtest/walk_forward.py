#!/usr/bin/env python3
"""
Walk-forward backtest (v8.6) — valide la robustesse de la stratégie sur des
fenêtres temporelles consécutives non chevauchantes (out-of-sample roulant).

Usage :
  python backtest/walk_forward.py --coin SOL --windows 6 --window-days 14
  python backtest/walk_forward.py --coin BTC --windows 4 --window-days 21 --export
"""

import io
import sys
import os
import argparse
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backtest.backtest import load_data, Backtester
from utils.walkforward import walk_forward_windows, summarize_walkforward


def run(coin, n_windows, window_days, equity):
    end_dt = datetime.now(timezone.utc)
    windows = walk_forward_windows(end_dt, n_windows, window_days)

    print(f"\n{'═'*64}")
    print(f"  WALK-FORWARD — {coin} | {n_windows} fenêtres × {window_days}j")
    print(f"{'═'*64}")
    header = f"  {'Fenêtre':<23} {'Trades':>6} {'Win%':>6} {'PnL%':>8} {'PF':>6} {'DD%':>6}"
    print(header)
    print("  " + "-" * 60)

    per_window = []
    for (f_dt, t_dt) in windows:
        label = f"{f_dt.strftime('%m-%d')}→{t_dt.strftime('%m-%d')}"
        try:
            buf = io.StringIO()
            old, sys.stdout = sys.stdout, buf
            data = load_data(coin, from_date=f_dt, to_date=t_dt)
            bt = Backtester(coin, *data, initial_equity=equity)
            res = bt.run()
            sys.stdout = old
        except Exception as e:
            sys.stdout = old
            print(f"  {label:<23} ERREUR: {e}")
            per_window.append(None)
            continue

        if not res:
            print(f"  {label:<23} {'—':>6} (pas de trade / données insuffisantes)")
            per_window.append({"total_pnl_pct": 0.0})
            continue

        per_window.append(res)
        print(f"  {label:<23} {res.get('total_trades',0):>6} "
              f"{res.get('win_rate_pct',0):>6.1f} {res.get('total_pnl_pct',0):>+8.2f} "
              f"{res.get('profit_factor',0):>6.2f} {res.get('max_drawdown_pct',0):>6.2f}")

    s = summarize_walkforward(per_window)
    print("\n  " + "-" * 60)
    print(f"  ROBUSTESSE : {s['profitable']}/{s['n_windows']} fenêtres profitables "
          f"({s['consistency_pct']}%)")
    print(f"  PnL moyen : {s['mean_pnl_pct']:+.2f}% ± {s['std_pnl_pct']:.2f}%  "
          f"| meilleure {s['best_pnl_pct']:+.2f}% | pire {s['worst_pnl_pct']:+.2f}%")
    verdict = ("✅ robuste (constante)" if s['consistency_pct'] >= 70
               else "⚠️ instable (dépend de la période)" if s['consistency_pct'] >= 40
               else "🚨 peu fiable")
    print(f"  Verdict : {verdict}")
    return s


def main():
    p = argparse.ArgumentParser(description="Walk-forward backtest")
    p.add_argument("--coin", default="SOL")
    p.add_argument("--windows", type=int, default=6)
    p.add_argument("--window-days", type=int, default=14)
    p.add_argument("--equity", type=float, default=1000.0)
    args = p.parse_args()
    run(args.coin, args.windows, args.window_days, args.equity)


if __name__ == "__main__":
    main()
