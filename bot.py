import json
import os
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta

API_URL = "https://apiv2.nobitex.ir/market/udf/history"
STATE_FILE = "paper_state.json"

INITIAL_CAPITAL = 5.0
FEE_RATE = 0.0013

MARKETS = [
    ("BTCUSDT", "بیت‌کوین"),
    ("ETHUSDT", "اتریوم"),
]


def now_utc():
    return datetime.now(timezone.utc)


def get_candles(symbol, resolution, count=80):
    params = urllib.parse.urlencode({
        "symbol": symbol,
        "resolution": str(resolution),
        "to": int(time.time()),
        "countback": count,
    })

    request = urllib.request.Request(
        f"{API_URL}?{params}",
        headers={"User-Agent": "NobitexPaperBot/2.0"}
    )

    with urllib.request.urlopen(request, timeout=20) as response:
        data = json.loads(response.read().decode("utf-8"))

    if data.get("s") != "ok":
        raise ValueError(data.get("errmsg", "دریافت کندل ناموفق بود"))

    closes = data.get("c", [])
    times = data.get("t", [])

    if len(closes) != len(times) or len(closes) < 30:
        raise ValueError("اطلاعات کندل کافی نیست")

    # آخرین کندل ممکن است هنوز بسته نشده باشد.
    closes = [float(x) for x in closes[:-1]]
    closed_times = times[:-1]

    if len(closes) < 25:
        raise ValueError("کندل بسته‌شده کافی نیست")

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


def analyze_market(short_closes, long_closes):
    short_fast = ema(short_closes[-40:], 5)
    short_slow = ema(short_closes[-40:], 13)
    momentum = rsi(short_closes, 14)
    price = short_closes[-1]

    long_fast = ema(long_closes[-40:], 9)
    long_slow = ema(long_closes[-40:], 21)
    long_price = long_closes[-1]

    bullish_trend = (
        long_fast > long_slow
        and long_price > long_slow
    )

    bearish_trend = (
        long_fast < long_slow
        and long_price < long_slow
    )

    bullish_momentum = (
        short_fast > short_slow
        and price > short_fast
        and 48 <= momentum <= 75
    )

    bearish_momentum = (
        short_fast < short_slow
        and price < short_fast
        and 25 <= momentum <= 52
    )

    if bullish_trend and bullish_momentum:
        return "BUY", momentum, "تأیید روند صعودی"

    if bearish_trend and bearish_momentum:
        return "SELL", momentum, "تأیید روند نزولی"

    return "WAIT", momentum, "شرایط ورود کامل نیست"


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            state = json.load(file)
    else:
        allocation = INITIAL_CAPITAL / len(MARKETS)

        state = {
            "initial_capital": INITIAL_CAPITAL,
            "fee_rate": FEE_RATE,
            "positions": {
                symbol: {
                    "cash": allocation,
                    "quantity": 0.0,
                    "entry_price": 0.0,
                    "entry_cost": 0.0,
                    "last_candle": 0,
                }
                for symbol, _ in MARKETS
            },
            "trades": [],
        }

    # سازگاری با فایل ذخیره‌شده نسخه قبلی
    state.setdefault("trades", [])
    state.setdefault("initial_capital", INITIAL_CAPITAL)
    state.setdefault("fee_rate", FEE_RATE)

    for symbol, _ in MARKETS:
        state["positions"].setdefault(
            symbol,
            {
                "cash": 0.0,
                "quantity": 0.0,
                "entry_price": 0.0,
                "entry_cost": 0.0,
                "last_candle": 0,
            }
        )

        position = state["positions"][symbol]
        position.setdefault("entry_cost", 0.0)
        position.setdefault("entry_price", 0.0)
        position.setdefault("last_candle", 0)

    return state


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as file:
        json.dump(state, file, indent=2, ensure_ascii=False)


def record_trade(state, symbol, side, price, quantity, pnl=None):
    state["trades"].append({
        "time_utc": now_utc().isoformat(),
        "symbol": symbol,
        "side": side,
        "price": round(price, 8),
        "quantity": round(quantity, 12),
        "realized_pnl": (
            round(pnl, 8) if pnl is not None else None
        ),
    })


def report_period(trades, start_time, end_time):
    closed = [
        trade for trade in trades
        if trade["side"] == "SELL"
        and trade.get("realized_pnl") is not None
        and start_time <= datetime.fromisoformat(
            trade["time_utc"]
        ) < end_time
    ]

    if not closed:
        return 0, 0.0, 0, 0

    profit = sum(t["realized_pnl"] for t in closed)
    wins = sum(1 for t in closed if t["realized_pnl"] > 0)
    losses = sum(1 for t in closed if t["realized_pnl"] < 0)

    return len(closed), profit, wins, losses


