#!/usr/bin/env python3
"""Bot de trading autonome Solana memecoins — tourne en parallèle du bot Coinbase."""

import asyncio
import aiohttp
import os
import json
import logging
import re
import base64
import socket
from datetime import datetime, timedelta, timezone
from collections import defaultdict

# ─── Patch DNS (Railway / environnements sans résolution DNS système) ─────────
_orig_getaddrinfo = socket.getaddrinfo
def _patched_getaddrinfo(host, port, *args, **kwargs):
    try:
        return _orig_getaddrinfo(host, port, *args, **kwargs)
    except socket.gaierror:
        try:
            import dns.resolver
            r = dns.resolver.Resolver()
            r.nameservers = ["8.8.8.8", "1.1.1.1"]
            ip = str(r.resolve(host)[0])
            return _orig_getaddrinfo(ip, port, *args, **kwargs)
        except Exception:
            raise
socket.getaddrinfo = _patched_getaddrinfo

# ─── Configuration ────────────────────────────────────────────
SOLANA_PRIVATE_KEY  = os.environ.get("SOLANA_PRIVATE_KEY", "")
HELIUS_RPC_URL      = os.environ.get("HELIUS_RPC_URL", "https://api.mainnet-beta.solana.com")
TELEGRAM_API_ID     = os.environ.get("TELEGRAM_API_ID", "")
TELEGRAM_API_HASH   = os.environ.get("TELEGRAM_API_HASH", "")
TELEGRAM_TOKEN      = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID             = os.environ.get("CHAT_ID", "").strip()

USDC_MINT      = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDC_DECIMALS  = 6

TRADE_USDC      = 2.0
PYRAMID_USDC    = 1.0
MAX_POSITIONS   = 5
MAX_TOTAL_USDC  = 10.0
MIN_USDC        = 2.0
MAX_PYRAMIDS    = 3
PYRAMID_COOL    = 30 * 60   # 30 min
PYRAMID_TRIGGER = 30.0      # +30%
SLIPPAGE_BPS    = 1500      # 15%

# Endpoints Jupiter testés au démarrage — le premier qui répond est utilisé
_JUP_ENDPOINTS = [
    "https://api.jup.ag/swap/v1",
    "https://api.jup.ag/swap/v1",
]
_JUP_BASE: str = _JUP_ENDPOINTS[0]  # mis à jour par _init_jupiter_endpoint()
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"

STOP_LOSS_PCT   = -30.0
TP_HALF_PCT     = 50.0      # vendre 50% à +50%
TP_FULL_PCT     = 100.0     # vendre 100% à +100%
TRAILING_PCT    = 25.0      # trailing -25% depuis le pic (actif si pic >= +50%)

SCAN_INTERVAL    = 60
MONITOR_INTERVAL = 30
PYRAMID_INTERVAL = 300

CHANNELS         = ["pumping_sol", "solana_degens", "dexscreener_trending"]
SIGNAL_WINDOW    = 300   # 5 min
SIGNAL_THRESHOLD = 3     # 3 canaux distincts

SOL_ADDR_RE = re.compile(r'\b[1-9A-HJ-NP-Za-km-z]{32,44}\b')

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
logger = logging.getLogger("solana_bot")

# ─── État global ──────────────────────────────────────────────
positions    = {}   # mint -> dict
blacklist    = set()
tg_mentions  = defaultdict(lambda: defaultdict(list))  # mint -> channel -> [datetime]

POSITIONS_FILE = "solana_positions.json"
BLACKLIST_FILE  = "solana_blacklist.json"


def _load_state():
    global positions, blacklist
    if os.path.exists(POSITIONS_FILE):
        with open(POSITIONS_FILE) as f:
            positions = json.load(f)
    if os.path.exists(BLACKLIST_FILE):
        with open(BLACKLIST_FILE) as f:
            blacklist = set(json.load(f))


def _save_state():
    with open(POSITIONS_FILE, "w") as f:
        json.dump(positions, f, indent=2, default=str)
    with open(BLACKLIST_FILE, "w") as f:
        json.dump(list(blacklist), f)


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


# ─── Utilitaires HTTP ─────────────────────────────────────────
async def fetch_json(session: aiohttp.ClientSession, url: str, **kw):
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10), **kw) as r:
            if r.status == 200:
                return await r.json(content_type=None)
    except Exception as e:
        logger.debug(f"fetch_json {url[:60]}: {e}")
    return None


