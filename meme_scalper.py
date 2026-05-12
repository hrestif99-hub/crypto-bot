#!/usr/bin/env python3
"""Meme token scalper — liste fixe de 10 tokens Solana avec signaux RSI/MACD."""

import asyncio
import aiohttp
import json
import os
import logging
import base64
import requests
from datetime import datetime, timezone
from urllib.parse import urlencode

from dotenv import load_dotenv
load_dotenv()

# ─── Configuration ────────────────────────────────────────────
SOLANA_PRIVATE_KEY = os.environ.get("SOLANA_PRIVATE_KEY", "")
HELIUS_RPC_URL     = os.environ.get("HELIUS_RPC_URL", "https://api.mainnet-beta.solana.com")
TELEGRAM_TOKEN     = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID            = os.environ.get("CHAT_ID", "").strip()
DRY_RUN            = os.environ.get("DRY_RUN", "false").lower() == "true"

USDC_MINT     = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDC_DECIMALS = 6
JUP_BASE      = "https://lite-api.jup.ag/swap/v1"

TOKENS = {
    "BONK":     "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
    "WIF":      "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm",
    "GIGA":     "63LfDmNb3MQ8mw9MtZ2To9bEA2M71kZUUGq5tiJxcqj9",
    "POPCAT":   "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr",
    "MEW":      "MEW1gQWJ3nEXg2qgERiKu7FAFj79PHvQVREQUzScPP5",
    "BABYTROLL":"placeholder_address_babytroll",
    "BOME":     "ukHH6c7mMyiWCf1b9pnWe25TSpkDDt3H5pQZgZ74J82",
    "MYRO":     "HhJpBhRRn4g56VsyLuT8DL5Bv31HkXqsrahTTUCZeZg4",
    "SLERF":    "7BgBvyjrZX1YKz4oh9mjb8ZScatkkwb8DzFx7LoiVkM3",
    "SAMO":     "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU",
}

TRADE_USDC          = 5.0
MAX_POSITIONS       = 5
MAX_TOTAL_USDC      = 25.0
MIN_USDC            = 6.0
SCAN_INTERVAL       = 60
MONITOR_INTERVAL    = 30

SLIPPAGE_BPS        = 1500
JITO_TIP_LAMPORTS   = int(os.environ.get("JITO_TIP_LAMPORTS", "100000"))
MIN_SOL_FOR_RENT    = 0.005   # SOL minimum : rent ATA (~0.00204) + fees + marge

TP1_PCT             = 20.0
TP2_PCT             = 50.0
SL_PCT              = -10.0
TRAILING_PCT        = 20.0
TRAILING_ACTIVATION = 20.0

POSITIONS_FILE = "positions.json"

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
logger = logging.getLogger("meme_scalper")

# ─── État global ──────────────────────────────────────────────
positions = {}


def load_state():
    global positions
    if os.path.exists(POSITIONS_FILE):
        with open(POSITIONS_FILE) as f:
            positions = json.load(f)


def save_state():
    with open(POSITIONS_FILE, "w") as f:
        json.dump(positions, f, indent=2)


# ─── Keypair Solana ───────────────────────────────────────────
_keypair = None


def get_keypair():
    global _keypair
    if _keypair is not None:
        return _keypair
    if not SOLANA_PRIVATE_KEY:
        return None
    try:
        from solders.keypair import Keypair
        import base58 as b58
        raw = b58.b58decode(SOLANA_PRIVATE_KEY)
        _keypair = Keypair.from_bytes(raw)
        logger.info(f"Wallet chargé : {_keypair.pubkey()}")
        return _keypair
    except Exception:
        pass
    try:
        from solders.keypair import Keypair
        _keypair = Keypair.from_bytes(bytes(json.loads(SOLANA_PRIVATE_KEY)))
        return _keypair
    except Exception as e:
        logger.error(f"Keypair invalide : {e}")
        return None


# ─── Telegram ────────────────────────────────────────────────
async def send_tg(text: str):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        async with aiohttp.ClientSession() as s:
            await s.post(url, json={"chat_id": CHAT_ID, "text": text},
                         timeout=aiohttp.ClientTimeout(total=10))
    except Exception as e:
        logger.error(f"send_tg: {e}")