def main():
    state = load_state()
    total_equity = 0.0
    successful_markets = 0

    current_time = now_utc()
    today_start = current_time.replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    tomorrow_start = today_start + timedelta(days=1)

    # شروع هفته بر اساس دوشنبه UTC
    week_start = (
        today_start
        - timedelta(days=today_start.weekday())
    )
    next_week_start = week_start + timedelta(days=7)

    print("=" * 45)
    print("ربات آزمایشی نوبیتکس - نسخه دوم")
    print("سرمایه اولیه:", state["initial_capital"], "USDT")
    print("کارمزد فرضی هر طرف:", FEE_RATE * 100, "%")
    print("حالت: معاملات مجازی؛ بدون سفارش واقعی")
    print("زمان UTC:", current_time.strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 45)

    for symbol, name in MARKETS:
        position = state["positions"][symbol]

        try:
            short_closes, candle_time = get_candles(symbol, 5)
            long_closes, _ = get_candles(symbol, 15)

            signal, momentum, reason = analyze_market(
                short_closes, long_closes
            )
            price = short_closes[-1]

            print(f"\n--- {name} ({symbol}) ---")
            print(f"قیمت: {price}")
            print(f"RSI: {momentum:.2f}")
            print(f"سیگنال: {signal}")
            print(f"دلیل: {reason}")

            # فقط یک بار برای هر کندل ۵ دقیقه‌ای تصمیم معاملاتی بگیر.
            if candle_time != position["last_candle"]:
                position["last_candle"] = candle_time

                if signal == "BUY" and position["quantity"] == 0:
                    cash = position["cash"]

                    if cash > 0:
                        quantity = cash * (1 - FEE_RATE) / price

                        position["quantity"] = quantity
                        position["entry_price"] = price
                        position["entry_cost"] = cash
                        position["cash"] = 0.0

                        record_trade(
                            state, symbol, "BUY", price, quantity
                        )
                        print("معامله مجازی: خرید ثبت شد")

                elif signal == "SELL" and position["quantity"] > 0:
                    quantity = position["quantity"]
                    proceeds = quantity * price * (1 - FEE_RATE)
                    cost = position["entry_cost"]

                    # سازگاری با موقعیتی که از نسخه قبلی باقی مانده
                    if cost <= 0:
                        cost = quantity * position["entry_price"] / (
                            1 - FEE_RATE
                        )

                    realized_pnl = proceeds - cost

                    position["cash"] = proceeds
                    position["quantity"] = 0.0
                    position["entry_price"] = 0.0
                    position["entry_cost"] = 0.0

                    record_trade(
                        state,
                        symbol,
                        "SELL",
                        price,
                        quantity,
                        realized_pnl,
                    )
                    print(
                        "فروش مجازی ثبت شد؛ سود/زیان:",
                        f"{realized_pnl:+.4f} USDT"
                    )

                else:
                    print("معامله جدیدی انجام نشد")

            equity = (
                position["cash"]
                + position["quantity"] * price
            )

            total_equity += equity
            successful_markets += 1

            print(f"ارزش این بخش: {equity:.4f} USDT")
            print(f"موجودی نقدی: {position['cash']:.4f} USDT")
            print(f"مقدار دارایی: {position['quantity']:.8f}")

        except Exception as error:
            print(f"خطا در تحلیل {name}: {error}")

    save_state(state)

    print("\n" + "=" * 45)

    if successful_markets == len(MARKETS):
        initial = float(state["initial_capital"])
        print(f"ارزش کل سرمایه مجازی: {total_equity:.4f} USDT")
        print(
            f"سود/زیان کل نسبت به سرمایه اولیه: "
            f"{total_equity - initial:+.4f} USDT"
        )
    else:
        print("ارزش کل سرمایه محاسبه نشد؛ دریافت اطلاعات یک ارز ناموفق بود.")

    trades = state["trades"]

    count, profit, wins, losses = report_period(
        trades, today_start, tomorrow_start
    )
    print("\n--- گزارش امروز (UTC) ---")
    print("تعداد معاملات بسته‌شده:", count)
    print(f"سود/زیان تحقق‌یافته: {profit:+.4f} USDT")
    print("معاملات سودده:", wins)
    print("معاملات زیان‌ده:", losses)

    count, profit, wins, losses = report_period(
        trades, week_start, next_week_start
    )
    print("\n--- گزارش این هفته (UTC) ---")
    print("تعداد معاملات بسته‌شده:", count)
    print(f"سود/زیان تحقق‌یافته: {profit:+.4f} USDT")
    print("معاملات سودده:", wins)
    print("معاملات زیان‌ده:", losses)

    print("\nاطلاعات و سابقه معاملات ذخیره شد.")
    print("=" * 45)


if __name__ == "__main__":
    main()
