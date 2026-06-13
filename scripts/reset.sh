#!/bin/bash
# Reset complet : arrête tout, remet 200 USDC par bot, vide positions/historique, relance.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1

echo "=== RESET COMPLET ==="
pkill -f meme_scalper.py 2>/dev/null
pkill -f solana_bot.py 2>/dev/null
pkill -f dashboard.py 2>/dev/null
sleep 2

mkdir -p data
PAPER='{"balance_usdc":200.0,"total_invested":0.0,"pnl_total":0.0,"wins":0,"losses":0,"trades_closed":[]}'

echo '{}'     > data/meme_positions.json
echo "$PAPER" > data/meme_state.json
echo '{}'     > data/meme_cooldown.json
echo '[]'     > data/meme_trades.json

echo '{}'     > data/solana_positions.json
echo "$PAPER" > data/solana_state.json
echo '{}'     > data/solana_blacklist.json
echo '[]'     > data/solana_trades.json

: > data/meme_logs.txt
: > data/solana_logs.txt

echo "✅ États remis à zéro (200 USDC chaque bot)"
bash "$ROOT/scripts/run_all.sh"
