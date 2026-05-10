import os
import json
import asyncio
import logging
import base64
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
import aiohttp

from signals import analyze_signals
from coinbase import (
    get_available_products, get_product_price, get_eur_balance,
    place_market_buy, place_market_sell, get_portfolio,
    place_market_buy_usdc, place_market_sell_usdc,
    get_usdc_balance, get_portfolio_with_history
)
from trader import (
    add_trade, update_trade_peak, should_sell, close_trade,
    get_active_trades, get_closed_trades, get_trade_summary,
    increment_stop_confirmation, reset_stop_confirmation
)

# ─── Configuration ───────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "").strip()
if not CHAT_ID:
    CHAT_ID = "8746800281"
print(f"DEBUG CHAT_ID: '{CHAT_ID}'")
ANTHROPIC_API_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")
COINBASE_API_KEY    = os.environ.get("COINBASE_API_KEY", "")
COINBASE_API_SECRET = os.environ.get("COINBASE_API_SECRET", "")

# Parametres globaux (les valeurs par trade sont maintenant dans signals.py)
CHECK_INTERVAL       = 120    # Surveillance positions toutes les 2 min
SCANNER_INTERVAL     = 3600   # Scan toutes les heures
NEW_LISTINGS_INTERVAL = 1800

POSITIONS_FILE     = "positions.json"
SEEN_LISTINGS_FILE = "seen_listings.json"
PENDING_BUYS_FILE  = "pending_buys.json"

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)


def round_quantity(qty: float) -> float:
    """Arrondit une quantite selon sa taille pour respecter les limites Coinbase."""
    if qty > 1:
        return round(qty, 4)
    elif qty > 0.01:
        return round(qty, 6)
    else:
        return round(qty, 8)


# ─── Gestion positions manuelles ─────────────────────────────

