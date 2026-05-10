import time
import json
import aiohttp
import logging
import jwt
from cryptography.hazmat.primitives.serialization import load_pem_private_key

logger = logging.getLogger(__name__)

COINBASE_API_URL = "https://api.coinbase.com"


def get_jwt_token(api_key_name, private_key_pem, method, path):
    """Genere un token JWT pour l API Coinbase Advanced."""
    uri = f"{method} api.coinbase.com{path}"
    pem_clean = private_key_pem.replace("\\n", "\n")
    private_key = load_pem_private_key(pem_clean.encode("utf-8"), password=None)
    payload = {
        "sub": api_key_name,
        "iss": "cdp",
        "nbf": int(time.time()),
        "exp": int(time.time()) + 120,
        "uri": uri,
    }
    token = jwt.encode(
        payload,
        private_key,
        algorithm="ES256",
        headers={"kid": api_key_name, "nonce": str(int(time.time() * 1000))}
    )
    return token


def get_headers(api_key_name, private_key_pem, method, path):
    token = get_jwt_token(api_key_name, private_key_pem, method, path)
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def _parse_json(raw: bytes, context: str) -> dict | list | None:
    """Parse les bytes JSON et logue le corps brut en cas d'erreur."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        preview = raw[:300].decode("utf-8", errors="replace")
        logger.error(f"[{context}] JSON invalide : {e} — body: {preview}")
        return None


async def get_accounts(api_key, api_secret):
    path = "/api/v3/brokerage/accounts"
    headers = get_headers(api_key, api_secret, "GET", path)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                COINBASE_API_URL + path,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as r:
                raw = await r.read()
                logger.debug(f"[get_accounts] HTTP {r.status}, {len(raw)} octets")
                data = _parse_json(raw, f"get_accounts HTTP {r.status}")
                if data is None:
                    return []
                return data.get("accounts", [])
    except Exception as e:
        logger.error(f"[get_accounts] Erreur réseau : {e}")
        return []


async def get_portfolio(api_key, api_secret):
    accounts = await get_accounts(api_key, api_secret)
    portfolio = {}
    for acc in accounts:
        balance = float(acc.get("available_balance", {}).get("value", 0))
        currency = acc.get("currency", "")
        if balance > 0 and currency:
            portfolio[currency] = balance
    return portfolio


async def get_product_price(product_id, api_key=None, api_secret=None):
    path = f"/api/v3/brokerage/products/{product_id}"
    headers = get_headers(api_key, api_secret, "GET", path) if api_key else {}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                COINBASE_API_URL + path,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as r:
                raw = await r.read()
                logger.debug(f"[get_product_price] {product_id} HTTP {r.status}, {len(raw)} octets")
                data = _parse_json(raw, f"get_product_price {product_id} HTTP {r.status}")
                if data is None:
                    return None
                price = data.get("price")
                if not price:
                    return None
                price_float = float(price)
                return price_float if price_float > 0 else None
    except Exception as e:
        logger.error(f"[get_product_price] {product_id} erreur réseau : {e}")
        return None


async def get_available_products(api_key=None, api_secret=None):
    path = "/api/v3/brokerage/products"
    params = "?quote_currency_id=EUR&product_type=SPOT"
    headers = get_headers(api_key, api_secret, "GET", path) if api_key else {}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                COINBASE_API_URL + path + params,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15)
            ) as r:
                raw = await r.read()
                logger.info(f"[get_available_products] HTTP {r.status}, {len(raw)} octets reçus")
                data = _parse_json(raw, f"get_available_products HTTP {r.status}")
                if data is None:
                    return []
                products = data.get("products", [])
                result = [
                    p for p in products
                    if not p.get("is_disabled", True)
                    and p.get("status") == "online"
                ]
                logger.info(f"[get_available_products] {len(result)} produits actifs sur {len(products)} total")
                return result
    except Exception as e:
        logger.error(f"[get_available_products] Erreur réseau : {e}")
        return []


async def place_market_buy(api_key, api_secret, product_id, amount_eur):
    path = "/api/v3/brokerage/orders"
    order = {
        "client_order_id": f"bot_{int(time.time())}",
        "product_id": product_id,
        "side": "BUY",
        "order_configuration": {
            "market_market_ioc": {
                "quote_size": str(round(amount_eur, 2))
            }
        }
    }
    body = json.dumps(order)
    headers = get_headers(api_key, api_secret, "POST", path)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                COINBASE_API_URL + path,
                headers=headers,
                data=body,
                timeout=aiohttp.ClientTimeout(total=15)
            ) as r:
                raw = await r.read()
                logger.info(f"[place_market_buy] {product_id} HTTP {r.status}, {len(raw)} octets")
                data = _parse_json(raw, f"place_market_buy {product_id} HTTP {r.status}")
                if data is None:
                    return False, f"Réponse invalide (HTTP {r.status})", {}
                if data.get("success"):
                    return True, data.get("order_id", ""), data
                else:
                    error = data.get("error_response", {}).get("message", str(data))
                    return False, error, data
    except Exception as e:
        logger.error(f"[place_market_buy] {product_id} erreur réseau : {e}")
        return False, str(e), {}


async def place_market_sell(api_key, api_secret, product_id, quantity):
    path = "/api/v3/brokerage/orders"
    order = {
        "client_order_id": f"bot_sell_{int(time.time())}",
        "product_id": product_id,
        "side": "SELL",
        "order_configuration": {
            "market_market_ioc": {
                "base_size": str(quantity)
            }
        }
    }
    body = json.dumps(order)
    headers = get_headers(api_key, api_secret, "POST", path)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                COINBASE_API_URL + path,
                headers=headers,
                data=body,
                timeout=aiohttp.ClientTimeout(total=15)
            ) as r:
                raw = await r.read()
                logger.info(f"[place_market_sell] {product_id} HTTP {r.status}, {len(raw)} octets")
                data = _parse_json(raw, f"place_market_sell {product_id} HTTP {r.status}")
                if data is None:
                    return False, f"Réponse invalide (HTTP {r.status})", {}
                if data.get("success"):
                    return True, data.get("order_id", ""), data
                else:
                    error = data.get("error_response", {}).get("message", str(data))
                    return False, error, data
    except Exception as e:
        logger.error(f"[place_market_sell] {product_id} erreur réseau : {e}")
        return False, str(e), {}


async def get_eur_balance(api_key, api_secret):
    portfolio = await get_portfolio(api_key, api_secret)
    return portfolio.get("EUR", 0.0)


async def get_usdc_balance(api_key, api_secret):
    portfolio = await get_portfolio(api_key, api_secret)
    return portfolio.get("USDC", 0.0)


async def convert_eur_to_usdc(api_key, api_secret, amount_eur):
    """
    Convertit des EUR en USDC via la paire USDC-EUR.
    Retourne (success, usdc_obtenu, message)
    """
    success, order_id, data = await place_market_buy(
        api_key, api_secret, "USDC-EUR", amount_eur
    )
    if success:
        # On estime l'USDC obtenu : 1 EUR ≈ 1/taux USDC-EUR
        # En pratique on recupere le solde USDC apres conversion
        import asyncio
        await asyncio.sleep(1)
        usdc = await get_usdc_balance(api_key, api_secret)
        logger.info(f"[convert_eur_to_usdc] {amount_eur} EUR → ~{usdc:.2f} USDC")
        return True, usdc, order_id
    else:
        return False, 0.0, order_id


async def convert_usdc_to_eur(api_key, api_secret, amount_usdc):
    """
    Convertit des USDC en EUR via la paire USDC-EUR.
    Retourne (success, eur_obtenu, message)
    """
    success, order_id, data = await place_market_sell(
        api_key, api_secret, "USDC-EUR", amount_usdc
    )
    if success:
        import asyncio
        await asyncio.sleep(1)
        eur = await get_eur_balance(api_key, api_secret)
        logger.info(f"[convert_usdc_to_eur] {amount_usdc:.2f} USDC → ~{eur:.2f} EUR")
        return True, eur, order_id
    else:
        return False, 0.0, order_id


async def place_market_buy_usdc(api_key, api_secret, product_id_usdc, amount_eur):
    """
    Achete un coin paire USDC en convertissant d'abord les EUR en USDC.
    product_id_usdc : ex "PEPE-USDC"
    amount_eur      : montant en euros a investir

    Retourne (success, quantity, entry_price_usdc, message)
    """
    # 1. Recuperer le prix USDC-EUR pour estimer le montant USDC
    usdc_eur_price = await get_product_price("USDC-EUR", api_key, api_secret)
    if not usdc_eur_price:
        return False, 0, 0, "Prix USDC-EUR introuvable"

    amount_usdc = amount_eur * usdc_eur_price  # EUR → USDC (ex: 15 EUR * 0.92 = 13.8 USDC)

    # 2. Verifier solde EUR suffisant
    eur_balance = await get_eur_balance(api_key, api_secret)
    if eur_balance < amount_eur:
        return False, 0, 0, f"Solde EUR insuffisant ({eur_balance:.2f} EUR)"

    # 3. Convertir EUR → USDC
    ok, usdc_dispo, _ = await convert_eur_to_usdc(api_key, api_secret, amount_eur)
    if not ok or usdc_dispo < amount_usdc * 0.95:
        return False, 0, 0, "Echec conversion EUR → USDC"

    # 4. Acheter le coin avec USDC
    success, order_id, data = await place_market_buy(
        api_key, api_secret, product_id_usdc, amount_usdc
    )
    if not success:
        return False, 0, 0, order_id

    # 5. Recuperer le prix d'entree
    import asyncio
    await asyncio.sleep(1)
    entry_price = await get_product_price(product_id_usdc, api_key, api_secret)
    quantity    = amount_usdc / entry_price if entry_price else 0

    return True, quantity, entry_price, order_id


async def place_market_sell_usdc(api_key, api_secret, product_id_usdc, quantity):
    """
    Vend un coin paire USDC et reconvertit les USDC en EUR automatiquement.
    Retourne (success, eur_recupere, sell_price, message)
    """
    # 1. Vendre le coin → USDC
    success, order_id, data = await place_market_sell(
        api_key, api_secret, product_id_usdc, quantity
    )
    if not success:
        return False, 0, 0, order_id

    import asyncio
    await asyncio.sleep(1)

    # 2. Recuperer le solde USDC obtenu
    usdc_balance = await get_usdc_balance(api_key, api_secret)
    sell_price   = await get_product_price(product_id_usdc, api_key, api_secret)

    if usdc_balance <= 0:
        return True, 0, sell_price, "Vente OK mais USDC non detecte — convertis manuellement"

    # 3. Reconvertir USDC → EUR
    ok, eur_final, _ = await convert_usdc_to_eur(api_key, api_secret, usdc_balance)
    if not ok:
        return True, 0, sell_price, f"Vente OK mais conversion USDC→EUR echouee ({usdc_balance:.2f} USDC restants)"

    return True, eur_final, sell_price, order_id
