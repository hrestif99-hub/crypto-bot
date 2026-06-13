#!/usr/bin/env python3
"""
ÉTAPE 2 du backtest — moteur de rejeu.

Rejoue la stratégie meme_scalper sur l'historique (CSV de fetch_data.py),
bougie par bougie, avec frais + slippage. Produit un bilan honnête.

Principes de fidélité :
  • La DÉCISION de sortie vient de engine.decide_exit (EXACTEMENT la logique
    du bot live — aucune duplication).
  • Les indicateurs d'entrée viennent de common.roc_15m / rel_volume / vwap
    (EXACTEMENT le calcul du bot live).
  • Intra-bougie : on évalue les prix dans l'ordre open→low→high→close
    (le pire d'abord) → hypothèse pessimiste, donc honnête.
  • Frais + slippage appliqués à chaque achat ET chaque vente.

Usage :
    python3 backtest/backtester.py                 # baseline sur la config actuelle
    python3 backtest/backtester.py BONKUSDT        # un seul symbole
"""

import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config as C
from common import roc_15m, rel_volume, vwap
from engine import decide_exit

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
INTERVAL = "5m"
CANDLE_MIN = 5                     # minutes par bougie
WARMUP = 48                        # bougies nécessaires aux indicateurs

# Coûts par côté (achat / vente). Conservateur pour tokens liquides type BONK/WIF.
BUY_COST_PCT  = 0.6
SELL_COST_PCT = 0.6


def load_candles(symbol: str) -> list:
    """Retourne des lignes [time, open, high, low, close, volume] (floats),
    dans l'ordre d'index attendu par les indicateurs de common.py."""
    path = os.path.join(DATA_DIR, f"{symbol}_{INTERVAL}.csv")
    rows = []
    with open(path) as f:
        for r in csv.DictReader(f):
            rows.append([
                int(r["open_time"]), float(r["open"]), float(r["high"]),
                float(r["low"]), float(r["close"]), float(r["volume"]),
            ])
    return rows


def entry_signal(window: list, cfg) -> bool:
    """Réplique check_entry de meme_scalper : volume + ROC + VWAP."""
    # Volume 24h approximatif (USD) sur les 288 dernières bougies
    last = window[-288:]
    vol24 = sum(c[5] * c[4] for c in last)
    if vol24 < cfg.MIN_VOL24_USD:
        return False
    if roc_15m(window) < cfg.ENTRY_ROC_15M:
        return False
    if rel_volume(window) < cfg.ENTRY_REL_VOL:
        return False
    vw = vwap(window)
    price = window[-1][4]
    if vw > 0 and price < vw * cfg.ENTRY_VWAP_TOL:
        return False
    return True


# ─── Séries d'indicateurs (O(n), calculées une fois) ─────────────────
def _ema_series(values, period):
    out = [None] * len(values)
    if len(values) < period:
        return out
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    k = 2 / (period + 1)
    prev = seed
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def _ema_of_series(series, period):
    """EMA d'une série contenant des None en tête (pour le signal MACD)."""
    out = [None] * len(series)
    defined = [(i, v) for i, v in enumerate(series) if v is not None]
    if len(defined) < period:
        return out
    seed = sum(v for _, v in defined[:period]) / period
    out[defined[period - 1][0]] = seed
    k = 2 / (period + 1)
    prev = seed
    for j in range(period, len(defined)):
        i, v = defined[j]
        prev = v * k + prev * (1 - k)
        out[i] = prev
    return out


def _rsi_series(values, period=14):
    out = [None] * len(values)
    if len(values) < period + 1:
        return out
    g = sum(max(values[i] - values[i - 1], 0) for i in range(1, period + 1)) / period
    l = sum(max(values[i - 1] - values[i], 0) for i in range(1, period + 1)) / period
    out[period] = 100.0 if l == 0 else 100 - 100 / (1 + g / l)
    for i in range(period + 1, len(values)):
        d = values[i] - values[i - 1]
        g = (g * (period - 1) + max(d, 0)) / period
        l = (l * (period - 1) + max(-d, 0)) / period
        out[i] = 100.0 if l == 0 else 100 - 100 / (1 + g / l)
    return out


def _sma_std_series(values, period=20):
    sma = [None] * len(values)
    std = [None] * len(values)
    s = sq = 0.0
    for i, v in enumerate(values):
        s += v; sq += v * v
        if i >= period:
            ov = values[i - period]; s -= ov; sq -= ov * ov
        if i >= period - 1:
            m = s / period
            sma[i] = m
            std[i] = max(0.0, sq / period - m * m) ** 0.5
    return sma, std


