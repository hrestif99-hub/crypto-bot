#!/usr/bin/env python3
"""
ÉTAPE 4bis — recherche de paramètres pour l'entrée MEAN-REVERSION.

Hypothèse différente du momentum : sur des tokens bruités, un creux marqué
sous le VWAP a tendance à rebondir. On achète la faiblesse, pas la force.

Même méthode anti-illusion que optimize.py : grille → IN-sample → seuil de
trades → robustesse BONK *et* WIF → validation OUT-of-sample.

Usage : python3 backtest/optimize_meanrev.py
"""

import os
import sys
import itertools
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config as C
from backtester import load_candles, precompute_signals, run, _entry_meanrev

SYMBOLS     = ["BONKUSDT", "WIFUSDT"]
SPLIT_RATIO = 0.70
MIN_TRADES  = 40

GRID = {
    "MR_DIP_PCT":    [2, 4, 6, 8],      # prix au moins X% sous le VWAP
    "MR_ROC_DROP":   [0, 2, 4],         # chute récente >= X% (0 = pas d'exigence)
    "STALL_MINUTES": [240, 1440],
    "TP1_PCT":       [8, 15],
    "SL_PCT":        [-8, -15],
}


def make_cfg(**ov):
    base = {
        "TRADE_USDC": C.Meme.TRADE_USDC,
        "SL_PCT": C.Meme.SL_PCT, "TP1_PCT": C.Meme.TP1_PCT, "TP1_SELL": C.Meme.TP1_SELL,
        "TP2_PCT": C.Meme.TP2_PCT, "TP2_SELL": C.Meme.TP2_SELL,
        "TRAIL_ARM_PCT": C.Meme.TRAIL_ARM_PCT, "TRAIL_GIVEBACK_PCT": C.Meme.TRAIL_GIVEBACK_PCT,
        "STALL_MINUTES": C.Meme.STALL_MINUTES, "STALL_BAND_PCT": C.Meme.STALL_BAND_PCT,
        "MIN_VOL24_USD": C.Meme.MIN_VOL24_USD,
        "COOLDOWN_HOURS": C.Meme.COOLDOWN_HOURS, "COOLDOWN_SL_HOURS": C.Meme.COOLDOWN_SL_HOURS,
        "MR_DIP_PCT": 4.0, "MR_ROC_DROP": 2.0,
    }
    base.update(ov)
    base["TP2_PCT"] = base["TP1_PCT"] * 2
    return SimpleNamespace(**base)


def combined(data, cfg, lo, hi):
    tot_pnl = tot_tr = wins = 0
    for sym in SYMBOLS:
        candles, sigs, split = data[sym]
        m = run(sym, candles, cfg, signals=sigs, start=lo(split), end=hi(split),
                entry_fn=_entry_meanrev)
        tot_pnl += m["pnl_usdc"]; tot_tr += m["trades"]; wins += m["trades"] * m["win_rate"] / 100
    return tot_pnl, tot_tr, (wins / tot_tr * 100 if tot_tr else 0)


def main():
    print("=== MEAN-REVERSION — chargement + pre-calcul ===")
    data = {}
    for sym in SYMBOLS:
        candles = load_candles(sym)
        data[sym] = (candles, precompute_signals(candles), int(len(candles) * SPLIT_RATIO))
        print(f"  {sym}: {len(candles):,} bougies")

    IN  = (lambda s: None, lambda s: s)
    OUT = (lambda s: s,    lambda s: None)

    keys = list(GRID)
    combos = list(itertools.product(*GRID.values()))
    print(f"\n=== {len(combos)} combinaisons sur l'IN-SAMPLE ===")
    results = []
    for vals in combos:
        ov = dict(zip(keys, vals))
        pnl, tr, wr = combined(data, make_cfg(**ov), *IN)
        if tr >= MIN_TRADES:
            results.append((pnl, tr, wr, ov))
    results.sort(key=lambda r: r[0], reverse=True)

    print(f"\n--- TOP 12 IN-SAMPLE (>= {MIN_TRADES} trades) -> valides sur OUT ---")
    print(f"{'DIP':>4} {'DROP':>4} {'STALL':>6} {'TP1':>4} {'SL':>4} | "
          f"{'IN PnL':>8} {'tr':>4} {'WR':>4} | {'OUT PnL':>8} {'tr':>4} {'WR':>4} | verdict")
    for pnl, tr, wr, ov in results[:12]:
        o_pnl, o_tr, o_wr = combined(data, make_cfg(**ov), *OUT)
        stall = "off" if ov["STALL_MINUTES"] >= 10**9 else ov["STALL_MINUTES"]
        verdict = "SURVIT" if (pnl > 0 and o_pnl > 0) else ("out<=0" if pnl > 0 else "in<=0")
        print(f"{ov['MR_DIP_PCT']:>4} {ov['MR_ROC_DROP']:>4} {str(stall):>6} {ov['TP1_PCT']:>4} "
              f"{ov['SL_PCT']:>4} | {pnl:>+8.2f} {tr:>4} {wr:>3.0f}% | "
              f"{o_pnl:>+8.2f} {o_tr:>4} {o_wr:>3.0f}% | {verdict}")

    survivors = [r for r in results if r[0] > 0 and combined(data, make_cfg(**r[3]), *OUT)[0] > 0]
    print(f"\n=== {len(survivors)} config(s) rentable(s) IN *et* OUT sur {len(results)} testees ===")
    if not survivors:
        print("Aucune. Le mean-reversion ne tient pas non plus hors-echantillon.")


if __name__ == "__main__":
    main()
