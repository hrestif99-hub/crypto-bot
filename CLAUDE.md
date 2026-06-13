# CRYPTO-BOT — CONTEXTE

## Serveur
- Hetzner VPS : `root@62.238.40.233` (Ubuntu 24.04)
- Connexion : `ssh root@62.238.40.233`
- Dossier serveur cible : `/root/crypto-bot/` (un seul dossier, plus de submodules)

## GitHub
- Repo : `https://github.com/hrestif99-hub/crypto-bot`
- Branche : `master`

## Structure (source unique de vérité)
- `config.py` — TOUS les paramètres. **Le seul fichier à éditer pour régler.**
- `common.py` — infra partagée (Jupiter/Telegram/marché/persistance/risque).
- `engine.py` — moteur de sorties partagé (stall → TP1 → TP2 → trailing → SL).
- `meme_scalper.py` — bot tokens établis (liste fixe dans `config.Meme.TOKENS`).
- `solana_bot.py` — bot nouveaux tokens (Pump.fun + DexScreener).
- `dashboard/dashboard.py` — Flask port 8080, lit `data/`.
- `data/` — états runtime (jamais commité).

## Paramètres actuels (profil conservateur)
| | meme_scalper | solana_bot |
|---|---|---|
| Taille position | 5 USDC | 2 USDC |
| Max positions | 3 | 3 |
| Stop-loss | -8 % | -25 % |
| TP1 | +15 % (vendre 50 %) | +40 % (vendre 60 %) |
| TP2 | +30 % (vendre 50 % du reste) | +100 % (vendre 50 % du reste) |
| Trailing | armé +15 %, recul -8 % | armé +30 %, recul -20 % |
| Stall-exit | 60 min | 60 min |

Risque global : circuit breaker 3 pertes → 90 min ; limite journalière -8 USDC → pause 1 h.

## Commandes
```bash
bash scripts/run_all.sh   # démarre bots + dashboard
bash scripts/reset.sh     # reset 200 USDC + relance
bash scripts/deploy.sh    # git pull + relance (sur le serveur)
```

## Mode
- `PAPER_MODE=true` par défaut (faux argent). Trade réel = `PAPER_MODE=false` dans `.env`.

## Migration depuis l'ancienne structure
Ancien : `/root/meme_scalper/`, `/root/crypto-agent/`, `/root/dashboard/` (submodules, fichiers dupliqués).
Nouveau : tout dans `/root/crypto-bot/`. Une fois la bascule validée, supprimer les anciens dossiers.
