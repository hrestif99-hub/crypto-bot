#!/usr/bin/env python3
"""
ÉTAPE 5 — test des indicateurs recommandés par les forums.

On prend les indicateurs les plus cités (RSI, MACD, Bollinger, EMA cross) et on
les passe au MÊME protocole rigoureux : grille IN-sample → seuil de trades →
robustesse sur tout le panel → validation OUT-of-sample.

Le forum donne les candidats ; ce script donne la réponse.

Usage : python3 backtest/optimize_indicators.py [SYMBOLES...]
"""

import os
import sys
import itertools
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config as C
from backtester import (load_candles, precompute_signals, run,
                        _entry_rsi, _entry_macd, _entry_bb, _entry_ema)

SYMBOLS     = sys.argv[1:] if len(sys.argv) > 1 else ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
SPLIT_RATIO = 0.70
MIN_TRADES  = 40

# Indicateur : (nom, fonction d'entrée, grille de params propres à l'entrée)
INDICATORS = [
    ("RSI <seuil",   _entry_rsi,  {"RSI_BUY": [25, 30, 35]}),
    ("MACD cross",   _entry_macd, {}),
    ("Bollinger",    _entry_bb,   {"BB_K": [1.5, 2.0, 2.5]}),
    ("EMA 9/21",     _entry_ema,  {"EMA_FAST": [9],  "EMA_SLOW": [21]}),
    ("EMA 50/200",   _entry_ema,  {"EMA_FAST": [50], "EMA_SLOW": [200]}),
]
EXIT_GRID = {"STALL_MINUTES": [240, 1440], "TP1_PCT": [8, 15], "SL_PCT": [-8, -15]}


def make_cfg(**ov):
    base = {
        "TRADE_USDC": C.Meme.TRADE_USDC, "MIN_VOL24_USD": C.Meme.MIN_VOL24_USD,
        "SL_PCT": C.Meme.SL_PCT, "TP1_PCT": C.Meme.TP1_PCT, "TP1_SELL": C.Meme.TP1_SELL,
        "TP2_PCT": C.Meme.TP2_PCT, "TP2_SELL": C.Meme.TP2_SELL,
        "TRAIL_ARM_PCT": C.Meme.TRAIL_ARM_PCT, "TRAIL_GIVEBACK_PCT": C.Meme.TRAIL_GIVEBACK_PCT,
        "STALL_MINUTES": C.Meme.STALL_MINUTES, "STALL_BAND_PCT": C.Meme.STALL_BAND_PCT,
        "COOLDOWN_HOURS": C.Meme.COOLDOWN_HOURS, "COOLDOWN_SL_HOURS": C.Meme.COOLDOWN_SL_HOURS,
        "RSI_BUY": 30, "BB_K": 2.0, "EMA_FAST": 9, "EMA_SLOW": 21,
    }
    base.update(ov)
    base["TP2_PCT"] = base["TP1_PCT"] * 2
    return SimpleNamespace(**base)


def combined(data, cfg, entry_fn, lo, hi):
    tot_pnl = tot_tr = wins = 0
    for sym in data:
        candles, sigs, split = data[sym]
        m = run(sym, candles, cfg, signals=sigs, start=lo(split), end=hi(split), entry_fn=entry_fn)
        tot_pnl += m["pnl_usdc"]; tot_tr += m["trades"]; wins += m["trades"] * m["win_rate"] / 100
    return tot_pnl, tot_tr, (wins / tot_tr * 100 if tot_tr else 0)


def search(data, name, entry_fn, entry_grid):
    keys = list(entry_grid) + list(EXIT_GRID)
    space = [entry_grid[k] for k in entry_grid] + [EXIT_GRID[k] for k in EXIT_GRID]
    IN  = (lambda s: None, lambda s: s)
    OUT = (lambda s: s,    lambda s: None)
    best = None
    survivors = 0
    tested = 0
    for vals in itertools.product(*space):
        ov = dict(zip(keys, vals))
        pnl, tr, wr = combined(data, make_cfg(**ov), entry_fn, *IN)
        if tr < MIN_TRADES:
            continue
        tested += 1
        o_pnl, o_tr, o_wr = combined(data, make_cfg(**ov), entry_fn, *OUT)
        if pnl > 0 and o_pnl > 0:
            survivors += 1
        if best is None or pnl > best[0]:
            best = (pnl, tr, wr, o_pnl, o_tr, o_wr, ov)
    return best, survivors, tested


def main():
    print("=== INDICATEURS POPULAIRES — chargement + pre-calcul ===")
    data = {}
    for sym in SYMBOLS:
        try:
            candles = load_candles(sym)
        except FileNotFoundError:
            print(f"  {sym}: absent"); continue
        data[sym] = (candles, precompute_signals(candles), int(len(candles) * SPLIT_RATIO))
    print(f"  {len(data)} symboles charges\n")

    print(f"{'Indicateur':<12} | {'meilleure config IN':<34} | {'IN PnL':>8} {'WR':>4} | "
          f"{'OUT PnL':>8} {'WR':>4} | survivants")
    print("-" * 105)
    total_surv = 0
    for name, fn, grid in INDICATORS:
        best, surv, tested = search(data, name, fn, grid)
        total_surv += surv
        if best is None:
            print(f"{name:<12} | (pas assez de trades)")
            continue
        pnl, tr, wr, o_pnl, o_tr, o_wr, ov = best
        cfgs = " ".join(f"{k}={v}" for k, v in ov.items())
        print(f"{name:<12} | {cfgs:<34} | {pnl:>+8.2f} {wr:>3.0f}% | "
              f"{o_pnl:>+8.2f} {o_wr:>3.0f}% | {surv}/{tested}")

    print("-" * 105)
    print(f"\n=== {total_surv} configuration(s) rentable(s) IN *et* OUT, tous indicateurs confondus ===")
    if total_surv == 0:
        print("Aucune. Les indicateurs des forums ne battent pas le hasard apres frais,")
        print("hors-echantillon, sur ce panel. Confirme : pas d'edge dans la TA prix au 5min.")


if __name__ == "__main__":
    main()
