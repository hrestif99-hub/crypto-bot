#!/usr/bin/env python3
"""
common.py — infrastructure partagée par les deux bots.

Contient :
  • Telegram          (notify)
  • Persistance JSON  (positions, paper wallet, cooldowns, trades)
  • Données marché    (DexScreener prix/paires, GeckoTerminal OHLCV, RugCheck)
  • Exécution Jupiter (quote/swap/buy/sell/smart_sell) — code éprouvé préservé
  • RiskState         (circuit breaker, série de pertes, perte journalière)

Aucun paramètre ici : tout vient de config.py.
"""

import asyncio
import json
import os
import base64
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import aiohttp
import requests

import config as C


# ════════════════════════════════════════════════════════════════════
#  TELEGRAM
# ════════════════════════════════════════════════════════════════════
async def send_tg(text: str):
    if C.PAPER_MODE:
        text = "PAPER | " + text
    if not C.TELEGRAM_TOKEN or not C.CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{C.TELEGRAM_TOKEN}/sendMessage"
    try:
        async with aiohttp.ClientSession() as s:
            await s.post(url, json={"chat_id": C.CHAT_ID, "text": text},
                         timeout=aiohttp.ClientTimeout(total=10))
    except Exception:
        pass


# ════════════════════════════════════════════════════════════════════
#  PERSISTANCE JSON
# ════════════════════════════════════════════════════════════════════
def ensure_data_dir():
    os.makedirs(C.DATA_DIR, exist_ok=True)


def load_json(path: str, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default() if callable(default) else default


def save_json(path: str, data):
    ensure_data_dir()
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp, path)


def new_paper_state():
    return {
        "balance_usdc":   C.PAPER_START_USDC,
        "total_invested": 0.0,
        "pnl_total":      0.0,
        "wins":           0,
        "losses":         0,
        "trades_closed":  [],
    }


def append_trade(path: str, record: dict):
    trades = load_json(path, list)
    if not isinstance(trades, list):
        trades = []
    trades.append(record)
    save_json(path, trades)


# ════════════════════════════════════════════════════════════════════
#  RISK STATE — circuit breaker / série / perte journalière
# ════════════════════════════════════════════════════════════════════
class RiskState:
    def __init__(self, logger):
        self.logger = logger
        self.consecutive_losses = 0
        self.circuit_breaker_until = None
        self.daily_pnl = 0.0
        self.day = datetime.now(timezone.utc).date()

    def _roll_day(self):
        today = datetime.now(timezone.utc).date()
        if today != self.day:
            self.day = today
            self.daily_pnl = 0.0

    def register(self, pnl: float):
        """À appeler après CHAQUE clôture complète de position."""
        self._roll_day()
        self.daily_pnl += pnl
        if pnl < 0:
            self.consecutive_losses += 1
            if self.consecutive_losses >= C.Risk.MAX_CONSECUTIVE_LOSSES:
                self.circuit_breaker_until = (
                    datetime.now(timezone.utc) + timedelta(minutes=C.Risk.CIRCUIT_BREAKER_MINUTES)
                )
                self.consecutive_losses = 0
                self.logger.warning(
                    f"[CIRCUIT BREAKER] {C.Risk.MAX_CONSECUTIVE_LOSSES} pertes — "
                    f"pause {C.Risk.CIRCUIT_BREAKER_MINUTES}min"
                )
                asyncio.create_task(send_tg(
                    f"CIRCUIT BREAKER\n{C.Risk.MAX_CONSECUTIVE_LOSSES} pertes consécutives\n"
                    f"Pause {C.Risk.CIRCUIT_BREAKER_MINUTES} min"
                ))
        else:
            self.consecutive_losses = 0

    def register_gain(self, gain: float):
        """Gain partiel (TP) : ne casse pas la série, compte dans le journalier."""
        self._roll_day()
        self.daily_pnl += gain
        if gain > 0:
            self.consecutive_losses = 0

    def blocked(self) -> bool:
        self._roll_day()
        if self.circuit_breaker_until and datetime.now(timezone.utc) < self.circuit_breaker_until:
            return True
        if self.daily_pnl <= C.Risk.DAILY_LOSS_LIMIT_USDC:
            return True
        return False

    def daily_limit_hit(self) -> bool:
        self._roll_day()
        return self.daily_pnl <= C.Risk.DAILY_LOSS_LIMIT_USDC


