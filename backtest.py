import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

API_URL = "https://apiv2.nobitex.ir"
MARKETS = ("BTCUSDT", "ETHUSDT")
STRATEGIES = ("TREND", "REVERSAL")

RESOLUTION = 5
DAYS = 14
INITIAL_CAPITAL = 5.0
FEE_RATE = 0.0013
STOP_LOSS = 0.02
TAKE_PROFIT = 0.04


def api_get(url):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Nobitex-Paper-Backtest/1.0"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_candles(symbol):
    now = int(time.time())
    interval = RESOLUTION * 60
    start = now - DAYS * 86400
    cursor = start
    candles = {}

    while cursor < now:
        end = min(now, cursor + interval * 200)

        query = urllib.parse.urlencode({
            "symbol": symbol,
            "resolution": str(RESOLUTION),
            "from": str(cursor),
            "to": str(end),
        })

        data = api_get(
            f"{API_URL}/market/udf/history?{query}"
        )

        if data.get("s") not in ("ok", "no_data"):
            raise RuntimeError(
                f"دریافت اطلاعات {symbol} ناموفق بود: {data.get('s')}"
            )

        timestamps = data.get("t", [])
        closes = data.get("c", [])

        if len(timestamps) != len(closes):
            raise RuntimeError("اطلاعات کندل‌ها ناقص است.")

        for ts, price in zip(timestamps, closes):
            if ts is None or price is None:
                continue

            ts = int(ts)
            price = float(price)

            if price > 0 and ts + interval <= now:
                candles[ts] = price

        if end >= now