def load_positions():
    if os.path.exists(POSITIONS_FILE):
        with open(POSITIONS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_positions(positions):
    with open(POSITIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(positions, f, indent=2, ensure_ascii=False)

def add_manual_position(coin, amount_eur, entry_price, date=None):
    positions = load_positions()
    coin = coin.upper()
    key  = f"{coin}_{datetime.now().strftime('%Y%m%d%H%M%S')}"
    positions[key] = {
        "coin":        coin,
        "amount_eur":  amount_eur,
        "entry_price": entry_price,
        "quantity":    amount_eur / entry_price,
        "date":        date or datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    save_positions(positions)
    return key, positions[key]


# ─── Gestion achats en attente ────────────────────────────────

def load_pending():
    if os.path.exists(PENDING_BUYS_FILE):
        with open(PENDING_BUYS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_pending(pending):
    with open(PENDING_BUYS_FILE, "w", encoding="utf-8") as f:
        json.dump(pending, f, indent=2)

def add_pending(product_id, symbol, analysis):
    pending = load_pending()
    pending[product_id] = {
        "product_id": product_id,
        "symbol":     symbol,
        "analysis":   analysis,
        "timestamp":  datetime.now().strftime("%Y-%m-%d %H:%M")
    }
    save_pending(pending)

def remove_pending(product_id):
    pending = load_pending()
    if product_id in pending:
        del pending[product_id]
        save_pending(pending)

def load_seen_listings():
    if os.path.exists(SEEN_LISTINGS_FILE):
        with open(SEEN_LISTINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []

def save_seen_listings(seen):
    with open(SEEN_LISTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(seen, f)


# ─── Prix CoinGecko (positions manuelles) ────────────────────

COIN_IDS = {
    "BTC": "bitcoin", "ETH": "ethereum", "BNB": "binancecoin",
    "SOL": "solana", "ADA": "cardano", "XRP": "ripple",
    "DOGE": "dogecoin", "AVAX": "avalanche-2", "DOT": "polkadot",
    "MATIC": "matic-network", "LINK": "chainlink", "LTC": "litecoin",
    "JTO": "jito-governance-token", "BILL": "billions-network",
}

async def get_coingecko_prices(coins):
    ids     = [COIN_IDS.get(c.upper(), c.lower()) for c in coins]
    ids_str = ",".join(set(ids))
    url     = (
        f"https://api.coingecko.com/api/v3/simple/price"
        f"?ids={ids_str}&vs_currencies=eur&include_24hr_change=true"
    )
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                data   = await r.json()
                result = {}
                for coin in coins:
                    coin_id = COIN_IDS.get(coin.upper(), coin.lower())
                    if coin_id in data:
                        result[coin.upper()] = {
                            "price":     data[coin_id]["eur"],
                            "change_24h": data[coin_id].get("eur_24h_change", 0)
                        }
                return result
    except Exception as e:
        logger.error(f"Erreur CoinGecko: {e}")
        return {}


# ─── Score solidité projet (nouvelles listings) ───────────────

async def get_coin_details(coin_id):
    url = (
        f"https://api.coingecko.com/api/v3/coins/{coin_id}"
        f"?localization=false&tickers=false&community_data=true&developer_data=true"
    )
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status != 200:
                    return None
                return await r.json()
    except Exception as e:
        logger.error(f"Erreur details: {e}")
        return None

def score_project(details):
    if not details:
        return 0, [], []
    score, signaux, alertes = 0, [], []
    if details.get("links", {}).get("homepage", [None])[0]:
        score += 1; signaux.append("Site web present")
    else:
        alertes.append("Pas de site web")
    if details.get("links", {}).get("whitepaper"):
        score += 1; signaux.append("Whitepaper present")
    else:
        alertes.append("Pas de whitepaper")
    dev = details.get("developer_data", {})
    if dev.get("commit_count_4_weeks", 0) > 0:
        score += 2; signaux.append(f"GitHub actif ({dev['commit_count_4_weeks']} commits/mois)")
    else:
        alertes.append("GitHub inactif")
    twitter = (details.get("community_data", {}).get("twitter_followers", 0) or 0)
    if twitter > 10000:
        score += 1; signaux.append(f"Twitter : {twitter:,} followers")
    else:
        alertes.append(f"Peu de followers ({twitter})")
    market_cap = details.get("market_data", {}).get("market_cap", {}).get("eur", 0) or 0
    if market_cap > 100000:
        score += 1; signaux.append(f"Market cap : {market_cap:,.0f} EUR")
    else:
        alertes.append("Market cap tres faible")
    desc = details.get("description", {}).get("en", "")
    if desc and len(desc) > 100:
        score += 1; signaux.append("Projet decrit")
    else:
        alertes.append("Pas de description")
    return round(score), signaux, alertes


# ─── Analyse screenshot ───────────────────────────────────────

async def analyze_screenshot(image_bytes):
    image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
    prompt = """Analyse ce screenshot de trade crypto.
Reponds UNIQUEMENT en JSON :
{
  "coin": "symbole en majuscules",
  "amount_eur": montant investi en euros,
  "entry_price": prix achat en euros,
  "date": "YYYY-MM-DD ou null"
}
Si montant en USD, multiplie par 0.92. UNIQUEMENT le JSON."""
    headers = {
        "Content-Type": "application/json",
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01"
    }
    body = {
        "model": "claude-opus-4-6",
        "max_tokens": 300,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image_b64}},
                {"type": "text", "text": prompt}
            ]
        }]
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.anthropic.com/v1/messages",
                headers=headers, json=body,
                timeout=aiohttp.ClientTimeout(total=30)
            ) as r:
                data = await r.json()
                text = data["content"][0]["text"].strip()
                if "```" in text:
                    text = text.split("```")[1]
                    if text.startswith("json"):
                        text = text[4:]
                return json.loads(text.strip())
    except Exception as e:
        logger.error(f"Erreur screenshot: {e}")
        return None


# ─── Construction des messages Telegram ──────────────────────

def build_opportunity_message(symbol, analysis):
    """
    Construit le message Telegram selon le profil du coin.

    STANDARD      : message classique, infos de base
    ULTRA VOLATILE: message étendu avec ATR, OBV, Squeeze + avertissement risque
    """
    barre   = "|" * analysis["score"] + "." * (analysis["score_max"] - analysis["score"])
    is_uv   = analysis.get("is_ultra_volatile", False)
    atr     = analysis.get("atr_pct")
    squeeze = analysis.get("bollinger_squeeze", False)
    obv_up  = analysis.get("obv_haussier", False)
    obv_acc = analysis.get("obv_acceleration", 0)

    signaux_str = "\n".join(f"+ {s}" for s in analysis["signaux"])
    alertes_str = (
        "\n\nAlertes :\n" + "\n".join(f"! {a}" for a in analysis["alertes"])
        if analysis["alertes"] else ""
    )

    if is_uv:
        nb_uv         = analysis.get("nb_criteres_uv", 0)
        atr_label     = f"{atr:.1f}%" if atr else "N/A"
        timeframe     = analysis.get("timeframe", "H1")
        vol_mcap      = analysis.get("vol_mcap_ratio")
        is_wakeup     = analysis.get("is_wakeup", False)
        spike_ratio   = analysis.get("spike_ratio", 0)
        inact_pct     = analysis.get("inactivity_pct", 0)

        obv_label = (
            f"Haussier fort (acc. {obv_acc:.2f})" if obv_up and obv_acc > 0.1
            else "Haussier" if obv_up
            else "Baissier"
        )
        squeeze_label = "OUI — explosion imminente !" if squeeze else "Non"
        vol_mcap_label = f"{vol_mcap:.0f}%" if vol_mcap else "N/A"
        wakeup_label  = f"OUI — x{spike_ratio:.0f} vs periode inactive !" if is_wakeup else f"Non ({inact_pct:.0f}% inactif)"

        msg = (
            f"ULTRA VOLATILE — OPPORTUNITE DETECTEE\n"
            f"{'=' * 34}\n\n"
            f"Crypto    : {symbol}\n"
            f"Prix      : {analysis['price']:,.6f} EUR\n"
            f"Timeframe : {timeframe}\n"
            f"Score     : {analysis['score']}/{analysis['score_max']} [{barre}] {analysis['niveau']}\n"
            f"Criteres UV : {nb_uv}/3 valides\n\n"
            f"--- Indicateurs volatilite ---\n"
            f"ATR (mouvement/bougie)  : {atr_label}  {'OK' if atr and atr > 6 else '--'}\n"
            f"OBV (accumulation)      : {obv_label}  {'OK' if obv_up else '--'}\n"
            f"Bollinger Squeeze       : {squeeze_label}  {'OK' if squeeze else '--'}\n"
            f"Vol / Market Cap        : {vol_mcap_label}\n"
            f"Reveil apres inactivite : {wakeup_label}\n\n"
            f"--- Signaux ---\n"
            f"{signaux_str}"
            f"{alertes_str}\n\n"
            f"Variation 24h : {analysis['change_24h']:+.1f}%\n"
            f"Variation 3j  : {analysis['change_72h']:+.1f}%\n"
            f"Volume        : x{analysis['volume_ratio']:.1f} vs moyenne\n\n"
            f"--- Regles automatiques ---\n"
            f"Stop loss     : {analysis['stop_loss_pct']}%\n"
            f"Trailing stop : -{analysis['trailing_stop_pct']}% depuis le pic\n"
            f"Montant MAX   : {analysis['montant_max_eur']:.0f} EUR\n\n"
            f"RISQUE ELEVE — max {analysis['montant_max_eur']:.0f} EUR\n\n"
            f"Veux-tu acheter ?"
        )
    else:
        # ── Message STANDARD ──────────────────────────────────
        msg = (
            f"OPPORTUNITE DETECTEE\n\n"
            f"Crypto : {symbol}\n"
            f"Prix   : {analysis['price']:,.4f} EUR\n"
            f"Score  : {analysis['score']}/{analysis['score_max']} [{barre}] {analysis['niveau']}\n\n"
            f"Signaux :\n"
            f"{signaux_str}"
            f"{alertes_str}\n\n"
            f"Variation 24h : {analysis['change_24h']:+.1f}%\n"
            f"Variation 3j  : {analysis['change_72h']:+.1f}%\n"
            f"Volume        : x{analysis['volume_ratio']:.1f} vs moyenne\n\n"
            f"Stop loss : {analysis['stop_loss_pct']}%  |  "
            f"Trailing : -{analysis['trailing_stop_pct']}%  |  "
            f"Max : {analysis['montant_max_eur']:.0f} EUR\n\n"
            f"Veux-tu acheter ?"
        )

    return msg


# ─── Surveillance des trades actifs ──────────────────────────

async def monitor_trades(app):
    trades = get_active_trades()
    if not trades:
        return

    for key, trade in trades.items():
        try:
            product_id    = trade["product_id"]
            current_price = await get_product_price(product_id, COINBASE_API_KEY, COINBASE_API_SECRET)
            if not current_price or current_price <= 0:
                continue

            update_trade_peak(key, current_price)
            decision, reason = should_sell(trade, current_price)

            if decision == "PENDING_STOP":
                # ULTRA VOLATILE : stop atteint mais attend confirmation
                count = increment_stop_confirmation(key)
                logger.info(f"[monitor] {trade['symbol']} PENDING_STOP confirmation {count}/2 — {reason}")
                # On ne vend pas encore, on attend le prochain cycle (2 min)
                continue

            # Si le prix est remonté au-dessus du stop, on remet le compteur à zéro
            if not decision and trade.get("stop_loss_confirmations", 0) > 0:
                reset_stop_confirmation(key)

            if decision is True:
                is_usdc = product_id.endswith("-USDC")
                qty_to_sell = round_quantity(trade["quantity"])
                if is_usdc:
                    success, eur_recupere, sell_price, order_id = await place_market_sell_usdc(
                        COINBASE_API_KEY, COINBASE_API_SECRET,
                        product_id, qty_to_sell
                    )
                    if not sell_price:
                        sell_price = current_price
                else:
                    success, order_id, _ = await place_market_sell(
                        COINBASE_API_KEY, COINBASE_API_SECRET,
                        product_id, qty_to_sell
                    )
                    sell_price = current_price

                if success:
                    pnl_pct, pnl_eur = close_trade(key, sell_price, reason)
                    signe  = "+" if pnl_eur >= 0 else ""
                    profil = "ULTRA VOLATILE" if trade.get("is_ultra_volatile") else "STANDARD"
                    usdc_note = " (USDC→EUR converti auto)" if is_usdc else ""
                    msg = (
                        f"VENTE AUTOMATIQUE\n\n"
                        f"Crypto  : {trade['symbol']}  [{profil}]\n"
                        f"Raison  : {reason}\n"
                        f"Entree  : {trade['entry_price']:,.4f} EUR\n"
                        f"Vente   : {sell_price:,.4f} EUR{usdc_note}\n"
                        f"Investi : {trade['amount_eur']:.2f} EUR\n"
                        f"P&L     : {signe}{pnl_eur:.2f} EUR ({signe}{pnl_pct:.1f}%)\n"
                        f"Pic atteint : +{trade['peak_pct']:.1f}%"
                    )
                    await app.bot.send_message(chat_id=CHAT_ID, text=msg)
                else:
                    await app.bot.send_message(
                        chat_id=CHAT_ID,
                        text=f"Erreur vente {trade['symbol']} : {order_id}\nVendez manuellement !"
                    )
        except Exception as e:
            logger.error(f"Erreur monitoring trade {key}: {e}")


# ─── Scanner opportunites ─────────────────────────────────────

async def scan_opportunities(app):
    bot = app.bot if hasattr(app, 'run_polling') else app
    try:
        products = await get_available_products(COINBASE_API_KEY, COINBASE_API_SECRET)
        if not products:
            return

        products = [p for p in products if p.get("quote_currency_id") in ("EUR", "USDC")]
        products = sorted(
            products,
            key=lambda x: float(x.get("volume_24h", 0) or 0),
            reverse=True
        )[:300]

        opportunities = []
        for product in products:
            product_id = product.get("product_id", "?")
            symbol     = product.get("base_currency_id", "?")
            volume_24h = float(product.get("volume_24h", 0) or 0)
            market_cap = float(product.get("quote_volume_24h", 0) or 0)
            try:
                analysis = await analyze_signals(
                    product_id, COINBASE_API_KEY, COINBASE_API_SECRET,
                    volume_24h=volume_24h,
                    market_cap=market_cap
                )
                if not analysis:
                    continue

                # UV uniquement (score suffisant) ou momentum fort (+30% sur 24h)
                is_uv_valid = analysis.get("is_ultra_volatile") and analysis["score"] >= analysis["min_score_alerte"]
                if is_uv_valid or analysis.get("is_momentum_alert"):
                    opportunities.append((product_id, symbol, analysis))
            except Exception as e:
                logger.error(f"[scan_opportunities] {product_id} : {e}", exc_info=True)
                continue

            await asyncio.sleep(0.3)

        for product_id, symbol, analysis in opportunities:
            try:
                add_pending(product_id, symbol, analysis)

                keyboard = [[
                    InlineKeyboardButton("Oui, acheter", callback_data=f"buy_yes_{product_id}"),
                    InlineKeyboardButton("Non",          callback_data=f"buy_no_{product_id}")
                ]]
                reply_markup = InlineKeyboardMarkup(keyboard)

                msg = build_opportunity_message(symbol, analysis)

                await bot.send_message(
                    chat_id=CHAT_ID,
                    text=msg,
                    reply_markup=reply_markup
                )
                await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"[scan_opportunities] envoi {product_id} : {e}", exc_info=True)
                continue

    except Exception as e:
        logger.error(f"Erreur scan opportunites: {e}", exc_info=True)


