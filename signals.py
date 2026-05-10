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

def calc_vol_mcap_ratio(volume_24h, market_cap):
    """
    Ratio Volume 24h / Market Cap en %.
    Signal cle pre-pump sur les micro caps.

    Seuils :
      < 5%   → activite normale
      5-15%  → activite elevee
      > 15%  → activite anormale — signal fort
      > 50%  → spike extreme — pump potentiel imminent
    """
    if not market_cap or market_cap <= 0 or not volume_24h:
        return None
    return round((volume_24h / market_cap) * 100, 2)


def detect_inactivity_wakeup(volumes, closes, inactivity_periods=20, spike_factor=5.0):
    """
    Detecte un coin qui etait inactif et vient de se reveiller.

    Signal : volume tres faible pendant N periodes, puis spike soudain.
    C'est un des signaux les plus discriminants pour les pumps sur micro caps.

    Retourne :
      is_wakeup      : True si le coin se reveille apres inactivite
      inactivity_pct : % de bougies recentes avec volume quasi nul
      spike_ratio    : ratio entre le volume actuel et la moyenne de la periode inactive
    """
    if len(volumes) < inactivity_periods + 3:
        return False, 0.0, 0.0

    # Volume moyen sur la periode d'inactivite (bougies N+3 a N)
    periode_inactive = volumes[-(inactivity_periods + 3):-3]
    avg_inactive     = sum(periode_inactive) / len(periode_inactive) if periode_inactive else 0

    if avg_inactive <= 0:
        return False, 0.0, 0.0

    # Volume actuel (moyenne des 3 dernieres bougies)
    vol_recent   = sum(volumes[-3:]) / 3
    spike_ratio  = vol_recent / avg_inactive

    # Pourcentage de bougies avec volume < 10% de la moyenne generale
    avg_global   = sum(volumes) / len(volumes)
    bougies_inactives = sum(1 for v in periode_inactive if v < avg_global * 0.1)
    inactivity_pct    = (bougies_inactives / len(periode_inactive)) * 100

    # Wakeup si : periode inactive detectee ET spike soudain
    is_wakeup = inactivity_pct >= 40 and spike_ratio >= spike_factor

    return is_wakeup, round(inactivity_pct, 1), round(spike_ratio, 1)


