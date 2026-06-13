#!/usr/bin/env python3
"""
============================================================
 CONFIG — SOURCE UNIQUE DE VÉRITÉ POUR TOUS LES PARAMÈTRES
============================================================
Tu ne modifies QUE ce fichier pour changer un paramètre.
Les deux bots et le dashboard lisent ici.

Profil : CONSERVATEUR RÉALISTE.
  - On coupe les pertes vite (stops serrés).
  - On sécurise les gains tôt (on vend une grosse part au 1er palier).
  - Petites positions, exposition totale plafonnée.
  - Garde-fous : circuit breaker, limite de perte journalière, cooldowns.

Rappel honnête : aucun jeu de paramètres ne rend le scalping de
memecoins rentable de façon fiable. Ces réglages servent à PERDRE
LENTEMENT et à tester proprement en paper mode, pas à garantir un gain.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ─── Secrets (toujours via .env, jamais en dur) ──────────────────────
SOLANA_PRIVATE_KEY = os.environ.get("SOLANA_PRIVATE_KEY", "")
HELIUS_RPC_URL     = os.environ.get("HELIUS_RPC_URL", "https://api.mainnet-beta.solana.com")
TELEGRAM_TOKEN     = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID            = os.environ.get("CHAT_ID", "").strip()

# ─── Modes d'exécution ───────────────────────────────────────────────
# PAPER_MODE défaut = True : par sécurité, on ne trade JAMAIS en réel
# tant que tu n'as pas explicitement mis PAPER_MODE=false dans .env.
PAPER_MODE = os.environ.get("PAPER_MODE", "true").lower() == "true"
DRY_RUN    = os.environ.get("DRY_RUN", "false").lower() == "true"

# ─── Constantes chaîne Solana / Jupiter ──────────────────────────────
USDC_MINT     = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDC_DECIMALS = 6
JUPITER_BASE  = "https://lite-api.jup.ag/swap/v1"
SLIPPAGE_BPS  = 1500
SELL_SLIPPAGE_LADDER = [SLIPPAGE_BPS, 3000, 5000, 9900]  # escalade si la vente échoue
JITO_TIP_LAMPORTS = int(os.environ.get("JITO_TIP_LAMPORTS", "100000"))
MIN_SOL_FOR_RENT  = 0.005

# ─── Bankroll paper ──────────────────────────────────────────────────
PAPER_START_USDC = 200.0

# ─── Chemins de données (TOUT au même endroit : ./data) ──────────────
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))

MEME_POSITIONS_FILE = os.path.join(DATA_DIR, "meme_positions.json")
MEME_STATE_FILE     = os.path.join(DATA_DIR, "meme_state.json")
MEME_COOLDOWN_FILE  = os.path.join(DATA_DIR, "meme_cooldown.json")
MEME_TRADES_FILE    = os.path.join(DATA_DIR, "meme_trades.json")

SOL_POSITIONS_FILE  = os.path.join(DATA_DIR, "solana_positions.json")
SOL_STATE_FILE      = os.path.join(DATA_DIR, "solana_state.json")
SOL_BLACKLIST_FILE  = os.path.join(DATA_DIR, "solana_blacklist.json")
SOL_TRADES_FILE     = os.path.join(DATA_DIR, "solana_trades.json")

MEME_LOG_FILE = os.path.join(DATA_DIR, "meme_logs.txt")
SOL_LOG_FILE  = os.path.join(DATA_DIR, "solana_logs.txt")


# ════════════════════════════════════════════════════════════════════
#  MEME SCALPER — tokens établis (BONK, WIF, etc.)
#  Volatilité plus faible → stops serrés, profits modestes.
# ════════════════════════════════════════════════════════════════════
class Meme:
    # Taille & exposition
    TRADE_USDC      = 5.0      # 2.5 % de 200 USDC par position
    MAX_POSITIONS   = 3        # peu de positions → suivi simple
    MAX_TOTAL_USDC  = 15.0     # exposition totale plafonnée à 7.5 %
    MIN_USDC        = 5.0

    # Sorties
    SL_PCT          = -8.0     # stop serré : on coupe vite
    TP1_PCT         = 15.0     # +15 % → vendre 50 % (sécuriser le capital)
    TP1_SELL        = 0.50
    TP2_PCT         = 30.0     # +30 % → vendre 50 % du reste
    TP2_SELL        = 0.50
    TRAIL_ARM_PCT      = 15.0  # trailing armé dès +15 %
    TRAIL_GIVEBACK_PCT = 8.0   # on rend 8 % depuis le pic

    # Stall-exit (position qui stagne)
    STALL_MINUTES   = 60
    STALL_BAND_PCT  = 5.0      # stagne si |PnL| < 5 % après STALL_MINUTES

    # Filtres d'entrée
    MIN_VOL24_USD   = 150_000  # écarte les tokens morts ; le vrai filtre est le momentum
    ENTRY_ROC_15M   = 2.0      # momentum 15 min ≥ +2 %
    ENTRY_REL_VOL   = 1.3      # volume ≥ 1.3× la moyenne récente
    ENTRY_VWAP_TOL  = 0.98     # tolérance sous le VWAP (rejet si prix < VWAP × 0.98)

    # Cooldowns
    COOLDOWN_HOURS    = 12
    COOLDOWN_SL_HOURS = 24

    # Liste fixe surveillée
    TOKENS = {
        "BONK":   "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
        "WIF":    "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm",
        "GIGA":   "63LfDmNb3MQ8mw9MtZ2To9bEA2M71kZUUGq5tiJxcqj9",
        "POPCAT": "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr",
        "MEW":    "MEW1gQWJ3nEXg2qgERiKu7FAFj79PHvQVREQUzScPP5",
        "BOME":   "ukHH6c7mMyiWCf1b9pnWe25TSpkDDt3H5pQZgZ74J82",
        "SLERF":  "7BgBvyjrZX1YKz4oh9mjb8ZScatkkwb8DzFx7LoiVkM3",
        "SAMO":   "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU",
    }


# ════════════════════════════════════════════════════════════════════
#  SOLANA BOT — nouveaux tokens (Pump.fun / DexScreener)
#  Volatilité extrême → stop plus large, position minuscule, filtres durs.
# ════════════════════════════════════════════════════════════════════
class Solana:
    # Taille & exposition
    TRADE_USDC      = 2.0      # minuscule : tokens neufs = loterie
    MAX_POSITIONS   = 3
    MAX_TOTAL_USDC  = 6.0
    MIN_USDC        = 2.0

    # Sorties
    SL_PCT          = -25.0    # large car bonding curve très volatile
    TP1_PCT         = 40.0     # +40 % → vendre 60 % (récupérer la mise)
    TP1_SELL        = 0.60
    TP2_PCT         = 100.0    # +100 % → vendre 50 % du reste
    TP2_SELL        = 0.50
    TRAIL_ARM_PCT      = 30.0
    TRAIL_GIVEBACK_PCT = 20.0

    # Stall-exit
    STALL_MINUTES   = 60
    STALL_BAND_PCT  = 10.0

    # Filtres qualité (anti-rug)
    MIN_LIQUIDITY_USD  = 15_000
    MIN_VOL_5M         = 1_000
    MIN_AGE_SECONDS    = 120     # > 2 min : éviter le snipe instantané
    MIN_SCORE          = 60
    VOL5_OVERRIDE_PH1  = 10.0    # vol5m faible toléré si priceChange 1h ≥ ce seuil

    # Cooldowns
    COOLDOWN_HOURS    = 24
    COOLDOWN_SL_HOURS = 48


# ════════════════════════════════════════════════════════════════════
#  RISQUE GLOBAL — partagé par les deux bots
# ════════════════════════════════════════════════════════════════════
class Risk:
    MAX_CONSECUTIVE_LOSSES  = 3      # 3 pertes d'affilée → pause
    CIRCUIT_BREAKER_MINUTES = 90
    DAILY_LOSS_LIMIT_USDC   = -8.0   # 4 % du bankroll → kill-switch
    DAILY_PAUSE_SECONDS     = 3600   # durée de pause après kill-switch journalier
    RUGCHECK_MAX_SCORE      = 800    # score RugCheck max toléré (au-dessus = rejet)


# ─── Boucles (secondes) ──────────────────────────────────────────────
class Loop:
    MEME_SCAN     = 12
    MEME_MONITOR  = 30
    SOL_SCAN      = 20
    SOL_MONITOR   = 10
    WATCHDOG      = 60
