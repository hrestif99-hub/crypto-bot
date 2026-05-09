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
    place_market_buy, place_market_sell, get_portfolio
)
from trader import (
    add_trade, update_trade_peak, should_sell, close_trade,
    get_active_trades, get_closed_trades, get_trade_summary
)

# ─── Configuration ───────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "").strip()
if not CHAT_ID:
    CHAT_ID = "8746800281"
print(f"DEBUG CHAT_ID: '{CHAT_ID}'")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
COINBASE_API_KEY = os.environ.get("COINBASE_API_KEY", "")
COINBASE_API_SECRET = os.environ.get("COINBASE_API_SECRET", "")

STOP_LOSS_PCT = -25.0
TRAILING_STOP_PCT = 15.0
MIN_SIGNAL_SCORE = 4        # Minimum 4/6 signaux pour alerter
MAX_AMOUNT_EUR = 50.0       # Maximum par trade
CHECK_INTERVAL = 120        # Verification positions toutes les 2 min
SCANNER_INTERVAL = 3600     # Scan toutes les heures
NEW_LISTINGS_INTERVAL = 1800

POSITIONS_FILE = "positions.json"
SEEN_LISTINGS_FILE = "seen_listings.json"
PENDING_BUYS_FILE = "pending_buys.json"

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)


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
    key = f"{coin}_{datetime.now().strftime('%Y%m%d%H%M%S')}"
    positions[key] = {
        "coin": coin,
        "amount_eur": amount_eur,
        "entry_price": entry_price,
        "quantity": amount_eur / entry_price,
        "date": date or datetime.now().strftime("%Y-%m-%d %H:%M"),
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
        "symbol": symbol,
        "analysis": analysis,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M")
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


# ─── Prix CoinGecko (pour positions manuelles) ───────────────

COIN_IDS = {
    "BTC": "bitcoin", "ETH": "ethereum", "BNB": "binancecoin",
    "SOL": "solana", "ADA": "cardano", "XRP": "ripple",
    "DOGE": "dogecoin", "AVAX": "avalanche-2", "DOT": "polkadot",
    "MATIC": "matic-network", "LINK": "chainlink", "LTC": "litecoin",
    "JTO": "jito-governance-token", "BILL": "billions-network",
}

async def get_coingecko_prices(coins):
    ids = [COIN_IDS.get(c.upper(), c.lower()) for c in coins]
    ids_str = ",".join(set(ids))
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={ids_str}&vs_currencies=eur&include_24hr_change=true"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                data = await r.json()
                result = {}
                for coin in coins:
                    coin_id = COIN_IDS.get(coin.upper(), coin.lower())
                    if coin_id in data:
                        result[coin.upper()] = {
                            "price": data[coin_id]["eur"],
                            "change_24h": data[coin_id].get("eur_24h_change", 0)
                        }
                return result
    except Exception as e:
        logger.error(f"Erreur CoinGecko: {e}")
        return {}


# ─── Score solidite projet ────────────────────────────────────

async def get_coin_details(coin_id):
    url = f"https://api.coingecko.com/api/v3/coins/{coin_id}?localization=false&tickers=false&community_data=true&developer_data=true"
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
    score = 0
    signaux = []
    alertes = []
    if details.get("links", {}).get("homepage", [None])[0]:
        score += 1
        signaux.append("Site web present")
    else:
        alertes.append("Pas de site web")
    if details.get("links", {}).get("whitepaper"):
        score += 1
        signaux.append("Whitepaper present")
    else:
        alertes.append("Pas de whitepaper")
    dev = details.get("developer_data", {})
    if dev.get("commit_count_4_weeks", 0) > 0:
        score += 2
        signaux.append(f"GitHub actif ({dev['commit_count_4_weeks']} commits/mois)")
    else:
        alertes.append("GitHub inactif")
    community = details.get("community_data", {})
    twitter = community.get("twitter_followers", 0) or 0
    if twitter > 10000:
        score += 1
        signaux.append(f"Twitter : {twitter:,} followers")
    else:
        alertes.append(f"Peu de followers ({twitter})")
    market_cap = details.get("market_data", {}).get("market_cap", {}).get("eur", 0) or 0
    if market_cap > 100000:
        score += 1
        signaux.append(f"Market cap : {market_cap:,.0f} EUR")
    else:
        alertes.append("Market cap tres faible")
    desc = details.get("description", {}).get("en", "")
    if desc and len(desc) > 100:
        score += 1
        signaux.append("Projet decrit")
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


# ─── Surveillance des trades actifs ──────────────────────────

async def monitor_trades(app):
    trades = get_active_trades()
    if not trades:
        return

    for key, trade in trades.items():
        try:
            product_id = trade["product_id"]
            current_price = await get_product_price(product_id, COINBASE_API_KEY, COINBASE_API_SECRET)
            if not current_price or current_price <= 0:
                continue

            update_trade_peak(key, current_price)
            sell, reason = should_sell(trade, current_price)

            if sell:
                success, order_id, _ = await place_market_sell(
                    COINBASE_API_KEY, COINBASE_API_SECRET,
                    product_id, trade["quantity"]
                )
                if success:
                    pnl_pct, pnl_eur = close_trade(key, current_price, reason)
                    signe = "+" if pnl_eur >= 0 else ""
                    msg = (
                        f"VENTE AUTOMATIQUE\n\n"
                        f"Crypto : {trade['symbol']}\n"
                        f"Raison : {reason}\n"
                        f"Prix entree : {trade['entry_price']:,.4f} EUR\n"
                        f"Prix vente : {current_price:,.4f} EUR\n"
                        f"Investi : {trade['amount_eur']:.2f} EUR\n"
                        f"P&L : {signe}{pnl_eur:.2f} EUR ({signe}{pnl_pct:.1f}%)\n"
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
    # Normalise : Application.bot → Bot, Bot.bot → User (bug), donc on détecte le type
    bot = app.bot if hasattr(app, 'run_polling') else app
    try:
        products = await get_available_products(COINBASE_API_KEY, COINBASE_API_SECRET)
        if not products:
            return

        # Limiter aux 50 premiers par volume pour eviter trop d appels API
        products = [p for p in products if p.get("quote_currency_id") == "EUR"]
        products = sorted(products, key=lambda x: float(x.get("volume_24h", 0) or 0), reverse=True)[:150]

        opportunities = []
        for product in products:
            product_id = product.get("product_id", "?")
            symbol = product.get("base_currency_id", "?")
            try:
                analysis = await analyze_signals(product_id, COINBASE_API_KEY, COINBASE_API_SECRET)
                if not analysis:
                    continue

                if analysis["score"] >= MIN_SIGNAL_SCORE:
                    opportunities.append((product_id, symbol, analysis))
            except Exception as e:
                logger.error(f"[scan_opportunities] {product_id} : {e}", exc_info=True)
                continue

            await asyncio.sleep(0.3)

        for product_id, symbol, analysis in opportunities:
            try:
                add_pending(product_id, symbol, analysis)
                barre = "|" * analysis["score"] + "." * (analysis["score_max"] - analysis["score"])

                keyboard = [
                    [
                        InlineKeyboardButton("Oui, acheter", callback_data=f"buy_yes_{product_id}"),
                        InlineKeyboardButton("Non", callback_data=f"buy_no_{product_id}")
                    ]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)

                msg = (
                    f"OPPORTUNITE DETECTEE\n\n"
                    f"Crypto : {symbol}\n"
                    f"Prix : {analysis['price']:,.4f} EUR\n"
                    f"Score : {analysis['score']}/{analysis['score_max']} [{barre}] {analysis['niveau']}\n\n"
                    f"Signaux :\n" +
                    "\n".join(f"+ {s}" for s in analysis["signaux"]) +
                    ("\n\nAlertes :\n" + "\n".join(f"! {a}" for a in analysis["alertes"]) if analysis["alertes"] else "") +
                    f"\n\nVariation 24h : {analysis['change_24h']:+.1f}%\n"
                    f"Variation 3j : {analysis['change_72h']:+.1f}%\n"
                    f"Volume : x{analysis['volume_ratio']:.1f} vs moyenne\n\n"
                    f"Veux-tu acheter ?"
                )

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

        seen = load_seen_listings()
        new_ones = [c for c in coins if c.get("id") and c["id"] not in seen]

        for coin in new_ones[:3]:
            seen.append(coin["id"])
            details = await get_coin_details(coin["id"])
            score, signaux, alertes = score_project(details)
            niveau = "SOLIDE" if score >= 5 else "MOYEN" if score >= 3 else "RISQUE"
            barre = "|" * score + "." * (7 - score)

            msg = (
                f"NOUVELLE CRYPTO LISTEE\n\n"
                f"Nom : {coin.get('name', '?')} ({coin.get('symbol', '?').upper()})\n"
                f"Score solidite : {score}/7 [{barre}] {niveau}\n\n"
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
        await query.edit_message_text(
            query.message.text + f"\n\nCombien d euros veux-tu investir ? (max {MAX_AMOUNT_EUR}€)\nReponds avec un nombre."
        )

    elif data.startswith("confirm_buy_"):
        parts = data.split("_")
        product_id = parts[2]
        amount = float(parts[3])

        pending = load_pending()
        if product_id not in pending:
            await query.edit_message_text("Cette opportunite a expire.")
            return

        info = pending[product_id]
        symbol = info["symbol"]

        await query.edit_message_text(f"Achat de {amount}€ de {symbol} en cours...")

        success, order_id, order_data = await place_market_buy(
            COINBASE_API_KEY, COINBASE_API_SECRET, product_id, amount
        )

        if success:
            current_price = await get_product_price(product_id, COINBASE_API_KEY, COINBASE_API_SECRET)
            if not current_price:
                remove_pending(product_id)
                await ctx.bot.send_message(
                    chat_id=CHAT_ID,
                    text=(
                        f"ACHAT {symbol} reussi (ordre {order_id}) mais prix introuvable.\n"
                        f"Ajoute le trade manuellement : /buy {symbol} {amount} <prix_achat>"
                    )
                )
                return
            quantity = amount / current_price
            key, trade = add_trade(product_id, symbol, amount, current_price, quantity, order_id)
            remove_pending(product_id)

            await ctx.bot.send_message(
                chat_id=CHAT_ID,
                text=(
                    f"ACHAT CONFIRME\n\n"
                    f"Crypto : {symbol}\n"
                    f"Montant : {amount:.2f} EUR\n"
                    f"Prix : {current_price:,.4f} EUR\n"
                    f"Quantite : {quantity:.6f}\n"
                    f"ID trade : {key}\n\n"
                    f"Trailing stop : -{TRAILING_STOP_PCT}% depuis pic\n"
                    f"Stop loss : {STOP_LOSS_PCT}%"
                )
            )
        else:
            await ctx.bot.send_message(
                chat_id=CHAT_ID,
                text=f"Erreur achat {symbol} : {order_id}"
            )


# ─── Gestion des messages texte (montant apres confirmation) ──

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if "pending_buy" in ctx.user_data:
        product_id = ctx.user_data["pending_buy"]
        try:
            amount = float(text.replace(",", ".").replace("€", "").strip())
            if amount <= 0:
                await update.message.reply_text("Le montant doit etre positif.")
                return
            if amount > MAX_AMOUNT_EUR:
                await update.message.reply_text(f"Maximum {MAX_AMOUNT_EUR}€ par trade.")
                return

            pending = load_pending()
            if product_id not in pending:
                await update.message.reply_text("Cette opportunite a expire.")
                del ctx.user_data["pending_buy"]
                return

            info = pending[product_id]
            symbol = info["symbol"]
            price = info["analysis"]["price"]

            keyboard = [[
                InlineKeyboardButton(f"Confirmer {amount}€", callback_data=f"confirm_buy_{product_id}_{amount}"),
                InlineKeyboardButton("Annuler", callback_data=f"buy_no_{product_id}")
            ]]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await update.message.reply_text(
                f"Confirmer l achat ?\n\n"
                f"Crypto : {symbol}\n"
                f"Montant : {amount:.2f} EUR\n"
                f"Prix actuel : {price:,.4f} EUR\n"
                f"Quantite estimee : {amount/price:.6f} {symbol}",
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
        "/recap — Resume complet\n"
        "/portefeuille — Ton solde Coinbase\n"
        "/scanner — Scan manuel opportunites\n"
        "/nouveautes — Nouvelles cryptos listees\n"
        "/buy BTC 500 65000 — Position manuelle\n"
        "/prix BTC ETH SOL — Prix actuels\n"
        "/historique — Tes trades passes\n\n"
        "Ou envoie un screenshot de ton trade !\n\n"
        f"Stop loss : {STOP_LOSS_PCT}% | Trailing stop : -{TRAILING_STOP_PCT}% depuis pic\n"
        f"Score minimum pour alerte : {MIN_SIGNAL_SCORE}/6"
    )
    await update.message.reply_text(msg)

async def cmd_portefeuille(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Recuperation de ton portefeuille Coinbase...")
    try:
        portfolio = await get_portfolio(COINBASE_API_KEY, COINBASE_API_SECRET)
        eur_balance = portfolio.get("EUR", 0)

        if not portfolio:
            await update.message.reply_text("Impossible de recuperer le portefeuille.")
            return

        msg = "TON PORTEFEUILLE COINBASE\n\n"
        msg += f"EUR disponible : {eur_balance:.2f} EUR\n\n"

        for currency, balance in portfolio.items():
            if currency != "EUR" and balance > 0:
                msg += f"{currency} : {balance:.6f}\n"

    except Exception as e:
        msg = f"Erreur : {e}"

    await update.message.reply_text(msg)

async def cmd_recap(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Calcul en cours...")

    # Trades automatiques actifs
    active_trades = get_active_trades()
    summary = get_trade_summary()

    msg = "RECAP COMPLET\n"
    msg += f"{datetime.now().strftime('%d/%m/%Y %H:%M')}\n"
    msg += "─────────────────\n\n"

    if active_trades:
        msg += "TRADES ACTIFS (auto)\n\n"
        for key, trade in active_trades.items():
            current_price = await get_product_price(
                trade["product_id"], COINBASE_API_KEY, COINBASE_API_SECRET
            )
            entry_price = trade["entry_price"]
            if current_price and entry_price:
                pct = ((current_price - entry_price) / entry_price) * 100
                pnl = (current_price - entry_price) * trade["quantity"]
                statut = "EN GAIN" if pct >= 0 else "EN PERTE"
                msg += (
                    f"{trade['symbol']} — {statut}\n"
                    f"Entree : {entry_price:,.4f} EUR\n"
                    f"Actuel : {current_price:,.4f} EUR\n"
                    f"P&L : {pnl:+.2f} EUR ({pct:+.1f}%)\n"
                    f"Pic : +{trade['peak_pct']:.1f}%\n\n"
                )
            else:
                msg += (
                    f"{trade['symbol']} — prix indisponible\n"
                    f"Entree : {entry_price:,.4f} EUR\n"
                    f"Pic : +{trade['peak_pct']:.1f}%\n\n"
                )
            await asyncio.sleep(0.3)

    # Positions manuelles
    positions = load_positions()
    if positions:
        msg += "POSITIONS MANUELLES\n\n"
        coins = list(set(p["coin"] for p in positions.values()))
        prices = await get_coingecko_prices(coins)

        total_investi = 0
        total_actuel = 0

        for key, pos in positions.items():
            coin = pos["coin"]
            info = prices.get(coin, {})
            current = info.get("price", 0)
            change_24h = info.get("change_24h", 0)
            pct = ((current - pos["entry_price"]) / pos["entry_price"] * 100) if current else 0
            val = pos["quantity"] * current if current else 0
            pnl = val - pos["amount_eur"]
            statut = "EN GAIN" if pct >= 0 else "EN PERTE"

            msg += (
                f"{coin} — {statut}\n"
                f"Entree : {pos['entry_price']:,.4f} EUR\n"
                f"Actuel : {current:,.4f} EUR\n"
                f"24h : {change_24h:+.1f}%\n"
                f"Perf : {pct:+.1f}% | P&L : {pnl:+.2f} EUR\n\n"
            )
            total_investi += pos["amount_eur"]
            total_actuel += val

        total_pnl = total_actuel - total_investi
        total_pct = (total_pnl / total_investi * 100) if total_investi else 0
        msg += f"Total positions : {total_pnl:+.2f} EUR ({total_pct:+.1f}%)\n\n"

    # Bilan global
    msg += "─────────────────\n"
    msg += f"BILAN GLOBAL\n"
    msg += f"Trades auto actifs : {summary['active_count']}\n"
    msg += f"Trades clos : {summary['closed_count']}\n"
    msg += f"P&L trades clos : {summary['total_pnl_closed']:+.2f} EUR\n"
    if summary['closed_count'] > 0:
        msg += f"Win rate : {summary['winrate']:.0f}% ({summary['wins']}W / {summary['losses']}L)"

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
        pnl = trade.get("pnl_eur", 0)
        pct = trade.get("pnl_pct", 0)
        signe = "+" if pnl >= 0 else ""
        msg += (
            f"{trade['symbol']} — {trade['date']}\n"
            f"Entree : {trade['entry_price']:,.4f} | Sortie : {trade.get('sell_price', 0):,.4f}\n"
            f"P&L : {signe}{pnl:.2f} EUR ({signe}{pct:.1f}%)\n"
            f"Raison : {trade.get('sell_reason', '?')}\n\n"
        )

    summary = get_trade_summary()
    msg += f"Total P&L : {summary['total_pnl_closed']:+.2f} EUR\n"
    msg += f"Win rate : {summary['winrate']:.0f}%"
    await update.message.reply_text(msg)

async def cmd_buy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args
    if len(args) < 2:
        await update.message.reply_text("Usage : /buy BTC 500 [prix_optionnel] [date]")
        return
    try:
        coin = args[0].upper()
        amount = float(args[1])
        date = None

        if len(args) >= 3:
            price = float(args[2])
            date = args[3] if len(args) > 3 else None
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
    coins = [c.upper() for c in ctx.args] if ctx.args else ["BTC", "ETH", "SOL"]
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
    file = await ctx.bot.get_file(photo.file_id)
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


# ─── Boucles automatiques ─────────────────────────────────────

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
                active = get_active_trades()
                msg = (
                    f"RECAP DU MATIN — {now.strftime('%d/%m/%Y')}\n\n"
                    f"Trades actifs : {summary['active_count']}\n"
                    f"P&L total clos : {summary['total_pnl_closed']:+.2f} EUR\n"
                    f"Win rate : {summary['winrate']:.0f}%\n\n"
                    f"Tape /recap pour le detail complet."
                )
                await app.bot.send_message(chat_id=CHAT_ID, text=msg)
            except Exception as e:
                logger.error(f"Erreur morning recap: {e}")
        await asyncio.sleep(60)


# ─── Main ─────────────────────────────────────────────────────

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("recap", cmd_recap))
    app.add_handler(CommandHandler("positions", cmd_recap))
    app.add_handler(CommandHandler("portefeuille", cmd_portefeuille))
    app.add_handler(CommandHandler("scanner", cmd_scanner))
    app.add_handler(CommandHandler("nouveautes", cmd_nouveautes))
    app.add_handler(CommandHandler("historique", cmd_historique))
    app.add_handler(CommandHandler("buy", cmd_buy))
    app.add_handler(CommandHandler("prix", cmd_prix))
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