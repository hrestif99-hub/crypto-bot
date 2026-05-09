import json
import os
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

TRADES_FILE = "trades.json"

# ─── Gestion des trades actifs ────────────────────────────────

def load_trades():
    if os.path.exists(TRADES_FILE):
        with open(TRADES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_trades(trades):
    with open(TRADES_FILE, "w", encoding="utf-8") as f:
        json.dump(trades, f, indent=2, ensure_ascii=False)

def add_trade(product_id, symbol, amount_eur, entry_price, quantity, order_id):
    trades = load_trades()
    key = f"{symbol}_{int(__import__('time').time())}"
    trades[key] = {
        "product_id": product_id,
        "symbol": symbol,
        "amount_eur": amount_eur,
        "entry_price": entry_price,
        "quantity": quantity,
        "order_id": order_id,
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "peak_price": entry_price,       # Prix le plus haut atteint
        "peak_pct": 0.0,                 # % gain maximum atteint
        "status": "active",
        "trailing_stop_pct": 15.0,       # Vend si baisse de 15% depuis le pic
        "stop_loss_pct": -25.0,          # Vend si perte de 25% depuis entree
    }
    save_trades(trades)
    return key, trades[key]

def update_trade_peak(key, current_price):
    """Met a jour le pic de prix pour le trailing stop."""
    trades = load_trades()
    if key not in trades:
        return
    trade = trades[key]
    entry_price = trade["entry_price"]
    if not entry_price:
        return
    current_pct = ((current_price - entry_price) / entry_price) * 100

    if current_price > trade["peak_price"]:
        trades[key]["peak_price"] = current_price
        trades[key]["peak_pct"] = current_pct

    save_trades(trades)

def should_sell(trade, current_price):
    """
    Determine si on doit vendre selon le trailing stop et stop loss.
    Retourne (bool, raison)
    """
    entry_price = trade["entry_price"]
    peak_price = trade["peak_price"]

    if not entry_price or not peak_price:
        return False, ""

    pct_from_entry = ((current_price - entry_price) / entry_price) * 100
    pct_from_peak = ((current_price - peak_price) / peak_price) * 100

    # Stop loss dur — perte trop importante depuis entree
    if pct_from_entry <= trade["stop_loss_pct"]:
        return True, f"STOP LOSS : {pct_from_entry:.1f}% depuis entree"

    # Trailing stop — baisse depuis le pic
    # Seulement si on a deja fait au least +10% de gain
    if trade["peak_pct"] >= 10 and pct_from_peak <= -trade["trailing_stop_pct"]:
        return True, f"TRAILING STOP : -{trade['trailing_stop_pct']}% depuis pic (+{trade['peak_pct']:.1f}%)"

    return False, ""

def close_trade(key, sell_price, reason):
    """Marque un trade comme clos."""
    trades = load_trades()
    if key not in trades:
        return
    trade = trades[key]
    entry_price = trade["entry_price"]
    if entry_price:
        pnl_pct = ((sell_price - entry_price) / entry_price) * 100
        pnl_eur = (sell_price - entry_price) * trade["quantity"]
    else:
        pnl_pct = 0.0
        pnl_eur = 0.0

    trades[key]["status"] = "closed"
    trades[key]["sell_price"] = sell_price
    trades[key]["sell_date"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    trades[key]["sell_reason"] = reason
    trades[key]["pnl_pct"] = pnl_pct
    trades[key]["pnl_eur"] = pnl_eur
    save_trades(trades)
    return pnl_pct, pnl_eur

def get_active_trades():
    trades = load_trades()
    return {k: v for k, v in trades.items() if v.get("status") == "active"}

def get_closed_trades():
    trades = load_trades()
    return {k: v for k, v in trades.items() if v.get("status") == "closed"}

def get_trade_summary():
    """Retourne un resume de tous les trades."""
    trades = load_trades()
    active = {k: v for k, v in trades.items() if v.get("status") == "active"}
    closed = {k: v for k, v in trades.items() if v.get("status") == "closed"}

    total_invested = sum(t["amount_eur"] for t in active.values())
    total_pnl_closed = sum(t.get("pnl_eur", 0) for t in closed.values())
    wins = sum(1 for t in closed.values() if t.get("pnl_eur", 0) > 0)
    losses = sum(1 for t in closed.values() if t.get("pnl_eur", 0) <= 0)

    return {
        "active_count": len(active),
        "closed_count": len(closed),
        "total_invested": total_invested,
        "total_pnl_closed": total_pnl_closed,
        "wins": wins,
        "losses": losses,
        "winrate": (wins / len(closed) * 100) if closed else 0
    }
