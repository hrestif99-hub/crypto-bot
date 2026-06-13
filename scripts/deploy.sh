#!/bin/bash
# Sur le serveur : récupère la dernière version + redémarre tout.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1

echo "→ git pull"
git fetch origin && git reset --hard origin/master

pkill -f meme_scalper.py 2>/dev/null
pkill -f solana_bot.py 2>/dev/null
pkill -f dashboard.py 2>/dev/null
sleep 2

bash "$ROOT/scripts/run_all.sh"
