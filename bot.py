import json
import os
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta

# ==========================================
# تنظیمات اصلی
# ==========================================

API_URL = "https://apiv2.nobitex.ir/market/udf/history"
STATE_FILE = "paper_state_v3.json"

INITIAL_CAPITAL = 5.0
FEE_RATE = 0.0013

STOP_LOSS = 0.02       # حد ضرر ۲ درصد
TAKE_PROFIT = 0.04     # حد سود ۴ درصد

MARKETS = [
    ("BTCUSDT", "بیت‌کوین"),
    ("ETHUSDT", "اتریوم"),
]

STRATEGIES = ["TREND", "REVERSAL"]


# ==========================================
# ابزارهای عمومی
# ==========================================

def now_utc():
    return datetime.now(timezone.utc)


def fetch_json(url):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "NobitexPaperBot/3.0"}
    )

    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def get_candles(symbol, resolution, count=100):
    params = urllib.parse.urlencode({
        "symbol": symbol,
        "resolution": str(resolution),
        "to": int(time.time()),
        "countback": count,
    })

    data = fetch_json(f"{API_URL}?{params}")

    if data.get("s") != "ok":
        raise ValueError(data.get("errmsg", "خطای دریافت کندل"))

    closes = data.get("c", [])
    times = data.get("t", [])

    if len(closes) != len(times) or len(closes) < 35:
        raise ValueError("اطلاعات کندل کافی نیست")

    # کندل آخر ممکن است هنوز بسته نشده باشد.
    closes = [float(x) for x in closes[:-1]]
    times = times[:-1]

    if len(closes) < 30:
        raise ValueError("کندل بسته‌شده کافی نیست")

    return closes, int(times[-1])


# ==========================================
# اندیکاتورها
# ==========================================

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
        for i in range(1, len(values))
    ]

    gains = [max(change, 0.0) for change in changes]
    losses = [max(-change, 0.0) for change in changes]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(changes)):
        avg_gain = (
            avg_gain * (period - 1) + gains[i]
        ) / period

        avg_loss = (
            avg_loss * (period - 1) + losses[i]
        ) / period

    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


# ==========================================
# تحلیل استراتژی اول: دنبال کردن روند
# ==========================================

def trend_strategy(short_closes, long_closes):
    price = short_closes[-1]
    momentum = rsi(short_closes, 14)

    short_fast = ema(short_closes, 9)
    short_slow = ema(short_closes, 21)

    long_fast = ema(long_closes, 9)
    long_slow = ema(long_closes, 21)

    bullish = (
        long_fast > long_slow
        and short_fast > short_slow
        and price > short_fast
        and 50 <= momentum <= 70
    )

    bearish = (
        long_fast < long_slow
        and short_fast < short_slow
        and price < short_fast
        and 30 <= momentum <= 50
    )

    if bullish:
        return "BUY", momentum, "تأیید روند صعودی"

    if bearish:
        return "SELL", momentum, "تأیید روند نزولی"

    return "WAIT", momentum, "شرایط روند کامل نیست"


# ==========================================
# تحلیل استراتژی دوم: بازگشت از اشباع فروش
# ==========================================

def reversal_strategy(short_closes, long_closes):
    price = short_closes[-1]
    momentum = rsi(short_closes, 14)

    fast = ema(short_closes, 5)
    slow = ema(short_closes, 13)

    long_fast = ema(long_closes, 9)
    long_slow = ema(long_closes, 21)

    # خرید فقط پس از نشانه اولیه برگشت قیمت
    bullish_recovery = (
        momentum < 35
        and price > fast
        and fast > slow
        and long_fast >= long_slow
    )

    # فروش در ضعف روند و افزایش RSI از محدوده پایین
    bearish_recovery = (
        momentum > 65
        and price < fast
        and fast < slow
        and long_fast <= long_slow
    )

    if bullish_recovery:
        return "BUY", momentum, "نشانه بازگشت صعودی"

    if bearish_recovery:
        return "SELL", momentum, "نشانه بازگشت نزولی"

    return "WAIT", momentum, "تأیید بازگشت مشاهده نشد"


# ==========================================
# وضعیت و سرمایه مجازی
# ==========================================

