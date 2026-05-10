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


def _format_quantity(qty: float) -> str:
    """
    Formate une quantite en string avec precision fixe pour Coinbase.
    str(round(x, 8)) peut produire plus de 8 decimales a cause du float.
    f"{x:.Nf}" garantit exactement N decimales.
    """
    qty = float(qty)
    if qty > 1:
        return f"{qty:.4f}"
    elif qty > 0.01:
        return f"{qty:.6f}"
    else:
        return f"{qty:.8f}"


async def place_market_sell(api_key, api_secret, product_id, quantity):
    path = "/api/v3/brokerage/orders"
    qty_str = _format_quantity(quantity)
    order = {
        "client_order_id": f"bot_sell_{int(time.time())}",
        "product_id": product_id,
        "side": "SELL",
        "order_configuration": {
            "market_market_ioc": {
                "base_size": qty_str
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


async def get_order_history(api_key, api_secret, limit=100):
    """
    Recupere l'historique des ordres FILLED (executes) depuis Coinbase.
    Retourne une liste d'ordres avec symbol, side, prix, quantite, date.
    """
    path = "/api/v3/brokerage/orders/historical/batch"
    params = f"?order_status=FILLED&limit={limit}&sort_by=LAST_FILL_TIME&order_side=BUY"
    headers = get_headers(api_key, api_secret, "GET", path)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                COINBASE_API_URL + path + params,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15)
            ) as r:
                raw = await r.read()
                data = _parse_json(raw, f"get_order_history HTTP {r.status}")
                if data is None:
                    return []
                orders = data.get("orders", [])
                result = []
                for o in orders:
                    product_id = o.get("product_id", "")
                    symbol = product_id.replace("-EUR", "").replace("-USDC", "")
                    filled_size  = float(o.get("filled_size", 0) or 0)
                    filled_value = float(o.get("filled_value", 0) or 0)
                    avg_price    = filled_value / filled_size if filled_size > 0 else 0
                    result.append({
                        "product_id":  product_id,
                        "symbol":      symbol,
                        "side":        o.get("side", ""),
                        "quantity":    filled_size,
                        "total_eur":   filled_value,
                        "avg_price":   avg_price,
                        "date":        o.get("last_fill_time", "")[:16].replace("T", " "),
                        "order_id":    o.get("order_id", ""),
                    })
                return result
    except Exception as e:
        logger.error(f"[get_order_history] Erreur : {e}")
        return []


async def get_portfolio_with_history(api_key, api_secret):
    """
    Retourne le portefeuille complet avec pour chaque crypto :
    - balance actuelle
    - prix moyen d'achat (depuis l'historique des ordres)
    - montant total investi
    - paire de trading (EUR ou USDC)
    """
    portfolio = await get_portfolio(api_key, api_secret)
    orders    = await get_order_history(api_key, api_secret)

    # Grouper les ordres par symbol pour calculer le prix moyen d'achat
    buy_history = {}
    for o in orders:
        sym = o["symbol"]
        if sym not in buy_history:
            buy_history[sym] = {"total_qty": 0, "total_eur": 0, "orders": []}
        buy_history[sym]["total_qty"] += o["quantity"]
        buy_history[sym]["total_eur"] += o["total_eur"]
        buy_history[sym]["orders"].append(o)

    result = {}
    for currency, balance in portfolio.items():
        if currency in ("EUR", "USDC") or balance <= 0:
            continue

        history = buy_history.get(currency, {})
        total_qty = history.get("total_qty", 0)
        total_eur = history.get("total_eur", 0)
        avg_price = total_eur / total_qty if total_qty > 0 else 0
        orders_list = history.get("orders", [])

        # Determiner la paire de trading
        pair_eur  = f"{currency}-EUR"
        pair_usdc = f"{currency}-USDC"
        # Regarder l'historique pour savoir quelle paire a ete utilisee
        product_ids = [o["product_id"] for o in orders_list]
        if pair_usdc in product_ids:
            product_id = pair_usdc
        else:
            product_id = pair_eur

        result[currency] = {
            "symbol":     currency,
            "product_id": product_id,
            "balance":    balance,
            "avg_price":  avg_price,
            "total_invested_eur": total_eur,
            "last_buy_date": orders_list[0]["date"] if orders_list else "?",
            "nb_achats":  len(orders_list),
            "orders":     orders_list,
        }

    return result



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


async def place_market_buy_usdc(api_key, api_secret, product_id_usdc, amount_usdc):
    """
    Achete un coin paire USDC directement depuis le solde USDC disponible.
    product_id_usdc : ex "PEPE-USDC"
    amount_usdc     : montant en USDC a investir

    Retourne (success, quantity, entry_price_usdc, message)
    """
    # 1. Verifier solde USDC suffisant
    usdc_balance = await get_usdc_balance(api_key, api_secret)
    if usdc_balance < amount_usdc:
        return False, 0, 0, (
            f"Solde USDC insuffisant ({usdc_balance:.2f} USDC) — "
            f"convertis tes EUR en USDC sur l'app Coinbase"
        )

    # 2. Acheter le coin avec USDC
    success, order_id, data = await place_market_buy(
        api_key, api_secret, product_id_usdc, amount_usdc
    )
    if not success:
        return False, 0, 0, order_id

    # 3. Recuperer le prix d'entree
    import asyncio
    await asyncio.sleep(1)
    entry_price = await get_product_price(product_id_usdc, api_key, api_secret)
    quantity    = amount_usdc / entry_price if entry_price else 0

    return True, quantity, entry_price, order_id


async def place_market_sell_usdc(api_key, api_secret, product_id_usdc, quantity):
    """
    Vend un coin paire USDC. Les USDC restent dans le portefeuille.
    Retourne (success, usdc_obtenu, sell_price, order_id)
    """
    qty_str = _format_quantity(float(quantity))
    logger.info(f"[place_market_sell_usdc] {product_id_usdc} quantite={qty_str}")
    success, order_id, data = await place_market_sell(
        api_key, api_secret, product_id_usdc, qty_str
    )
    if not success:
        return False, 0, 0, order_id

    import asyncio
    await asyncio.sleep(1)

    sell_price  = await get_product_price(product_id_usdc, api_key, api_secret)
    usdc_obtenu = float(quantity) * sell_price if sell_price else 0

    return True, usdc_obtenu, sell_price, order_id
