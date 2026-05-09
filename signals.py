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


def calc_rsi(closes, period=14):
    """Calcule le RSI."""
    if len(closes) < period + 1:
        return None
    gains = []
    losses = []
    for i in range(1, period + 1):
        diff = closes[i] - closes[i - 1]
        if diff > 0:
            gains.append(diff)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(diff))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    for i in range(period + 1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gain = diff if diff > 0 else 0
        loss = abs(diff) if diff < 0 else 0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period

    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calc_macd(closes, fast=12, slow=26, signal=9):
    """Calcule le MACD."""
    if len(closes) < slow + signal:
        return None, None, None

    def ema(data, period):
        k = 2 / (period + 1)
        ema_val = data[0]
        for price in data[1:]:
            ema_val = price * k + ema_val * (1 - k)
        return ema_val

    ema_fast = ema(closes[-fast*2:], fast)
    ema_slow = ema(closes[-slow*2:], slow)
    macd_line = ema_fast - ema_slow

    macd_values = []
    for i in range(slow, len(closes)):
        ef = ema(closes[max(0, i-fast*2):i], fast)
        es = ema(closes[max(0, i-slow*2):i], slow)
        macd_values.append(ef - es)

    if len(macd_values) < signal:
        return macd_line, None, None

    signal_line = ema(macd_values[-signal*2:], signal)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


async def analyze_signals(product_id, api_key, api_secret):
    """
    Analyse tous les signaux techniques pour un produit.
    Retourne un dict avec tous les indicateurs et un score global.
    """
    try:
        candles = await get_candles(product_id, api_key, api_secret, granularity="ONE_HOUR", limit=100)

        if not candles or len(candles) < 30:
            return None

        candles = sorted(candles, key=lambda x: x["start"])

        closes = [float(c["close"]) for c in candles]
        volumes = [float(c["volume"]) for c in candles]
        highs = [float(c["high"]) for c in candles]
        lows = [float(c["low"]) for c in candles]

        current_price = closes[-1]
        current_volume = volumes[-1]

        rsi = calc_rsi(closes)
        macd_line, signal_line, histogram = calc_macd(closes)

        avg_volume_20 = sum(volumes[-21:-1]) / 20
        volume_ratio = current_volume / avg_volume_20 if avg_volume_20 > 0 else 0

        price_24h_ago = closes[-25] if len(closes) >= 25 else closes[0]
        price_72h_ago = closes[-73] if len(closes) >= 73 else closes[0]

        change_24h = ((current_price - price_24h_ago) / price_24h_ago) * 100 if price_24h_ago else 0
        change_72h = ((current_price - price_72h_ago) / price_72h_ago) * 100 if price_72h_ago else 0

        bullish_candles = sum(1 for i in range(-3, 0) if closes[i] > closes[i-1])

        recent_high = max(highs[-24:])
        recent_low = min(lows[-24:])
        price_position = ((current_price - recent_low) / (recent_high - recent_low) * 100) if recent_high != recent_low else 50

        score = 0
        signaux = []
        alertes = []

        if rsi is not None:
            if 40 <= rsi <= 65:
                score += 1
                signaux.append(f"RSI favorable ({rsi:.0f})")
            elif rsi > 75:
                alertes.append(f"RSI suracheté ({rsi:.0f})")
            elif rsi < 30:
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

        return {
            "product_id": product_id,
            "price": current_price,
            "rsi": rsi,
            "macd": macd_line,
            "macd_signal": signal_line,
            "macd_histogram": histogram,
            "volume_ratio": volume_ratio,
            "change_24h": change_24h,
            "change_72h": change_72h,
            "price_position": price_position,
            "score": score,
            "score_max": 6,
            "signaux": signaux,
            "alertes": alertes,
            "niveau": "FORT" if score >= 5 else "MOYEN" if score >= 4 else "FAIBLE"
        }
    except Exception as e:
        logger.error(f"[analyze_signals] {product_id} : {e}", exc_info=True)
        return None
