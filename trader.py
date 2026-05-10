import json
import os
import logging
import urllib.request
import urllib.error
from datetime import datetime

logger = logging.getLogger(__name__)

TRADES_FILE = "trades.json"

# ─── Variables Railway pour persistance ──────────────────────
# Railway API pour lire/ecrire les variables d'environnement
RAILWAY_TOKEN   = os.environ.get("RAILWAY_TOKEN", "")
RAILWAY_PROJECT = os.environ.get("RAILWAY_PROJECT_ID", "")
RAILWAY_ENV     = os.environ.get("RAILWAY_ENVIRONMENT_ID", "")
RAILWAY_SERVICE = os.environ.get("RAILWAY_SERVICE_ID", "")

def _railway_available():
    return bool(RAILWAY_TOKEN and RAILWAY_PROJECT and RAILWAY_ENV and RAILWAY_SERVICE)


def _fetch_railway_variable(name):
    """Lit une variable Railway via l'API GraphQL en temps reel."""
    if not _railway_available():
        return None
    query = """
    query variables($projectId: String!, $environmentId: String!, $serviceId: String!) {
      variables(projectId: $projectId, environmentId: $environmentId, serviceId: $serviceId)
    }
    """
    variables = {
        "projectId":     RAILWAY_PROJECT,
        "environmentId": RAILWAY_ENV,
        "serviceId":     RAILWAY_SERVICE,
    }
    payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    req = urllib.request.Request(
        "https://backboard.railway.com/graphql/v2",
        data=payload,
        headers={
            "Content-Type":  "application/json",
            "Authorization": f"Bearer {RAILWAY_TOKEN}",
        },
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            variables_data = result.get("data", {}).get("variables", {})
            return variables_data.get(name)
    except Exception as e:
        logger.error(f"[fetch_railway_variable] Erreur : {e}")
        return None


def load_trades():
    """
    Charge les trades depuis :
    1. API Railway en temps reel (persistant entre redeplois)
    2. Variable d'environnement TRADES_DATA (fallback)
    3. Fichier local trades.json (fallback final)
    """
    # Priorite 1 : API Railway en temps reel
    if _railway_available():
        raw = _fetch_railway_variable("TRADES_DATA")
        if raw:
            try:
                trades = json.loads(raw)
                # Sync local pour les autres fonctions
                try:
                    with open(TRADES_FILE, "w", encoding="utf-8") as f:
                        json.dump(trades, f, indent=2, ensure_ascii=False)
                except Exception:
                    pass
                return trades
            except json.JSONDecodeError as e:
                logger.error(f"[load_trades] TRADES_DATA Railway invalide : {e}")

    # Priorite 2 : variable d'environnement au demarrage
    raw = os.environ.get("TRADES_DATA", "")
    if raw and raw != "{}":
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass

    # Priorite 3 : fichier local
    if os.path.exists(TRADES_FILE):
        try:
            with open(TRADES_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"[load_trades] Erreur lecture fichier : {e}")

    return {}


def save_trades(trades):
    """
    Sauvegarde les trades :
    1. Dans le fichier local trades.json (immédiat)
    2. Met a jour la variable TRADES_DATA via Railway API (persistant)
    """
    # Toujours sauvegarder en local
    try:
        with open(TRADES_FILE, "w", encoding="utf-8") as f:
            json.dump(trades, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"[save_trades] Erreur ecriture fichier : {e}")

    # Mettre a jour la variable Railway si token disponible
    if _railway_available():
        try:
            _update_railway_variable("TRADES_DATA", json.dumps(trades, ensure_ascii=False))
        except Exception as e:
            logger.error(f"[save_trades] Erreur mise a jour Railway : {e}")


def _update_railway_variable(name, value):
    """Met a jour une variable d'environnement Railway via l'API GraphQL."""
    query = """
    mutation upsertVariables($input: VariableCollectionUpsertInput!) {
      variableCollectionUpsert(input: $input)
    }
    """
    variables = {
        "input": {
            "projectId":     RAILWAY_PROJECT,
            "environmentId": RAILWAY_ENV,
            "serviceId":     RAILWAY_SERVICE,
            "variables":     {name: value}
        }
    }
    payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    req = urllib.request.Request(
        "https://backboard.railway.com/graphql/v2",
        data=payload,
        headers={
            "Content-Type":  "application/json",
            "Authorization": f"Bearer {RAILWAY_TOKEN}",
        },
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        result = json.loads(resp.read())
        if "errors" in result:
            logger.error(f"[Railway API] Erreurs : {result['errors']}")
        else:
            logger.debug(f"[Railway API] Variable {name} mise a jour")


def add_trade(product_id, symbol, amount_eur, entry_price, quantity, order_id,
              is_ultra_volatile=False, stop_loss_pct=None, trailing_stop_pct=None):
    """
    Enregistre un nouveau trade avec les parametres adaptes au profil.

    STANDARD      : stop -25%,  trailing 15% fixe,  activation a +10%
    ULTRA VOLATILE: stop -35%,  trailing dynamique (min 20%), activation a +15%

    L'activation plus haute pour ULTRA VOLATILE evite de se faire sortir
    sur les -15% routiniers de ces coins avant qu'ils partent vraiment.
    """
    trades = load_trades()
    key    = f"{symbol}_{int(__import__('time').time())}"

    if is_ultra_volatile:
        sl                  = stop_loss_pct     if stop_loss_pct     is not None else -35.0
        ts                  = trailing_stop_pct if trailing_stop_pct is not None else 20.0
        trailing_activation = 15.0
    else:
        sl                  = stop_loss_pct     if stop_loss_pct     is not None else -25.0
        ts                  = trailing_stop_pct if trailing_stop_pct is not None else 15.0
        trailing_activation = 10.0

    trades[key] = {
        "product_id":              product_id,
        "symbol":                  symbol,
        "amount_eur":              amount_eur,
        "entry_price":             entry_price,
        "quantity":                quantity,
        "order_id":                order_id,
        "date":                    datetime.now().strftime("%Y-%m-%d %H:%M"),
        "peak_price":              entry_price,
        "peak_pct":                0.0,
        "status":                  "active",
        "is_ultra_volatile":       is_ultra_volatile,
        "stop_loss_pct":           sl,
        "trailing_stop_pct":       ts,
        "trailing_activation":     trailing_activation,
        # Compteur anti-wick : pour ULTRA VOLATILE, on demande 2 bougies
        # consecutives sous le stop avant de vendre (evite les faux declenchements)
        "stop_loss_confirmations": 0,
    }
    save_trades(trades)
    logger.info(
        f"[add_trade] {symbol} "
        f"profil={'ULTRA VOLATILE' if is_ultra_volatile else 'STANDARD'} "
        f"stop={sl}% trailing={ts}% activation={trailing_activation}%"
    )
    return key, trades[key]


def update_trade_peak(key, current_price):
    """Met a jour le pic de prix pour le trailing stop."""
    trades      = load_trades()
    if key not in trades:
        return
    trade       = trades[key]
    entry_price = trade["entry_price"]
    if not entry_price:
        return

    current_pct = ((current_price - entry_price) / entry_price) * 100
    if current_price > trade["peak_price"]:
        trades[key]["peak_price"] = current_price
        trades[key]["peak_pct"]   = current_pct

    save_trades(trades)


def should_sell(trade, current_price):
    """
    Determine si on doit vendre.

    Retourne : (decision, raison)
      True          → vendre maintenant
      "PENDING_STOP"→ stop atteint mais en attente de confirmation (ULTRA VOLATILE)
      False         → ne pas vendre
    """
    entry_price = trade["entry_price"]
    peak_price  = trade["peak_price"]

    if not entry_price or not peak_price:
        return False, ""

    pct_from_entry      = ((current_price - entry_price) / entry_price) * 100
    pct_from_peak       = ((current_price - peak_price)  / peak_price)  * 100
    trailing_activation = trade.get("trailing_activation", 10.0)
    is_ultra            = trade.get("is_ultra_volatile", False)

    # ── Stop loss ─────────────────────────────────────────────
    if pct_from_entry <= trade["stop_loss_pct"]:
        if is_ultra:
            # ULTRA VOLATILE : 2 confirmations consecutives requises (anti-wick)
            confirmations = trade.get("stop_loss_confirmations", 0)
            if confirmations >= 2:
                return True, f"STOP LOSS confirme : {pct_from_entry:.1f}% depuis entree"
            else:
                return "PENDING_STOP", (
                    f"Stop loss atteint ({pct_from_entry:.1f}%) "
                    f"— confirmation {confirmations + 1}/2 en attente"
                )
        else:
            return True, f"STOP LOSS : {pct_from_entry:.1f}% depuis entree"

    # ── Trailing stop ─────────────────────────────────────────
    if trade["peak_pct"] >= trailing_activation and pct_from_peak <= -trade["trailing_stop_pct"]:
        return True, (
            f"TRAILING STOP : -{trade['trailing_stop_pct']}% depuis pic "
            f"(pic +{trade['peak_pct']:.1f}%, actuel {pct_from_entry:+.1f}%)"
        )

    return False, ""


def increment_stop_confirmation(key):
    """Incremente le compteur anti-wick pour ULTRA VOLATILE."""
    trades = load_trades()
    if key in trades:
        trades[key]["stop_loss_confirmations"] = (
            trades[key].get("stop_loss_confirmations", 0) + 1
        )
        save_trades(trades)
    return trades.get(key, {}).get("stop_loss_confirmations", 0)


def reset_stop_confirmation(key):
    """Remet le compteur a zero si le prix remonte au-dessus du stop."""
    trades = load_trades()
    if key in trades and trades[key].get("stop_loss_confirmations", 0) > 0:
        trades[key]["stop_loss_confirmations"] = 0
        save_trades(trades)


def close_trade(key, sell_price, reason):
    """Marque un trade comme clos et calcule le P&L."""
    trades      = load_trades()
    if key not in trades:
        return 0.0, 0.0
    trade       = trades[key]
    entry_price = trade["entry_price"]

    if entry_price:
        pnl_pct = ((sell_price - entry_price) / entry_price) * 100
        pnl_eur = (sell_price - entry_price) * trade["quantity"]
    else:
        pnl_pct = 0.0
        pnl_eur = 0.0

    trades[key].update({
        "status":     "closed",
        "sell_price": sell_price,
        "sell_date":  datetime.now().strftime("%Y-%m-%d %H:%M"),
        "sell_reason": reason,
        "pnl_pct":    pnl_pct,
        "pnl_eur":    pnl_eur,
    })
    save_trades(trades)
    return pnl_pct, pnl_eur


def get_active_trades():
    trades = load_trades()
    return {k: v for k, v in trades.items() if v.get("status") == "active"}


def get_closed_trades():
    trades = load_trades()
    return {k: v for k, v in trades.items() if v.get("status") == "closed"}


def get_trade_summary():
    """Retourne un resume global des trades, avec stats separees par profil."""
    trades  = load_trades()
    active  = {k: v for k, v in trades.items() if v.get("status") == "active"}
    closed  = {k: v for k, v in trades.items() if v.get("status") == "closed"}

    wins   = sum(1 for t in closed.values() if t.get("pnl_eur", 0) > 0)
    losses = sum(1 for t in closed.values() if t.get("pnl_eur", 0) <= 0)

    closed_std   = {k: v for k, v in closed.items() if not v.get("is_ultra_volatile")}
    closed_ultra = {k: v for k, v in closed.items() if     v.get("is_ultra_volatile")}

    return {
        "active_count":     len(active),
        "closed_count":     len(closed),
        "total_invested":   sum(t["amount_eur"] for t in active.values()),
        "total_pnl_closed": sum(t.get("pnl_eur", 0) for t in closed.values()),
        "wins":             wins,
        "losses":           losses,
        "winrate":          (wins / len(closed) * 100) if closed else 0,
        # Stats separees
        "standard_count":   len(closed_std),
        "standard_pnl":     sum(t.get("pnl_eur", 0) for t in closed_std.values()),
        "ultra_count":      len(closed_ultra),
        "ultra_pnl":        sum(t.get("pnl_eur", 0) for t in closed_ultra.values()),
    }
