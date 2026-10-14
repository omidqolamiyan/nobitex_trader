import json
import os
import time
import urllib.request
import urllib.parse
import datetime

# Nobitex Paper Trader - Version 5
# معاملات واقعی عمداً غیرفعال هستند.
API_URL = "https://apiv2.nobitex.ir"
STATE_FILE = "paper_state_v4.json"  # برای حفظ فایل و سوابق نسخه چهارم
STATE_VERSION = 4                   # ساختار فایل ذخیره با نسخه چهارم سازگار است
BOT_VERSION = 5
INITIAL_CAPITAL = 5.0
FEE_RATE = 0.0013
STOP_LOSS = 0.02       # زیان خالص هدف، پس از کارمزدها
TAKE_PROFIT = 0.04     # سود خالص هدف، پس از کارمزدها
MIN_BUY_CASH = 0.50
CANDLE_RESOLUTION_MINUTES = 5
CANDLE_COUNT = 200

MARKETS = {
    "BTCUSDT": ("btc", "usdt"),
    "ETHUSDT": ("eth", "usdt"),
}
STRATEGIES = ("TREND", "REVERSAL")
LIVE_TRADING = False


def api_get(url):
    request = urllib.request.Request(
        url, headers={"User-Agent": "Nobitex-Paper-Trader/5.0"}
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def get_current_price(symbol):
    src, dst = MARKETS[symbol]
    query = urllib.parse.urlencode({"srcCurrency": src, "dstCurrency": dst})
    data = api_get(f"{API_URL}/market/stats?{query}")

    if data.get("status") != "ok":
        raise RuntimeError(f"دریافت قیمت {symbol} ناموفق بود")

    market = data.get("stats", {}).get(f"{src}-{dst}")
    if not market:
        raise RuntimeError(f"قیمت بازار {src}-{dst} در پاسخ API پیدا نشد")

    price = float(market.get("latest") or market.get("mark") or 0)
    if price <= 0:
        raise RuntimeError(f"قیمت نامعتبر برای {symbol}")
    return price


def get_candles(symbol, resolution=CANDLE_RESOLUTION_MINUTES, count=CANDLE_COUNT):
    now = int(time.time())
    start = now - resolution * 60 * count
    query = urllib.parse.urlencode({
        "symbol": symbol,
        "resolution": str(resolution),
        "from": str(start),
        "to": str(now),
    })
    data = api_get(f"{API_URL}/market/udf/history?{query}")

    if data.get("s") != "ok":
        raise RuntimeError(f"دریافت کندل‌های {symbol} ناموفق بود")

    times, closes = data.get("t", []), data.get("c", [])
    if len(times) != len(closes):
        raise RuntimeError(f"اطلاعات کندل {symbol} ناقص است")

    # فقط کندل‌های بسته‌شده را نگه می‌داریم؛ کندل جاری نباید در سیگنال دخالت کند.
    candles = []
    for timestamp, close in zip(times, closes):
        if close is None:
            continue
        close = float(close)
        timestamp = int(timestamp)
        if close > 0 and timestamp + resolution * 60 <= now:
            candles.append((timestamp, close))

    candles.sort(key=lambda item: item[0])
    # حذف زمان‌های تکراری احتمالی
    unique = {}
    for timestamp, close in candles:
        unique[timestamp] = close
    candles = sorted(unique.items())

    if len(candles) < 30:
        raise RuntimeError(
            f"کندل بسته‌شده کافی برای {symbol} دریافت نشد: {len(candles)}"
        )
    return candles


def ema(values, period):
    if len(values) < period:
        return None
    alpha = 2 / (period + 1)
    result = sum(values[:period]) / period
    for value in values[period:]:
        result = value * alpha + result * (1 - alpha)
    return result


def rsi(values, period=14):
    if len(values) <= period:
        return None

    changes = [values[i] - values[i - 1] for i in range(1, len(values))]
    gains = [max(change, 0) for change in changes]
    losses = [max(-change, 0) for change in changes]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def new_state():
    allocation = INITIAL_CAPITAL / (len(STRATEGIES) * len(MARKETS))
    strategies = {}
    for strategy in STRATEGIES:
        strategies[strategy] = {}
        for symbol in MARKETS:
            strategies[strategy][symbol] = {
                "cash": allocation,
                "qty": 0.0,
                "entry_price": 0.0,
                "entry_cost": 0.0,
                "last_candle": 0,
            }
    return {
        "version": STATE_VERSION,
        "initial_capital": INITIAL_CAPITAL,
        "fee_rate": FEE_RATE,
        "strategies": strategies,
        "trades": [],
    }


def load_state():
    if not os.path.exists(STATE_FILE):
        print("هشدار: فایل وضعیت پیدا نشد؛ وضعیت اولیه ساخته می‌شود.")
        print("اگر قبلاً معامله مجازی داشته‌ای، بررسی کن فایل در مخزن موجود باشد.")
        return new_state()

    with open(STATE_FILE, "r", encoding="utf-8") as file:
        state = json.load(file)

    if state.get("version") != STATE_VERSION:
        raise RuntimeError(
            "فایل وضعیت با ساختار مورد انتظار سازگار نیست؛ برای جلوگیری از "
            "از دست رفتن سوابق، فایل به‌صورت خودکار بازنویسی نشد."
        )

    # بررسی حداقلی ساختار برای جلوگیری از شروع با اطلاعات ناقص
    if not isinstance(state.get("strategies"), dict) or not isinstance(state.get("trades"), list):
        raise RuntimeError("ساختار فایل وضعیت ناقص است؛ فایل ذخیره نشد.")

    for strategy in STRATEGIES:
        if strategy not in state["strategies"]:
            raise RuntimeError(f"استراتژی {strategy} در فایل وضعیت وجود ندارد.")
        for symbol in MARKETS:
            if symbol not in state["strategies"][strategy]:
                raise RuntimeError(f"بازار {symbol} در فایل وضعیت وجود ندارد.")
            position = state["strategies"][strategy][symbol]
            for key in ("cash", "qty", "entry_price", "entry_cost", "last_candle"):
                if key not in position:
                    raise RuntimeError(f"فیلد {key} برای {strategy}/{symbol} وجود ندارد.")
    return state


def save_state(state):
    temporary_file = STATE_FILE + ".tmp"
    with open(temporary_file, "w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, indent=2)
        file.write("\n")
    os.replace(temporary_file, STATE_FILE)


def get_signal(strategy, candles):
    times = [item[0] for item in candles]
    closes = [item[1] for item in candles]
    fast_ema = ema(closes, 9)
    slow_ema = ema(closes, 21)
    current_rsi = rsi(closes)

    if fast_ema is None or slow_ema is None or current_rsi is None:
        return "WAIT", current_rsi, "اطلاعات کافی نیست", times[-1]

    last_price = closes[-1]
    previous_price = closes[-2]
    signal, reason = "WAIT", "شرایط ورود کامل نیست"

    if strategy == "TREND":
        # ورود فقط با تأیید روند و RSI؛ خروج روندی نیازمند ضعف هم‌زمان است.
        if fast_ema > slow_ema and last_price > fast_ema and 52 <= current_rsi <= 67:
            signal, reason = "BUY", "روند صعودی تأییدشده و RSI مناسب"
        elif fast_ema < slow_ema and current_rsi < 48:
            signal, reason = "SELL", "ضعف تأییدشده روند"
        else:
            reason = "فیلتر روند یا RSI تأیید نشد"

    elif strategy == "REVERSAL":
        # ورود برگشتی فقط پس از مشاهده حرکت صعودی در کندل بسته‌شده.
        if current_rsi <= 30 and last_price > previous_price:
            signal, reason = "BUY", "اشباع فروش و نشانه اولیه برگشت"
        elif current_rsi >= 68 and last_price < previous_price:
            signal, reason = "SELL", "اشباع خرید و نشانه اولیه برگشت نزولی"
        else:
            reason = "تأیید برگشت مشاهده نشد"

    return signal, current_rsi, reason, times[-1]


def record_trade(state, strategy, symbol, side, price, qty, pnl, reason):
    state["trades"].append({
        "time_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "strategy": strategy,
        "symbol": symbol,
        "side": side,
        "price": price,
        "qty": qty,
        "pnl": pnl,
        "reason": reason,
    })
    if len(state["trades"]) > 5000:
        state["trades"] = state["trades"][-5000:]


def buy(state, strategy, symbol, position, price, reason):
    cash = float(position["cash"])
    if cash < MIN_BUY_CASH:
        print("وجه نقد مجازی برای خرید کافی نیست.")
        return

    # مقدار خرید طوری محاسبه می‌شود که مبلغ خرید به‌علاوه کارمزد از نقدینگی بیشتر نشود.
    qty = cash / (price * (1 + FEE_RATE))
    if qty <= 0:
        print("مقدار خرید نامعتبر است.")
        return

    position["qty"] = qty
    position["entry_price"] = price
    position["entry_cost"] = cash
    position["cash"] = 0.0
    record_trade(state, strategy, symbol, "BUY", price, qty, 0.0, reason)
    print(
        f"خرید مجازی {symbol}: قیمت={price:.6f} | "
        f"مقدار={qty:.10f} | مبلغ با کارمزد={cash:.6f} USDT | علت={reason}"
    )


def sell(state, strategy, symbol, position, price, reason):
    qty = float(position["qty"])
    if qty <= 0:
        return

    proceeds = qty * price * (1 - FEE_RATE)
    entry_cost = float(position["entry_cost"])
    pnl = proceeds - entry_cost

    position["cash"] = float(position["cash"]) + proceeds
    position["qty"] = 0.0
    position["entry_price"] = 0.0
    position["entry_cost"] = 0.0

    record_trade(state, strategy, symbol, "SELL", price, qty, pnl, reason)
    print(
        f"فروش مجازی {symbol}: قیمت={price:.6f} | "
        f"سود/زیان خالص={pnl:+.6f} USDT | علت={reason}"
    )


def net_return(position, price):
    """بازده خالص فرضی موقعیت باز، با لحاظ کارمزد خروج."""
    entry_cost = float(position.get("entry_cost", 0.0))
    qty = float(position.get("qty", 0.0))
    if entry_cost <= 0 or qty <= 0:
        return 0.0
    estimated_proceeds = qty * price * (1 - FEE_RATE)
    return (estimated_proceeds - entry_cost) / entry_cost


def check_risk_controls(state, strategy, symbol, position, price):
    if position["qty"] <= 0 or position["entry_cost"] <= 0:
        return False

    current_net_return = net_return(position, price)
    if current_net_return <= -STOP_LOSS:
        sell(state, strategy, symbol, position, price, "STOP_LOSS_NET")
        return True
    if current_net_return >= TAKE_PROFIT:
        sell(state, strategy, symbol, position, price, "TAKE_PROFIT_NET")
        return True
    return False


def position_equity(position, price):
    return float(position["cash"]) + float(position["qty"]) * price * (1 - FEE_RATE)


def strategy_report(state, prices):
    initial_per_strategy = INITIAL_CAPITAL / len(STRATEGIES)
    print("\n" + "=" * 48)
    print("گزارش مقایسه استراتژی‌ها - نسخه پنجم")
    print("=" * 48)

    for strategy in STRATEGIES:
        strategy_positions = state["strategies"][strategy]
        equity = sum(
            position_equity(strategy_positions[symbol], prices[symbol])
            for symbol in MARKETS if symbol in prices
        )
        if len(prices) == len(MARKETS):
            pnl = equity - initial_per_strategy
            return_pct = pnl / initial_per_strategy * 100
            print(f"\nاستراتژی: {strategy}")
            print(f"سرمایه اولیه: {initial_per_strategy:.4f} USDT")
            print(f"ارزش فعلی با برآورد کارمزد خروج: {equity:.4f} USDT")
            print(f"سود/زیان کل برآوردی: {pnl:+.4f} USDT")
            print(f"بازده کل برآوردی: {return_pct:+.2f}%")
        else:
            print(f"\nاستراتژی: {strategy}")
            print("ارزش کل و بازده نمایش داده نشد؛ قیمت یک یا چند بازار موجود نیست.")

        closed = [
            trade for trade in state["trades"]
            if trade.get("strategy") == strategy and trade.get("side") == "SELL"
        ]
        wins = sum(1 for trade in closed if float(trade.get("pnl", 0)) > 0)
        losses = sum(1 for trade in closed if float(trade.get("pnl", 0)) < 0)
        realized_pnl = sum(float(trade.get("pnl", 0)) for trade in closed)
        win_rate = wins / len(closed) * 100 if closed else 0.0
        print(f"معاملات بسته‌شده: {len(closed)}")
        print(f"معاملات سودده: {wins}")
        print(f"معاملات زیان‌ده: {losses}")
        print(f"درصد برد: {win_rate:.2f}%")
        print(f"سود/زیان تحقق‌یافته: {realized_pnl:+.4f} USDT")

    print(f"\nتعداد کل معاملات ثبت‌شده (خرید و فروش): {len(state['trades'])}")
    print("تمام اعداد مربوط به معاملات مجازی هستند؛ سود آینده تضمین‌شده نیست.")
    print("=" * 48)


def main():
    print(f"ربات آزمایشی نوبیتکس - نسخه {BOT_VERSION}")
    print(f"سرمایه مجازی اولیه: {INITIAL_CAPITAL:.2f} USDT")
    print(f"کارمزد هر طرف: {FEE_RATE * 100:.2f}%")
    print(f"حد ضرر خالص هدف: {STOP_LOSS * 100:.1f}%")
    print(f"حد سود خالص هدف: {TAKE_PROFIT * 100:.1f}%")
    print("حالت: فقط معاملات مجازی")
    print(f"ارسال سفارش واقعی: {'فعال' if LIVE_TRADING else 'غیرفعال'}")
    print(
        "زمان UTC:",
        datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
    )

    state = load_state()
    prices = {}
    candles_by_market = {}

    for symbol in MARKETS:
        try:
            prices[symbol] = get_current_price(symbol)
            print(f"\n{symbol}: {prices[symbol]:.6f} USDT")
        except Exception as error:
            print(f"خطا در دریافت قیمت {symbol}: {error}")
            continue

        try:
            candles_by_market[symbol] = get_candles(symbol)
            print(f"کندل‌های بسته‌شده {symbol}: {len(candles_by_market[symbol])}")
        except Exception as error:
            print(f"خطا در دریافت کندل‌های {symbol}: {error}")

    if not prices:
        raise RuntimeError("قیمت هیچ بازاری دریافت نشد؛ وضعیت معاملات تغییر نکرد.")

    for strategy in STRATEGIES:
        print(f"\n--- استراتژی {strategy} ---")
        for symbol in MARKETS:
            if symbol not in prices:
                print(f"{symbol}: به علت خطای دریافت قیمت رد شد.")
                continue

            position = state["strategies"][strategy][symbol]
            price = prices[symbol]

            # حد ضرر و حد سود در هر اجرا بررسی می‌شوند، حتی اگر کندل جدیدی نباشد.
            risk_closed = check_risk_controls(
                state, strategy, symbol, position, price
            )

            if symbol not in candles_by_market:
                print(f"{symbol}: کندل در دسترس نیست؛ سیگنال جدید اجرا نشد.")
                continue

            signal, current_rsi, reason, candle_time = get_signal(
                strategy, candles_by_market[symbol]
            )
            rsi_text = f"{current_rsi:.2f}" if current_rsi is not None else "نامشخص"
            print(f"{symbol} | RSI: {rsi_text} | سیگنال: {signal} | {reason}")

            if candle_time == position["last_candle"]:
                print("کندل جدیدی نیست؛ سیگنال تکراری اجرا نشد.")
                continue

            position["last_candle"] = candle_time
            if risk_closed:
                continue

            if signal == "BUY" and position["qty"] == 0:
                buy(state, strategy, symbol, position, price, reason)
            elif signal == "SELL" and position["qty"] > 0:
                sell(state, strategy, symbol, position, price, reason)
            else:
                print("معامله جدیدی انجام نشد.")

    strategy_report(state, prices)
    save_state(state)
    print(f"\nوضعیت معاملات در {STATE_FILE} ذخیره شد.")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print("خطای اجرای ربات:", error)
        raise