async def post_json(session: aiohttp.ClientSession, url: str, payload: dict):
    try:
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status == 200:
                return await r.json(content_type=None)
    except Exception as e:
        logger.debug(f"post_json {url[:60]}: {e}")
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
    pubkey_str = str(kp.pubkey())
    try:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTokenAccountsByOwner",
            "params": [
                pubkey_str,
                {"mint": USDC_MINT},
                {"encoding": "jsonParsed"},
            ],
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(
                HELIUS_RPC_URL, json=payload, timeout=aiohttp.ClientTimeout(total=30)
            ) as r:
                http_status = r.status
                data = await r.json(content_type=None)

        if http_status != 200:
            logger.error(f"get_usdc_balance: HTTP {http_status} — réponse: {data}")
            return 0.0

        rpc_error = data.get("error")
        if rpc_error:
            logger.error(f"get_usdc_balance: erreur RPC {rpc_error}")
            return 0.0

        accounts = data.get("result", {}).get("value") or []
        logger.debug(f"get_usdc_balance: wallet={pubkey_str[:8]}… {len(accounts)} compte(s) USDC trouvé(s)")

        if not accounts:
            logger.info(f"get_usdc_balance: aucun compte USDC pour {pubkey_str[:8]}… (wallet vide ou mauvaise clé ?)")
            return 0.0

        total = 0.0
        for acct in accounts:
            token_info = (
                acct.get("account", {})
                    .get("data", {})
                    .get("parsed", {})
                    .get("info", {})
            )
            token_amount = token_info.get("tokenAmount", {})
            ui_amount = token_amount.get("uiAmount")
            raw_amount = token_amount.get("amount", "0")
            logger.debug(
                f"  compte USDC mint={token_info.get('mint', '?')[:8]}… "
                f"uiAmount={ui_amount} rawAmount={raw_amount}"
            )
            total += float(ui_amount or 0)

        return total
    except asyncio.TimeoutError:
        logger.error("get_usdc_balance: timeout RPC Solana")
        return 0.0
    except Exception as e:
        logger.error(f"get_usdc_balance: {e}")
        return 0.0


# ─── DexScreener ─────────────────────────────────────────────
async def fetch_dexscreener_new(session: aiohttp.ClientSession) -> list[dict]:
    data = await fetch_json(session, "https://api.dexscreener.com/token-profiles/latest/v1")
    if not data:
        return []
    items = data if isinstance(data, list) else data.get("data", [])
    return [
        {"address": item.get("tokenAddress") or item.get("address"), "source": "dexscreener"}
        for item in items
        if item.get("chainId") == "solana" and (item.get("tokenAddress") or item.get("address"))
    ]


async def fetch_dexscreener_pair(session: aiohttp.ClientSession, mint: str):
    data = await fetch_json(session, f"https://api.dexscreener.com/latest/dex/tokens/{mint}")
    if not data:
        return None
    pairs = [p for p in (data.get("pairs") or []) if p.get("chainId") == "solana"]
    if not pairs:
        return None
    pairs.sort(key=lambda x: float((x.get("liquidity") or {}).get("usd", 0) or 0), reverse=True)
    return pairs[0]


# ─── Pump.fun ────────────────────────────────────────────────
async def fetch_pumpfun_new(session: aiohttp.ClientSession) -> list[dict]:
    url = "https://frontend-api.pump.fun/coins?offset=0&limit=50&sort=created_timestamp&order=DESC"
    data = await fetch_json(session, url)
    if not data:
        return []
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    result = []
    for item in (data if isinstance(data, list) else []):
        mint = item.get("mint")
        if not mint:
            continue
        ts = item.get("created_timestamp", 0)
        if ts:
            created = datetime.fromtimestamp(ts / 1000 if ts > 1e10 else ts, timezone.utc).replace(tzinfo=None)
            age_min = (now - created).total_seconds() / 60
        else:
            age_min = 999.0
        result.append({
            "address":    mint,
            "symbol":     item.get("symbol", "?"),
            "name":       item.get("name", "?"),
            "age_min":    age_min,
            "market_cap": float(item.get("usd_market_cap", 0) or 0),
            "source":     "pumpfun",
        })
    return result


# ─── GoPlus ──────────────────────────────────────────────────
async def check_goplus(session: aiohttp.ClientSession, mint: str):
    url = f"https://api.gopluslabs.io/api/v1/solana/token_security?contract_addresses={mint}"
    data = await fetch_json(session, url)
    if not data:
        return None
    result = data.get("result", {})
    return result.get(mint) or result.get(mint.lower())


def goplus_red_flags(gp: dict) -> list[str]:
    if not gp:
        return []
    flags = []
    if str(gp.get("is_honeypot", "0")) == "1":
        flags.append("honeypot")
    sell_tax = float(gp.get("sell_tax", 0) or 0)
    if sell_tax > 10:
        flags.append(f"sell_tax={sell_tax:.0f}%")
    if str(gp.get("is_mintable", "0")) == "1" or gp.get("mint_authority"):
        flags.append("mintable")
    top10 = float(gp.get("top_10_holder_rate", 0) or 0)
    if top10 > 0.5:
        flags.append(f"top10={top10*100:.0f}%")
    return flags


def goplus_minor_flags(gp: dict) -> int:
    if not gp:
        return 0
    count = 0
    if gp.get("freeze_authority"):
        count += 1
    if str(gp.get("can_take_back_ownership", "0")) == "1":
        count += 1
    return count


