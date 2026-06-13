#!/usr/bin/env python3
"""
ÉTAPE 3 du backtest — découpage in-sample / out-of-sample.

Le garde-fou anti-overfitting : on découpe l'historique en deux dans l'ordre
chronologique.
  • IN-SAMPLE  (70% premiers)  : la zone où l'on aura le droit d'optimiser (étape 4).
  • OUT-SAMPLE (30% derniers)  : zone JAMAIS touchée, sert uniquement à valider.

Un réglage n'est crédible que s'il marche AUSSI sur l'out-of-sample.
S'il brille en in-sample et s'effondre en out-of-sample → c'est de l'illusion
(curve-fitting), on le jette.

Usage :
    python3 backtest/evaluate.py                  # config actuelle, IN vs OUT
    python3 backtest/evaluate.py BONKUSDT
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config as C
from backtester import load_candles, run

SPLIT_RATIO = 0.70   # 70% optimisation / 30% validation


def split(candles: list):
    n = int(len(candles) * SPLIT_RATIO)
    return candles[:n], candles[n:]


def line(label: str, m: dict) -> str:
    return (
        f"  {label:<10} trades {m['trades']:>3} | winrate {m['win_rate']:>5.1f}% | "
        f"PnL {m['pnl_usdc']:>+7.2f} | moy {m['avg_trade']:>+5.2f} | "
        f"PF {m['profit_factor']:>4.2f} | maxDD {m['max_dd']:>+6.2f} | "
        f"expo {m['exposure']:>4.1f}% | {m['by_reason']}"
    )


def evaluate(symbol: str, cfg) -> tuple:
    candles = load_candles(symbol)
    ins, oos = split(candles)
    return run(symbol, ins, cfg), run(symbol, oos, cfg)


def main():
    symbols = [sys.argv[1]] if len(sys.argv) > 1 else ["BONKUSDT", "WIFUSDT"]
    cfg = C.Meme
    print("=== IN-SAMPLE (70%) vs OUT-OF-SAMPLE (30%) — config actuelle ===")
    print(f"SL {cfg.SL_PCT}% TP1 +{cfg.TP1_PCT}% TP2 +{cfg.TP2_PCT}% "
          f"stall {cfg.STALL_MINUTES}min | ROC>={cfg.ENTRY_ROC_15M}% VolRel>={cfg.ENTRY_REL_VOL}x")
    print("-" * 120)
    for symbol in symbols:
        try:
            candles = load_candles(symbol)
        except FileNotFoundError:
            print(f"{symbol}: pas de données")
            continue
        d0 = candles[0][0]; d1 = candles[-1][0]
        ins, oos = split(candles)
        print(f"{symbol}  ({len(candles):,} bougies)")
        print(line("IN", run(symbol, ins, cfg)))
        print(line("OUT", run(symbol, oos, cfg)))
        print()


if __name__ == "__main__":
    main()
