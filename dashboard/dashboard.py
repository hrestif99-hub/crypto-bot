#!/usr/bin/env python3
"""
dashboard.py — monitoring temps réel des deux bots.

Lit les états dans ./data (via config.py), même structure pour les deux bots.
URL : http://<serveur>:8080  —  mot de passe : DASHBOARD_PASSWORD (.env)
"""

import os
import sys
import json
import time
from datetime import datetime, timedelta
from functools import wraps

from flask import Flask, render_template, request, session, redirect, url_for, jsonify
import requests

# Import config depuis le dossier parent
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config as C

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(24).hex())
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "purecrypto2026")
SESSION_HOURS = int(os.environ.get("SESSION_HOURS", "24"))

_price_cache: dict = {}
CACHE_TTL = 30


def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("authenticated"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


def read_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default if default is not None else {}


def read_tail(path, n=200):
    try:
        with open(path, "r", errors="replace") as f:
            return f.readlines()[-n:]
    except Exception:
        return []


def fetch_prices(mints):
    now = time.time()
    stale = [m for m in mints if m not in _price_cache or now - _price_cache[m]["ts"] > CACHE_TTL]
    for i in range(0, len(stale), 30):
        chunk = stale[i:i + 30]
        try:
            url = f"https://api.dexscreener.com/latest/dex/tokens/{','.join(chunk)}"
            data = requests.get(url, timeout=10).json()
            for pair in data.get("pairs") or []:
                addr = (pair.get("baseToken") or {}).get("address", "")
                try:
                    price = float(pair.get("priceUsd") or 0)
                except (ValueError, TypeError):
                    price = 0.0
                try:
                    vol = float((pair.get("volume") or {}).get("h24") or 0)
                except (ValueError, TypeError):
                    vol = 0.0
                if addr and price > 0 and vol >= _price_cache.get(addr, {}).get("vol", 0):
                    _price_cache[addr] = {"price": price, "vol": vol, "ts": now}
        except Exception:
            pass
    return {m: _price_cache[m]["price"] for m in mints if m in _price_cache}


def bot_summary(state, positions, trades_file, prices, label):
    """Construit le résumé d'un bot (structure identique pour les deux)."""
    bal     = float(state.get("balance_usdc", C.PAPER_START_USDC))
    pnl     = float(state.get("pnl_total", 0))
    wins    = int(state.get("wins", 0))
    losses  = int(state.get("losses", 0))
    total   = wins + losses
    winrate = round(wins / total * 100, 1) if total else 0

    pos_out = []
    for mint, p in positions.items():
        cur   = prices.get(mint, 0)
        entry = float(p.get("entry_price") or 0)
        inv   = float(p.get("amount_usdc") or 0)
        pp    = ((cur - entry) / entry * 100) if entry > 0 and cur > 0 else 0
        pu    = (cur / entry - 1) * inv if entry > 0 and cur > 0 else 0
        pos_out.append({
            "bot": label, "symbol": p.get("symbol", mint[:6]), "mint": mint,
            "amount_usdc": round(inv, 2), "entry_price": entry, "current_price": cur,
            "pnl_pct": round(pp, 2), "pnl_usdc": round(pu, 2),
            "open_ts": p.get("open_ts", ""),
            "tp1_done": p.get("tp1_done", False), "tp2_done": p.get("tp2_done", False),
        })

    history = list(state.get("trades_closed", []))
    history.sort(key=lambda t: t.get("ts", ""), reverse=True)
    return {
        "balance": round(bal, 2), "pnl_total": round(pnl, 2),
        "wins": wins, "losses": losses, "win_rate": winrate, "trades_total": total,
        "positions": pos_out, "history": history,
    }


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        if request.form.get("password") == DASHBOARD_PASSWORD:
            session["authenticated"] = True
            session.permanent = True
            app.permanent_session_lifetime = timedelta(hours=SESSION_HOURS)
            return redirect(url_for("index"))
        error = "Mot de passe incorrect"
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@require_auth
def index():
    return render_template("index.html")


@app.route("/api/data")
@require_auth
def api_data():
    meme_pos   = read_json(C.MEME_POSITIONS_FILE)
    meme_state = read_json(C.MEME_STATE_FILE)
    sol_pos    = read_json(C.SOL_POSITIONS_FILE)
    sol_state  = read_json(C.SOL_STATE_FILE)

    mints = [m for m in list(meme_pos.keys()) + list(sol_pos.keys()) if m]
    prices = fetch_prices(mints) if mints else {}

    return jsonify({
        "meme":   bot_summary(meme_state, meme_pos, C.MEME_TRADES_FILE, prices, "meme_scalper"),
        "solana": bot_summary(sol_state, sol_pos, C.SOL_TRADES_FILE, prices, "solana_bot"),
        "updated_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
    })


@app.route("/api/logs/<bot>")
@require_auth
def api_logs(bot):
    path = {"meme": C.MEME_LOG_FILE, "solana": C.SOL_LOG_FILE}.get(bot)
    if not path:
        return jsonify([])
    lines = read_tail(path, 200)
    fq = request.args.get("filter", "").upper()
    if fq:
        lines = [l for l in lines if fq in l.upper()]
    return jsonify([l.rstrip("\n") for l in lines])


@app.route("/api/token_chart/<mint>")
@require_auth
def api_token_chart(mint):
    gt = {"Accept": "application/json;version=20230302"}
    pool = None
    try:
        d = requests.get(
            f"https://api.geckoterminal.com/api/v2/networks/solana/tokens/{mint}?include=top_pools",
            timeout=8, headers=gt).json()
        pools = d.get("data", {}).get("relationships", {}).get("top_pools", {}).get("data", [])
        if pools:
            pool = pools[0]["id"].split("_", 1)[-1]
    except Exception:
        pass
    if pool:
        try:
            rows = requests.get(
                f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{pool}/ohlcv/hour"
                f"?limit=24&currency=usd&token=base", timeout=8, headers=gt
            ).json().get("data", {}).get("attributes", {}).get("ohlcv_list", [])
            if rows:
                pts = sorted([{"ts": int(r[0]) * 1000, "price": float(r[4])} for r in rows],
                             key=lambda p: p["ts"])
                return jsonify({"points": pts, "source": "geckoterminal"})
        except Exception:
            pass
    return jsonify({"points": [], "source": "none"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), debug=False)
