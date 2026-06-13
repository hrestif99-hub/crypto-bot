#!/usr/bin/env python3
"""
engine.py — moteur de trading PARTAGÉ par les deux bots.

Une seule échelle de sorties, générique sur un profil (config.Meme ou
config.Solana) :

    STALL-EXIT  →  TP1  →  TP2  →  TRAILING  →  STOP-LOSS

Les bots ne réimplémentent rien : ils appellent manage_position().
Pour changer un comportement de sortie, on touche UNIQUEMENT ici + config.py.
"""

from datetime import datetime, timezone

import config as C
from common import get_price, sell, append_trade, send_tg


def open_position(mint: str, symbol: str, amount_usdc: float,
                  entry_price: float, qty_raw: int, extra: dict | None = None) -> dict:
    """Structure de position standardisée (identique pour les deux bots)."""
    pos = {
        "symbol":      symbol,
        "mint":        mint,
        "amount_usdc": amount_usdc,
        "entry_price": entry_price,
        "qty_raw":     qty_raw,
        "peak_price":  entry_price,
        "tp1_done":    False,
        "tp2_done":    False,
        "open_ts":     datetime.now(timezone.utc).isoformat(),
    }
    if extra:
        pos.update(extra)
    return pos


def _age_minutes(open_ts: str) -> float:
    try:
        dt = datetime.fromisoformat(str(open_ts))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 60
    except Exception:
        return 0.0


def _record(paper_state: dict, trades_file: str, *, symbol, mint, reason,
            entry, current, amount, pnl, open_ts, closed: bool):
    paper_state["pnl_total"] += pnl
    if closed:
        paper_state["wins" if pnl > 0 else "losses"] += 1
        paper_state["trades_closed"].append({
            "symbol": symbol, "pnl": round(pnl, 4), "reason": reason,
            "ts": datetime.now(timezone.utc).isoformat(),
        })
    append_trade(trades_file, {
        "symbol": symbol, "mint": mint, "reason": reason,
        "entry_price": entry, "exit_price": current,
        "amount_usdc": round(amount, 4), "pnl": round(pnl, 4),
        "pnl_pct": round((current - entry) / entry * 100, 2) if entry > 0 else 0,
        "open_ts": open_ts, "closed_at": datetime.now(timezone.utc).isoformat(),
        "closed": closed,
    })


def decide_exit(entry, peak, current, age_min, tp1_done, tp2_done, cfg):
    """
    DÉCISION DE SORTIE PURE — sans I/O, sans horloge.
    Partagée à l'identique par le bot live (manage_position) et le backtest.

    Retourne (reason, sell_fraction, closes) ou None :
      • reason         : "STALL_EXIT" | "TP1" | "TP2" | "TRAILING" | "SL"
      • sell_fraction  : part de la QUANTITÉ COURANTE à vendre (1.0 = tout)
      • closes         : True si la position est entièrement fermée
    L'ordre de priorité est : stall → TP1 → TP2 → trailing → SL.
    """
    if entry <= 0:
        return None
    pct_e     = (current - entry) / entry * 100
    pct_p     = (current - peak) / peak * 100 if peak > 0 else 0
    peak_gain = (peak - entry) / entry * 100

    if age_min >= cfg.STALL_MINUTES and abs(pct_e) < cfg.STALL_BAND_PCT:
        return ("STALL_EXIT", 1.0, True)
    if not tp1_done and pct_e >= cfg.TP1_PCT:
        return ("TP1", cfg.TP1_SELL, False)
    if tp1_done and not tp2_done and pct_e >= cfg.TP2_PCT:
        return ("TP2", cfg.TP2_SELL, False)
    if peak_gain >= cfg.TRAIL_ARM_PCT and pct_p <= -cfg.TRAIL_GIVEBACK_PCT:
        return ("TRAILING", 1.0, True)
    if pct_e <= cfg.SL_PCT:
        return ("SL", 1.0, True)
    return None


async def manage_position(session, mint, positions, cfg, paper_state, risk, logger,
                          bot_label, trades_file, set_cooldown, save_positions):
    """
    Gère UNE position live selon le profil `cfg` (C.Meme ou C.Solana).
    Toute la logique de décision vit dans decide_exit() ; ici on ne fait
    que l'I/O (prix, vente, persistance, cooldown, notification).
    """
    pos = positions.get(mint)
    if not pos:
        return
    symbol  = pos["symbol"]
    entry   = pos["entry_price"]
    open_ts = pos.get("open_ts", "")

    current = await get_price(session, mint)
    if current <= 0:
        return

    # — Mise à jour du pic —
    if current > pos.get("peak_price", entry):
        pos["peak_price"] = current
        save_positions()
    peak = pos.get("peak_price", entry)
    qty  = pos["qty_raw"]
    age  = _age_minutes(open_ts)

    decision = decide_exit(entry, peak, current, age,
                           pos.get("tp1_done"), pos.get("tp2_done"), cfg)
    if not decision:
        return
    reason, frac, closes = decision
    sell_qty = int(qty * frac)
    if sell_qty <= 0:
        return

    ok, _, usdc = await sell(session, mint, sell_qty, pos, paper_state, logger, current_hint=current)
    if not ok:
        return

    if not closes:
        # ─── Prise de profit partielle (TP1 / TP2) ───
        gain = usdc - pos["amount_usdc"] * frac
        pos["tp1_done" if reason == "TP1" else "tp2_done"] = True
        pos["qty_raw"]     -= sell_qty
        pos["amount_usdc"] *= (1 - frac)
        if reason == "TP1":
            pos["peak_price"] = current      # on repart du nouveau prix pour le trailing
        risk.register_gain(gain)
        _record(paper_state, trades_file, symbol=symbol, mint=mint, reason=reason,
                entry=entry, current=current, amount=usdc, pnl=gain,
                open_ts=open_ts, closed=False)
        save_positions()
        pct = cfg.TP1_PCT if reason == "TP1" else cfg.TP2_PCT
        suffix = "" if reason == "TP1" else " du reste"
        await send_tg(f"{bot_label} — {reason} +{pct:.0f}%\nToken : {symbol}\n"
                      f"Vendu {frac*100:.0f}%{suffix} | Gain {gain:+.2f} USDC")
        return

    # ─── Clôture totale (STALL / TRAILING / SL) ───
    pnl = usdc - pos["amount_usdc"]
    risk.register(pnl)
    _record(paper_state, trades_file, symbol=symbol, mint=mint, reason=reason,
            entry=entry, current=current, amount=pos["amount_usdc"], pnl=pnl,
            open_ts=open_ts, closed=True)
    set_cooldown(symbol, mint, "sl" if reason == "SL" else reason.lower())
    del positions[mint]
    save_positions()

    if reason == "STALL_EXIT":
        await send_tg(f"{bot_label} — STALL EXIT\nToken : {symbol}\n"
                      f"Âge   : {age:.0f}min sans momentum\nPnL   : {pnl:+.2f} USDC")
    elif reason == "TRAILING":
        peak_gain = (peak - entry) / entry * 100
        pct_p     = (current - peak) / peak * 100 if peak > 0 else 0
        await send_tg(f"{bot_label} — TRAILING\nToken : {symbol}\n"
                      f"Pic +{peak_gain:.1f}% | Recul {pct_p:.1f}%\nPnL {pnl:+.2f} USDC")
    else:  # SL
        await send_tg(f"{bot_label} — STOP LOSS {cfg.SL_PCT:.0f}%\nToken : {symbol}\n"
                      f"Prix ${current:.6f}\nPnL {pnl:+.2f} USDC")