async def analyze_signals(product_id, api_key, api_secret, volume_24h=0, market_cap=0):
    """
    Analyse tous les signaux techniques pour un produit.

    Detecte automatiquement si le coin est STANDARD ou ULTRA VOLATILE.

    STANDARD      : bougies H1,   score /6,  seuil 4,  stop -25%, trailing 15%,  max 50 EUR
    ULTRA VOLATILE: bougies 15min, score /11, seuil 6,  stop -35%, trailing ATR*2.5, max 15 EUR

    Nouveaux signaux UV :
      - Bougies 15min (au lieu de H1) pour detecter les mouvements rapides
      - Volume/Market Cap ratio (spike anormal vs taille du coin)
      - Detection inactivite + reveil (coin mort qui se reveille = signal fort)
    """
    try:
        # ── Etape 1 : charge d'abord les bougies 15min ────────
        # On classifie sur 15min directement (Option C)
        # ATR sur 15min est bien plus sensible aux mouvements rapides
        candles_15 = await get_candles(product_id, api_key, api_secret, granularity="FIFTEEN_MINUTE", limit=100)
        candles_h1 = await get_candles(product_id, api_key, api_secret, granularity="ONE_HOUR", limit=100)

        if not candles_h1 or len(candles_h1) < 30:
            return None

        candles_h1 = sorted(candles_h1, key=lambda x: x["start"])
        highs_h1   = [float(c["high"])  for c in candles_h1]
        lows_h1    = [float(c["low"])   for c in candles_h1]
        closes_h1  = [float(c["close"]) for c in candles_h1]

        # ATR sur 15min pour la classification UV (seuil 4%)
        # Sur 15min, ATR > 4% = coin qui bouge de 4% par quart d'heure = vraiment explosif
        if candles_15 and len(candles_15) >= 30:
            candles_15  = sorted(candles_15, key=lambda x: x["start"])
            highs_15    = [float(c["high"])  for c in candles_15]
            lows_15     = [float(c["low"])   for c in candles_15]
            closes_15   = [float(c["close"]) for c in candles_15]
            atr_15      = calc_atr(highs_15, lows_15, closes_15)
        else:
            atr_15 = None

        # Classification UV initiale
        # ATR > 3% sur 15min OU volume 24h < 50 000 EUR
        atr_ultra    = (atr_15 is not None and atr_15 > 3.0)
        volume_micro = (volume_24h > 0 and volume_24h < 50_000)
        candidat_uv  = atr_ultra or volume_micro

        # Log debug pour comprendre pourquoi les coins ne passent pas en UV
        if atr_15 is not None and atr_15 > 1.5:
            logger.info(
                f"[UV_DEBUG] {product_id} | ATR15={atr_15:.2f}% | "
                f"atr_ultra={atr_ultra} | vol24h={volume_24h:.0f} | "
                f"volume_micro={volume_micro} | candidat_uv={candidat_uv}"
            )

        # ── Etape 2 : choisir le timeframe d'analyse ───────────
        if candidat_uv and candles_15 and len(candles_15) >= 30:
            candles         = candles_15
            timeframe_label = "15min"
        else:
            candles         = candles_h1
            timeframe_label = "H1"

        closes  = [float(c["close"])  for c in candles]
        volumes = [float(c["volume"]) for c in candles]
        highs   = [float(c["high"])   for c in candles]
        lows    = [float(c["low"])    for c in candles]

        current_price  = closes[-1]
        current_volume = volumes[-1]

        # ── Indicateurs de base ───────────────────────────────
        rsi                               = calc_rsi(closes)
        macd_line, signal_line, histogram = calc_macd(closes)

        avg_volume_20 = sum(volumes[-21:-1]) / 20
        volume_ratio  = current_volume / avg_volume_20 if avg_volume_20 > 0 else 0

        # Pour UV en 15min : 24h = 96 bougies, 72h = 288 bougies
        # Pour STD en H1   : 24h = 24 bougies, 72h = 72 bougies
        if candidat_uv and timeframe_label == "15min":
            idx_24h = 96
            idx_72h = 288
        else:
            idx_24h = 25
            idx_72h = 73

        price_24h_ago = closes[-idx_24h] if len(closes) >= idx_24h else closes[0]
        price_72h_ago = closes[-idx_72h] if len(closes) >= idx_72h else closes[0]

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

        # Nouveaux indicateurs UV
        vol_mcap_ratio                        = calc_vol_mcap_ratio(volume_24h, market_cap)
        is_wakeup, inactivity_pct, spike_ratio = detect_inactivity_wakeup(volumes, closes)

        # ── Classification finale ULTRA VOLATILE ──────────────
        # ATR > 4% sur 15min obligatoire + au moins 1 autre (OBV ou Squeeze)
        critere_atr     = atr_ultra  # ATR > 4% sur 15min
        critere_obv     = obv_haussier
        critere_squeeze = is_squeeze
        nb_criteres_uv  = sum([critere_atr, critere_obv, critere_squeeze])

        is_ultra_volatile = critere_atr and (critere_obv or critere_squeeze)

        # Log debug classification finale
        if candidat_uv:
            logger.info(
                f"[UV_DEBUG] {product_id} CANDIDAT | "
                f"critere_atr={critere_atr} | critere_obv={critere_obv} | "
                f"critere_squeeze={critere_squeeze} | "
                f"is_ultra_volatile={is_ultra_volatile} | "
                f"obv_accel={obv_accel:.3f} | band_width={band_width}"
            )

        # ── Scoring ───────────────────────────────────────────
        score   = 0
        signaux = []
        alertes = []

        if is_ultra_volatile:
            # ── Score ULTRA VOLATILE sur 5 points ────────────
            # RSI et MACD retires : trop lents pour les UV
            # Seuls les signaux pertinents pour les explosions rapides
            score_max = 5

            # 1. ATR > 3% sur 15min
            if atr_15 is not None and atr_15 > 3.0:
                score += 1
                signaux.append(f"ATR {atr_15:.1f}%/bougie (15min) — mouvement rapide en cours")
            else:
                alertes.append(f"ATR faible {atr_15:.1f}% sur 15min" if atr_15 else "ATR indisponible")

            # 2. OBV haussier
            if obv_haussier:
                score += 1
                if obv_accel > 0.1:
                    signaux.append(f"OBV en forte hausse — accumulation agressive")
                else:
                    signaux.append(f"OBV haussier — accumulation silencieuse en cours")
            else:
                alertes.append("OBV baissier — pas d'accumulation visible")

            # 3. Bollinger Squeeze
            if is_squeeze and band_width is not None:
                score += 1
                signaux.append(f"Bollinger Squeeze ({band_width:.1f}%) — explosion imminente")
            else:
                alertes.append(f"Pas de squeeze ({band_width:.1f}%)" if band_width else "Squeeze indisponible")

            # 4. Vol / Market Cap > 15%
            if vol_mcap_ratio is not None:
                if vol_mcap_ratio > 50:
                    score += 1
                    signaux.append(f"Vol/MktCap {vol_mcap_ratio:.0f}% — spike extreme, pump potentiel")
                elif vol_mcap_ratio > 15:
                    score += 1
                    signaux.append(f"Vol/MktCap {vol_mcap_ratio:.0f}% — activite anormale")
                else:
                    alertes.append(f"Vol/MktCap {vol_mcap_ratio:.0f}% — activite normale")
            else:
                alertes.append("Vol/MktCap indisponible")

            # 5. Reveil apres inactivite
            if is_wakeup:
                score += 1
                signaux.append(f"REVEIL apres inactivite — volume x{spike_ratio:.0f} vs periode dormante")
            else:
                alertes.append(f"Pas de reveil detecte")

            min_score_alerte = 2

        else:
            # ── Score STANDARD sur 6 points ──────────────────
            score_max = 6

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

            if change_24h >= 40:
                score += 1
                signaux.append(f"MOMENTUM FORT : +{change_24h:.1f}% sur 24h")
            elif change_24h >= 3:
                score += 1
                signaux.append(f"Momentum 24h : +{change_24h:.1f}%")

            if bullish_candles >= 2:
                score += 1
                signaux.append(f"{bullish_candles}/3 bougies haussières")

            min_score_alerte = 5

        # ── Niveau de confiance ───────────────────────────────
        ratio = score / score_max if score_max > 0 else 0
        if ratio >= 0.8:
            niveau = "FORT"
        elif ratio >= 0.6:
            niveau = "MOYEN"
        else:
            niveau = "FAIBLE"

        # ── Parametres de trading adaptes au profil ───────────
        if is_ultra_volatile:
            stop_loss_pct = -35.0
            trailing_stop = round(max(20.0, (atr_pct or 6.0) * 2.5), 1)
            montant_max   = 15.0
        else:
            stop_loss_pct = -25.0
            trailing_stop = 15.0
            montant_max   = 50.0

        return {
            "product_id":        product_id,
            "price":             current_price,
            "timeframe":         timeframe_label,
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
            # Nouveaux indicateurs UV
            "vol_mcap_ratio":    vol_mcap_ratio,
            "is_wakeup":         is_wakeup,
            "inactivity_pct":    inactivity_pct,
            "spike_ratio":       spike_ratio,
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
            # Bypass : force l'alerte si +30% sur 24h peu importe le score
            "is_momentum_alert":   change_24h >= 30,
        }

    except Exception as e:
        logger.error(f"[analyze_signals] {product_id} : {e}", exc_info=True)
        return None
