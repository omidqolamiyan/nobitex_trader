import json
import urllib.request
import urllib.parse
import time
from datetime import datetime, timezone


API_URL = "https://apiv2.nobitex.ir/market/udf/history"

MARKETS = [
    ("BTCUSDT", "بیت‌کوین"),
    ("ETHUSDT", "اتریوم"),
]


def get_candles(symbol, resolution, count=60):
    params = urllib.parse.urlencode({
        "symbol": symbol,
        "resolution": str(resolution),
        "to": int(time.time()),
        "countback": count,
    })

    url = f"{API_URL}?{params}"

    request = urllib.request.Request(
        url,
        headers={"User-Agent": "NobitexMarketBot/1.0"}
    )

    with urllib.request.urlopen(request, timeout=20) as response:
        data = json.loads(response.read().decode("utf-8"))

    if data.get("s") != "ok":
        raise ValueError(
            data.get("errmsg", "دریافت کندل ناموفق بود")
        )

    candles = data.get("c", [])

    if len(candles) < 25:
        raise ValueError("تعداد کندل‌های دریافتی کافی نیست")

    # حذف آخرین کندل؛ ممکن است هنوز بسته نشده باشد.
    return [float(x) for x in candles[:-1]]


def ema(values, period):
    if len(values) < period:
        raise ValueError("داده کافی برای محاسبه EMA وجود ندارد")

    result = sum(values[:period]) / period
    multiplier = 2 / (period + 1)

    for value in values[period:]:
        result = (
            value * multiplier
            + result * (1 - multiplier)
        )

    return result


def rsi(values, period=14):
    if len(values) < period + 1:
        raise ValueError("داده کافی برای محاسبه RSI وجود ندارد")

    changes = [
        values[i] - values[i - 1]
        for i in range(len(values) - period, len(values))
    ]

    gains = [max(change, 0) for change in changes]
    losses = [max(-change, 0) for change in changes]

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def analyze_scalping(closes):
    fast = ema(closes[-30:], 5)
    slow = ema(closes[-30:], 13)
    momentum = rsi(closes, 14)
    price = closes[-1]

    if fast > slow and price > fast and 50 < momentum < 70:
        return "BUY"

    if fast < slow and price < fast and 30 < momentum < 50:
        return "SELL"

    return "WAIT"


def analyze_trend(closes):
    fast = ema(closes[-40:], 9)
    slow = ema(closes[-40:], 21)
    price = closes[-1]

    if fast > slow and price > slow:
        return "BUY"

    if fast < slow and price < slow:
        return "SELL"

    return "WAIT"


def combine_signals(scalping, trend):
    if scalping == "BUY" and trend == "BUY":
        return "BUY"

    if scalping == "SELL" and trend == "SELL":
        return "SELL"

    return "WAIT"


def main():
    print("=" * 40)
    print("ربات تحلیل آزمایشی نوبیتکس")
    print(
        "زمان UTC:",
        datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    )
    print("حالت: فقط تحلیل؛ بدون معامله واقعی")
    print("=" * 40)

    for symbol, name in MARKETS:
        print(f"\n--- {name} ({symbol}) ---")

        try:
            # اسکالپینگ: کندل‌های ۵ دقیقه‌ای
            short_candles = get_candles(symbol, 5)

            # دنبال‌کردن روند: کندل‌های ۱۵ دقیقه‌ای
            long_candles = get_candles(symbol, 15)

            scalp_signal = analyze_scalping(short_candles)
            trend_signal = analyze_trend(long_candles)

            final_signal = combine_signals(
                scalp_signal,
                trend_signal
            )

            print(
                "قیمت آخرین کندل ۵ دقیقه‌ای:",
                short_candles[-1]
            )
            print("سیگنال اسکالپینگ:", scalp_signal)
            print("سیگنال روند:", trend_signal)
            print("نتیجه ترکیبی:", final_signal)

            if final_signal == "BUY":
                print("وضعیت: فرصت احتمالی خرید؛ نیازمند بررسی بیشتر")

            elif final_signal == "SELL":
                print("وضعیت: سیگنال احتمالی فروش؛ نیازمند بررسی بیشتر")

            else:
                print("وضعیت: فعلاً سیگنال ترکیبی نداریم")

        except Exception as error:
            print("خطا در تحلیل بازار:", error)


if __name__ == "__main__":
    main()