def precompute_signals(candles: list) -> list:
    """Pré-calcule TOUS les indicateurs par bougie — INDÉPENDANT des params.
    Renvoie un dict par bougie (None tant que pas assez d'historique).
    Calculé une fois par token, réutilisé par toutes les configs testées."""
    n = len(candles)
    closes = [c[4] for c in candles]
    ema12 = _ema_series(closes, 12)
    ema26 = _ema_series(closes, 26)
    macd  = [(ema12[i] - ema26[i]) if ema12[i] is not None and ema26[i] is not None else None
             for i in range(n)]
    macd_sig = _ema_of_series(macd, 9)
    rsi = _rsi_series(closes, 14)
    sma20, std20 = _sma_std_series(closes, 20)
    emap = {p: _ema_series(closes, p) for p in (9, 21, 50, 200)}

    sigs = [None] * n
    for i in range(288, n):                 # 288 = warmup complet (vol24 sur 24h)
        w = candles[max(0, i - 287):i + 1]
        sigs[i] = {
            "close":  closes[i],
            "roc":    roc_15m(w), "relvol": rel_volume(w), "vwap": vwap(w),
            "vol24":  sum(c[5] * c[4] for c in w),
            "rsi":    rsi[i],
            "macd":   macd[i], "macd_sig": macd_sig[i],
            "macd_prev": macd[i - 1], "sig_prev": macd_sig[i - 1],
            "bb_mid": sma20[i], "bb_std": std20[i],
            "ema":      {p: emap[p][i]     for p in emap},
            "ema_prev": {p: emap[p][i - 1] for p in emap},
        }
    return sigs


def _entry_ok(sig, close, cfg) -> bool:
    """Entrée MOMENTUM (stratégie historique) : on achète la force."""
    if sig["vol24"] < cfg.MIN_VOL24_USD:                       return False
    if sig["roc"] < cfg.ENTRY_ROC_15M:                         return False
    if sig["relvol"] < cfg.ENTRY_REL_VOL:                      return False
    if sig["vwap"] > 0 and close < sig["vwap"] * cfg.ENTRY_VWAP_TOL: return False
    return True


def _entry_meanrev(sig, close, cfg) -> bool:
    """MEAN-REVERSION : prix nettement sous le VWAP + chute récente."""
    if sig["vol24"] < cfg.MIN_VOL24_USD:                  return False
    if sig["vwap"] <= 0:                                  return False
    if close > sig["vwap"] * (1 - cfg.MR_DIP_PCT / 100):  return False
    if sig["roc"] > -cfg.MR_ROC_DROP:                     return False
    return True


def _entry_rsi(sig, close, cfg) -> bool:
    """RSI : achat en zone de survente (RSI < seuil)."""
    if sig["vol24"] < cfg.MIN_VOL24_USD: return False
    if sig["rsi"] is None:               return False
    return sig["rsi"] < cfg.RSI_BUY


def _entry_macd(sig, close, cfg) -> bool:
    """MACD : croisement haussier (MACD passe au-dessus de sa ligne de signal)."""
    if sig["vol24"] < cfg.MIN_VOL24_USD: return False
    if None in (sig["macd"], sig["macd_sig"], sig["macd_prev"], sig["sig_prev"]): return False
    return sig["macd_prev"] <= sig["sig_prev"] and sig["macd"] > sig["macd_sig"]


def _entry_bb(sig, close, cfg) -> bool:
    """Bollinger : achat sous la bande inférieure (retour à la moyenne)."""
    if sig["vol24"] < cfg.MIN_VOL24_USD:      return False
    if sig["bb_mid"] is None:                 return False
    return close < sig["bb_mid"] - cfg.BB_K * sig["bb_std"]


def _entry_ema(sig, close, cfg) -> bool:
    """EMA cross : croisement haussier (EMA rapide passe au-dessus de l'EMA lente)."""
    if sig["vol24"] < cfg.MIN_VOL24_USD: return False
    f, s = cfg.EMA_FAST, cfg.EMA_SLOW
    ef, es   = sig["ema"][f], sig["ema"][s]
    efp, esp = sig["ema_prev"][f], sig["ema_prev"][s]
    if None in (ef, es, efp, esp): return False
    return efp <= esp and ef > es