# ─── Scoring ─────────────────────────────────────────────────
def compute_score(pair, pumpfun: dict | None, gp, tg_bonus: bool) -> tuple[int, str]:
    score = 0
    parts = []

    liq = float((pair.get("liquidity") or {}).get("usd", 0) or 0) if pair else 0.0
    if liq > 50_000:   score += 20; parts.append(f"Liq ${liq:,.0f}(+20)")
    elif liq > 20_000: score += 15; parts.append(f"Liq ${liq:,.0f}(+15)")
    elif liq > 10_000: score += 10; parts.append(f"Liq ${liq:,.0f}(+10)")
    elif liq > 5_000:  score += 5;  parts.append(f"Liq ${liq:,.0f}(+5)")

    vol5  = float((pair.get("volume") or {}).get("m5", 0) or 0) if pair else 0.0
    mcap  = float(pair.get("marketCap", 0) or pair.get("fdv", 0) or 0) if pair else 0.0
    if mcap == 0 and pumpfun:
        mcap = pumpfun.get("market_cap", 0)
    ratio = (vol5 / mcap * 100) if mcap > 0 else 0
    if ratio > 50:   score += 20; parts.append(f"Vol/MCap {ratio:.0f}%(+20)")
    elif ratio > 30: score += 15; parts.append(f"Vol/MCap {ratio:.0f}%(+15)")
    elif ratio > 10: score += 10; parts.append(f"Vol/MCap {ratio:.0f}%(+10)")

    age_min = pumpfun.get("age_min", 999) if pumpfun else 999.0
    if pair and age_min == 999:
        cat = pair.get("pairCreatedAt", 0)
        if cat:
            age_min = (datetime.now(timezone.utc).replace(tzinfo=None) - datetime.fromtimestamp(cat / 1000, timezone.utc).replace(tzinfo=None)).total_seconds() / 60
    if age_min < 2:    score += 15; parts.append(f"Age {age_min:.1f}min(+15)")
    elif age_min < 5:  score += 10; parts.append(f"Age {age_min:.1f}min(+10)")
    elif age_min < 10: score += 5;  parts.append(f"Age {age_min:.1f}min(+5)")

    holders = int((gp or {}).get("holder_count", 0) or 0)
    if holders > 500:   score += 15; parts.append(f"Holders {holders}(+15)")
    elif holders > 200: score += 10; parts.append(f"Holders {holders}(+10)")
    elif holders > 100: score += 5;  parts.append(f"Holders {holders}(+5)")

    price5 = float((pair.get("priceChange") or {}).get("m5", 0) or 0) if pair else 0.0
    if price5 > 20:   score += 15; parts.append(f"+{price5:.0f}%/5min(+15)")
    elif price5 > 10: score += 10; parts.append(f"+{price5:.0f}%/5min(+10)")
    elif price5 > 5:  score += 5;  parts.append(f"+{price5:.0f}%/5min(+5)")

    if gp is None:
        score += 5; parts.append("GoPlus N/A(+5)")
    elif goplus_minor_flags(gp) == 0:
        score += 15; parts.append("GoPlus clean(+15)")
    else:
        score += 5; parts.append("GoPlus mineur(+5)")

    if tg_bonus:
        score += 15; parts.append("TG 3 canaux(+15)")

    return score, " | ".join(parts)


# ─── Jupiter : quote avec log complet en cas d'erreur ─────────
async def _jup_quote(session: aiohttp.ClientSession, url: str) -> dict | None:
    for attempt in range(1, 4):
        try:
            logger.info(f"Jupiter quote tentative {attempt}/3…")
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                body = await r.text()
                if r.status == 200:
                    return json.loads(body)
                # Erreur HTTP applicative → pas de retry (400 = mint invalide, etc.)
                logger.error(
                    f"Jupiter quote HTTP {r.status}\n"
                    f"  URL  : {url}\n"
                    f"  Body : {body[:800]}"
                )
                return None
        except Exception as e:
            logger.error(f"Jupiter quote tentative {attempt}/3 échouée: {e}")
            if attempt < 3:
                await asyncio.sleep(1)
    logger.error("Jupiter inaccessible après 3 tentatives")
    return None


async def _jup_swap(session: aiohttp.ClientSession, payload: dict) -> dict | None:
    url = f"{_JUP_BASE}/swap"
    try:
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=20)) as r:
            body = await r.text()
            if r.status == 200:
                return json.loads(body)
            logger.error(
                f"Jupiter swap HTTP {r.status}\n"
                f"  Body : {body[:800]}"
            )
            return None
    except Exception as e:
        logger.error(f"Jupiter swap exception: {e}")
        return None


