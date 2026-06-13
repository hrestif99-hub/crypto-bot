#!/usr/bin/env python3
"""
solana_bot.py — scanner de NOUVEAUX tokens Solana (Pump.fun / DexScreener).

Tokens neufs = très risqués → position minuscule, filtres qualité durs,
stop large (volatilité bonding curve). Sorties via engine.manage_position
(profil C.Solana). Tous les paramètres sont dans config.py (classe Solana).
"""

import asyncio
import socket
import logging
from datetime import datetime, timezone, timedelta

import aiohttp

import config as C
from common import (
    send_tg, get_keypair, get_best_pair, rugcheck_safe, get_usdc_balance,
    buy, load_json, save_json, new_paper_state, ensure_data_dir, RiskState,
)
from engine import open_position, manage_position

# ─── Patch DNS (fallback 8.8.8.8 / 1.1.1.1) ──────────────────────────
_orig_getaddrinfo = socket.getaddrinfo
def _patched(host, port, *a, **k):
    try:
        return _orig_getaddrinfo(host, port, *a, **k)
    except socket.gaierror:
        import dns.resolver
        r = dns.resolver.Resolver()
        r.nameservers = ["8.8.8.8", "1.1.1.1"]
        return _orig_getaddrinfo(str(r.resolve(host)[0]), port, *a, **k)
socket.getaddrinfo = _patched

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
logger = logging.getLogger("solana_bot")

BOT = "SOLANA"
CFG = C.Solana

positions: dict = {}
paper_state: dict = {}
blacklist: dict = {}          # mint -> iso expiry
risk = RiskState(logger)


# ─── Persistance ─────────────────────────────────────────────────────
def load_all():
    global positions, paper_state, blacklist
    ensure_data_dir()
    positions   = load_json(C.SOL_POSITIONS_FILE, dict)
    paper_state = load_json(C.SOL_STATE_FILE, new_paper_state)
    blacklist   = load_json(C.SOL_BLACKLIST_FILE, dict)


def save_positions():
    save_json(C.SOL_POSITIONS_FILE, positions)


def save_paper():
    save_json(C.SOL_STATE_FILE, paper_state)


def save_blacklist():
    save_json(C.SOL_BLACKLIST_FILE, blacklist)


def set_cooldown(symbol, mint, reason):
    hours = CFG.COOLDOWN_SL_HOURS if reason == "sl" else CFG.COOLDOWN_HOURS
    blacklist[mint] = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()
    save_blacklist()


def blacklisted(mint) -> bool:
    exp = blacklist.get(mint)
    if not exp:
        return False
    try:
        return datetime.now(timezone.utc) < datetime.fromisoformat(exp)
    except Exception:
        return False


def reject(mint, minutes=15):
    blacklist[mint] = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()
    save_blacklist()


# ─── Sources de tokens ───────────────────────────────────────────────
async def fetch_json(session, url):
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status == 200:
                return await r.json(content_type=None)
    except Exception:
        pass
    return None


async def fetch_pumpfun(session):
    url = ("https://frontend-api.pump.fun/coins"
           "?offset=0&limit=50&sort=created_timestamp&order=DESC")
    data = await fetch_json(session, url)
    out = []
    now = datetime.now(timezone.utc)
    for item in (data or []) if isinstance(data, list) else []:
        mint = item.get("mint")
        if not mint:
            continue
        ts = item.get("created_timestamp", 0)
        if ts:
            created = datetime.fromtimestamp(ts / 1000 if ts > 1e10 else ts, timezone.utc)
            age_s = (now - created).total_seconds()
        else:
            age_s = 9999
        out.append({"mint": mint, "symbol": item.get("symbol", "?"), "age_s": age_s})
    return out


async def fetch_dexscreener(session):
    data = await fetch_json(session, "https://api.dexscreener.com/token-profiles/latest/v1")
    items = data if isinstance(data, list) else (data or {}).get("data", [])
    return [
        {"mint": it.get("tokenAddress") or it.get("address"), "symbol": "?", "age_s": 9999}
        for it in (items or [])
        if it.get("chainId") == "solana" and (it.get("tokenAddress") or it.get("address"))
    ]