# ════════════════════════════════════════════════════════════════════
#  DONNÉES MARCHÉ
# ════════════════════════════════════════════════════════════════════
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
        pairs.sort(key=lambda x: float((x.get("liquidity") or {}).get("usd", 0) or 0), reverse=True)
        return pairs[0]
    except Exception:
        return None


async def get_price(session: aiohttp.ClientSession, mint: str) -> float:
    pair = await get_best_pair(session, mint)
    return float(pair.get("priceUsd", 0) or 0) if pair else 0.0


async def get_ohlcv(session: aiohttp.ClientSession, pool: str, limit: int = 50) -> list:
    url = f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{pool}/ohlcv/minute"
    try:
        async with session.get(
            url, params={"aggregate": 5, "limit": limit, "currency": "usd"},
            headers={"Accept": "application/json"},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as r:
            if r.status != 200:
                return []
            data = await r.json(content_type=None)
        rows = data.get("data", {}).get("attributes", {}).get("ohlcv_list", [])
        return sorted(rows, key=lambda x: x[0])
    except Exception:
        return []


async def rugcheck_safe(session: aiohttp.ClientSession, mint: str) -> bool:
    """True si le token passe RugCheck (ou si l'API est indisponible — fail-open léger)."""
    try:
        url = f"https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status != 200:
                return True
            data = await r.json(content_type=None)
        for risk in data.get("risks", []):
            if risk.get("level") == "critical":
                return False
        return data.get("score", 0) < 800
    except Exception:
        return True


# ════════════════════════════════════════════════════════════════════
#  KEYPAIR SOLANA
# ════════════════════════════════════════════════════════════════════
_keypair = None


def get_keypair():
    global _keypair
    if _keypair is not None:
        return _keypair
    if not C.SOLANA_PRIVATE_KEY:
        return None
    try:
        from solders.keypair import Keypair
        import base58 as b58
        _keypair = Keypair.from_bytes(b58.b58decode(C.SOLANA_PRIVATE_KEY))
        return _keypair
    except Exception:
        pass
    try:
        from solders.keypair import Keypair
        _keypair = Keypair.from_bytes(bytes(json.loads(C.SOLANA_PRIVATE_KEY)))
        return _keypair
    except Exception:
        return None


async def get_usdc_balance() -> float:
    kp = get_keypair()
    if not kp:
        return 0.0
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner",
        "params": [str(kp.pubkey()), {"mint": C.USDC_MINT}, {"encoding": "jsonParsed"}],
    }
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(C.HELIUS_RPC_URL, json=payload,
                              timeout=aiohttp.ClientTimeout(total=30)) as r:
                data = await r.json(content_type=None)
        total = 0.0
        for acct in data.get("result", {}).get("value") or []:
            ui = (acct.get("account", {}).get("data", {}).get("parsed", {})
                      .get("info", {}).get("tokenAmount", {}).get("uiAmount"))
            total += float(ui or 0)
        return total
    except Exception:
        return 0.0


# ════════════════════════════════════════════════════════════════════
#  JUPITER — quote / swap (code éprouvé préservé)
# ════════════════════════════════════════════════════════════════════
async def _jup_quote(session: aiohttp.ClientSession, url: str):
    for attempt in range(1, 4):
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                body = await r.text()
                if r.status == 200:
                    return json.loads(body)
                if "NO_ROUTES_FOUND" in body:
                    return "NO_ROUTES_FOUND"
                if r.status == 429:
                    return "RATE_LIMITED"
                return None
        except Exception:
            if attempt < 3:
                await asyncio.sleep(1)
    return None