# ─── Pump.fun fallback buy ────────────────────────────────────
async def pump_buy(session: aiohttp.ClientSession, mint: str, amount_usdc: float) -> tuple[bool, str, int]:
    """Achète via PumpPortal quand Jupiter n'a pas de route. Retourne (success, sig, qty_raw)."""
    if DRY_RUN:
        logger.info(f"[DRY RUN] Pump.fun achat simulé : {amount_usdc} USDC de {mint[:8]}")
        return True, "dry_run_sig", 1000000
    kp = get_keypair()
    if not kp:
        return False, "keypair manquant", 0
    try:
        async with session.post(
            "https://pumpportal.fun/api/trade",
            json={
                "action":            "buy",
                "mint":              mint,
                "amount":            amount_usdc,
                "denominatedInSol":  "false",
                "slippage":          15,
                "priorityFee":       0.005,
                "pool":              "pump",
            },
            timeout=aiohttp.ClientTimeout(total=20),
        ) as r:
            body = await r.text()
            if r.status != 200:
                logger.error(f"pump_buy: HTTP {r.status} — {body[:400]}")
                return False, f"pump HTTP {r.status}", 0
            data = json.loads(body)
            if "transaction" not in data:
                logger.error(f"pump_buy: pas de transaction dans la réponse — {body[:400]}")
                return False, "pump: transaction absente", 0
        from solders.transaction import VersionedTransaction
        from solana.rpc.async_api import AsyncClient
        raw    = base64.b64decode(data["transaction"])
        tx     = VersionedTransaction.from_bytes(raw)
        signed = VersionedTransaction(tx.message, [kp])
        async with AsyncClient(HELIUS_RPC_URL) as client:
            result = await asyncio.wait_for(
                client.send_raw_transaction(bytes(signed)), timeout=30
            )
        sig = str(result.value)
        logger.info(f"pump_buy {mint[:8]} sig={sig[:12]}")
        return True, sig, 1000000  # qty_raw inconnu depuis pumpportal
    except asyncio.TimeoutError:
        logger.error(f"pump_buy: timeout ({mint[:8]})")
        return False, "timeout", 0
    except Exception as e:
        logger.error(f"pump_buy: {e}")
        return False, str(e), 0


# ─── Jupiter buy/sell ─────────────────────────────────────────
async def jupiter_buy(session: aiohttp.ClientSession, mint: str, amount_usdc: float) -> tuple[bool, str, int]:
    """Achète via USDC. Retourne (success, sig, quantity_raw)."""
    if DRY_RUN:
        logger.info(f"[DRY RUN] Achat simulé : {amount_usdc} USDC de {mint[:8]}")
        return True, "dry_run_sig", 1000000
    kp = get_keypair()
    if not kp:
        return False, "keypair manquant", 0
    amount_raw = int(amount_usdc * 10**USDC_DECIMALS)  # 2.0 USDC → 2_000_000
    quote_url = (
        f"{_JUP_BASE}/quote"
        f"?inputMint={USDC_MINT}&outputMint={mint}"
        f"&amount={amount_raw}&slippageBps={SLIPPAGE_BPS}&maxAccounts=20"
    )
    logger.info(f"jupiter_buy: quote {mint[:8]}… amount_raw={amount_raw} ({amount_usdc} USDC) slippage={SLIPPAGE_BPS}bps")
    quote = await _jup_quote(session, quote_url)
    if not quote:
        return False, "quote échoué (voir logs)", 0
    if "error" in quote:
        if "NO_ROUTES_FOUND" in str(quote.get("error", "")):
            logger.warning(f"jupiter_buy: NO_ROUTES_FOUND pour {mint[:8]}, fallback pump.fun")
            return await pump_buy(session, mint, amount_usdc)
        logger.error(f"jupiter_buy: erreur Jupiter quote: {quote['error']}")
        return False, f"quote error: {quote['error']}", 0
    swap = await _jup_swap(session, {
        "quoteResponse":              quote,
        "userPublicKey":              str(kp.pubkey()),
        "wrapAndUnwrapSol":           True,
        "dynamicComputeUnitLimit":    True,
        "prioritizationFeeLamports":  "auto",
    })
    if not swap or "swapTransaction" not in swap:
        logger.error(f"jupiter_buy: swapTransaction absent — réponse: {swap}")
        return False, "swap échoué", 0
    try:
        from solders.transaction import VersionedTransaction
        from solana.rpc.async_api import AsyncClient
        raw     = base64.b64decode(swap["swapTransaction"])
        tx      = VersionedTransaction.from_bytes(raw)
        signed  = VersionedTransaction(tx.message, [kp])
        async with AsyncClient(HELIUS_RPC_URL) as client:
            result = await asyncio.wait_for(
                client.send_raw_transaction(bytes(signed)), timeout=30
            )
        sig = str(result.value)
        qty_raw = int(quote.get("outAmount", 0))
        logger.info(f"BUY {mint[:8]} qty_raw={qty_raw} sig={sig[:12]}")
        return True, sig, qty_raw
    except asyncio.TimeoutError:
        logger.error(f"jupiter_buy: timeout RPC Solana ({mint[:8]})")
        return False, "timeout", 0
    except Exception as e:
        logger.error(f"jupiter_buy sign/send: {e}")
        return False, str(e), 0