# ─── Scoring (0-100) ─────────────────────────────────────────────────
def compute_score(pair) -> tuple[int, str]:
    score, parts = 0, []
    liq = float((pair.get("liquidity") or {}).get("usd", 0) or 0) if pair else 0
    if   liq > 100_000: score += 25; parts.append(f"Liq{liq/1000:.0f}K")
    elif liq > 50_000:  score += 20; parts.append(f"Liq{liq/1000:.0f}K")
    elif liq > 20_000:  score += 12; parts.append(f"Liq{liq/1000:.0f}K")
    elif liq > 10_000:  score += 6;  parts.append(f"Liq{liq/1000:.0f}K")

    ph1 = float((pair.get("priceChange") or {}).get("h1", 0) or 0) if pair else 0
    if   ph1 > 100: score += 30; parts.append(f"+{ph1:.0f}%/1h")
    elif ph1 > 50:  score += 25; parts.append(f"+{ph1:.0f}%/1h")
    elif ph1 > 20:  score += 18; parts.append(f"+{ph1:.0f}%/1h")
    elif ph1 > 10:  score += 10; parts.append(f"+{ph1:.0f}%/1h")

    v1 = float((pair.get("volume") or {}).get("h1", 0) or 0) if pair else 0
    if   v1 > 200_000: score += 20; parts.append(f"Vol1h{v1/1000:.0f}K")
    elif v1 > 50_000:  score += 15; parts.append(f"Vol1h{v1/1000:.0f}K")
    elif v1 > 10_000:  score += 8;  parts.append(f"Vol1h{v1/1000:.0f}K")

    tx = (pair.get("txns") or {}).get("h1") or {} if pair else {}
    buys, sells = int(tx.get("buys", 0) or 0), int(tx.get("sells", 0) or 0)
    if buys and sells:
        ratio = buys / (buys + sells)
        if   ratio > 0.70: score += 15; parts.append(f"Buys{ratio*100:.0f}%")
        elif ratio > 0.55: score += 8;  parts.append(f"Buys{ratio*100:.0f}%")

    return score, " | ".join(parts)


# ─── Traitement d'un candidat ────────────────────────────────────────
async def process_token(session, mint, symbol, age_s):
    if risk.blocked() or mint in positions or blacklisted(mint):
        return
    if len(positions) >= CFG.MAX_POSITIONS:
        return
    exposed = sum(p["amount_usdc"] for p in positions.values())
    if exposed + CFG.TRADE_USDC > CFG.MAX_TOTAL_USDC:
        return

    pair = await get_best_pair(session, mint)
    if not pair:
        return
    liq  = float((pair.get("liquidity") or {}).get("usd", 0) or 0)
    vol5 = float((pair.get("volume") or {}).get("m5", 0) or 0)
    ph1  = float((pair.get("priceChange") or {}).get("h1", 0) or 0)

    # Âge : récupéré de la paire si pump.fun ne l'a pas donné
    if age_s >= 9999:
        cat = pair.get("pairCreatedAt", 0)
        if cat:
            age_s = (datetime.now(timezone.utc)
                     - datetime.fromtimestamp(cat / 1000, timezone.utc)).total_seconds()

    if age_s < CFG.MIN_AGE_SECONDS:
        return
    if liq < CFG.MIN_LIQUIDITY_USD:
        return
    if vol5 < CFG.MIN_VOL_5M and ph1 < CFG.VOL5_OVERRIDE_PH1:
        return

    if not await rugcheck_safe(session, mint):
        logger.info(f"[{symbol}] BLOQUÉ — RugCheck")
        reject(mint)
        return

    score, reasons = compute_score(pair)
    if score < CFG.MIN_SCORE:
        logger.info(f"[{symbol}] BLOQUÉ — score {score} < {CFG.MIN_SCORE}")
        reject(mint)
        return

    bal = paper_state["balance_usdc"] if C.PAPER_MODE else await get_usdc_balance()
    if bal < CFG.MIN_USDC:
        logger.info(f"[{symbol}] BLOQUÉ — solde {bal:.2f} < {CFG.MIN_USDC}")
        return

    entry_price = float(pair.get("priceUsd", 0) or 0)
    logger.info(f"[{symbol}] SIGNAL ✅ score={score} liq=${liq:,.0f} age={age_s:.0f}s — achat")

    ok, sig, qty = await buy(session, mint, CFG.TRADE_USDC, paper_state, logger)
    if not ok or qty == 0:
        logger.error(f"[{symbol}] achat échoué : {sig}")
        reject(mint)
        return
    if entry_price <= 0 and qty > 0:
        entry_price = CFG.TRADE_USDC / (qty / 1e6)
    if not symbol or symbol == "?":
        symbol = (pair.get("baseToken") or {}).get("symbol") or mint[:6]
    positions[mint] = open_position(mint, symbol, CFG.TRADE_USDC, entry_price, qty,
                                    extra={"score": score, "reasons": reasons})
    save_positions()
    save_paper()
    await send_tg(f"{BOT} — ACHAT\nToken : {symbol}\nScore : {score}/100\n"
                  f"Montant : {CFG.TRADE_USDC} USDC\n{reasons}")


