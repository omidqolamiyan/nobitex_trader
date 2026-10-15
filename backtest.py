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

    print(f"دریافت اطلاعات {symbol} ...")

    while cursor < now:
        end = min(now, cursor + interval * 200)

        query = urllib.parse.urlencode({
            "symbol": symbol,
            "resolution": str(RESOLUTION),
            "from": str(cursor),
            "to": str(end),
        })

        url = f"{API_URL}/market/udf/history?{query}"
        data = api_get(url)

        status = data.get("s")

        if status not in ("ok", "no_data"):
            raise RuntimeError(
                f"خطا در دریافت {symbol}: {status}"
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

        if end >= now:
            break

        cursor = end
        time.sleep(0.1)

    result = sorted(candles.items())

    if len(result) < 50:
        raise RuntimeError(
            f"تعداد کندل‌های {symbol} کافی نیست: {len(result)}"
        )

    print(f"{symbol}: تعداد کندل‌ها = {len(result)}")

    return result


def ema(values, period):
    if len(values) < period:
        return None

    alpha = 2.0 / (period + 1)
    result = sum(values[:period]) / period

    for value in values[period:]:
        result = value * alpha + result * (1 - alpha)

    return result


def rsi(values, period=14):
    if len(values) <= period:
        return None

    changes = [
        values[i] - values[i - 1]
        for i in range(1, len(values))
    ]

    gains = [max(change, 0) for change in changes]
    losses = [max(-change, 0) for change in changes]

    average_gain = sum(gains[:period]) / period
    average_loss = sum(losses[:period]) / period

    for i in range(period, len(changes)):
        average_gain = (
            average_gain * (period - 1) + gains[i]
        ) / period

        average_loss = (
            average_loss * (period - 1) + losses[i]
        ) / period

    if average_loss == 0:
        return 100.0 if average_gain > 0 else 50.0

    relative_strength = average_gain / average_loss

    return 100 - (100 / (1 + relative_strength))


def get_signal(strategy, closes):
    if len(closes) < 30:
        return "WAIT"

    fast_ema = ema(closes, 9)
    slow_ema = ema(closes, 21)
    current_rsi = rsi(closes)

    if fast_ema is None or slow_ema is None:
        return "WAIT"

    if current_rsi is None:
        return "WAIT"

    price = closes[-1]
    previous_price = closes[-2]

    if strategy == "TREND":
        if (
            fast_ema > slow_ema
            and price > fast_ema
            and 52 <= current_rsi <= 67
        ):
            return "BUY"

        if fast_ema < slow_ema and current_rsi < 48:
            return "SELL"

    elif strategy == "REVERSAL":
        if current_rsi <= 30 and price > previous_price:
            return "BUY"

        if current_rsi >= 68 and price < previous_price:
            return "SELL"

    return "WAIT"


def create_position():
    return {
        "cash": INITIAL_CAPITAL / (
            len(STRATEGIES) * len(MARKETS)
        ),
        "qty": 0.0,
        "entry_price": 0.0,
        "entry_cost": 0.0,
        "closed_trades": 0,
        "wins": 0,
        "losses": 0,
        "realized_pnl": 0.0,
    }


def buy(position, price):
    cash = position["cash"]

    if cash <= 0:
        return

    quantity = cash / (price * (1 + FEE_RATE))

    if quantity <= 0:
        return

    position["qty"] = quantity
    position["entry_price"] = price
    position["entry_cost"] = cash
    position["cash"] = 0.0


def sell(position, price):
    quantity = position["qty"]

    if quantity <= 0:
        return

    proceeds = quantity * price * (1 - FEE_RATE)
    pnl = proceeds - position["entry_cost"]

    position["cash"] += proceeds
    position["realized_pnl"] += pnl
    position["closed_trades"] += 1

    if pnl > 0:
        position["wins"] += 1
    else:
        position["losses"] += 1

    position["qty"] = 0.0
    position["entry_price"] = 0.0
    position["entry_cost"] = 0.0


def run_backtest(symbol, candles):
    closes = [price for _, price in candles]
    positions = {
        strategy: create_position()
        for strategy in STRATEGIES
    }

    for i in range(30, len(candles)):
        price = closes[i]

        # سیگنال فقط از کندل‌های بسته‌شده قبلی ساخته می‌شود.
        previous_closes = closes[:i]

        for strategy in STRATEGIES:
            position = positions[strategy]

            if position["qty"] > 0:
                entry_cost = position["entry_cost"]

                estimated_value = (
                    position["qty"] * price * (1 - FEE_RATE)
                )

                net_return = (
                    estimated_value - entry_cost
                ) / entry_cost

                if (
                    net_return <= -STOP_LOSS
                    or net_return >= TAKE_PROFIT
                ):
                    sell(position, price)
                    continue

            signal = get_signal(strategy, previous_closes)

            if signal == "BUY" and position["qty"] == 0:
                buy(position, price)

            elif signal == "SELL" and position["qty"] > 0:
                sell(position, price)

    final_price = closes[-1]
    results = {}

    for strategy, position in positions.items():
        final_value = (
            position["cash"]
            + position["qty"] * final_price * (1 - FEE_RATE)
        )

        initial = (
            INITIAL_CAPITAL
            / (len(STRATEGIES) * len(MARKETS))
        )

        profit = final_value - initial

        results[strategy] = {
            "initial": initial,
            "final": final_value,
            "profit": profit,
            "return_pct": profit / initial * 100,
            "trades": position["closed_trades"],
            "wins": position["wins"],
            "losses": position["losses"],
            "realized_pnl": position["realized_pnl"],
        }

    return results


def main():
    print("=" * 45)
    print("بک‌تست تاریخی ربات آزمایشی نوبیتکس")
    print("=" * 45)

    print(f"سرمایه مجازی کل: {INITIAL_CAPITAL:.2f} USDT")
    print(f"مدت آزمایش: {DAYS} روز")
    print(f"فاصله کندل‌ها: {RESOLUTION} دقیقه")
    print(f"کارمزد هر طرف: {FEE_RATE * 100:.2f}%")
    print("معاملات واقعی: غیرفعال")
    print()

    all_results = {
        strategy: {
            "initial": 0.0,
            "final": 0.0,
            "profit": 0.0,
            "trades": 0,
            "wins": 0,
            "losses": 0,
        }
        for strategy in STRATEGIES
    }

    for symbol in MARKETS:
        candles = fetch_candles(symbol)

        first_time = datetime.fromtimestamp(
            candles[0][0], timezone.utc
        ).strftime("%Y-%m-%d %H:%M UTC")

        last_time = datetime.fromtimestamp(
            candles[-1][0], timezone.utc
        ).strftime("%Y-%m-%d %H:%M UTC")

        print(f"بازه {symbol}: {first_time} تا {last_time}")

        results = run_backtest(symbol, candles)

        for strategy, result in results.items():
            all_results[strategy]["initial"] += result["initial"]
            all_results[strategy]["final"] += result["final"]
            all_results[strategy]["profit"]