# ─── Nouvelles listings ───────────────────────────────────────

async def check_new_listings(app):
    url = "https://api.coingecko.com/api/v3/coins/list/new"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status != 200:
                    return
                raw = await r.read()
                try:
                    coins = json.loads(raw)
                except json.JSONDecodeError:
                    return

        seen     = load_seen_listings()
        new_ones = [c for c in coins if c.get("id") and c["id"] not in seen]

        for coin in new_ones[:3]:
            seen.append(coin["id"])
            details = await get_coin_details(coin["id"])
            score, signaux, alertes = score_project(details)
            niveau = "SOLIDE" if score >= 5 else "MOYEN" if score >= 3 else "RISQUE"
            barre  = "|" * score + "." * (7 - score)

            msg = (
                f"NOUVELLE CRYPTO LISTEE\n\n"
                f"Nom   : {coin.get('name', '?')} ({coin.get('symbol', '?').upper()})\n"
                f"Score : {score}/7 [{barre}] {niveau}\n\n"
            )
            if signaux:
                msg += "Points positifs :\n" + "\n".join(f"+ {s}" for s in signaux) + "\n\n"
            if alertes:
                msg += "Alertes :\n" + "\n".join(f"! {a}" for a in alertes[:3]) + "\n\n"
            msg += "RAPPEL : Nouvelles cryptos = risque tres eleve."

            await app.bot.send_message(chat_id=CHAT_ID, text=msg)
            await asyncio.sleep(2)

        save_seen_listings(seen[-500:])

    except Exception as e:
        logger.error(f"Erreur nouvelles listings: {e}")