async def jupiter_sell(session: aiohttp.ClientSession, mint: str, qty_raw: int) -> tuple[bool, str, float]:
    """Vend qty_raw tokens. Retourne (success, sig, usdc_received)."""
    if DRY_RUN:
        amount_usdc = qty_raw / 10**6
        logger.info(f"[DRY RUN] Vente simulée : {qty_raw} tokens de {mint[:8]}")
        return True, "dry_run_sig", amount_usdc
    kp = get_keypair()
    if not kp or qty_raw <= 0:
        return False, "keypair/qty manquant", 0.0
    quote_url = (
        f"{_JUP_BASE}/quote"
        f"?inputMint={mint}&outputMint={USDC_MINT}"
        f"&amount={qty_raw}&slippageBps={SLIPPAGE_BPS}&maxAccounts=20"
    )
    logger.info(f"jupiter_sell: quote {mint[:8]}… qty_raw={qty_raw} slippage={SLIPPAGE_BPS}bps")
    quote = await _jup_quote(session, quote_url)
    if not quote:
        return False, "quote vente échoué (voir logs)", 0.0
    if "error" in quote:
        logger.error(f"jupiter_sell: erreur Jupiter quote: {quote['error']}")
        return False, f"quote error: {quote['error']}", 0.0
    swap = await _jup_swap(session, {
        "quoteResponse":              quote,
        "userPublicKey":              str(kp.pubkey()),
        "wrapAndUnwrapSol":           True,
        "dynamicComputeUnitLimit":    True,
        "prioritizationFeeLamports":  "auto",
    })
    if not swap or "swapTransaction" not in swap:
        logger.error(f"jupiter_sell: swapTransaction absent — réponse: {swap}")
        return False, "swap vente échoué", 0.0
    try:
        from solders.transaction import VersionedTransaction
        from solana.rpc.async_api import AsyncClient
        raw    = base64.b64decode(swap["swapTransaction"])
        tx     = VersionedTransaction.from_bytes(raw)
        signed = VersionedTransaction(tx.message, [kp])
        async with AsyncClient(HELIUS_RPC_URL) as client:
            result = await asyncio.wait_for(
                client.send_raw_transaction(bytes(signed)), timeout=30
            )
        sig = str(result.value)
        usdc = int(quote.get("outAmount", 0)) / 10**USDC_DECIMALS
        logger.info(f"SELL {mint[:8]} usdc={usdc:.2f} sig={sig[:12]}")
        return True, sig, usdc
    except asyncio.TimeoutError:
        logger.error(f"jupiter_sell: timeout RPC Solana ({mint[:8]})")
        return False, "timeout", 0.0
    except Exception as e:
        logger.error(f"jupiter_sell sign/send: {e}")
        return False, str(e), 0.0


# ─── Prix actuel ─────────────────────────────────────────────
async def get_token_price_usd(session: aiohttp.ClientSession, mint: str) -> float:
    pair = await fetch_dexscreener_pair(session, mint)
    if not pair:
        return 0.0
    return float(pair.get("priceUsd", 0) or 0)


# ─── Gestion de position ──────────────────────────────────────
def open_pos(mint: str, symbol: str, amount_usdc: float, entry_price: float,
             qty_raw: int, score: int, reasons: str, sig: str):
    positions[mint] = {
        "mint":          mint,
        "symbol":        symbol,
        "amount_usdc":   amount_usdc,
        "entry_price":   entry_price,
        "qty_raw":       qty_raw,
        "peak_price":    entry_price,
        "half_sold":     False,
        "pyramid_count": 0,
        "last_pyramid":  None,
        "score":         score,
        "reasons":       reasons,
        "sig":           sig,
        "opened_at":     datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
    }
    _save_state()


def close_pos(mint: str):
    if mint in positions:
        del positions[mint]
    blacklist.add(mint)
    _save_state()


