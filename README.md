# crypto-bot

Deux bots de paper-trading Solana + un dashboard temps réel.
**Structure unique, un seul fichier de configuration.**

```
crypto-bot/
├── config.py          ← TOUS les paramètres (le seul fichier à éditer pour régler)
├── common.py          ← infra partagée (Jupiter, Telegram, marché, persistance, risque)
├── engine.py          ← moteur de sorties partagé (stall → TP1 → TP2 → trailing → SL)
├── meme_scalper.py    ← bot tokens établis (BONK, WIF…)
├── solana_bot.py      ← bot nouveaux tokens (Pump.fun)
├── dashboard/         ← Flask, port 8080
├── data/              ← états runtime (positions, soldes, historique) — non commité
└── scripts/           ← reset.sh / run_all.sh / deploy.sh
```

## Démarrage

```bash
pip install -r requirements.txt
cp .env.example .env          # puis remplis .env
bash scripts/run_all.sh
```

Dashboard : `http://<serveur>:8080` — mot de passe dans `.env` (`DASHBOARD_PASSWORD`).

## Commandes

| Action | Commande |
|---|---|
| Tout lancer | `bash scripts/run_all.sh` |
| Reset complet (200 USDC, historique vidé) | `bash scripts/reset.sh` |
| Déployer la dernière version sur le serveur | `bash scripts/deploy.sh` |
| Changer un paramètre | éditer **`config.py`** uniquement, puis relancer |

## Sécurité

- `PAPER_MODE=true` par défaut → **faux argent**. Le trade réel ne s'active
  qu'en mettant explicitement `PAPER_MODE=false` dans `.env`.
- Garde-fous actifs : circuit breaker (3 pertes → pause), limite de perte
  journalière, cooldown par token, exposition totale plafonnée.

## Note honnête

Le scalping de memecoins est à espérance négative après frais et slippage.
Ces paramètres sont **conservateurs** (couper vite, sécuriser tôt, petites
positions) pour limiter les pertes et tester proprement — pas pour garantir un gain.
