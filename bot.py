import json
import os
import time
import urllib.request
import urllib.parse
import datetime

API_URL = "https://apiv2.nobitex.ir"
STATE_FILE = "paper_state_v4.json"
INITIAL_CAPITAL = 5.0
FEE_RATE = 0.0013
STOP_LOSS = 0.02
TAKE_PROFIT = 0.04
MIN_BUY_CASH = 0.50

MARKETS = {
    "BTCUSDT": ("btc", "usdt"),
    "ETHUSDT": ("eth", "usdt"),
}
STRATEGIES = ("TREND", "REVERSAL")
LIVE_TRADING = False  # معاملات واقعی غیرفعال هستند


def api_get(url):
    request = urllib.request.Request(
        url, headers={"User-Agent": "Nobitex-Paper-Trader/4.0"}
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


def get_candles(symbol, resolution=5, count=200):
    now = int(time.time())
    start = now - resolution * 60 * count
    query = urllib.parse.urlencode({
        "symbol": symbol, "resolution": str(resolution),
        "from": str(start), "to": str(now)
    })
    data = api_get(f"{API_URL}/market/udf/history?{query}")

    if data.get("s") != "ok":
        raise RuntimeError(f"دریافت کندل‌های {symbol} ناموفق بود")

    times, closes = data.get("t", []), data.get("c", [])
    if len(times) != len(closes):
        raise RuntimeError(f"اطلاعات کندل {symbol} ناقص است")

    candles = [
        (int(t), float(c)) for t, c in zip(times, closes)
        if c is not None and float(c) > 0
    ]
    # کندل جاری ممکن است هنوز بسته نشده باشد.
    if len(candles) > 1:
        candles = candles[:-1]
    if len(candles) < 30:
        raise RuntimeError(f"تعداد کندل‌های کافی برای {symbol} دریافت نشد")
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
                "cash": allocation, "qty": 0.0,
                "entry_price": 0.0, "entry_cost": 0.0,
                "last_candle": 0
            }
    return {
        "version": 4, "initial_capital": INITIAL_CAPITAL,
        "fee_rate": FEE_RATE, "strategies": strategies, "trades": []
    }


def load_state():
    if not os.path.exists(STATE_FILE):
        print("وضعیت جدید معاملات مجازی ساخته می‌شود.")
        return new_state()
    with open(STATE_FILE, "r", encoding="utf-8") as file:
        state = json.load(file)
    if state.get("version") != 4:
        raise RuntimeError(
            "فایل وضعیت با نسخه چهارم سازگار نیست؛ برای جلوگیری از "
            "بازنویسی اطلاعات قبلی، آن را بررسی کن."
        )
    return state