def new_position(allocation):
    return {
        "cash": allocation,
        "quantity": 0.0,
        "entry_price": 0.0,
        "entry_cost": 0.0,
        "last_candle": 0,
        "last_exit_candle": 0,
    }


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            state = json.load(file)

        if state.get("version") != 3:
            raise ValueError(
                "نسخه فایل وضعیت ناسازگار است؛ "
                "برای شروع تازه، فایل وضعیت نسخه سوم را بررسی کنید."
            )

        return state

    # دو استراتژی مستقل؛ هرکدام مجموعاً ۲٫۵ دلار.
    allocation = INITIAL_CAPITAL / (
        len(STRATEGIES) * len(MARKETS)
    )

    state = {
        "version": 3,
        "initial_capital": INITIAL_CAPITAL,
        "fee_rate": FEE_RATE,
        "strategies": {},
        "trades": [],
    }

    for strategy in STRATEGIES:
        state["strategies"][strategy] = {
            symbol: new_position(allocation)
            for symbol, _ in MARKETS
        }

    return state


def save_state(state):
    temporary = STATE_FILE + ".tmp"

    with open(temporary, "w", encoding="utf-8") as file:
        json.dump(state, file, indent=2, ensure_ascii=False)

    os.replace(temporary, STATE_FILE)


# ==========================================
# ثبت معامله
# ==========================================

def record_trade(
    state, strategy, symbol, side,
    price, quantity, pnl=None, reason=""
):
    state["trades"].append({
        "time_utc": now_utc().isoformat(),
        "strategy": strategy,
        "symbol": symbol,
        "side": side,
        "price": round(price, 8),
        "quantity": round(quantity, 12),
        "realized_pnl": (
            round(pnl, 8) if pnl is not None else None
        ),
        "reason": reason,
    })


def buy_position(state, strategy, symbol, position, price, reason):
    cash = position["cash"]

    if cash <= 0 or price <= 0:
        return

    quantity = cash * (1 - FEE_RATE) / price

    position["quantity"] = quantity
    position["entry_price"] = price
    position["entry_cost"] = cash
    position["cash"] = 0.0

    record_trade(
        state, strategy, symbol, "BUY",
        price, quantity, reason=reason
    )


def sell_position(
    state, strategy, symbol, position, price, reason
):
    quantity = position["quantity"]

    if quantity <= 0 or price <= 0:
        return

    proceeds = quantity * price * (1 - FEE_RATE)
    cost = position["entry_cost"]
    pnl = proceeds - cost

    position["cash"] = proceeds
    position["quantity"] = 0.0
    position["entry_price"] = 0.0
    position["entry_cost"] = 0.0

    record_trade(
        state, strategy, symbol, "SELL",
        price, quantity, pnl=pnl, reason=reason
    )

    return pnl


# ==========================================
# بررسی حد سود، حد ضرر و سیگنال‌ها
# ==========================================

def process_market(
    state, strategy, symbol, name,
    position, short_closes, long_closes, candle_time
):
    price = short_closes[-1]

    if price <= 0:
        raise ValueError("قیمت نامعتبر است")

    if candle_time == position["last_candle"]:
        return

    position["last_candle"] = candle_time

    if position["quantity"] > 0:
        entry = position["entry_price"]

        if entry <= 0:
            raise ValueError("قیمت ورود نامعتبر است")

        change = price / entry - 1

        if change <= -STOP_LOSS:
            pnl = sell_position(
                state, strategy, symbol, position,
                price, "حد ضرر"
            )
            position["last_exit_candle"] = candle_time
            print(f"  خروج با حد ضرر؛ سود/زیان: {pnl:+.4f}")

        elif change >= TAKE_PROFIT:
            pnl = sell_position(
                state, strategy, symbol, position,
                price, "حد سود"
            )
            position["last_exit_candle"] = candle_time
            print(f"  خروج با حد سود؛ سود/زیان: {pnl:+.4f}")

        else:
            print(f"  موقعیت باز؛ تغییر قیمت: {change * 100:+.2f}%")

        return

    # از خرید مجدد در همان کندل خروج جلوگیری می‌شود.
    if position["last_exit_candle"] == candle_time:
        print("  ورود مجدد در همین کندل مجاز نیست")
        return

    if strategy == "TREND":
        signal, momentum, reason = trend_strategy(
            short_closes, long_closes
        )
    else:
        signal, momentum, reason = reversal_strategy(
            short_closes, long_closes
        )

    print(
        f"  {strategy} | RSI: {momentum:.2f} | "
        f"سیگنال: {signal} | {reason}"
    )

    if signal == "BUY":
        buy_position(
            state, strategy, symbol, position, price, reason
        )
        print("  خرید مجازی ثبت شد")

    elif signal == "SELL":
        # فروش فقط برای موقعیت باز مجاز است؛
        # در نبود موقعیت، فروش استقراضی انجام نمی‌شود.
        print("  موقعیت بازی برای فروش وجود ندارد")

    else:
        print("  معامله جدیدی انجام نشد")


