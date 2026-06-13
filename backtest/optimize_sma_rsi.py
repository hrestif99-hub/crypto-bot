#!/usr/bin/env python3
"""
Test de la stratégie "SMA court x SMA long + RSI" (tutos court-terme).
+ DÉMONSTRATION mois-par-mois : pourquoi juger sur une petite fenêtre trompe.

Usage : python3 backtest/optimize_sma_rsi.py [SYMBOLES...]
"""

import os
import sys
import itertools
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config as C
from backtester import load_candles, precompute_signals, run, _entry_sma_rsi

SYMBOLS     = sys.argv[1:] if len(sys.argv) > 1 else ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
SPLIT_RATIO = 0.70
MIN_TRADES  = 40
MONTH_CANDLES = 288 * 30        # ~30 jours de bougies 5 min

GRID = {
    "SMA_FAST": [3, 6], "SMA_SLOW": [12, 24, 48], "RSI_MIN": [40, 50],
    "STALL_MINUTES": [240, 1440], "TP1_PCT": [8, 15], "SL_PCT": [-8, -15],
}


def make_cfg(**ov):
    base = {
        "TRADE_USDC": C.Meme.TRADE_USDC, "MIN_VOL24_USD": C.Meme.MIN_VOL24_USD,
        "SL_PCT": C.Meme.SL_PCT, "TP1_PCT": C.Meme.TP1_PCT, "TP1_SELL": C.Meme.TP1_SELL,
        "TP2_PCT": C.Meme.TP2_PCT, "TP2_SELL": C.Meme.TP2_SELL,
        "TRAIL_ARM_PCT": C.Meme.TRAIL_ARM_PCT, "TRAIL_GIVEBACK_PCT": C.Meme.TRAIL_GIVEBACK_PCT,
        "STALL_MINUTES": C.Meme.STALL_MINUTES, "STALL_BAND_PCT": C.Meme.STALL_BAND_PCT,
        "COOLDOWN_HOURS": C.Meme.COOLDOWN_HOURS, "COOLDOWN_SL_HOURS": C.Meme.COOLDOWN_SL_HOURS,
        "SMA_FAST": 6, "SMA_SLOW": 24, "RSI_MIN": 50,
    }
    base.update(ov)
    base["TP2_PCT"] = base["TP1_PCT"] * 2
    return SimpleNamespace(**base)


def window(data, cfg, lo, hi):
    """PnL/trades cumulés sur le panel pour la tranche d'indices [lo, hi)."""
    pnl = tr = wins = 0
    for sym in data:
        candles, sigs, split = data[sym]
        a = lo(split) if callable(lo) else lo
        b = hi(split) if callable(hi) else hi
        m = run(sym, candles, cfg, signals=sigs, start=a, end=b, entry_fn=_entry_sma_rsi)
        pnl += m["pnl_usdc"]; tr += m["trades"]; wins += m["trades"] * m["win_rate"] / 100
    return pnl, tr, (wins / tr * 100 if tr else 0)


def main():
    print("=== SMA court x SMA long + RSI ===")
    data = {}
    for sym in SYMBOLS:
        try:
            candles = load_candles(sym)
        except FileNotFoundError:
            continue
        data[sym] = (candles, precompute_signals(candles), int(len(candles) * SPLIT_RATIO))
    print(f"{len(data)} symboles charges\n")

    IN  = (lambda s: None, lambda s: s)
    OUT = (lambda s: s,    lambda s: None)

    keys = list(GRID)
    best = None
    survivors = tested = 0
    for vals in itertools.product(*GRID.values()):
        ov = dict(zip(keys, vals))
        pnl, tr, wr = window(data, make_cfg(**ov), *IN)
        if tr < MIN_TRADES:
            continue
        tested += 1
        o_pnl = window(data, make_cfg(**ov), *OUT)[0]
        if pnl > 0 and o_pnl > 0:
            survivors += 1
        if best is None or pnl > best[0]:
            best = (pnl, tr, wr, o_pnl, ov)

    pnl, tr, wr, o_pnl, ov = best
    print(f"Meilleure config IN : {ov}")
    print(f"  IN  PnL {pnl:+.2f} | trades {tr} | WR {wr:.0f}%")
    print(f"  OUT PnL {o_pnl:+.2f}")
    print(f"  Survivants IN+OUT : {survivors}/{tested}\n")

    # ── Démonstration : la MÊME meilleure config, mois par mois ──
    n = min(len(c) for c, _, _ in data.values())
    print("=== La MEME config, mois par mois (pourquoi une petite fenetre ment) ===")
    print(f"{'mois':>5} | {'PnL':>8} | {'trades':>6} | {'WR':>4}")
    cfg = make_cfg(**ov)
    for m in range(n // MONTH_CANDLES):
        lo, hi = m * MONTH_CANDLES, (m + 1) * MONTH_CANDLES
        p, t, w = window(data, cfg, lo, hi)
        flag = "  <- VERT (par chance ?)" if p > 0 else ""
        print(f"{m+1:>5} | {p:>+8.2f} | {t:>6} | {w:>3.0f}%{flag}")


if __name__ == "__main__":
    main()