# ─── Callback boutons Telegram ────────────────────────────────

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("buy_no_"):
        product_id = data.replace("buy_no_", "")
        remove_pending(product_id)
        await query.edit_message_text(query.message.text + "\n\nRefuse.")

    elif data.startswith("buy_yes_"):
        product_id = data.replace("buy_yes_", "")
        pending = load_pending()
        if product_id not in pending:
            await query.edit_message_text("Cette opportunite a expire.")
            return

        ctx.user_data["pending_buy"] = product_id
        analysis  = pending[product_id]["analysis"]
        montant_max = analysis.get("montant_max_eur", 50.0)
        is_uv     = analysis.get("is_ultra_volatile", False)
        warning   = "\nATTENTION : coin ultra volatile, reste sous {:.0f}€ !".format(montant_max) if is_uv else ""

        await query.edit_message_text(
            query.message.text
            + f"\n\nCombien d euros veux-tu investir ? (max {montant_max:.0f}€)\n"
            + "Reponds avec un nombre."
            + warning
        )

    # ── Vente depuis portefeuille Coinbase ────────────────────
    elif data.startswith("sell_coin_"):
        symbol   = data[len("sell_coin_"):]
        coin_data = ctx.user_data.get(f"sell_{symbol}")
        if not coin_data:
            await query.edit_message_text("Session expiree. Retape #vendre.")
            return

        ctx.user_data["pending_sell_coin"] = symbol
        val = coin_data["val_eur"]
        await query.edit_message_text(
            f"Combien veux-tu vendre ?\n\n"
            f"Crypto   : {symbol}\n"
            f"Balance  : {coin_data['balance']:.6f} {symbol}\n"
            f"Valeur   : ~{val:.2f} EUR\n\n"
            f"Reponds avec un montant en euros (max {val:.2f} EUR)"
        )

    elif data.startswith("sell_confirm_coin_"):
        suffix    = data[len("sell_confirm_coin_"):]
        last_sep  = suffix.rfind("_")
        symbol    = suffix[:last_sep]
        amount_eur = float(suffix[last_sep + 1:])

        coin_data = ctx.user_data.get(f"sell_{symbol}")
        if not coin_data:
            await query.edit_message_text("Session expiree. Retape #vendre.")
            return

        product_id    = coin_data["product_id"]
        balance       = coin_data["balance"]
        current_price = coin_data["current_price"]

        await query.edit_message_text(f"Vente de {amount_eur:.2f} EUR de {symbol} en cours...")

        # Calculer la quantite a vendre
        quantite = (amount_eur / current_price) if current_price else balance
        quantite = round_quantity(min(quantite, balance))

        # Formatage explicite avant envoi a Coinbase (evite str(float) a 15 decimales)
        if quantite > 1:
            qty_str = f"{quantite:.4f}"
        elif quantite > 0.01:
            qty_str = f"{quantite:.6f}"
        else:
            qty_str = f"{quantite:.8f}"

        is_usdc = product_id.endswith("-USDC")
        if is_usdc:
            success, eur_recupere, sell_price, order_id = await place_market_sell_usdc(
                COINBASE_API_KEY, COINBASE_API_SECRET, product_id, qty_str
            )
            if not sell_price:
                sell_price = current_price
        else:
            success, order_id, _ = await place_market_sell(
                COINBASE_API_KEY, COINBASE_API_SECRET, product_id, qty_str
            )
            sell_price = current_price

        if success:
            pnl_eur = (sell_price - (coin_data["val_eur"] / balance)) * quantite if balance else 0
            await ctx.bot.send_message(
                chat_id=CHAT_ID,
                text=(
                    f"VENTE EFFECTUEE\n\n"
                    f"Crypto  : {symbol}\n"
                    f"Vendu   : {quantite:.6f} {symbol}\n"
                    f"Montant : ~{amount_eur:.2f} EUR\n"
                    f"Prix    : {sell_price:,.4f} EUR\n"
                    + (f"(USDC → EUR converti auto)" if is_usdc else "")
                )
            )
        else:
            await ctx.bot.send_message(
                chat_id=CHAT_ID,
                text=f"Erreur vente {symbol} : {order_id}"
            )

    # ── Vente manuelle (ancien système) ──────────────────────
    elif data.startswith("sell_select_"):
        # Extraire le trade_key en retirant uniquement le prefixe
        trade_key = data[len("sell_select_"):]
        trades    = get_active_trades()
        if trade_key not in trades:
            await query.edit_message_text("Ce trade n'existe plus.")
            return
        trade         = trades[trade_key]
        current_price = await get_product_price(trade["product_id"], COINBASE_API_KEY, COINBASE_API_SECRET)
        val_actuelle  = (trade["quantity"] * current_price) if current_price else 0

        ctx.user_data["pending_sell"] = trade_key
        await query.edit_message_text(
            f"Combien veux-tu vendre ?\n\n"
            f"Crypto   : {trade['symbol']}\n"
            f"Investi  : {trade['amount_eur']:.2f} EUR\n"
            f"Valeur actuelle : {val_actuelle:.2f} EUR\n\n"
            f"Reponds avec un montant en euros (max {val_actuelle:.2f} EUR)"
        )

    elif data.startswith("sell_confirm_"):
        # Format : sell_confirm_TRADEKEY_AMOUNT
        # Le montant est apres le dernier underscore, le trade_key est le reste
        suffix    = data[len("sell_confirm_"):]
        last_sep  = suffix.rfind("_")
        trade_key  = suffix[:last_sep]
        amount_eur = float(suffix[last_sep + 1:])

        trades = get_active_trades()
        if trade_key not in trades:
            await query.edit_message_text("Ce trade n'existe plus.")
            return

        trade         = trades[trade_key]
        symbol        = trade["symbol"]
        product_id    = trade["product_id"]
        current_price = await get_product_price(product_id, COINBASE_API_KEY, COINBASE_API_SECRET)

        if not current_price:
            await query.edit_message_text(f"Prix introuvable pour {symbol}. Reessaie.")
            return

        # Calcul de la quantite a vendre selon le montant en euros
        quantite_totale = trade["quantity"]
        val_totale      = quantite_totale * current_price
        ratio_vente     = min(amount_eur / val_totale, 1.0)
        quantite_vendre = round_quantity(quantite_totale * ratio_vente)

        await query.edit_message_text(f"Vente de {amount_eur:.2f} EUR de {symbol} en cours...")

        success, order_id, _ = await place_market_sell(
            COINBASE_API_KEY, COINBASE_API_SECRET, product_id, quantite_vendre
        )

        if success:
            entry_price = trade["entry_price"]
            pnl_eur     = (current_price - entry_price) * quantite_vendre
            pnl_pct     = ((current_price - entry_price) / entry_price) * 100
            signe       = "+" if pnl_eur >= 0 else ""

            # Vente partielle ou totale ?
            if ratio_vente >= 0.99:
                # Vente totale : on clos le trade
                close_trade(trade_key, current_price, "Vente manuelle")
                reste_msg = "Position entierement fermee."
            else:
                # Vente partielle : on met a jour la quantite restante
                trades_all = get_active_trades()
                from trader import load_trades, save_trades
                all_t = load_trades()
                if trade_key in all_t:
                    all_t[trade_key]["quantity"]   -= quantite_vendre
                    all_t[trade_key]["amount_eur"] -= amount_eur
                    save_trades(all_t)
                reste_eur = (quantite_totale - quantite_vendre) * current_price
                reste_msg = f"Reste en portefeuille : {reste_eur:.2f} EUR"

            await ctx.bot.send_message(
                chat_id=CHAT_ID,
                text=(
                    f"VENTE EFFECTUEE\n\n"
                    f"Crypto  : {symbol}\n"
                    f"Vendu   : {amount_eur:.2f} EUR\n"
                    f"Prix    : {current_price:,.4f} EUR\n"
                    f"P&L     : {signe}{pnl_eur:.2f} EUR ({signe}{pnl_pct:.1f}%)\n"
                    f"{reste_msg}"
                )
            )
        else:
            await ctx.bot.send_message(
                chat_id=CHAT_ID,
                text=f"Erreur vente {symbol} : {order_id}"
            )

    elif data.startswith("sell_cancel_"):
        await query.edit_message_text("Vente annulee.")

    elif data.startswith("confirm_buy_"):
        parts      = data.split("_")
        product_id = parts[2]
        amount     = float(parts[3])

        pending = load_pending()
        if product_id not in pending:
            await query.edit_message_text("Cette opportunite a expire.")
            return

        info     = pending[product_id]
        symbol   = info["symbol"]
        analysis = info["analysis"]
        is_uv    = analysis.get("is_ultra_volatile", False)

        await query.edit_message_text(f"Achat de {amount}€ de {symbol} en cours...")

        is_usdc_pair = product_id.endswith("-USDC")

        if is_usdc_pair and amount < 5.0:
            await ctx.bot.send_message(
                chat_id=CHAT_ID,
                text=(
                    f"Montant minimum 5€ pour les paires USDC.\n\n"
                    f"{symbol} est coté en USDC — la conversion EUR→USDC\n"
                    f"nécessite au moins 5€ (limite Coinbase)."
                )
            )
            return

        if is_usdc_pair:
            # Achat via USDC : conversion EUR→USDC puis achat
            success, quantity, current_price, order_id = await place_market_buy_usdc(
                COINBASE_API_KEY, COINBASE_API_SECRET, product_id, amount
            )
        else:
            success, order_id, order_data = await place_market_buy(
                COINBASE_API_KEY, COINBASE_API_SECRET, product_id, amount
            )
            if success:
                current_price = await get_product_price(product_id, COINBASE_API_KEY, COINBASE_API_SECRET)
                quantity      = amount / current_price if current_price else 0

        if success:
            if not current_price:
                remove_pending(product_id)
                await ctx.bot.send_message(
                    chat_id=CHAT_ID,
                    text=(
                        f"ACHAT {symbol} reussi mais prix introuvable.\n"
                        f"Ajoute le trade manuellement : /buy {symbol} {amount} <prix_achat>"
                    )
                )
                return

            # On passe le profil et les parametres adaptes a add_trade
            key, trade = add_trade(
                product_id, symbol, amount, current_price, quantity, order_id,
                is_ultra_volatile=is_uv,
                stop_loss_pct=analysis.get("stop_loss_pct"),
                trailing_stop_pct=analysis.get("trailing_stop_pct"),
            )
            remove_pending(product_id)

            profil = "ULTRA VOLATILE" if is_uv else "STANDARD"
            msg = (
                f"ACHAT CONFIRME\n\n"
                f"Crypto   : {symbol}  [{profil}]\n"
                f"Montant  : {amount:.2f} EUR\n"
                f"Prix     : {current_price:,.6f} EUR\n"
                f"Quantite : {quantity:.6f}\n"
                f"ID trade : {key}\n\n"
                f"Stop loss     : {analysis.get('stop_loss_pct', -25):.0f}%\n"
                f"Trailing stop : -{analysis.get('trailing_stop_pct', 15):.0f}% depuis le pic\n"
            )
            if is_uv:
                msg += (
                    f"\nCoin ultra volatile : le stop loss\n"
                    f"ne se declenchera qu'apres 2 confirmations\n"
                    f"consecutives (anti-wick)."
                )
            await ctx.bot.send_message(chat_id=CHAT_ID, text=msg)
        else:
            await ctx.bot.send_message(
                chat_id=CHAT_ID,
                text=f"Erreur achat {symbol} : {order_id}"
            )