# ==========================================
# گزارش‌ها
# ==========================================

def report_period(trades, start_time, end_time):
    closed = [
        trade for trade in trades
        if trade["side"] == "SELL"
        and trade["realized_pnl"] is not None
        and start_time <= datetime.fromisoformat(
            trade["time_utc"]
        ) < end_time
    ]

    profit = sum(t["realized_pnl"] for t in closed)
    wins = sum(1 for t in closed if t["realized_pnl"] > 0)
    losses = sum(1 for t in closed if t["realized_pnl"] < 0)

    return len(closed), profit, wins, losses


def strategy_equity(state, strategy, prices):
    total = 0.0

    for symbol, position in state["strategies"][strategy].items():
        price = prices.get(symbol)

        if price is None:
            # اگر قیمت در دسترس نیست، ارزش کل قابل محاسبه نیست.
            return None

        total += position["cash"]
        total += position["quantity"] * price

    return total


# ==========================================
# اجرای اصلی
# ==========================================

def main():
    state = load_state()
    current_time = now_utc()

    today_start = current_time.replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    tomorrow_start = today_start + timedelta(days=1)

    week_start = today_start - timedelta(
        days=today_start.weekday()
    )
    next_week_start = week_start + timedelta(days=7)

    print("=" * 55)
    print("ربات آزمایشی نوبیتکس - نسخه سوم")
    print(f"سرمایه کل: {INITIAL_CAPITAL:.2f} USDT")
    print(f"کارمزد هر طرف: {FEE_RATE * 100:.2f}%")
    print("حالت: فقط معاملات مجازی")
    print("ارسال سفارش واقعی: غیرفعال")
    print("زمان UTC:", current_time.strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 55)

    market_data = {}
    prices = {}

    # دریافت قیمت‌ها فقط یک بار برای هر بازار
    for symbol, name in MARKETS:
        try:
            short_closes, candle_time = get_candles(symbol, 5)
            long_closes, _ = get_candles(symbol, 15)

            market_data[symbol] = (
                short_closes, long_closes, candle_time
            )
            prices[symbol] = short_closes[-1]

            print(f"\n{name} ({symbol})")
            print(f"قیمت: {prices[symbol]}")

        except Exception as error:
            print(f"\nخطا در دریافت اطلاعات {name}: {error}")

    # هر استراتژی سرمایه و موقعیت مستقل دارد.
    for strategy in STRATEGIES:
        print(f"\n{'-' * 45}")
        print(f"استراتژی: {strategy}")

        for symbol, name in MARKETS:
            if symbol not in market_data:
                print(f"{name}: اطلاعات بازار موجود نیست")
                continue

            short_closes, long_closes, candle_time = market_data[symbol]
            position = state["strategies"][strategy][symbol]

            try:
                process_market(
                    state, strategy, symbol, name,
                    position, short_closes, long_closes,
                    candle_time
                )

            except Exception as error:
                print(f"خطا در {name}: {error}")

    save_state(state)

    print("\n" + "=" * 55)
    print("ارزیابی سرمایه مجازی")

    allocation_per_strategy = (
        INITIAL_CAPITAL / len(STRATEGIES)
    )

    for strategy in STRATEGIES:
        equity = strategy_equity(state, strategy, prices)

        print(f"\nاستراتژی: {strategy}")

        if equity is None:
            print("ارزش کل محاسبه نشد؛ قیمت یک یا چند بازار موجود نیست")
        else:
            profit = equity - allocation_per_strategy
            print(f"ارزش فعلی: {equity:.4f} USDT")
            print(f"سود/زیان: {profit:+.4f} USDT")

    count, profit, wins, losses = report_period(
        state["trades"], today_start, tomorrow_start
    )

    print("\n--- گزارش امروز UTC ---")
    print("معاملات بسته‌شده:", count)
    print(f"سود/زیان تحقق‌یافته: {profit:+.4f} USDT")
    print("معاملات سودده:", wins)
    print("معاملات زیان‌ده:", losses)

    count, profit, wins, losses = report_period(
        state["trades"], week_start, next_week_start
    )

    print("\n--- گزارش این هفته UTC ---")
    print("معاملات بسته‌شده:", count)
    print(f"سود/زیان تحقق‌یافته: {profit:+.4f} USDT")
    print("معاملات سودده:", wins)
    print("معاملات زیان‌ده:", losses)

    print("\nتاریخچه معاملات در فایل وضعیت ذخیره شد.")
    print("=" * 55)


if __name__ == "__main__":
    main()
