#!/bin/bash
# Démarre les 3 process (bots + dashboard). À lancer depuis n'importe où.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1
mkdir -p data

echo "→ Démarrage meme_scalper"
nohup python3 meme_scalper.py >> data/meme_logs.txt 2>&1 &
sleep 2
echo "→ Démarrage solana_bot"
nohup python3 solana_bot.py >> data/solana_logs.txt 2>&1 &
sleep 2
echo "→ Démarrage dashboard"
nohup python3 dashboard/dashboard.py >> data/dashboard.log 2>&1 &
sleep 1
echo "✅ Tout est lancé. Dashboard : http://<serveur>:8080"