async def check_position(session: aiohttp.ClientSession, mint: str):
    pos = positions.get(mint)
    if not pos:
        return
    current = await get_token_price_usd(session, mint)
    if current <= 0:
        return

    entry  = pos["entry_price"]
    peak   = pos["peak_price"]
    qty    = pos["qty_raw"]
    symbol = pos["symbol"]
    pct_e  = (current - entry) / entry * 100
    pct_p  = (current - peak) / peak * 100

    if current > peak:
        positions[mint]["peak_price"] = current
        _save_state()
        peak = current

    # Take profit partiel +50% → vendre 50%
    if not pos["half_sold"] and pct_e >= TP_HALF_PCT:
        half = qty // 2
        ok, sig, usdc = await jupiter_sell(session, mint, half)
        if ok:
            pnl = usdc - pos["amount_usdc"] * 0.5
            positions[mint]["half_sold"]  = True
            positions[mint]["qty_raw"]   -= half
            positions[mint]["amount_usdc"] *= 0.5
            _save_state()
            await send_tg(
                f"SOLANA VENTE 50%\n\n"
                f"Token  : {symbol}\n"
                f"Raison : Take profit +{TP_HALF_PCT:.0f}%\n"
                f"USDC   : {usdc:.2f}\n"
                f"P&L    : {pnl:+.2f} USDC"
            )
        return

    # Take profit total +100%
    if pct_e >= TP_FULL_PCT:
        ok, sig, usdc = await jupiter_sell(session, mint, qty)
        if ok:
            pnl = usdc - pos["amount_usdc"]
            await send_tg(
                f"SOLANA VENTE TOTALE\n\n"
                f"Token  : {symbol}\n"
                f"Raison : Take profit +{TP_FULL_PCT:.0f}%\n"
                f"USDC   : {usdc:.2f}\n"
                f"P&L    : {pnl:+.2f} USDC"
            )
            close_pos(mint)
        return

    # Trailing stop −25% depuis le pic (actif seulement si pic a dépassé +50%)
    peak_pct_entry = (peak - entry) / entry * 100
    if peak_pct_entry >= TP_HALF_PCT and pct_p <= -TRAILING_PCT:
        ok, sig, usdc = await jupiter_sell(session, mint, qty)
        if ok:
            pnl = usdc - pos["amount_usdc"]
            await send_tg(
                f"SOLANA TRAILING STOP\n\n"
                f"Token  : {symbol}\n"
                f"Raison : Trailing -{TRAILING_PCT:.0f}% depuis pic\n"
                f"USDC   : {usdc:.2f}\n"
                f"P&L    : {pnl:+.2f} USDC"
            )
            close_pos(mint)
        return

    # Stop loss −30%
    if pct_e <= STOP_LOSS_PCT:
        ok, sig, usdc = await jupiter_sell(session, mint, qty)
        if ok:
            pnl = usdc - pos["amount_usdc"]
            await send_tg(
                f"SOLANA STOP LOSS\n\n"
                f"Token  : {symbol}\n"
                f"Raison : Stop loss {STOP_LOSS_PCT:.0f}%\n"
                f"USDC   : {usdc:.2f}\n"
                f"P&L    : {pnl:+.2f} USDC"
            )
            close_pos(mint)


# ─── Pyramiding ───────────────────────────────────────────────
async def check_pyramid(session: aiohttp.ClientSession, mint: str):
    pos = positions.get(mint)
    if not pos or pos.get("pyramid_count", 0) >= MAX_PYRAMIDS:
        return
    last = pos.get("last_pyramid")
    if last:
        elapsed = (datetime.now(timezone.utc).replace(tzinfo=None) - datetime.fromisoformat(last)).total_seconds()
        if elapsed < PYRAMID_COOL:
            return
    current = await get_token_price_usd(session, mint)
    if current <= 0:
        return
    pct = (current - pos["entry_price"]) / pos["entry_price"] * 100
    if pct < PYRAMID_TRIGGER:
        return
    usdc_bal = await get_usdc_balance()
    if usdc_bal < PYRAMID_USDC:
        return
    ok, sig, qty_raw = await jupiter_buy(session, mint, PYRAMID_USDC)
    if ok:
        n = pos.get("pyramid_count", 0) + 1
        positions[mint]["qty_raw"]       += qty_raw
        positions[mint]["amount_usdc"]   += PYRAMID_USDC
        positions[mint]["pyramid_count"]  = n
        positions[mint]["last_pyramid"]   = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        _save_state()
        await send_tg(
            f"SOLANA PYRAMIDING\n\n"
            f"Token  : {pos['symbol']}\n"
            f"+{pct:.1f}% depuis entrée\n"
            f"Ajout  : {PYRAMID_USDC} USDC (pyramide {n}/{MAX_PYRAMIDS})"
        )


# ─── Signal Telegram ──────────────────────────────────────────
def has_tg_signal(mint: str) -> bool:
    cutoff   = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=SIGNAL_WINDOW)
    channels = {ch for ch, ts_list in tg_mentions[mint].items()
                if any(t > cutoff for t in ts_list)}
    return len(channels) >= SIGNAL_THRESHOLD