def run(symbol: str, candles: list, cfg, signals=None, start=None, end=None, entry_fn=None) -> dict:
    """Backtest single-symbol, une position à la fois. Retourne les métriques.
    signals : cache de precompute_signals (sinon recalculé à la volée).
    start/end : bornes d'indices (pour découper IN-sample / OUT-of-sample).
    entry_fn : fonction d'entrée (défaut _entry_ok momentum ; _entry_meanrev pour le dip)."""
    start = WARMUP if start is None else max(start, WARMUP)
    end   = len(candles) if end is None else end
    ef    = entry_fn or _entry_ok
    pos = None              # {entry, qty, cost, peak, tp1, tp2, entry_i}
    cooldown_until = -1
    trades = []             # un trade = une entrée→clôture (PnL agrégé)
    cur_trade = None
    realized = 0.0
    peak_equity = 0.0
    max_dd = 0.0
    candles_in_market = 0

    def fill_buy(price):
        return price * (1 + BUY_COST_PCT / 100)

    def fill_sell(price):
        return price * (1 - SELL_COST_PCT / 100)

    for i in range(start, end):
        o, h, l, cl = candles[i][1], candles[i][2], candles[i][3], candles[i][4]

        # ── Gestion de la position ouverte (intra-bougie pessimiste) ──
        if pos:
            candles_in_market += 1
            for p in (o, l, h, cl):
                if not pos:
                    break
                pos["peak"] = max(pos["peak"], p)
                age = (i - pos["entry_i"]) * CANDLE_MIN
                d = decide_exit(pos["entry"], pos["peak"], p, age,
                                pos["tp1"], pos["tp2"], cfg)
                if not d:
                    continue
                reason, frac, closes = d
                sell_qty = pos["qty"] * frac
                proceeds = sell_qty * fill_sell(p)
                if closes:
                    pnl = proceeds - pos["cost"]
                    realized += pnl
                    cur_trade["pnl"] += pnl
                    cur_trade["exit_reason"] = reason
                    trades.append(cur_trade)
                    cur_trade = None
                    pos = None
                    cooldown_until = i + int(
                        (cfg.COOLDOWN_SL_HOURS if reason == "SL" else cfg.COOLDOWN_HOURS)
                        * 60 / CANDLE_MIN
                    )
                else:
                    gain = proceeds - pos["cost"] * frac
                    realized += gain
                    cur_trade["pnl"] += gain
                    pos["tp1" if reason == "TP1" else "tp2"] = True
                    pos["qty"]  -= sell_qty
                    pos["cost"] *= (1 - frac)
                    if reason == "TP1":
                        pos["peak"] = p
                # drawdown sur l'équity réalisé
                peak_equity = max(peak_equity, realized)
                max_dd = min(max_dd, realized - peak_equity)

        # ── Entrée ──
        elif i > cooldown_until and (
            ef(signals[i], cl, cfg) if signals is not None and signals[i]
            else (signals is None and entry_signal(candles[max(0, i - 287):i + 1], cfg))
        ):
            entry_px = fill_buy(cl)
            qty = cfg.TRADE_USDC / entry_px
            pos = {"entry": cl, "qty": qty, "cost": cfg.TRADE_USDC,
                   "peak": cl, "tp1": False, "tp2": False, "entry_i": i}
            cur_trade = {"entry_i": i, "entry_px": cl, "pnl": 0.0, "exit_reason": None}

    wins   = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gross_w = sum(t["pnl"] for t in wins)
    gross_l = -sum(t["pnl"] for t in losses)
    n = len(trades)
    return {
        "symbol":     symbol,
        "trades":     n,
        "win_rate":   (len(wins) / n * 100) if n else 0,
        "pnl_usdc":   realized,
        "pnl_pct":    realized / cfg.TRADE_USDC * 100,   # en R (multiples de la mise)
        "avg_trade":  (realized / n) if n else 0,
        "profit_factor": (gross_w / gross_l) if gross_l > 0 else float("inf"),
        "max_dd":     max_dd,
        "exposure":   candles_in_market / max(1, end - start) * 100,
        "by_reason":  _count_reasons(trades),
    }


def _count_reasons(trades):
    c = {}
    for t in trades:
        c[t["exit_reason"]] = c.get(t["exit_reason"], 0) + 1
    return c


def fmt(m: dict) -> str:
    return (
        f"{m['symbol']:<10} | trades {m['trades']:>3} | "
        f"winrate {m['win_rate']:>5.1f}% | "
        f"PnL {m['pnl_usdc']:>+7.2f} USDC ({m['pnl_pct']:>+6.1f}% mise) | "
        f"moy/trade {m['avg_trade']:>+5.2f} | "
        f"PF {m['profit_factor']:>4.2f} | "
        f"maxDD {m['max_dd']:>+6.2f} | "
        f"expo {m['exposure']:>4.1f}% | {m['by_reason']}"
    )


def main():
    symbols = [sys.argv[1]] if len(sys.argv) > 1 else ["BONKUSDT", "WIFUSDT"]
    cfg = C.Meme
    print("=== BACKTEST meme_scalper (config actuelle) ===")
    print(f"Coûts: {BUY_COST_PCT}%/achat + {SELL_COST_PCT}%/vente | "
          f"SL {cfg.SL_PCT}% TP1 +{cfg.TP1_PCT}% TP2 +{cfg.TP2_PCT}% "
          f"ROC>={cfg.ENTRY_ROC_15M}% VolRel>={cfg.ENTRY_REL_VOL}x")
    print("-" * 130)
    for symbol in symbols:
        try:
            candles = load_candles(symbol)
        except FileNotFoundError:
            print(f"{symbol}: pas de données (lance d'abord fetch_data.py)")
            continue
        print(fmt(run(symbol, candles, cfg)))


if __name__ == "__main__":
    main()
