import json
import aiohttp
import logging

logger = logging.getLogger(__name__)


async def get_candles(product_id, api_key, api_secret, granularity="ONE_HOUR", limit=100):
    """Recupere les chandeliers depuis Coinbase Advanced avec authentification JWT."""
    from coinbase import get_headers
    path = f"/api/v3/brokerage/products/{product_id}/candles"
    headers = get_headers(api_key, api_secret, "GET", path)
    url = f"https://api.coinbase.com{path}?granularity={granularity}&limit={limit}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as r:
                raw = await r.read()
                logger.debug(f"[get_candles] {product_id} HTTP {r.status}, {len(raw)} octets")
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError as e:
                    preview = raw[:300].decode("utf-8", errors="replace")
                    logger.error(f"[get_candles] {product_id} JSON invalide (HTTP {r.status}): {e} — body: {preview}")
                    return []
                return data.get("candles", [])
    except Exception as e:
        logger.error(f"[get_candles] {product_id} erreur réseau : {e}")
        return []


# ─── Indicateurs de base ──────────────────────────────────────

def calc_rsi(closes, period=14):
    """Calcule le RSI."""
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, period + 1):
        diff = closes[i] - closes[i - 1]
        gains.append(diff if diff > 0 else 0)
        losses.append(abs(diff) if diff < 0 else 0)

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    for i in range(period + 1, len(closes)):
        diff = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (period - 1) + (diff if diff > 0 else 0)) / period
        avg_loss = (avg_loss * (period - 1) + (abs(diff) if diff < 0 else 0)) / period

    if avg_loss == 0:
        return 100
    return 100 - (100 / (1 + avg_gain / avg_loss))


def calc_macd(closes, fast=12, slow=26, signal=9):
    """Calcule le MACD."""
    if len(closes) < slow + signal:
        return None, None, None

    def ema(data, period):
        k = 2 / (period + 1)
        val = data[0]
        for price in data[1:]:
            val = price * k + val * (1 - k)
        return val

    ema_fast = ema(closes[-fast * 2:], fast)
    ema_slow = ema(closes[-slow * 2:], slow)
    macd_line = ema_fast - ema_slow

    macd_values = []
    for i in range(slow, len(closes)):
        ef = ema(closes[max(0, i - fast * 2):i], fast)
        es = ema(closes[max(0, i - slow * 2):i], slow)
        macd_values.append(ef - es)

    if len(macd_values) < signal:
        return macd_line, None, None

    signal_line = ema(macd_values[-signal * 2:], signal)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


# ─── Nouveaux indicateurs ULTRA VOLATILE ─────────────────────

def calc_atr(highs, lows, closes, period=14):
    """
    Calcule l'ATR (Average True Range) en % du prix actuel.

    Seuils :
      < 3%  → STANDARD  (bitcoin-like, altcoin etabli)
      3-6%  → STANDARD  (altcoin normal, volatilite acceptable)
      > 6%  → ULTRA VOLATILE (memecoin, micro cap, explosion possible)
    """
    if len(closes) < period + 1:
        return None

    trs = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1])
        )
        trs.append(tr)

    if len(trs) < period:
        return None

    atr = sum(trs[-period:]) / period
    atr_pct = (atr / closes[-1]) * 100
    return round(atr_pct, 2)


def calc_obv(closes, volumes):
    """
    Calcule l'OBV (On-Balance Volume).

    Signal cle : si l'OBV monte alors que le prix n'a pas encore bouge
    = accumulation silencieuse = signal pre-pump.

    Retourne :
      obv_haussier  : True si OBV en hausse sur les 6 dernieres bougies
      acceleration  : vitesse de montee de l'OBV (> 0.1 = fort)
    """
    if len(closes) < 7 or len(volumes) < 7:
        return False, 0.0

    obv_list = [0]
    for i in range(1, len(closes)):
        if closes[i] > closes[i - 1]:
            obv_list.append(obv_list[-1] + volumes[i])
        elif closes[i] < closes[i - 1]:
            obv_list.append(obv_list[-1] - volumes[i])
        else:
            obv_list.append(obv_list[-1])

    obv_recent    = obv_list[-6:]
    obv_haussier  = obv_recent[-1] > obv_recent[0]
    base          = abs(obv_recent[0]) + 1
    acceleration  = (obv_recent[-1] - obv_recent[0]) / base

    return obv_haussier, round(acceleration, 4)


def calc_bollinger_squeeze(closes, period=20, std_mult=2.0):
    """
    Detecte le Bollinger Squeeze : bandes très serrees = explosion imminente.

    Quand les bandes se resserrent extremement, le marche accumule de l'energie.
    La prochaine bougie forte va partir tres fort dans une direction.

    Retourne :
      band_width_pct : largeur des bandes en % du prix (plus bas = plus serre)
      is_squeeze     : True si bandes < 8% du prix
      prix_position  : position du prix dans les bandes (0% = bas, 100% = haut)
    """
    if len(closes) < period:
        return None, False, 50.0

    recent   = closes[-period:]
    ma       = sum(recent) / period
    variance = sum((x - ma) ** 2 for x in recent) / period
    std      = variance ** 0.5

    upper = ma + std_mult * std
    lower = ma - std_mult * std

    band_width_pct = ((upper - lower) / ma) * 100
    is_squeeze     = band_width_pct < 8.0

    prix_position = (
        ((closes[-1] - lower) / (upper - lower)) * 100
        if upper != lower else 50.0
    )

    return round(band_width_pct, 2), is_squeeze, round(prix_position, 1)