# ─── Gestion des messages texte (montant apres confirmation) ──

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    # ── Déclencheur vente manuelle ────────────────────────────
    if text.lower() in ["#vendre", "#vente", "#sell"]:
        await update.message.reply_text("Recuperation de ton portefeuille...")
        portfolio = await get_portfolio(COINBASE_API_KEY, COINBASE_API_SECRET)

        # Filtrer EUR et USDC, garder seulement les cryptos
        cryptos = {k: v for k, v in portfolio.items()
                   if k not in ("EUR", "USDC") and v > 0}

        if not cryptos:
            await update.message.reply_text("Aucune crypto dans ton portefeuille Coinbase.")
            return

        msg     = "Quelle crypto veux-tu vendre ?\n\n"
        buttons = []

        for symbol, balance in cryptos.items():
            # Chercher le prix actuel
            price_eur  = await get_product_price(f"{symbol}-EUR", COINBASE_API_KEY, COINBASE_API_SECRET)
            price_usdc = await get_product_price(f"{symbol}-USDC", COINBASE_API_KEY, COINBASE_API_SECRET)

            if price_eur:
                current_price = price_eur
                product_id    = f"{symbol}-EUR"
            elif price_usdc:
                current_price = price_usdc
                product_id    = f"{symbol}-USDC"
            else:
                current_price = 0
                product_id    = f"{symbol}-EUR"

            val = balance * current_price if current_price else 0
            label = f"{symbol} — {balance:.4f} — ~{val:.2f} EUR"
            # Stocker product_id et balance dans le callback via user_data
            ctx.user_data[f"sell_{symbol}"] = {
                "symbol":      symbol,
                "product_id":  product_id,
                "balance":     balance,
                "current_price": current_price,
                "val_eur":     val,
            }
            buttons.append([InlineKeyboardButton(label, callback_data=f"sell_coin_{symbol}")])

        reply_markup = InlineKeyboardMarkup(buttons)
        await update.message.reply_text(msg, reply_markup=reply_markup)
        return

    # ── Montant vente depuis portefeuille Coinbase ────────────
    if "pending_sell_coin" in ctx.user_data:
        symbol    = ctx.user_data["pending_sell_coin"]
        coin_data = ctx.user_data.get(f"sell_{symbol}")
        try:
            amount = float(text.replace(",", ".").replace("€", "").strip())
            if not coin_data:
                await update.message.reply_text("Session expiree. Retape #vendre.")
                del ctx.user_data["pending_sell_coin"]
                return
            if amount <= 0:
                await update.message.reply_text("Le montant doit etre positif.")
                return
            if amount > coin_data["val_eur"] * 1.05:
                await update.message.reply_text(f"Maximum ~{coin_data['val_eur']:.2f} EUR.")
                return

            keyboard = [[
                InlineKeyboardButton(f"Confirmer {amount:.2f} EUR", callback_data=f"sell_confirm_coin_{symbol}_{amount}"),
                InlineKeyboardButton("Annuler", callback_data=f"sell_cancel_{symbol}")
            ]]
            await update.message.reply_text(
                f"Confirmer la vente ?\n\n"
                f"Crypto  : {symbol}\n"
                f"Montant : {amount:.2f} EUR\n"
                f"Prix    : {coin_data['current_price']:,.4f} EUR",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            del ctx.user_data["pending_sell_coin"]
        except ValueError:
            await update.message.reply_text("Envoie juste un nombre, ex: 5")
        return


        trade_key = ctx.user_data["pending_sell"]
        try:
            amount = float(text.replace(",", ".").replace("€", "").strip())
            if amount <= 0:
                await update.message.reply_text("Le montant doit etre positif.")
                return

            trades = get_active_trades()
            if trade_key not in trades:
                await update.message.reply_text("Ce trade n'existe plus.")
                del ctx.user_data["pending_sell"]
                return

            trade         = trades[trade_key]
            symbol        = trade["symbol"]
            current_price = await get_product_price(trade["product_id"], COINBASE_API_KEY, COINBASE_API_SECRET)
            val_actuelle  = (trade["quantity"] * current_price) if current_price else 0

            if amount > val_actuelle:
                await update.message.reply_text(
                    f"Maximum {val_actuelle:.2f} EUR (valeur actuelle de ta position)."
                )
                return

            keyboard = [[
                InlineKeyboardButton(f"Confirmer vente {amount:.2f} EUR", callback_data=f"sell_confirm_{trade_key}_{amount}"),
                InlineKeyboardButton("Annuler", callback_data=f"sell_cancel_{trade_key}")
            ]]
            reply_markup = InlineKeyboardMarkup(keyboard)

            quantite_vendre = (amount / current_price) if current_price else 0
            await update.message.reply_text(
                f"Confirmer la vente ?\n\n"
                f"Crypto   : {symbol}\n"
                f"Montant  : {amount:.2f} EUR\n"
                f"Prix actuel : {current_price:,.4f} EUR\n"
                f"Quantite : {quantite_vendre:.6f} {symbol}",
                reply_markup=reply_markup
            )
            del ctx.user_data["pending_sell"]

        except ValueError:
            await update.message.reply_text("Envoie juste un nombre, ex: 10")
        return

    if "pending_buy" in ctx.user_data:
        product_id = ctx.user_data["pending_buy"]
        try:
            amount = float(text.replace(",", ".").replace("€", "").strip())
            if amount <= 0:
                await update.message.reply_text("Le montant doit etre positif.")
                return

            pending = load_pending()
            if product_id not in pending:
                await update.message.reply_text("Cette opportunite a expire.")
                del ctx.user_data["pending_buy"]
                return

            info       = pending[product_id]
            symbol     = info["symbol"]
            analysis   = info["analysis"]
            price      = analysis["price"]
            montant_max = analysis.get("montant_max_eur", 50.0)
            is_uv      = analysis.get("is_ultra_volatile", False)

            if amount > montant_max:
                await update.message.reply_text(
                    f"Maximum {montant_max:.0f}€ pour ce coin"
                    + (" (ultra volatile — risque eleve)." if is_uv else ".")
                )
                return

            profil = "ULTRA VOLATILE" if is_uv else "STANDARD"
            keyboard = [[
                InlineKeyboardButton(f"Confirmer {amount}€", callback_data=f"confirm_buy_{product_id}_{amount}"),
                InlineKeyboardButton("Annuler",              callback_data=f"buy_no_{product_id}")
            ]]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await update.message.reply_text(
                f"Confirmer l achat ?\n\n"
                f"Crypto   : {symbol}  [{profil}]\n"
                f"Montant  : {amount:.2f} EUR\n"
                f"Prix     : {price:,.6f} EUR\n"
                f"Quantite : {amount / price:.6f} {symbol}\n\n"
                f"Stop loss     : {analysis.get('stop_loss_pct', -25):.0f}%\n"
                f"Trailing stop : -{analysis.get('trailing_stop_pct', 15):.0f}% depuis le pic",
                reply_markup=reply_markup
            )
            del ctx.user_data["pending_buy"]

        except ValueError:
            await update.message.reply_text("Envoie juste un nombre, ex: 20")


# ─── Commandes Telegram ───────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = (
        "Agent de Trading Crypto\n\n"
        "Commandes :\n\n"
        "/recap       — Resume complet\n"
        "/portefeuille — Ton solde Coinbase\n"
        "/scanner     — Scan manuel opportunites\n"
        "/nouveautes  — Nouvelles cryptos listees\n"
        "/prix BTC ETH SOL  — Prix actuels\n"
        "/historique  — Tes trades passes\n"
        "#vendre      — Vendre une position\n\n"
        "Ou envoie un screenshot de ton trade !\n\n"
        "Profils de trading :\n"
        "STANDARD      : stop -25%  | trailing -15% | max 50 EUR\n"
        "ULTRA VOLATILE: stop -35%  | trailing dynamique | max 15 EUR\n\n"
        "Le profil est detecte automatiquement selon la volatilite du coin."
    )
    await update.message.reply_text(msg)

