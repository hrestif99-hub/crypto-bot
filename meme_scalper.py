#!/usr/bin/env python3
"""
meme_scalper.py — scanner de tokens Solana ÉTABLIS (liste fixe).

Entrée : momentum court terme confirmé par le volume et le VWAP.
Sorties : déléguées au moteur partagé engine.manage_position (profil C.Meme).

Tous les paramètres sont dans config.py (classe Meme).
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta

import aiohttp

import config as C
from common import (
    send_tg, get_keypair, get_best_pair, get_ohlcv, rugcheck_safe,
    get_usdc_balance, buy, load_json, save_json, new_paper_state,
    ensure_data_dir, RiskState, roc_15m, rel_volume, vwap, get_price,
)
from engine import open_position, manage_position

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
logger = logging.getLogger("meme_scalper")

BOT = "MEME SCALPER"
CFG = C.Meme

positions: dict = {}
paper_state: dict = {}
cooldown: dict = {}          # symbol -> iso expiry
risk = RiskState(logger)


# ─── Persistance ─────────────────────────────────────────────────────
def load_all():
    global positions, paper_state, cooldown
    ensure_data_dir()
    positions   = load_json(C.MEME_POSITIONS_FILE, dict)
    paper_state = load_json(C.MEME_STATE_FILE, new_paper_state)
    cooldown    = load_json(C.MEME_COOLDOWN_FILE, dict)


def save_positions():
    save_json(C.MEME_POSITIONS_FILE, positions)


def save_paper():
    save_json(C.MEME_STATE_FILE, paper_state)


def set_cooldown(symbol, mint, reason):
    hours = CFG.COOLDOWN_SL_HOURS if reason == "sl" else CFG.COOLDOWN_HOURS
    cooldown[symbol] = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()
    save_json(C.MEME_COOLDOWN_FILE, cooldown)


def on_cooldown(symbol) -> bool:
    exp = cooldown.get(symbol)
    if not exp:
        return False
    try:
        return datetime.now(timezone.utc) < datetime.fromisoformat(exp)
    except Exception:
        return False


# ─── Entrée ──────────────────────────────────────────────────────────
async def check_entry(session, symbol, mint):
    if risk.blocked():
        return
    if on_cooldown(symbol) or mint in positions:
        return
    if len(positions) >= CFG.MAX_POSITIONS:
        return
    exposed = sum(p["amount_usdc"] for p in positions.values())
    if exposed + CFG.TRADE_USDC > CFG.MAX_TOTAL_USDC:
        return

    pair = await get_best_pair(session, mint)
    if not pair:
        return
    vol24 = float((pair.get("volume") or {}).get("h24", 0) or 0)
    if vol24 < CFG.MIN_VOL24_USD:
        logger.info(f"[{symbol}] BLOQUÉ — vol24h ${vol24:,.0f} < ${CFG.MIN_VOL24_USD:,}")
        return

    pool = pair.get("pairAddress")
    ohlcv = await get_ohlcv(session, pool, limit=50) if pool else []
    if len(ohlcv) < 10:
        logger.info(f"[{symbol}] BLOQUÉ — données OHLCV insuffisantes")
        return

    roc = roc_15m(ohlcv)
    rv  = rel_volume(ohlcv)
    vw  = vwap(ohlcv)
    price = ohlcv[-1][4]

    if roc < CFG.ENTRY_ROC_15M:
        logger.info(f"[{symbol}] BLOQUÉ — ROC15m {roc:.1f}% < {CFG.ENTRY_ROC_15M}%")
        return
    if rv < CFG.ENTRY_REL_VOL:
        logger.info(f"[{symbol}] BLOQUÉ — VolRel {rv:.2f}x < {CFG.ENTRY_REL_VOL}x")
        return
    if vw > 0 and price < vw * 0.98:
        logger.info(f"[{symbol}] BLOQUÉ — prix ${price:.6f} sous VWAP ${vw:.6f}")
        return

    if not await rugcheck_safe(session, mint):
        logger.info(f"[{symbol}] BLOQUÉ — RugCheck")
        return

    bal = paper_state["balance_usdc"] if C.PAPER_MODE else await get_usdc_balance()
    if bal < CFG.MIN_USDC:
        logger.info(f"[{symbol}] BLOQUÉ — solde {bal:.2f} < {CFG.MIN_USDC}")
        return

    entry_price = float(pair.get("priceUsd", 0) or 0)
    logger.info(f"[{symbol}] SIGNAL ✅ ROC {roc:.1f}% VolRel {rv:.2f}x — achat {CFG.TRADE_USDC} USDC")

    ok, sig, qty = await buy(session, mint, CFG.TRADE_USDC, paper_state, logger)
    if not ok or qty == 0:
        logger.error(f"[{symbol}] achat échoué : {sig}")
        return
    positions[mint] = open_position(mint, symbol, CFG.TRADE_USDC, entry_price, qty)
    save_positions()
    save_paper()
    await send_tg(f"{BOT} — ACHAT\nToken : {symbol}\nPrix  : ${entry_price:.6f}\n"
                  f"Montant : {CFG.TRADE_USDC} USDC")


# ─── Boucles ─────────────────────────────────────────────────────────
async def scanner_loop():
    await asyncio.sleep(10)
    while True:
        if risk.daily_limit_hit():
            logger.warning(f"[KILL-SWITCH] perte journalière {risk.daily_pnl:.2f} — pause 1h")
            await send_tg(f"🛑 {BOT} KILL-SWITCH\nPerte {risk.daily_pnl:.2f} USDC — pause 1h")
            await asyncio.sleep(3600)
        try:
            async with aiohttp.ClientSession() as s:
                for symbol, mint in CFG.TOKENS.items():
                    await check_entry(s, symbol, mint)
                    await asyncio.sleep(2)
        except Exception as e:
            logger.error(f"scanner_loop: {e}")
        await asyncio.sleep(C.Loop.MEME_SCAN)


async def monitor_loop():
    await asyncio.sleep(20)
    while True:
        try:
            async with aiohttp.ClientSession() as s:
                for mint in list(positions.keys()):
                    await manage_position(s, mint, positions, CFG, paper_state, risk, logger,
                                          BOT, C.MEME_TRADES_FILE, set_cooldown, save_positions)
                    save_paper()
                    await asyncio.sleep(1)
        except Exception as e:
            logger.error(f"monitor_loop: {e}")
        await asyncio.sleep(C.Loop.MEME_MONITOR)


async def watchdog_loop():
    while True:
        logger.info(f"[watchdog] {BOT} alive — positions={len(positions)} "
                    f"daily_pnl={risk.daily_pnl:+.2f} streak={risk.consecutive_losses}")
        await asyncio.sleep(C.Loop.WATCHDOG)


async def main():
    load_all()
    kp = get_keypair()
    logger.info(f"Wallet : {kp.pubkey() if kp else 'AUCUN (paper)'}")
    await send_tg(f"{BOT} DÉMARRÉ\nSL {CFG.SL_PCT:.0f}% | TP1 +{CFG.TP1_PCT:.0f}% "
                  f"TP2 +{CFG.TP2_PCT:.0f}%\nTrailing armé +{CFG.TRAIL_ARM_PCT:.0f}% "
                  f"recul {CFG.TRAIL_GIVEBACK_PCT:.0f}%\n{len(CFG.TOKENS)} tokens suivis")
    await asyncio.gather(scanner_loop(), monitor_loop(), watchdog_loop())


if __name__ == "__main__":
    asyncio.run(main())