def detect_rsi_divergence(closes, period=14, lookback=10):
    """
    Detecte une divergence haussiere RSI :
    Prix fait un nouveau plus bas MAIS RSI fait un plus bas plus haut.
    = la baisse s'epuise, renversement probable.
    """
    if len(closes) < lookback + period:
        return False

    rsi_values = []
    for i in range(lookback):
        end   = len(closes) - i
        start = max(0, end - period - 1)
        window = closes[start:end]
        rsi_val = calc_rsi(window, period)
        if rsi_val is not None:
            rsi_values.append(rsi_val)

    if len(rsi_values) < 5:
        return False

    recent_closes  = closes[-lookback:]
    price_lower    = recent_closes[-1] < min(recent_closes[:-1])
    rsi_higher     = rsi_values[-1] > min(rsi_values[:-1])

    return price_lower and rsi_higher


# ─── Analyse principale ───────────────────────────────────────

async def analyze_signals(product_id, api_key, api_secret, volume_24h=0):
    """
    Analyse tous les signaux techniques pour un produit.

    Detecte automatiquement si le coin est STANDARD ou ULTRA VOLATILE.

    Mode STANDARD       : score /6,  seuil alerte 4,  stop -25%,  trailing 15%,  max 50 EUR
    Mode ULTRA VOLATILE : score /9,  seuil alerte 5,  stop -35%,  trailing ATR*2.5,  max 15 EUR
    """
    try:
        candles = await get_candles(product_id, api_key, api_secret, granularity="ONE_HOUR", limit=100)

        if not candles or len(candles) < 30:
            return None

        candles = sorted(candles, key=lambda x: x["start"])

        closes  = [float(c["close"])  for c in candles]
        volumes = [float(c["volume"]) for c in candles]
        highs   = [float(c["high"])   for c in candles]
        lows    = [float(c["low"])    for c in candles]

        current_price  = closes[-1]
        current_volume = volumes[-1]

        # ── Indicateurs de base ───────────────────────────────
        rsi                          = calc_rsi(closes)
        macd_line, signal_line, histogram = calc_macd(closes)

        avg_volume_20  = sum(volumes[-21:-1]) / 20
        volume_ratio   = current_volume / avg_volume_20 if avg_volume_20 > 0 else 0

        price_24h_ago  = closes[-25] if len(closes) >= 25 else closes[0]
        price_72h_ago  = closes[-73] if len(closes) >= 73 else closes[0]

        change_24h = ((current_price - price_24h_ago) / price_24h_ago) * 100 if price_24h_ago else 0
        change_72h = ((current_price - price_72h_ago) / price_72h_ago) * 100 if price_72h_ago else 0

        bullish_candles = sum(1 for i in range(-3, 0) if closes[i] > closes[i - 1])

        recent_high    = max(highs[-24:])
        recent_low     = min(lows[-24:])
        price_position = (
            (current_price - recent_low) / (recent_high - recent_low) * 100
            if recent_high != recent_low else 50
        )

        # ── Indicateurs volatilite ────────────────────────────
        atr_pct                          = calc_atr(highs, lows, closes)
        obv_haussier, obv_accel          = calc_obv(closes, volumes)
        band_width, is_squeeze, boll_pos = calc_bollinger_squeeze(closes)
        rsi_divergence                   = detect_rsi_divergence(closes)

        # ── Classification ULTRA VOLATILE ─────────────────────
        # Etape 1 : eligibilite initiale
        # Un coin est CANDIDAT UV si ATR > 6% OU volume 24h < 500 000 EUR
        atr_ultra      = (atr_pct is not None and atr_pct > 6.0)
        volume_micro   = (volume_24h > 0 and volume_24h < 500_000)
        candidat_uv    = atr_ultra or volume_micro

        # Etape 2 : on calcule les 3 critères UV individuellement
        # (avant le scoring pour pouvoir compter)
        critere_atr     = atr_ultra                          # ATR > 6%
        critere_obv     = obv_haussier                       # OBV haussier
        critere_squeeze = is_squeeze                         # Bollinger Squeeze actif

        nb_criteres_uv = sum([critere_atr, critere_obv, critere_squeeze])

        # Etape 3 : classification finale
        # Pour etre ULTRA VOLATILE :
        # - ATR > 6% OBLIGATOIRE (le coin doit vraiment bouger fort)
        # - + au moins 1 autre critere parmi OBV ou Squeeze
        # Cela evite de classer de gros altcoins stables (UNI, LINK...) comme UV
        # meme si leur OBV et Squeeze sont actifs
        is_ultra_volatile = critere_atr and (critere_obv or critere_squeeze)

        # ── Scoring ───────────────────────────────────────────
        score   = 0
        signaux = []
        alertes = []

        # --- Points communs aux deux modes (6 pts max) ---

        if rsi is not None:
            if 40 <= rsi <= 65:
                score += 1
                signaux.append(f"RSI favorable ({rsi:.0f})")
            elif rsi > 75:
                alertes.append(f"RSI suracheté ({rsi:.0f})")
            elif rsi < 30:
                if rsi_divergence:
                    score += 1
                    signaux.append(f"RSI survendu + divergence haussiere ({rsi:.0f}) — retournement probable")
                else:
                    alertes.append(f"RSI survendu ({rsi:.0f})")

        if macd_line is not None and signal_line is not None:
            if macd_line > signal_line and histogram > 0:
                score += 1
                signaux.append("MACD haussier")
            elif macd_line < signal_line:
                alertes.append("MACD baissier")

        if volume_ratio >= 1.5:
            score += 1
            signaux.append(f"Volume x{volume_ratio:.1f} vs moyenne")
        elif volume_ratio < 0.7:
            alertes.append("Volume faible")

        if change_72h >= 5:
            score += 1
            signaux.append(f"Hausse 3j : +{change_72h:.1f}%")
        elif change_72h < -10:
            alertes.append(f"Baisse 3j : {change_72h:.1f}%")

        if 3 <= change_24h <= 25:
            score += 1
            signaux.append(f"Momentum 24h : +{change_24h:.1f}%")
        elif change_24h > 30:
            alertes.append(f"Hausse 24h trop forte ({change_24h:.1f}%)")

        if bullish_candles >= 2:
            score += 1
            signaux.append(f"{bullish_candles}/3 bougies haussières")

        score_max = 6

        # --- Points supplementaires ULTRA VOLATILE (+3 pts) ---
        if is_ultra_volatile:
            score_max = 9

            if atr_pct is not None and atr_pct > 6.0:
                score += 1
                signaux.append(f"ATR {atr_pct:.1f}%/bougie — fort potentiel de mouvement rapide")
            elif atr_pct is not None:
                alertes.append(f"ATR modéré {atr_pct:.1f}% — volatilité pas encore au maximum")

            if obv_haussier:
                score += 1
                if obv_accel > 0.1:
                    signaux.append(f"OBV en forte hausse — accumulation agressive détectée")
                else:
                    signaux.append(f"OBV haussier — accumulation silencieuse en cours")
            else:
                alertes.append("OBV baissier — pas d'accumulation visible")

            if is_squeeze and band_width is not None:
                score += 1
                signaux.append(f"Bollinger Squeeze ({band_width:.1f}%) — explosion de prix imminente")
            elif band_width is not None:
                alertes.append(f"Bandes Bollinger larges ({band_width:.1f}%) — pas de squeeze")

        # ── Niveau de confiance ───────────────────────────────
        ratio = score / score_max if score_max > 0 else 0
        if ratio >= 0.78:
            niveau = "FORT"
        elif ratio >= 0.55:
            niveau = "MOYEN"
        else:
            niveau = "FAIBLE"

        # ── Parametres de trading adaptes au profil ───────────
        if is_ultra_volatile:
            stop_loss_pct    = -35.0
            trailing_stop    = round(max(20.0, (atr_pct or 6.0) * 2.5), 1)
            montant_max      = 15.0
            min_score_alerte = 5
        else:
            stop_loss_pct    = -25.0
            trailing_stop    = 15.0
            montant_max      = 50.0
            min_score_alerte = 4

        return {
            "product_id":        product_id,
            "price":             current_price,
            "rsi":               rsi,
            "macd":              macd_line,
            "macd_signal":       signal_line,
            "macd_histogram":    histogram,
            "volume_ratio":      volume_ratio,
            "change_24h":        change_24h,
            "change_72h":        change_72h,
            "price_position":    price_position,
            # Indicateurs volatilite
            "atr_pct":           atr_pct,
            "obv_haussier":      obv_haussier,
            "obv_acceleration":  obv_accel,
            "bollinger_width":   band_width,
            "bollinger_squeeze": is_squeeze,
            "rsi_divergence":    rsi_divergence,
            # Classification
            "is_ultra_volatile":   is_ultra_volatile,
            "nb_criteres_uv":      nb_criteres_uv,
            "score":               score,
            "score_max":           score_max,
            "min_score_alerte":    min_score_alerte,
            "signaux":             signaux,
            "alertes":             alertes,
            "niveau":              niveau,
            # Parametres trading adaptes
            "stop_loss_pct":       stop_loss_pct,
            "trailing_stop_pct":   trailing_stop,
            "montant_max_eur":     montant_max,
        }

    except Exception as e:
        logger.error(f"[analyze_signals] {product_id} : {e}", exc_info=True)
        return None