async def _jup_swap(session: aiohttp.ClientSession, payload: dict):
    try:
        async with session.post(f"{C.JUPITER_BASE}/swap", json=payload,
                                timeout=aiohttp.ClientTimeout(total=20)) as r:
            if r.status == 200:
                return json.loads(await r.text())
    except Exception:
        pass
    return None


def _quote_url(input_mint: str, output_mint: str, amount: int, slippage: int) -> str:
    return f"{C.JUPITER_BASE}/quote?" + urlencode({
        "inputMint": input_mint, "outputMint": output_mint,
        "amount": amount, "slippageBps": slippage,
        "maxAccounts": 20, "restrictIntermediateTokens": "true",
    })


async def _sign_and_send(session: aiohttp.ClientSession, swap_tx_b64: str) -> str | None:
    """Refresh du blockhash + signature + envoi. Retourne la signature ou None."""
    kp = get_keypair()
    if not kp:
        return None
    from solders.transaction import VersionedTransaction
    from solders.message import MessageV0, Message as LegacyMessage
    from solders.hash import Hash
    from solana.rpc.async_api import AsyncClient

    tx_b64 = swap_tx_b64.replace('-', '+').replace('_', '/')
    raw = base64.b64decode(tx_b64 + '==')
    tx = VersionedTransaction.from_bytes(raw)

    bh_payload = {"jsonrpc": "2.0", "id": 1, "method": "getLatestBlockhash",
                  "params": [{"commitment": "finalized"}]}
    async with session.post(C.HELIUS_RPC_URL, json=bh_payload,
                            timeout=aiohttp.ClientTimeout(total=10)) as r:
        bh_data = await r.json(content_type=None)
    bh = Hash.from_string(bh_data["result"]["value"]["blockhash"])

    msg = tx.message
    if isinstance(msg, MessageV0):
        new_msg = MessageV0(msg.header, msg.account_keys, bh, msg.instructions, msg.address_table_lookups)
    else:
        new_msg = LegacyMessage(msg.header, msg.account_keys, bh, msg.instructions)
    signed = VersionedTransaction(new_msg, [kp])
    async with AsyncClient(C.HELIUS_RPC_URL) as client:
        result = await asyncio.wait_for(client.send_raw_transaction(bytes(signed)), timeout=30)
    return str(result.value)


# ════════════════════════════════════════════════════════════════════
#  BUY / SELL — avec branche paper mode
# ════════════════════════════════════════════════════════════════════
async def buy(session, mint: str, amount_usdc: float, paper_state: dict, logger) -> tuple:
    """Retourne (ok, sig, qty_raw)."""
    amount_raw = int(amount_usdc * 10 ** C.USDC_DECIMALS)
    quote = await _jup_quote(session, _quote_url(C.USDC_MINT, mint, amount_raw, C.SLIPPAGE_BPS))

    if C.PAPER_MODE:
        if isinstance(quote, dict) and "outAmount" in quote:
            qty_raw = int(quote["outAmount"])
        else:
            price = await get_price(session, mint)
            if price <= 0:
                return False, "paper: prix indisponible", 0
            qty_raw = int(amount_usdc / price * 1e6)
        paper_state["balance_usdc"]   -= amount_usdc
        paper_state["total_invested"] += amount_usdc
        logger.info(f"[PAPER] BUY {mint[:8]} qty={qty_raw} solde={paper_state['balance_usdc']:.2f}")
        return True, "paper_sig", qty_raw

    if C.DRY_RUN:
        return True, "dry_run", 1_000_000
    if not isinstance(quote, dict) or "outAmount" not in quote:
        return False, f"quote: {quote}", 0
    swap = await _jup_swap(session, {
        "quoteResponse": quote, "userPublicKey": str(get_keypair().pubkey()),
        "wrapAndUnwrapSol": True, "jitoTipLamports": C.JITO_TIP_LAMPORTS,
    })
    if not swap or "swapTransaction" not in swap:
        return False, "swap échoué", 0
    try:
        sig = await _sign_and_send(session, swap["swapTransaction"])
        if not sig:
            return False, "signature échouée", 0
        logger.info(f"BUY {mint[:8]} qty={quote.get('outAmount')} sig={sig[:12]}")
        return True, sig, int(quote.get("outAmount", 0))
    except Exception as e:
        return False, str(e), 0