# ─── Boucles ─────────────────────────────────────────────────────────
async def scanner_loop():
    await asyncio.sleep(12)
    while True:
        if risk.daily_limit_hit():
            logger.warning(f"[KILL-SWITCH] perte journalière {risk.daily_pnl:.2f} — pause 1h")
            await send_tg(f"🛑 {BOT} KILL-SWITCH\nPerte {risk.daily_pnl:.2f} USDC — pause")
            await asyncio.sleep(C.Risk.DAILY_PAUSE_SECONDS)
        try:
            async with aiohttp.ClientSession() as s:
                pump, dex = await asyncio.gather(fetch_pumpfun(s), fetch_dexscreener(s))
                seen = set()
                for tok in pump + dex:
                    if tok["mint"] not in seen:
                        seen.add(tok["mint"])
                        await process_token(s, tok["mint"], tok["symbol"], tok["age_s"])
                        await asyncio.sleep(0.5)
        except Exception as e:
            logger.error(f"scanner_loop: {e}")
        await asyncio.sleep(C.Loop.SOL_SCAN)


async def monitor_loop():
    await asyncio.sleep(20)
    while True:
        try:
            async with aiohttp.ClientSession() as s:
                for mint in list(positions.keys()):
                    await manage_position(s, mint, positions, CFG, paper_state, risk, logger,
                                          BOT, C.SOL_TRADES_FILE, set_cooldown, save_positions)
                    save_paper()
                    await asyncio.sleep(1)
        except Exception as e:
            logger.error(f"monitor_loop: {e}")
        await asyncio.sleep(C.Loop.SOL_MONITOR)


async def watchdog_loop():
    while True:
        logger.info(f"[watchdog] {BOT} alive — positions={len(positions)} "
                    f"daily_pnl={risk.daily_pnl:+.2f} streak={risk.consecutive_losses}")
        await asyncio.sleep(C.Loop.WATCHDOG)


async def main():
    load_all()
    kp = get_keypair()
    logger.info(f"Wallet : {kp.pubkey() if kp else 'AUCUN (paper)'}")
    await send_tg(f"{BOT} BOT DÉMARRÉ\nSL {CFG.SL_PCT:.0f}% | TP1 +{CFG.TP1_PCT:.0f}% "
                  f"TP2 +{CFG.TP2_PCT:.0f}%\nScore min {CFG.MIN_SCORE} | "
                  f"Liq min ${CFG.MIN_LIQUIDITY_USD:,}")
    await asyncio.gather(scanner_loop(), monitor_loop(), watchdog_loop())


if __name__ == "__main__":
    asyncio.run(main())