async def cmd_portefeuille(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Recuperation de ton portefeuille Coinbase...")
    try:
        portfolio   = await get_portfolio(COINBASE_API_KEY, COINBASE_API_SECRET)
        eur_balance = portfolio.get("EUR", 0)
        if not portfolio:
            await update.message.reply_text("Impossible de recuperer le portefeuille.")
            return
        msg  = "TON PORTEFEUILLE COINBASE\n\n"
        msg += f"EUR disponible : {eur_balance:.2f} EUR\n\n"
        for currency, balance in portfolio.items():
            if currency != "EUR" and balance > 0:
                msg += f"{currency} : {balance:.6f}\n"
    except Exception as e:
        msg = f"Erreur : {e}"
    await update.message.reply_text(msg)

async def cmd_recap(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Recuperation de ton portefeuille Coinbase...")

    portfolio_history = await get_portfolio_with_history(COINBASE_API_KEY, COINBASE_API_SECRET)
    eur_balance       = await get_eur_balance(COINBASE_API_KEY, COINBASE_API_SECRET)
    usdc_balance_val  = await get_usdc_balance(COINBASE_API_KEY, COINBASE_API_SECRET)

    msg  = f"PORTEFEUILLE COINBASE\n"
    msg += f"{datetime.now().strftime('%d/%m/%Y %H:%M')}\n"
    msg += "─────────────────\n\n"
    msg += f"EUR  disponible : {eur_balance:.2f} EUR\n"
    if usdc_balance_val > 0:
        msg += f"USDC disponible : {usdc_balance_val:.2f} USDC\n"
    msg += "\n"

    total_investi  = 0
    total_actuel   = 0

    for symbol, data in portfolio_history.items():
        balance   = data["balance"]
        avg_price = data["avg_price"]
        product_id = data["product_id"]

        # Prix actuel
        current_price = await get_product_price(product_id, COINBASE_API_KEY, COINBASE_API_SECRET)
        if not current_price:
            # Essayer l'autre paire
            alt = product_id.replace("-USDC", "-EUR") if "-USDC" in product_id else product_id.replace("-EUR", "-USDC")
            current_price = await get_product_price(alt, COINBASE_API_KEY, COINBASE_API_SECRET)

        val_actuelle = balance * current_price if current_price else 0
        investi      = data["total_invested_eur"]

        if avg_price and current_price:
            pct  = ((current_price - avg_price) / avg_price) * 100
            pnl  = val_actuelle - investi
            signe = "+" if pnl >= 0 else ""
            statut = "GAIN" if pct >= 0 else "PERTE"
        else:
            pct = pnl = 0
            signe = ""
            statut = "?"

        msg += f"{'─' * 20}\n"
        msg += f"{symbol} — {statut}\n"
        msg += f"Quantite   : {balance:.6f}\n"
        if current_price:
            msg += f"Prix actuel : {current_price:,.4f} EUR\n"
        if avg_price:
            msg += f"Prix moyen  : {avg_price:,.4f} EUR\n"
        if investi:
            msg += f"Investi     : {investi:.2f} EUR\n"
        if val_actuelle:
            msg += f"Valeur      : {val_actuelle:.2f} EUR\n"
        if pnl:
            msg += f"P&L         : {signe}{pnl:.2f} EUR ({signe}{pct:.1f}%)\n"
        msg += f"Dernier achat : {data['last_buy_date']}\n\n"

        total_investi += investi
        total_actuel  += val_actuelle
        await asyncio.sleep(0.2)

    if total_investi > 0:
        total_pnl = total_actuel - total_investi
        total_pct = (total_pnl / total_investi) * 100
        signe     = "+" if total_pnl >= 0 else ""
        msg += "─────────────────\n"
        msg += f"TOTAL INVESTI  : {total_investi:.2f} EUR\n"
        msg += f"VALEUR ACTUELLE: {total_actuel:.2f} EUR\n"
        msg += f"P&L TOTAL      : {signe}{total_pnl:.2f} EUR ({signe}{total_pct:.1f}%)\n"

    if not portfolio_history:
        msg += "Aucune crypto detectee dans ton portefeuille."

    await update.message.reply_text(msg)

async def cmd_scanner(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Scan en cours (peut prendre 1-2 minutes)...")
    await scan_opportunities(ctx.bot)
    await update.message.reply_text("Scan termine ! Les opportunites ont ete envoyees.")

async def cmd_nouveautes(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Recherche des nouvelles cryptos...")
    await check_new_listings(ctx.bot)

async def cmd_historique(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    closed = get_closed_trades()
    if not closed:
        await update.message.reply_text("Aucun trade clos pour l instant.")
        return

    msg = "HISTORIQUE DES TRADES\n\n"
    for key, trade in list(closed.items())[-10:]:
        pnl   = trade.get("pnl_eur", 0)
        pct   = trade.get("pnl_pct", 0)
        signe = "+" if pnl >= 0 else ""
        profil = "UV" if trade.get("is_ultra_volatile") else "STD"
        msg += (
            f"{trade['symbol']} [{profil}] — {trade['date']}\n"
            f"Entree : {trade['entry_price']:,.4f} | Sortie : {trade.get('sell_price', 0):,.4f}\n"
            f"P&L    : {signe}{pnl:.2f} EUR ({signe}{pct:.1f}%)\n"
            f"Raison : {trade.get('sell_reason', '?')}\n\n"
        )

    summary = get_trade_summary()
    msg += f"Total P&L : {summary['total_pnl_closed']:+.2f} EUR\n"
    msg += f"Win rate  : {summary['winrate']:.0f}%"
    await update.message.reply_text(msg)

async def cmd_buy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args
    if len(args) < 2:
        await update.message.reply_text("Usage : /buy BTC 500 [prix_optionnel] [date]")
        return
    try:
        coin   = args[0].upper()
        amount = float(args[1])
        date   = None

        if len(args) >= 3:
            price = float(args[2])
            date  = args[3] if len(args) > 3 else None
        else:
            product_id = f"{coin}-EUR"
            await update.message.reply_text(f"Recherche du prix {coin} sur Coinbase...")
            price = await get_product_price(product_id, COINBASE_API_KEY, COINBASE_API_SECRET)
            if not price:
                await update.message.reply_text(
                    f"Prix introuvable pour {product_id} sur Coinbase.\n"
                    f"Specifie le prix manuellement : /buy {coin} {amount} <prix>"
                )
                return

        key, pos = add_manual_position(coin, amount, price, date)
        await update.message.reply_text(
            f"Position manuelle ajoutee !\n\n"
            f"Crypto : {coin}\n"
            f"Montant : {amount:.2f} EUR\n"
            f"Prix entree : {price:,.4f} EUR\n"
            f"ID : {key}"
        )
    except ValueError:
        await update.message.reply_text("Erreur : montant et prix doivent etre des nombres.")

async def cmd_prix(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    coins  = [c.upper() for c in ctx.args] if ctx.args else ["BTC", "ETH", "SOL"]
    prices = await get_coingecko_prices(coins)
    if not prices:
        await update.message.reply_text("Impossible de recuperer les prix.")
        return
    msg = "Prix actuels\n\n"
    for coin, info in prices.items():
        msg += f"{coin} : {info['price']:,.4f} EUR ({info['change_24h']:+.1f}% 24h)\n"
    await update.message.reply_text(msg)

async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Screenshot recu ! Analyse en cours...")
    photo = update.message.photo[-1]
    file  = await ctx.bot.get_file(photo.file_id)
    async with aiohttp.ClientSession() as session:
        async with session.get(file.file_path) as r:
            image_bytes = await r.read()

    result = await analyze_screenshot(image_bytes)
    if not result or not result.get("coin") or not result.get("amount_eur") or not result.get("entry_price"):
        await update.message.reply_text(
            "Impossible de lire toutes les infos.\nUtilise : /buy BTC 500 65000"
        )
        return

    key, pos = add_manual_position(
        result["coin"], float(result["amount_eur"]),
        float(result["entry_price"]), result.get("date")
    )
    await update.message.reply_text(
        f"Position ajoutee depuis screenshot !\n\n"
        f"Crypto : {result['coin']}\n"
        f"Montant : {float(result['amount_eur']):.2f} EUR\n"
        f"Prix : {float(result['entry_price']):,.4f} EUR\n"
        f"ID : {key}"
    )


async def cmd_debug(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    from trader import _jsonbin_available, load_trades, JSONBIN_ID, JSONBIN_KEY
    jsonbin_ok = _jsonbin_available()
    trades     = get_active_trades()
    msg = (
        f"DEBUG JSONBIN\n\n"
        f"JSONBIN_BIN_ID : {'OK ' + JSONBIN_ID[:8] if JSONBIN_ID else 'VIDE'}\n"
        f"JSONBIN_KEY    : {'OK' if JSONBIN_KEY else 'VIDE'}\n"
        f"JSONBin dispo  : {'OUI' if jsonbin_ok else 'NON'}\n\n"
        f"Trades actifs  : {len(trades)}\n"
    )
    for key, t in trades.items():
        msg += f"- {t['symbol']} : {t['amount_eur']}EUR\n"
    await update.message.reply_text(msg)




async def monitoring_loop(app):
    while True:
        try:
            await monitor_trades(app)
        except Exception as e:
            logger.error(f"Erreur monitoring: {e}")
        await asyncio.sleep(CHECK_INTERVAL)

async def scanner_loop(app):
    await asyncio.sleep(60)
    while True:
        try:
            await scan_opportunities(app)
        except Exception as e:
            logger.error(f"Erreur scanner: {e}")
        await asyncio.sleep(SCANNER_INTERVAL)

async def listings_loop(app):
    await asyncio.sleep(120)
    while True:
        try:
            await check_new_listings(app)
        except Exception as e:
            logger.error(f"Erreur listings: {e}")
        await asyncio.sleep(NEW_LISTINGS_INTERVAL)

async def morning_recap(app):
    """Recap automatique chaque matin a 8h."""
    while True:
        now = datetime.now()
        if now.hour == 8 and now.minute == 0:
            try:
                summary = get_trade_summary()
                msg = (
                    f"RECAP DU MATIN — {now.strftime('%d/%m/%Y')}\n\n"
                    f"Trades actifs  : {summary['active_count']}\n"
                    f"P&L total clos : {summary['total_pnl_closed']:+.2f} EUR\n"
                    f"Win rate       : {summary['winrate']:.0f}%\n\n"
                    f"STANDARD       : {summary['standard_count']} trades | {summary['standard_pnl']:+.2f} EUR\n"
                    f"ULTRA VOLATILE : {summary['ultra_count']} trades | {summary['ultra_pnl']:+.2f} EUR\n\n"
                    f"Tape /recap pour le detail complet."
                )
                await app.bot.send_message(chat_id=CHAT_ID, text=msg)
            except Exception as e:
                logger.error(f"Erreur morning recap: {e}")
        await asyncio.sleep(60)


# ─── Main ─────────────────────────────────────────────────────

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("debug",        cmd_debug))
    app.add_handler(CommandHandler("start",        cmd_start))
    app.add_handler(CommandHandler("help",         cmd_start))
    app.add_handler(CommandHandler("recap",        cmd_recap))
    app.add_handler(CommandHandler("positions",    cmd_recap))
    app.add_handler(CommandHandler("portefeuille", cmd_portefeuille))
    app.add_handler(CommandHandler("scanner",      cmd_scanner))
    app.add_handler(CommandHandler("nouveautes",   cmd_nouveautes))
    app.add_handler(CommandHandler("historique",   cmd_historique))
    app.add_handler(CommandHandler("buy",          cmd_buy))
    app.add_handler(CommandHandler("prix",         cmd_prix))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    async def on_startup(app):
        asyncio.create_task(monitoring_loop(app))
        asyncio.create_task(scanner_loop(app))
        asyncio.create_task(listings_loop(app))
        asyncio.create_task(morning_recap(app))

    app.post_init = on_startup

    logger.info("Agent de trading demarre — surveillance + scanner + listings actifs")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