def save_state(state):
    temporary_file = STATE_FILE + ".tmp"
    with open(temporary_file, "w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, indent=2)
    os.replace(temporary_file, STATE_FILE)


def get_signal(strategy, candles):
    times = [item[0] for item in candles]
    closes = [item[1] for item in candles]
    fast_ema, slow_ema, current_rsi = ema(closes, 9), ema(closes, 21), rsi(closes)

    if fast_ema is None or slow_ema is None or current_rsi is None:
        return "WAIT", current_rsi, "اطلاعات کافی نیست", times[-1]

    last_price, previous_price = closes[-1], closes[-2]
    signal, reason = "WAIT", "شرایط ورود کامل نیست"

    if strategy == "TREND":
        if fast_ema > slow_ema and 50 <= current_rsi <= 68:
            signal, reason = "BUY", "روند صعودی و RSI مناسب"
        elif fast_ema < slow_ema or current_rsi >= 75:
            signal, reason = "SELL", "ضعف روند یا RSI بالا"
        else:
            reason = "شرایط روند کامل نیست"
    elif strategy == "REVERSAL":
        if current_rsi <= 30 and last_price > previous_price:
            signal, reason = "BUY", "احتمال برگشت صعودی"
        elif current_rsi >= 70 and last_price < previous_price:
            signal, reason = "SELL", "احتمال برگشت نزولی"
        else:
            reason = "تأیید بازگشت مشاهده نشد"

    return signal, current_rsi, reason, times[-1]


def record_trade(state, strategy, symbol, side, price, qty, pnl, reason):
    state["trades"].append({
        "time_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "strategy": strategy, "symbol": symbol, "side": side,
        "price": price, "qty": qty, "pnl": pnl, "reason": reason
    })
    if len(state["trades"]) > 5000:
        state["trades"] = state["trades"][-5000:]


def buy(state, strategy, symbol, position, price, reason):
    cash = position["cash"]
    if cash < MIN_BUY_CASH:
        print("وجه نقد مجازی برای خرید کافی نیست.")
        return
    qty = cash * (1 - FEE_RATE) / price
    position["qty"] = qty
    position["entry_price"] = price
    position["entry_cost"] = cash
    position["cash"] = 0.0
    record_trade(state, strategy, symbol, "BUY", price, qty, 0.0, reason)
    print(f"خرید مجازی {symbol}: قیمت={price:.6f} | مقدار={qty:.10f} | علت={reason}")


def sell(state, strategy, symbol, position, price, reason):
    qty = position["qty"]
    if qty <= 0:
        return
    proceeds = qty * price * (1 - FEE_RATE)
    pnl = proceeds - position["entry_cost"]
    position["cash"] += proceeds
    position["qty"] = 0.0
    position["entry_price"] = 0.0
    position["entry_cost"] = 0.0
    record_trade(state, strategy, symbol, "SELL", price, qty, pnl, reason)
    print(f"فروش مجازی {symbol}: قیمت={price:.6f} | سود/زیان={pnl:+.6f} USDT | علت={reason}")


def check_risk_controls(state, strategy, symbol, position, price):
    if position["qty"] <= 0 or position["entry_price"] <= 0:
        return False
    change = (price - position["entry_price"]) / position["entry_price"]
    if change <= -STOP_LOSS:
        sell(state, strategy, symbol, position, price, "STOP_LOSS")
        return True
    if change >= TAKE_PROFIT:
        sell(state, strategy, symbol, position, price, "TAKE_PROFIT")
        return True
    return False


def position_equity(position, price):
    return position["cash"] + position["qty"] * price * (1 - FEE_RATE)


def strategy_report(state, prices):
    initial_per_strategy = INITIAL_CAPITAL / len(STRATEGIES)
    print("\n" + "=" * 45)
    print("گزارش مقایسه استراتژی‌ها")
    print("=" * 45)

    for strategy in STRATEGIES:
        equity = sum(
            position_equity(state["strategies"][strategy][symbol], prices[symbol])
            for symbol in MARKETS if symbol in prices
        )
        # بازارهایی که قیمتشان دریافت نشده، در این گزارش از محاسبه حذف می‌شوند.
        pnl = equity - initial_per_strategy
        return_pct = pnl / initial_per_strategy * 100 if initial_per_strategy else 0
        closed = [
            trade for trade in state["trades"]
            if trade["strategy"] == strategy and trade["side"] == "SELL"
        ]
        wins = sum(1 for trade in closed if trade["pnl"] > 0)
        losses = sum(1 for trade in closed if trade["pnl"] < 0)
        realized_pnl = sum(trade["pnl"] for trade in closed)
        win_rate = wins / len(closed) * 100 if closed else 0

        print(f"\nاستراتژی: {strategy}")
        print(f"سرمایه اولیه: {initial_per_strategy:.4f} USDT")
        print(f"ارزش فعلی بازارهای قیمت‌گذاری‌شده: {equity:.4f} USDT")
        print(f"سود/زیان گزارش‌شده: {pnl:+.4f} USDT")
        print(f"بازده گزارش‌شده: {return_pct:+.2f}%")
        print(f"معاملات بسته‌شده: {len(closed)}")
        print(f"معاملات سودده: {wins}")
        print(f"معاملات زیان‌ده: {losses}")
        print(f"درصد برد: {win_rate:.2f}%")
        print(f"سود/زیان تحقق‌یافته: {realized_pnl:+.4f} USDT")

    print(f"\nتعداد کل معاملات ثبت‌شده: {len(state['trades'])}")
    print("این گزارش مربوط به معاملات مجازی است.")
    print("=" * 45)


def main():
    print("ربات آزمایشی نوبیتکس - نسخه چهارم")
    print(f"سرمایه مجازی کل: {INITIAL_CAPITAL:.2f} USDT")
    print(f"کارمزد هر طرف: {FEE_RATE * 100:.2f}%")
    print(f"حد ضرر: {STOP_LOSS * 100:.1f}%")
    print(f"حد سود: {TAKE_PROFIT * 100:.1f}%")
    print("حالت: فقط معاملات مجازی")
    print("ارسال سفارش واقعی: غیرفعال")
    print("زمان UTC:", datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))

    state = load_state()
    prices, candles_by_market = {}, {}

    for symbol in MARKETS:
        try:
            prices[symbol] = get_current_price(symbol)
            candles_by_market[symbol] = get_candles(symbol, resolution=5)
            print(f"\n{symbol}: {prices[symbol]:.6f} USDT")
        except Exception as error:
            print(f"خطا در دریافت اطلاعات {symbol}: {error}")

    if not prices:
        raise RuntimeError("قیمت هیچ بازاری دریافت نشد؛ وضعیت معاملات ذخیره نشد.")

    for strategy in STRATEGIES:
        print(f"\n--- استراتژی {strategy} ---")
        for symbol in MARKETS:
            if symbol not in prices:
                print(f"{symbol}: به علت خطای قیمت رد شد.")
                continue

            position = state["strategies"][strategy][symbol]
            price = prices[symbol]
            risk_closed = check_risk_controls(
                state, strategy, symbol, position, price
            )

            if symbol not in candles_by_market:
                continue

            signal, current_rsi, reason, candle_time = get_signal(
                strategy, candles_by_market[symbol]
            )
            rsi_text = f"{current_rsi:.2f}" if current_rsi is not None else "نامشخص"
            print(f"{symbol} | RSI: {rsi_text} | سیگنال: {signal} | {reason}")

            if candle_time == position["last_candle"]:
                if risk_closed:
                    print("حد ضرر/حد سود بررسی شد؛ کندل جدیدی وجود ندارد.")
                else:
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

    # برای جلوگیری از گزارش نادرست در صورت نبود قیمت یک بازار،
    # این نسخه گزارش را فقط وقتی قیمت همه بازارها موجود است چاپ می‌کند.
    if len(prices) == len(MARKETS):
        strategy_report(state, prices)
    else:
        print("\nگزارش کامل سرمایه چاپ نشد چون قیمت یک یا چند بازار دریافت نشد.")

    save_state(state)
    print(f"\nوضعیت معاملات در {STATE_FILE} ذخیره شد.")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print("خطای اجرای ربات:", error)
        raise