# ─── Traitement d'un token candidat ─────────────────────────
async def process_token(session: aiohttp.ClientSession, mint: str,
                        symbol: str, pumpfun: dict | None):
    if mint in blacklist or mint in positions:
        return

    tag = f"{symbol} ({mint[:8]}…)"
    pf_age = pumpfun.get("age_min", 999) if pumpfun else 999.0

    pair = await fetch_dexscreener_pair(session, mint)
    liq  = float((pair.get("liquidity") or {}).get("usd", 0) or 0) if pair else 0.0
    vol5 = float((pair.get("volume") or {}).get("m5", 0) or 0) if pair else 0.0

    age_min = pf_age
    if pair and age_min == 999:
        cat = pair.get("pairCreatedAt", 0)
        if cat:
            age_min = (datetime.now(timezone.utc).replace(tzinfo=None) - datetime.fromtimestamp(cat / 1000, timezone.utc).replace(tzinfo=None)).total_seconds() / 60

    # Filtres d'entrée — loggés individuellement pour diagnostic
    if age_min > 10:
        logger.debug(f"[rejet] {tag} trop vieux ({age_min:.1f}min > 10min)")
        blacklist.add(mint); _save_state(); return
    if liq < 5_000:
        logger.debug(f"[rejet] {tag} liquidité trop basse (${liq:,.0f} < $5 000)")
        blacklist.add(mint); _save_state(); return
    if vol5 < 1_000 and not has_tg_signal(mint):
        logger.debug(f"[rejet] {tag} volume 5min trop bas (${vol5:,.0f} < $1 000, pas de signal TG)")
        blacklist.add(mint); _save_state(); return

    logger.info(f"[candidat] {tag} age={age_min:.1f}min liq=${liq:,.0f} vol5m=${vol5:,.0f}")

    gp = await check_goplus(session, mint)
    if gp:
        flags = goplus_red_flags(gp)
        if flags:
            logger.info(f"[rejet] {tag} GoPlus flags={flags}")
            blacklist.add(mint); _save_state(); return

    tg_bonus = has_tg_signal(mint)
    score, reasons = compute_score(pair, pumpfun, gp, tg_bonus)
    logger.info(f"[score] {tag} {score}/100 — {reasons}")

    if score < 65:
        logger.info(f"[rejet] {tag} score insuffisant ({score}/100 < 65)")
        blacklist.add(mint); _save_state(); return

    if len(positions) >= MAX_POSITIONS:
        logger.info(f"[rejet] {tag} max positions atteint ({MAX_POSITIONS})")
        return
    exposed = sum(p["amount_usdc"] for p in positions.values())
    if exposed + TRADE_USDC > MAX_TOTAL_USDC:
        logger.info(f"[rejet] {tag} exposition max atteinte ({exposed:.2f}+{TRADE_USDC} > {MAX_TOTAL_USDC} USDC)")
        return

    usdc_bal = await get_usdc_balance()
    if usdc_bal < MIN_USDC:
        logger.info(f"[rejet] {tag} solde USDC insuffisant ({usdc_bal:.2f} < {MIN_USDC})")
        await send_tg(f"SOLANA BOT — Solde USDC bas ({usdc_bal:.2f}). Achats suspendus.")
        return

    logger.info(f"[achat] {tag} score={score}/100 solde={usdc_bal:.2f} USDC → achat {TRADE_USDC} USDC")
    ok, sig, qty_raw = await jupiter_buy(session, mint, TRADE_USDC)
    if not ok or qty_raw == 0:
        logger.error(f"[achat échoué] {tag} sig={sig}")
        blacklist.add(mint); _save_state(); return

    entry_price = float((pair or {}).get("priceUsd", 0) or 0)
    if entry_price <= 0:
        entry_price = TRADE_USDC / (qty_raw / 10**6) if qty_raw > 0 else 0
    open_pos(mint, symbol, TRADE_USDC, entry_price, qty_raw, score, reasons, sig)

    await send_tg(
        f"SOLANA ACHAT\n\n"
        f"Token   : {symbol}\n"
        f"Adresse : {mint}\n"
        f"Montant : {TRADE_USDC} USDC\n"
        f"Score   : {score}/100\n"
        f"Raisons : {reasons}"
    )


# ─── Boucle scanner ───────────────────────────────────────────
async def scanner_loop():
    await asyncio.sleep(15)
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                pf_tokens = await fetch_pumpfun_new(session)
                ds_tokens = await fetch_dexscreener_new(session)

                seen = set()
                for tok in pf_tokens:
                    mint = tok["address"]
                    if mint not in seen:
                        seen.add(mint)
                        await process_token(session, mint, tok.get("symbol", "?"), tok)
                        await asyncio.sleep(0.5)

                for tok in ds_tokens:
                    mint = tok["address"]
                    if mint not in seen:
                        seen.add(mint)
                        await process_token(session, mint, "?", None)
                        await asyncio.sleep(0.5)
        except Exception as e:
            logger.error(f"scanner_loop: {e}")
        await asyncio.sleep(SCAN_INTERVAL)


# ─── Boucle monitoring des positions ─────────────────────────
async def monitor_loop():
    await asyncio.sleep(30)
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                for mint in list(positions.keys()):
                    await check_position(session, mint)
                    await asyncio.sleep(1)
        except Exception as e:
            logger.error(f"monitor_loop: {e}")
        await asyncio.sleep(MONITOR_INTERVAL)


# ─── Boucle pyramiding ────────────────────────────────────────
async def pyramid_loop():
    await asyncio.sleep(60)
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                for mint in list(positions.keys()):
                    await check_pyramid(session, mint)
                    await asyncio.sleep(1)
        except Exception as e:
            logger.error(f"pyramid_loop: {e}")
        await asyncio.sleep(PYRAMID_INTERVAL)