async def sell(session, mint: str, qty_raw: int, pos: dict, paper_state: dict, logger) -> tuple:
    """Vente avec escalade de slippage. Retourne (ok, sig, usdc_reçu)."""
    if C.PAPER_MODE:
        quote = await _jup_quote(session, _quote_url(mint, C.USDC_MINT, qty_raw, C.SLIPPAGE_BPS))
        if isinstance(quote, dict) and "outAmount" in quote:
            usdc = int(quote["outAmount"]) / 10 ** C.USDC_DECIMALS
        else:
            entry   = pos.get("entry_price", 0)
            current = await get_price(session, mint)
            if entry <= 0 or current <= 0:
                return False, "paper: données manquantes", 0.0
            total_qty = pos.get("qty_raw", qty_raw) or qty_raw
            usdc = (qty_raw / total_qty) * pos.get("amount_usdc", 0) * (current / entry)
        paper_state["balance_usdc"] += usdc
        logger.info(f"[PAPER] SELL {mint[:8]} usdc=+{usdc:.2f} solde={paper_state['balance_usdc']:.2f}")
        return True, "paper_sig", usdc

    if C.DRY_RUN:
        return True, "dry_run", 2.0
    kp = get_keypair()
    if not kp or qty_raw <= 0:
        return False, "keypair/qty", 0.0

    for i, slip in enumerate([C.SLIPPAGE_BPS, 3000, 5000, 9900]):
        if i > 0:
            await asyncio.sleep(2)
        quote = await _jup_quote(session, _quote_url(mint, C.USDC_MINT, qty_raw, slip))
        if quote == "RATE_LIMITED":
            await asyncio.sleep(10)
            quote = await _jup_quote(session, _quote_url(mint, C.USDC_MINT, qty_raw, slip))
        if not isinstance(quote, dict) or "outAmount" not in quote:
            continue
        swap = await _jup_swap(session, {
            "quoteResponse": quote, "userPublicKey": str(kp.pubkey()),
            "wrapAndUnwrapSol": True, "jitoTipLamports": C.JITO_TIP_LAMPORTS,
        })
        if not swap or "swapTransaction" not in swap:
            continue
        try:
            sig = await _sign_and_send(session, swap["swapTransaction"])
            if not sig:
                continue
            usdc = int(quote.get("outAmount", 0)) / 10 ** C.USDC_DECIMALS
            logger.info(f"SELL {mint[:8]} usdc={usdc:.2f} slip={slip} sig={sig[:12]}")
            return True, sig, usdc
        except Exception:
            continue
    return False, "toutes tentatives échouées", 0.0


# ════════════════════════════════════════════════════════════════════
#  INDICATEURS (entrée meme_scalper)
# ════════════════════════════════════════════════════════════════════
def roc_15m(ohlcv: list) -> float:
    """Rate of change sur ~15 min (4 bougies de 5 min)."""
    try:
        if len(ohlcv) >= 4:
            now, ago = ohlcv[-1][4], ohlcv[-4][4]
            if ago > 0:
                return (now - ago) / ago * 100
    except Exception:
        pass
    return 0.0


def rel_volume(ohlcv: list) -> float:
    """Volume courant / moyenne récente."""
    try:
        if len(ohlcv) >= 7:
            vols = [c[5] for c in ohlcv[-48:]]
            ma = sum(vols) / len(vols) if vols else 1
            return ohlcv[-1][5] / ma if ma > 0 else 1.0
    except Exception:
        pass
    return 1.0


def vwap(ohlcv: list) -> float:
    try:
        if len(ohlcv) >= 10:
            tpv = sum(((c[2] + c[3] + c[4]) / 3) * c[5] for c in ohlcv[-48:])
            vol = sum(c[5] for c in ohlcv[-48:])
            return tpv / vol if vol > 0 else 0.0
    except Exception:
        pass
    return 0.0
