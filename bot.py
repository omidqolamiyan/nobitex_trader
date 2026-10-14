import json
import os
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone

API_URL = "https://apiv2.nobitex.ir/market/udf/history"

STATE_FILE = "paper_state.json"

INITIAL_CAPITAL = 5.0
FEE_RATE = 0.0013

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
        headers={"User-Agent": "NobitexPaperBot/1.0"}
    )

    with urllib.request.urlopen(request, timeout=20) as response:
        data = json.loads(response.read().decode("utf-8"))

    if data.get("s") != "ok":
        raise ValueError(
            data.get("errmsg", "دریافت کندل ناموفق بود")
        )

    closes = data.get("c", [])
    times = data.get("t", [])

    if len(closes) != len(times) or len(closes) < 25:
        raise ValueError("اطلاعات کندل کافی نیست")

    # آخرین کندل ممکن است هنوز بسته نشده باشد.
    closes = [float(x) for x in closes[:-1]]
    closed_times = times[:-1]

    return closes, int(closed_times[-1])


def ema(values, period):
    if len(values) < period:
        raise ValueError("داده کافی برای EMA وجود ندارد")

    result = sum(values[:period]) / period
    multiplier = 2 / (period + 1)

    for value in values[period:]:
        result = value * multiplier + result * (1 - multiplier)

    return result


def rsi(values, period=14):
    if len(values) < period + 1:
        raise ValueError("داده کافی برای RSI وجود ندارد")

    changes = [
        values[i] - values[i - 1]
        for i in range(len(values) - period, len(values))
    ]

    gains = [max(x, 0) for x in changes]
    losses = [max(-x, 0) for x in changes]

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


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            return json.load(file)

    allocation = INITIAL_CAPITAL / len(MARKETS)

    return {
        "initial_capital": INITIAL_CAPITAL,
        "fee_rate": FEE_RATE,
        "positions": {
            symbol: {
                "cash": allocation,
                "quantity": 0.0,
                "entry_price": 0.0,
                "last_candle": 0,
            }
            for symbol, _ in MARKETS
        }
    }


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as file:
        json.dump(state, file, indent=2, ensure_ascii=False)


def main():
    state = load_state()
    total_equity = 0.0

    print("=" * 42)
    print("ربات معاملات مجازی نوبیتکس")
    print("سرمایه اولیه:", INITIAL_CAPITAL, "USDT")
    print("کارمزد فرضی هر طرف معامله:", FEE_RATE * 100, "%")
    print("حالت: بدون معامله واقعی")
    print(
        "زمان UTC:",
        datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    )
    print("=" * 42)

    for symbol, name in MARKETS:
        position = state["positions"][symbol]

        try:
            short_closes, candle_time = get_candles(symbol, 5)
            long_closes, _ = get_candles(symbol, 15)

            scalp = analyze_scalping(short_closes)
            trend = analyze_trend(long_closes)
            signal = combine_signals(scalp, trend)

            price = short_closes[-1]

            print(f"\n--- {name} ({symbol}) ---")
            print("قیمت:", price)
            print("اسکالپینگ:", scalp)
            print("روند:", trend)
            print("سیگنال نهایی:", signal)

            # هر کندل پنج‌دقیقه‌ای فقط یک بار بررسی معاملاتی می‌شود.
            if candle_time != position["last_candle"]:
                position["last_candle"] = candle_time

                if signal == "BUY" and position["quantity"] == 0:
                    cash = position["cash"]

                    if cash > 0:
                        position["quantity"] = (
                            cash * (1 - FEE_RATE) / price
                        )
                        position["entry_price"] = price
                        position["cash"] = 0.0

                        print("معامله مجازی: خرید انجام شد")

                elif signal == "SELL" and position["quantity"] > 0:
                    proceeds = (
                        position["quantity"]
                        * price
                        * (1 - FEE_RATE)
                    )

                    position["cash"] = proceeds
                    position["quantity"] = 0.0
                    position["entry_price"] = 0.0

                    print("معامله مجازی: فروش انجام شد")

                else:
                    print("معامله جدیدی انجام نشد")

            equity = (
                position["cash"]
                + position["quantity"] * price
            )

            total_equity += equity

            print(f"ارزش مجازی این بخش: {equity:.4f} USDT")
            print(f"موجودی نقدی: {position['cash']:.4f} USDT")
            print(f"مقدار دارایی: {position['quantity']:.8f}")

        except Exception as error:
            print(f"خطا در تحلیل {name}: {error}")

            # اگر دریافت قیمت ناموفق باشد، ارزش این بخش
            # فعلاً قابل محاسبه نیست.
            total_equity = None

    save_state(state)

    if total_equity is not None:
        profit = total_equity - INITIAL_CAPITAL

        print("\n" + "=" * 42)
        print(f"ارزش کل سرمایه مجازی: {total_equity:.4f} USDT")
        print(f"سود/زیان نسبت به سرمایه اولیه: {profit:+.4f} USDT")
        print("=" * 42)

    print("اطلاعات معاملات در", STATE_FILE, "ذخیره شد.")


if __name__ == "__main__":
    main()