# ─── Boucle Telethon ─────────────────────────────────────────
async def telethon_loop():
    if not TELEGRAM_API_ID or not TELEGRAM_API_HASH:
        logger.info("Telethon désactivé (variables manquantes)")
        return
    if not os.path.exists("solana_bot_session.session"):
        logger.warning(
            "Telethon : pas de session. Lancez `python solana_bot.py --auth` "
            "en local pour vous authentifier."
        )
        return
    from telethon import TelegramClient, events
    from telethon.errors import UsernameNotOccupiedError, UsernameInvalidError

    while True:  # boucle de reconnexion automatique
        try:
            client = TelegramClient("solana_bot_session", int(TELEGRAM_API_ID), TELEGRAM_API_HASH)
            await asyncio.wait_for(client.start(), timeout=30)

            # Résoudre chaque canal individuellement avec timeout
            valid_entities = []
            for ch in CHANNELS:
                try:
                    entity = await asyncio.wait_for(client.get_entity(ch), timeout=30)
                    valid_entities.append(entity)
                    logger.info(f"Telethon : canal @{ch} résolu")
                except asyncio.TimeoutError:
                    logger.warning(f"Telethon : timeout résolution @{ch}, ignoré")
                except (UsernameNotOccupiedError, UsernameInvalidError, ValueError) as e:
                    logger.warning(f"Telethon : canal @{ch} introuvable, ignoré ({e})")
                except Exception as e:
                    logger.warning(f"Telethon : erreur résolution @{ch}, ignoré ({e})")

            if not valid_entities:
                logger.warning("Telethon : aucun canal valide — scraping désactivé")
                await client.disconnect()
                return

            @client.on(events.NewMessage(chats=valid_entities))
            async def on_msg(event):
                try:
                    text    = event.raw_text or ""
                    channel = getattr(event.chat, "username", "unknown")
                    for addr in SOL_ADDR_RE.findall(text):
                        if 32 <= len(addr) <= 44:
                            tg_mentions[addr][channel].append(datetime.now(timezone.utc).replace(tzinfo=None))
                            logger.debug(f"Signal TG {addr[:8]} @{channel}")
                except Exception as e:
                    logger.error(f"on_msg handler: {e}")

            logger.info(f"Telethon connecté — écoute {len(valid_entities)} canaux")
            await client.run_until_disconnected()
            logger.warning("Telethon : déconnecté — reconnexion dans 30s")
        except asyncio.TimeoutError:
            logger.error("Telethon : timeout connexion/start — reconnexion dans 30s")
        except Exception as e:
            logger.error(f"telethon_loop: {e} — reconnexion dans 30s")
        await asyncio.sleep(30)


# ─── Watchdog ────────────────────────────────────────────────
async def watchdog_loop():
    while True:
        logger.info(
            f"[watchdog] bot alive — positions={len(positions)} blacklist={len(blacklist)}"
        )
        await asyncio.sleep(60)


# ─── Auth locale (one-shot) ───────────────────────────────────
async def auth_telethon():
    """Lance l'authentification Telethon interactive (à faire une seule fois en local)."""
    from telethon import TelegramClient
    client = TelegramClient("solana_bot_session", int(TELEGRAM_API_ID), TELEGRAM_API_HASH)
    await client.start()
    print(f"Authentifié : {await client.get_me()}")
    await client.disconnect()


# ─── Sélection de l'endpoint Jupiter au démarrage ────────────
async def _init_jupiter_endpoint():
    global _JUP_BASE
    test_url = (
        "https://api.jup.ag/swap/v1/quote"
        "?inputMint=EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
        "&outputMint=So11111111111111111111111111111111111111112"
        "&amount=1000000&slippageBps=50"
    )
    async with aiohttp.ClientSession() as session:
        for base in _JUP_ENDPOINTS:
            try:
                async with session.get(
                    test_url, timeout=aiohttp.ClientTimeout(total=10)
                ) as r:
                    if r.status == 200:
                        data = await r.json()
                        if "outAmount" in data:
                            _JUP_BASE = base
                            logger.info(f"Jupiter endpoint sélectionné : {base} ✓")
                            return
                    logger.warning(f"Jupiter {base} : HTTP {r.status}, essai suivant…")
            except Exception as e:
                logger.warning(f"Jupiter {base} inaccessible : {e}, essai suivant…")
    logger.error(
        f"Jupiter inaccessible sur tous les endpoints {_JUP_ENDPOINTS}\n"
        f"  → Les achats/ventes seront impossibles jusqu'à rétablissement DNS"
    )


# ─── Main ─────────────────────────────────────────────────────
async def main():
    _load_state()
    kp = get_keypair()
    if not kp:
        logger.error("Keypair Solana invalide — bot démarré sans capacité de trade")
    else:
        logger.info(f"Wallet : {kp.pubkey()}")

    await _init_jupiter_endpoint()
    await send_tg("SOLANA MEMECOIN BOT DÉMARRÉ\nScan DexScreener + Pump.fun actif")

    await asyncio.gather(
        scanner_loop(),
        monitor_loop(),
        pyramid_loop(),
        telethon_loop(),
        watchdog_loop(),
    )


if __name__ == "__main__":
    import sys
    if "--auth" in sys.argv:
        asyncio.run(auth_telethon())
    else:
        asyncio.run(main())
