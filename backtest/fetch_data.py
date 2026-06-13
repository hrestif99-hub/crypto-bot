#!/usr/bin/env python3
"""
ÉTAPE 1 du backtest — téléchargement de l'historique de prix.

Récupère les bougies 5 min de Binance (gratuit, sans clé) pour BONK/WIF
et les stocke en CSV dans backtest/data/.

Usage :
    python3 backtest/fetch_data.py            # 6 mois par défaut
    python3 backtest/fetch_data.py 12         # 12 mois

Rien à voir avec les bots en prod : c'est un outil d'analyse offline.
"""

import csv
import os
import sys
import time
from datetime import datetime, timezone, timedelta

import requests

BINANCE_KLINES = "https://api.binance.com/api/v3/klines"
INTERVAL       = "5m"
SYMBOLS        = ["BONKUSDT", "WIFUSDT"]
DATA_DIR       = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

# 1000 bougies max par requête ; en 5 min ça fait ~3.47 jours par appel
MAX_LIMIT   = 1000
INTERVAL_MS = 5 * 60 * 1000


def fetch_symbol(symbol: str, months: int) -> list:
    """Pagine en avant depuis (now - months) jusqu'à maintenant."""
    start = int((datetime.now(timezone.utc) - timedelta(days=30 * months)).timestamp() * 1000)
    now   = int(datetime.now(timezone.utc).timestamp() * 1000)
    rows  = []
    while start < now:
        try:
            r = requests.get(BINANCE_KLINES, params={
                "symbol": symbol, "interval": INTERVAL,
                "startTime": start, "limit": MAX_LIMIT,
            }, timeout=15)
            if r.status_code != 200:
                print(f"  [{symbol}] HTTP {r.status_code} : {r.text[:120]}")
                break
            batch = r.json()
        except Exception as e:
            print(f"  [{symbol}] erreur réseau : {e}")
            break
        if not batch:
            break
        # Binance kline : [openTime, open, high, low, close, volume, closeTime, ...]
        for k in batch:
            rows.append([k[0], k[1], k[2], k[3], k[4], k[5]])
        last_open = batch[-1][0]
        start = last_open + INTERVAL_MS
        if len(batch) < MAX_LIMIT:
            break
        time.sleep(0.25)  # courtoisie envers l'API
    return rows


def save_csv(symbol: str, rows: list) -> str:
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, f"{symbol}_{INTERVAL}.csv")
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["open_time", "open", "high", "low", "close", "volume"])
        w.writerows(rows)
    return path


def main():
    months = int(sys.argv[1]) if len(sys.argv) > 1 else 6
    print(f"=== Telechargement {INTERVAL} sur ~{months} mois ===")
    for symbol in SYMBOLS:
        print(f"-> {symbol}")
        rows = fetch_symbol(symbol, months)
        if not rows:
            print(f"  [!] aucune donnee pour {symbol}")
            continue
        path = save_csv(symbol, rows)
        d0 = datetime.fromtimestamp(rows[0][0] / 1000, timezone.utc).strftime("%Y-%m-%d")
        d1 = datetime.fromtimestamp(rows[-1][0] / 1000, timezone.utc).strftime("%Y-%m-%d")
        print(f"  [OK] {len(rows):,} bougies  ({d0} -> {d1})  ->  {path}")


if __name__ == "__main__":
    main()