# ─── Balance USDC on-chain ────────────────────────────────────
async def get_usdc_balance() -> float:
    kp = get_keypair()
    if not kp:
        return 0.0
    try:
        payload = {
            "jsonrpc": "2.0", "id": 1,
            "method":  "getTokenAccountsByOwner",
            "params":  [str(kp.pubkey()), {"mint": USDC_MINT}, {"encoding": "jsonParsed"}],
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(
                HELIUS_RPC_URL, json=payload, timeout=aiohttp.ClientTimeout(total=30)
            ) as r:
                data = await r.json(content_type=None)
        accounts = data.get("result", {}).get("value") or []
        total = 0.0
        for acct in accounts:
            ui = (
                acct.get("account", {})
                    .get("data", {})
                    .get("parsed", {})
                    .get("info", {})
                    .get("tokenAmount", {})
                    .get("uiAmount")
            )
            total += float(ui or 0)
        return total
    except Exception as e:
        logger.error(f"get_usdc_balance: {e}")
        return 0.0


# ─── Balance SOL on-chain ────────────────────────────────────
async def get_sol_balance(session: aiohttp.ClientSession) -> float:
    kp = get_keypair()
    if not kp:
        return 0.0
    payload = {
        "jsonrpc": "2.0", "id": 1,
        "method":  "getBalance",
        "params":  [str(kp.pubkey())],
    }
    try:
        async with session.post(
            HELIUS_RPC_URL, json=payload, timeout=aiohttp.ClientTimeout(total=10)
        ) as r:
            data = await r.json(content_type=None)
        lamports = data.get("result", {}).get("value", 0)
        return lamports / 1e9
    except Exception as e:
        logger.error(f"get_sol_balance: {e}")
        return 0.0


# ─── Indicateurs techniques ───────────────────────────────────
def compute_rsi(closes: list, period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    return 100 - 100 / (1 + avg_gain / avg_loss)


def _ema(values: list, period: int) -> list:
    if len(values) < period:
        return []
    k = 2 / (period + 1)
    result = [sum(values[:period]) / period]
    for v in values[period:]:
        result.append(v * k + result[-1] * (1 - k))
    return result


def compute_macd(closes: list, fast: int = 12, slow: int = 26, signal: int = 9) -> tuple:
    """Retourne (macd_line, signal_line). (0, 0) si données insuffisantes."""
    if len(closes) < slow + signal:
        return 0.0, 0.0
    ema_fast = _ema(closes, fast)
    ema_slow = _ema(closes, slow)
    offset = len(ema_fast) - len(ema_slow)
    macd_series = [f - s for f, s in zip(ema_fast[offset:], ema_slow)]
    if len(macd_series) < signal:
        return 0.0, 0.0
    sig_series = _ema(macd_series, signal)
    if not sig_series:
        return 0.0, 0.0
    return macd_series[-1], sig_series[-1]


def volume_ratio(volumes: list, period: int = 20) -> float:
    """Ratio volume dernière bougie / moyenne des 20 précédentes."""
    if len(volumes) < period + 1:
        return 1.0
    avg = sum(volumes[-period - 1:-1]) / period
    return volumes[-1] / avg if avg > 0 else 1.0


# ─── DexScreener ─────────────────────────────────────────────
async def get_best_pair(session: aiohttp.ClientSession, mint: str) -> dict | None:
    try:
        async with session.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
            timeout=aiohttp.ClientTimeout(total=10)
        ) as r:
            if r.status != 200:
                return None
            data = await r.json(content_type=None)
        pairs = [p for p in (data.get("pairs") or []) if p.get("chainId") == "solana"]
        if not pairs:
            return None
        pairs.sort(key=lambda x: float((x.get("volume") or {}).get("h24", 0) or 0), reverse=True)
        return pairs[0]
    except Exception as e:
        logger.error(f"get_best_pair {mint[:8]}: {e}")
        return None


async def get_current_price(session: aiohttp.ClientSession, mint: str) -> float:
    pair = await get_best_pair(session, mint)
    return float(pair.get("priceUsd", 0) or 0) if pair else 0.0


# ─── GeckoTerminal OHLCV ──────────────────────────────────────
async def get_ohlcv(session: aiohttp.ClientSession, pair_address: str, limit: int = 50) -> list:
    """Retourne liste de [timestamp, open, high, low, close, volume] triée ASC."""
    url = (
        f"https://api.geckoterminal.com/api/v2/networks/solana"
        f"/pools/{pair_address}/ohlcv/minute"
    )
    try:
        async with session.get(
            url,
            params={"aggregate": 5, "limit": limit, "currency": "usd"},
            headers={"Accept": "application/json"},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as r:
            if r.status != 200:
                logger.debug(f"get_ohlcv HTTP {r.status} pour {pair_address[:12]}")
                return []
            data = await r.json(content_type=None)
        ohlcv = data.get("data", {}).get("attributes", {}).get("ohlcv_list", [])
        return sorted(ohlcv, key=lambda x: x[0])
    except Exception as e:
        logger.error(f"get_ohlcv {pair_address[:12]}: {e}")
        return []


# ─── Jupiter : quote ─────────────────────────────────────────
async def _jup_quote(session: aiohttp.ClientSession, url: str) -> dict | None:
    for attempt in range(1, 4):
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                body = await r.text()
                if r.status == 200:
                    return json.loads(body)
                logger.error(f"Jupiter quote HTTP {r.status}: {body[:400]}")
                return None
        except Exception as e:
            logger.error(f"Jupiter quote tentative {attempt}/3: {e}")
            if attempt < 3:
                await asyncio.sleep(1)
    return None


async def _jup_swap(session: aiohttp.ClientSession, payload: dict) -> dict | None:
    try:
        async with session.post(
            f"{JUP_BASE}/swap", json=payload, timeout=aiohttp.ClientTimeout(total=20)
        ) as r:
            body = await r.text()
            if r.status == 200:
                return json.loads(body)
            logger.error(f"Jupiter swap HTTP {r.status}: {body[:400]}")
    except Exception as e:
        logger.error(f"Jupiter swap: {e}")
    return None


# ─── Jupiter buy/sell ─────────────────────────────────────────
async def jupiter_buy(session: aiohttp.ClientSession, mint: str, amount_usdc: float) -> tuple:
    """Achète `amount_usdc` USDC de `mint`. Retourne (success, sig, qty_raw)."""
    kp = get_keypair()
    if DRY_RUN:
        logger.info(f"[DRY RUN] Achat {amount_usdc} USDC {mint[:8]}")
        return True, "dry_run_sig", 1_000_000
    if not kp:
        return False, "keypair manquant", 0
    amount_raw = int(amount_usdc * 10 ** USDC_DECIMALS)
    quote_url = f"{JUP_BASE}/quote?" + urlencode({
        "inputMint":   USDC_MINT,
        "outputMint":  mint,
        "amount":      amount_raw,
        "slippageBps": SLIPPAGE_BPS,
        "maxAccounts": 20,
    })
    quote = await _jup_quote(session, quote_url)
    if not quote:
        return False, "quote échoué", 0
    if "error" in quote:
        return False, f"quote error: {quote['error']}", 0
    swap = await _jup_swap(session, {
        "quoteResponse":    quote,
        "userPublicKey":    str(kp.pubkey()),
        "wrapAndUnwrapSol": True,
    })
    if not swap or "swapTransaction" not in swap:
        return False, "swap échoué", 0
    try:
        from solders.transaction import VersionedTransaction
        from solana.rpc.async_api import AsyncClient
        tx_b64 = swap["swapTransaction"].replace('-', '+').replace('_', '/')
        raw    = base64.b64decode(tx_b64 + '==')
        tx     = VersionedTransaction.from_bytes(raw)
        signed = VersionedTransaction(tx.message, [kp])
        async with AsyncClient(HELIUS_RPC_URL) as client:
            result = await asyncio.wait_for(client.send_raw_transaction(bytes(signed)), timeout=30)
        sig = str(result.value)
        qty = int(quote.get("outAmount", 0))
        logger.info(f"BUY {mint[:8]} qty={qty} sig={sig[:12]}")
        return True, sig, qty
    except asyncio.TimeoutError:
        return False, "timeout RPC", 0
    except Exception as e:
        logger.error(f"jupiter_buy sign/send: {e}")
        return False, str(e), 0


async def jupiter_sell(session: aiohttp.ClientSession, mint: str, qty_raw: int) -> tuple:
    """Vend `qty_raw` tokens. Retourne (success, sig, usdc_reçu)."""
    kp = get_keypair()
    if DRY_RUN:
        logger.info(f"[DRY RUN] Vente {qty_raw} tokens {mint[:8]}")
        return True, "dry_run_sig", TRADE_USDC * 1.1
    if not kp or qty_raw <= 0:
        return False, "keypair/qty manquant", 0.0
    quote_url = f"{JUP_BASE}/quote?" + urlencode({
        "inputMint":   mint,
        "outputMint":  USDC_MINT,
        "amount":      qty_raw,
        "slippageBps": SLIPPAGE_BPS,
        "maxAccounts": 20,
    })
    quote = await _jup_quote(session, quote_url)
    if not quote:
        return False, "quote vente échoué", 0.0
    if "error" in quote:
        return False, f"quote error: {quote['error']}", 0.0
    swap = await _jup_swap(session, {
        "quoteResponse":    quote,
        "userPublicKey":    str(kp.pubkey()),
        "wrapAndUnwrapSol": True,
    })
    if not swap or "swapTransaction" not in swap:
        return False, "swap vente échoué", 0.0
    try:
        from solders.transaction import VersionedTransaction
        from solana.rpc.async_api import AsyncClient
        tx_b64 = swap["swapTransaction"].replace('-', '+').replace('_', '/')
        raw    = base64.b64decode(tx_b64 + '==')
        tx     = VersionedTransaction.from_bytes(raw)
        signed = VersionedTransaction(tx.message, [kp])
        async with AsyncClient(HELIUS_RPC_URL) as client:
            result = await asyncio.wait_for(client.send_raw_transaction(bytes(signed)), timeout=30)
        sig  = str(result.value)
        usdc = int(quote.get("outAmount", 0)) / 10 ** USDC_DECIMALS
        logger.info(f"SELL {mint[:8]} usdc={usdc:.2f} sig={sig[:12]}")
        return True, sig, usdc
    except asyncio.TimeoutError:
        return False, "timeout RPC", 0.0
    except Exception as e:
        logger.error(f"jupiter_sell sign/send: {e}")
        return False, str(e), 0.0


# ─── Logique d'entrée ─────────────────────────────────────────
async def check_entry(session: aiohttp.ClientSession, symbol: str, mint: str):
    sol_bal = await get_sol_balance(session)
    if sol_bal < MIN_SOL_FOR_RENT:
        logger.warning(f"[{symbol}] SOL insuffisant ({sol_bal:.4f} SOL) — achat annulé")
        return

    if mint in positions:
        return
    if len(positions) >= MAX_POSITIONS:
        return
    exposed = sum(p["amount_usdc"] for p in positions.values())
    if exposed + TRADE_USDC > MAX_TOTAL_USDC:
        return

    pair = await get_best_pair(session, mint)
    if not pair:
        logger.debug(f"{symbol}: pas de paire DexScreener")
        return

    pair_address = pair.get("pairAddress")
    if not pair_address:
        return

    ohlcv = await get_ohlcv(session, pair_address, limit=50)
    if len(ohlcv) < 35:
        logger.debug(f"{symbol}: données insuffisantes ({len(ohlcv)} bougies)")
        return

    closes  = [float(c[4]) for c in ohlcv]
    volumes = [float(c[5]) for c in ohlcv]

    rsi              = compute_rsi(closes)
    macd_line, sig_l = compute_macd(closes)
    vol_r            = volume_ratio(volumes)
    bull_candles     = sum(1 for i in [-3, -2, -1] if closes[i] > closes[i - 1])

    logger.info(
        f"[scan] {symbol} RSI={rsi:.1f} MACD={macd_line:.6f}/{sig_l:.6f} "
        f"VolR={vol_r:.2f} BullC={bull_candles}/3"
    )

    if not (35 <= rsi <= 72):
        return
    if macd_line <= sig_l:
        return
    if vol_r < 1.1:
        return
    if bull_candles < 2:
        return

    usdc_bal = await get_usdc_balance()
    if usdc_bal < MIN_USDC:
        logger.info(f"{symbol}: solde USDC insuffisant ({usdc_bal:.2f} < {MIN_USDC})")
        return

    entry_price = float(pair.get("priceUsd", 0) or 0)
    logger.info(f"[achat] {symbol} entry=${entry_price:.6f} RSI={rsi:.1f} VolR={vol_r:.2f}")

    ok, sig, qty_raw = await jupiter_buy(session, mint, TRADE_USDC)
    if not ok or qty_raw == 0:
        logger.error(f"[achat échoué] {symbol}: {sig}")
        return

    positions[mint] = {
        "symbol":      symbol,
        "mint":        mint,
        "amount_usdc": TRADE_USDC,
        "entry_price": entry_price,
        "qty_raw":     qty_raw,
        "peak_price":  entry_price,
        "tp1_done":    False,
        "open_ts":     datetime.now(timezone.utc).isoformat(),
    }
    save_state()

    await send_tg(
        f"MEME SCALPER — ACHAT\n\n"
        f"Token  : {symbol}\n"
        f"Prix   : ${entry_price:.6f}\n"
        f"Montant: {TRADE_USDC} USDC\n"
        f"RSI    : {rsi:.1f} | VolR: {vol_r:.2f}"
    )


# ─── Logique de sortie ────────────────────────────────────────
async def check_exits(session: aiohttp.ClientSession):
    for mint in list(positions.keys()):
        pos    = positions.get(mint)
        if not pos:
            continue
        symbol = pos["symbol"]
        entry  = pos["entry_price"]
        qty    = pos["qty_raw"]
        peak   = pos["peak_price"]

        current = await get_current_price(session, mint)
        if current <= 0:
            continue

        if current > peak:
            positions[mint]["peak_price"] = current
            peak = current
            save_state()

        pct_entry = (current - entry) / entry * 100 if entry > 0 else 0
        pct_peak  = (current - peak)  / peak  * 100 if peak  > 0 else 0

        # TP1 : +20% → vendre 50%
        if not pos["tp1_done"] and pct_entry >= TP1_PCT:
            half = qty // 2
            ok, sig, usdc = await jupiter_sell(session, mint, half)
            if ok:
                gain = usdc - pos["amount_usdc"] * 0.5
                positions[mint]["tp1_done"]    = True
                positions[mint]["qty_raw"]    -= half
                positions[mint]["amount_usdc"] *= 0.5
                save_state()
                await send_tg(
                    f"MEME SCALPER — TP1 +{TP1_PCT:.0f}%\n\n"
                    f"Token  : {symbol}\n"
                    f"Prix   : ${current:.6f}\n"
                    f"Reçu   : {usdc:.2f} USDC\n"
                    f"Gain   : {gain:+.2f} USDC"
                )
            continue

        # TP2 : +50% → vendre tout
        if pct_entry >= TP2_PCT:
            ok, sig, usdc = await jupiter_sell(session, mint, qty)
            if ok:
                pnl = usdc - pos["amount_usdc"]
                del positions[mint]
                save_state()
                await send_tg(
                    f"MEME SCALPER — TP2 +{TP2_PCT:.0f}%\n\n"
                    f"Token  : {symbol}\n"
                    f"Prix   : ${current:.6f}\n"
                    f"Reçu   : {usdc:.2f} USDC\n"
                    f"PnL    : {pnl:+.2f} USDC"
                )
            continue

        # Trailing stop : actif si peak >= +20%, recul >= -20%
        peak_pct = (peak - entry) / entry * 100 if entry > 0 else 0
        if peak_pct >= TRAILING_ACTIVATION and pct_peak <= -TRAILING_PCT:
            ok, sig, usdc = await jupiter_sell(session, mint, qty)
            if ok:
                pnl = usdc - pos["amount_usdc"]
                del positions[mint]
                save_state()
                await send_tg(
                    f"MEME SCALPER — TRAILING STOP\n\n"
                    f"Token  : {symbol}\n"
                    f"Prix   : ${current:.6f}\n"
                    f"Recul  : {pct_peak:.1f}% depuis pic\n"
                    f"PnL    : {pnl:+.2f} USDC"
                )
            continue

        # Stop loss : -10%
        if pct_entry <= SL_PCT:
            ok, sig, usdc = await jupiter_sell(session, mint, qty)
            if ok:
                pnl = usdc - pos["amount_usdc"]
                del positions[mint]
                save_state()
                await send_tg(
                    f"MEME SCALPER — STOP LOSS {SL_PCT:.0f}%\n\n"
                    f"Token  : {symbol}\n"
                    f"Prix   : ${current:.6f}\n"
                    f"Perte  : {pnl:+.2f} USDC"
                )


# ─── Boucles principales ──────────────────────────────────────
async def scanner_loop():
    await asyncio.sleep(15)
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                for symbol, mint in TOKENS.items():
                    await check_entry(session, symbol, mint)
                    await asyncio.sleep(2)
        except Exception as e:
            logger.error(f"scanner_loop: {e}")
        await asyncio.sleep(SCAN_INTERVAL)


async def monitor_loop():
    await asyncio.sleep(30)
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                await check_exits(session)
        except Exception as e:
            logger.error(f"monitor_loop: {e}")
        await asyncio.sleep(MONITOR_INTERVAL)


async def watchdog_loop():
    while True:
        logger.info(f"[watchdog] meme_scalper alive — positions={len(positions)}")
        await asyncio.sleep(60)


# ─── Main ─────────────────────────────────────────────────────
async def main():
    load_state()
    kp = get_keypair()
    if kp:
        logger.info(f"Wallet : {kp.pubkey()}")
    else:
        logger.error("Keypair invalide — bot démarré sans capacité de trade")
    await send_tg("MEME SCALPER DÉMARRÉ — 10 tokens surveillés")
    await asyncio.gather(
        scanner_loop(),
        monitor_loop(),
        watchdog_loop(),
    )


if __name__ == "__main__":
    asyncio.run(main())
