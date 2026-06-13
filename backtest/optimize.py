#!/usr/bin/env python3
"""
ÉTAPE 4 du backtest — recherche de paramètres.

Méthode anti-illusion :
  1. On balaie une grille de paramètres à fort levier.
  2. On optimise UNIQUEMENT sur l'IN-SAMPLE (70% premiers).
  3. On exige un nombre minimum de trades (sinon non significatif).
  4. On exige que ça marche sur BONK *ET* WIF (robustesse inter-token).
  5. On valide les meilleurs sur l'OUT-OF-SAMPLE (30% jamais vus).
  6. On ne garde que ce qui survit aux deux. Sinon : honnêtement, rien.

Usage : python3 backtest/optimize.py
"""

import os
import sys
import itertools
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config as C
from backtester import load_candles, precompute_signals, run

SYMBOLS     = sys.argv[1:] if len(sys.argv) > 1 else ["BONKUSDT", "WIFUSDT"]
SPLIT_RATIO = 0.70
MIN_TRADES  = 40          # seuil de significativité (combiné, in-sample)

# ─── Grille de paramètres à fort levier ──────────────────────────────
GRID = {
    "STALL_MINUTES":  [120, 240, 480, 1440, 10**9],   # 10**9 = stall désactivé
    "TP1_PCT":        [8, 12, 20, 30],
    "SL_PCT":         [-6, -10, -15],
    "ENTRY_ROC_15M":  [1.5, 3.0],
}
# Le reste reste aux valeurs par défaut de config.Meme.


def make_cfg(**overrides):
    base = {
        "TRADE_USDC": C.Meme.TRADE_USDC,
        "SL_PCT": C.Meme.SL_PCT, "TP1_PCT": C.Meme.TP1_PCT, "TP1_SELL": C.Meme.TP1_SELL,
        "TP2_PCT": C.Meme.TP2_PCT, "TP2_SELL": C.Meme.TP2_SELL,
        "TRAIL_ARM_PCT": C.Meme.TRAIL_ARM_PCT, "TRAIL_GIVEBACK_PCT": C.Meme.TRAIL_GIVEBACK_PCT,
        "STALL_MINUTES": C.Meme.STALL_MINUTES, "STALL_BAND_PCT": C.Meme.STALL_BAND_PCT,
        "MIN_VOL24_USD": C.Meme.MIN_VOL24_USD, "ENTRY_ROC_15M": C.Meme.ENTRY_ROC_15M,
        "ENTRY_REL_VOL": C.Meme.ENTRY_REL_VOL, "ENTRY_VWAP_TOL": C.Meme.ENTRY_VWAP_TOL,
        "COOLDOWN_HOURS": C.Meme.COOLDOWN_HOURS, "COOLDOWN_SL_HOURS": C.Meme.COOLDOWN_SL_HOURS,
    }
    base.update(overrides)
    # garde une échelle de TP cohérente : TP2 au-dessus de TP1
    base["TP2_PCT"] = base["TP1_PCT"] * 2
    return SimpleNamespace(**base)


def combined(data, cfg, lo, hi):
    """Somme BONK+WIF sur la tranche [lo, hi)."""
    tot_pnl = tot_tr = wins = 0
    for sym in SYMBOLS:
        candles, sigs, split = data[sym]
        m = run(sym, candles, cfg, signals=sigs, start=lo(split), end=hi(split))
        tot_pnl += m["pnl_usdc"]
        tot_tr  += m["trades"]
        wins    += m["trades"] * m["win_rate"] / 100
    wr = (wins / tot_tr * 100) if tot_tr else 0
    return tot_pnl, tot_tr, wr


def main():
    print("=== Chargement + pré-calcul des indicateurs (une fois) ===")
    data = {}
    for sym in SYMBOLS:
        candles = load_candles(sym)
        sigs = precompute_signals(candles)
        split = int(len(candles) * SPLIT_RATIO)
        data[sym] = (candles, sigs, split)
        print(f"  {sym}: {len(candles):,} bougies, split @ {split:,}")

    IN  = (lambda s: None, lambda s: s)   # [WARMUP, split)
    OUT = (lambda s: s,    lambda s: None) # [split, fin)

    # Baseline (config actuelle)
    b_in  = combined(data, make_cfg(), *IN)
    b_out = combined(data, make_cfg(), *OUT)
    print(f"\nBASELINE (config actuelle)")
    print(f"  IN : PnL {b_in[0]:+.2f} | trades {b_in[1]} | WR {b_in[2]:.1f}%")
    print(f"  OUT: PnL {b_out[0]:+.2f} | trades {b_out[1]} | WR {b_out[2]:.1f}%")

    # ── Recherche sur IN-SAMPLE ──
    keys = list(GRID)
    combos = list(itertools.product(*GRID.values()))
    print(f"\n=== Recherche : {len(combos)} combinaisons sur l'IN-SAMPLE ===")
    results = []
    for vals in combos:
        ov = dict(zip(keys, vals))
        cfg = make_cfg(**ov)
        pnl, tr, wr = combined(data, cfg, *IN)
        if tr >= MIN_TRADES:
            results.append((pnl, tr, wr, ov))
    results.sort(key=lambda r: r[0], reverse=True)

    print(f"\n--- TOP 12 IN-SAMPLE (>= {MIN_TRADES} trades) -> valides sur OUT-OF-SAMPLE ---")
    print(f"{'STALL':>8} {'TP1':>4} {'SL':>4} {'ROC':>4} | "
          f"{'IN PnL':>8} {'tr':>4} {'WR':>5} | {'OUT PnL':>8} {'tr':>4} {'WR':>5} | verdict")
    for pnl, tr, wr, ov in results[:12]:
        o_pnl, o_tr, o_wr = combined(data, make_cfg(**ov), *OUT)
        stall = "off" if ov["STALL_MINUTES"] >= 10**9 else ov["STALL_MINUTES"]
        survives = pnl > 0 and o_pnl > 0
        verdict = "SURVIT" if survives else ("out<=0" if pnl > 0 else "in<=0")
        print(f"{str(stall):>8} {ov['TP1_PCT']:>4} {ov['SL_PCT']:>4} {ov['ENTRY_ROC_15M']:>4} | "
              f"{pnl:>+8.2f} {tr:>4} {wr:>4.0f}% | {o_pnl:>+8.2f} {o_tr:>4} {o_wr:>4.0f}% | {verdict}")

    survivors = [(p, t, w, o) for p, t, w, o in results
                 if p > 0 and combined(data, make_cfg(**o), *OUT)[0] > 0]
    print(f"\n=== {len(survivors)} configuration(s) rentable(s) IN *et* OUT sur {len(results)} testées ===")
    if not survivors:
        print("Aucune. Réponse honnête : sur ces 2 tokens et cette période, aucun")
        print("réglage long-only ne tient la route hors-échantillon. Ne pas mettre")
        print("d'argent réel ; au mieux affiner l'entrée, au pire abandonner ce marché.")


if __name__ == "__main__":
    main()
