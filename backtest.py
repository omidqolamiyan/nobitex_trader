import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

API_URL = "https://apiv2.nobitex.ir/market/udf/history"

LOOKBACK_DAYS = 60
RESOLUTION = "5"
INITIAL_CAPITAL = 5.0
FEE_RATE = 0.0013
SLIPPAGE_RATE = 0.0005
STOP_LOSS = 0.025
TAKE_PROFIT = 0.05

MARKETS = ["BTCUSDT", "ETHUSDT"]
STRATEGIES = [
    "BUY_HOLD",
    "EMA_TREND",
    "RSI_REVERSAL",
    "BOLLINGER_REVERSION",
    "MOMENTUM_BREAKOUT",
]

REPORT_FILE = "backtest_report.json"


def utc_text(timestamp):
    return datetime.fromtimestamp(
        timestamp, timezone.utc
    ).strftime("%Y-%m-%d %H:%M UTC")


def fetch_chunk(symbol, start_ts, end_ts):
    params = {
        "symbol": symbol,
        "resolution": RESOLUTION,
        "from": start_ts,
        "to": end_ts,
    }

    url = API_URL + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Nobitex-Paper-Backtest/6.0",
            "Accept": "application/json",
        },
    )

    with urllib.request.urlopen(request, timeout=30) as response:
        data = json.loads(response.read().decode("utf-8"))

    if data.get("s") == "no_data":
        return []

    if data.get("s") != "ok":
        raise RuntimeError(
            f"{symbol}: API error: {data}"
        )

    timestamps = data.get("t", [])
    opens = data.get("o", [])
    highs = data.get("h", [])
    lows = data.get("l", [])
    closes = data.get("c", [])

    lengths = [
        len(timestamps), len(opens), len(highs),
        len(lows), len(closes)
    ]

    if len(set(lengths)) != 1:
        raise RuntimeError(
            f"{symbol}: inconsistent candle arrays: {lengths}"
        )

    rows = []

    for i in range(len(timestamps)):
        row = {
            "t": int(timestamps[i]),
            "o": float(opens[i]),
            "h": float(highs[i]),
            "l": float(lows[i]),
            "c": float(closes[i]),
        }

        if row["c"] > 0 and row["o"] > 0:
            rows.append(row)

    return rows


def fetch_history(symbol):
    now = int(time.time())
    start = now - LOOKBACK_DAYS * 86400

    # One-day chunks contain approximately 288 five-minute candles,
    # below Nobitex's documented 500-candle response limit.
    chunk_seconds = 86400
    all_rows = {}
    cursor = start
    request_count = 0

    print(
        f"\nFetching {symbol}: "
        f"{LOOKBACK_DAYS} days of 5-minute candles"
    )

    while cursor < now:
        chunk_end = min(cursor + chunk_seconds, now)

        try:
            rows = fetch_chunk(symbol, cursor, chunk_end)
        except Exception as exc:
            raise RuntimeError(
                f"{symbol}: failed near {utc_text(cursor)}: {exc}"
            ) from exc

        request_count += 1

        for row in rows:
            if start <= row["t"] <= now:
                all_rows[row["t"]] = row

        print(
            f"{symbol}: request {request_count}, "
            f"candles received {len(rows)}, "
            f"unique total {len(all_rows)}"
        )

        cursor = chunk_end

        # Respect the documented public API request limit.
        time.sleep(2.1)

    result = [
        all_rows[t] for t in sorted(all_rows)
    ]

    expected = LOOKBACK_DAYS * 24 * 12
    coverage = len(result) / expected * 100 if expected else 0

    print(
        f"{symbol}: {len(result)} unique candles; "
        f"expected approximately {expected}; "
        f"coverage {coverage:.1f}%"
    )

    if result:
        print(
            f"{symbol}: {utc_text(result[0]['t'])} "
            f"-> {utc_text(result[-1]['t'])}"
        )

    # Never silently claim a complete 60-day test with only one week
    # of data. Missing candles can still occur naturally.
    if len(result) < expected * 0.80:
        raise RuntimeError(
            f"{symbol}: insufficient history. "
            f"Expected about {expected} candles, "
            f"received {len(result)}. "
            "Backtest stopped to avoid misleading results."
        )

    if len(result) < 200:
        raise RuntimeError(
            f"{symbol}: too few candles for a meaningful test."
        )

    return result


def ema(values, period):
    output = [None] * len(values)

    if len(values) < period:
        return output

    current = sum(values[:period]) / period
    output[period - 1] = current
    alpha = 2.0 / (period + 1)

    for i in range(period, len(values)):
        current = (
            values[i] * alpha
            + current * (1 - alpha)
        )
        output[i] = current

    return output


def rsi(values, period=14):
    output = [None] * len(values)

    if len(values) <= period:
        return output

    gains = []
    losses = []

    for i in range(1, period + 1):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    def value(gain, loss):
        if loss == 0:
            return 100.0 if gain > 0 else 50.0
        rs = gain / loss
        return 100 - 100 / (1 + rs)

    output[period] = value(avg_gain, avg_loss)

    for i in range(period + 1, len(values)):
        change = values[i] - values[i - 1]
        gain = max(change, 0)
        loss = max(-change, 0)

        avg_gain = (
            avg_gain * (period - 1) + gain
        ) / period

        avg_loss = (
            avg_loss * (period - 1) + loss
        ) / period

        output[i] = value(avg_gain, avg_loss)

    return output


def make_signals(candles, strategy):
    closes = [x["c"] for x in candles]
    fast = ema(closes, 9)
    slow = ema(closes, 21)
    rsi_values = rsi(closes, 14)

    signals = ["WAIT"] * len(candles)

    for i in range(60, len(candles)):
        # Signals use only information available at the previous close.
        j = i - 1

        if strategy == "BUY_HOLD":
            if i == 60:
                signals[i] = "BUY"

        elif strategy == "EMA_TREND":
            if fast[j] is None or slow[j] is None:
                continue

            if fast[j] > slow[j] and closes[j] > slow[j]:
                signals[i] = "BUY"
            elif fast[j] < slow[j]:
                signals[i] = "SELL"

        elif strategy == "RSI_REVERSAL":
            if rsi_values[j] is None:
                continue

            if rsi_values[j] < 30:
                signals[i] = "BUY"
            elif rsi_values[j] > 65:
                signals[i] = "SELL"

        elif strategy == "BOLLINGER_REVERSION":
            if j < 20:
                continue

            window = closes[j - 19:j + 1]
            mean = sum(window) / len(window)
            variance = sum(
                (x - mean) ** 2 for x in window
            ) / len(window)

            deviation = variance ** 0.5
            lower = mean - 2 * deviation
            upper = mean + 2 * deviation

            if closes[j] < lower:
                signals[i] = "BUY"
            elif closes[j] > mean:
                signals[i] = "SELL"

        elif strategy == "MOMENTUM_BREAKOUT":
            if j < 20:
                continue

            previous_high = max(
                x["h"] for x in candles[j - 20:j]
            )

            previous_low = min(
                x["l"] for x in candles[j - 20:j]
            )

            if closes[j] > previous_high:
                signals[i] = "BUY"
            elif closes[j] < previous_low:
                signals[i] = "SELL"

    return signals


def execute_buy(cash, price):
    # Slippage increases the simulated purchase price.
    fill_price = price * (1 + SLIPPAGE_RATE)
    spend = cash / (1 + FEE_RATE)
    qty = spend / fill_price
    fee = spend * FEE_RATE

    return 0.0, qty, fill_price, spend + fee


def execute_sell(qty, price):
    fill_price = price * (1 - SLIPPAGE_RATE)
    gross = qty * fill_price
    fee = gross * FEE_RATE
    proceeds = gross - fee

    return proceeds, fill_price


def simulate(candles, strategy, start_index=60):
    signals = make_signals(candles, strategy)

    cash = INITIAL_CAPITAL
    qty = 0.0
    entry_price = None
    entry_cost = 0.0

    closed_trades = 0
    wins = 0
    losses = 0
    equity_curve = []
    trade_log = []

    start_index = max(start_index, 60)

    for i in range(start_index, len(candles)):
        candle = candles[i]
        price_open = candle["o"]

        if qty > 0:
            stop_price = entry_price * (1 - STOP_LOSS)
            target_price = entry_price * (1 + TAKE_PROFIT)

            # Conservative assumption if both levels are touched
            # within the same candle: assume stop loss occurs first.
            exit_price = None
            exit_reason = None

            if candle["l"] <= stop_price:
                exit_price = stop_price
                exit_reason = "STOP_LOSS"
            elif candle["h"] >= target_price:
                exit_price = target_price
                exit_reason = "TAKE_PROFIT"
            elif signals[i] == "SELL":
                exit_price = price_open
                exit_reason = "SIGNAL"

            if exit_price is not None:
                proceeds, fill_price = execute_sell(
                    qty, exit_price
                )

                profit = proceeds - entry_cost
                cash = proceeds

                closed_trades += 1

                if profit > 0:
                    wins += 1
                else:
                    losses += 1

                trade_log.append({
                    "exit_time": candle["t"],
                    "reason": exit_reason,
                    "entry_price": entry_price,
                    "exit_price": fill_price,
                    "profit": profit,
                })

                qty = 0.0
                entry_price = None
                entry_cost = 0.0

        if qty == 0 and cash >= 0.50 and signals[i] == "BUY":
            original_cash = cash

            cash, qty, entry_price, entry_cost = execute_buy(
                original_cash, price_open
            )

        equity = cash + qty * candle["c"]
        equity_curve.append(equity)

    if not equity_curve:
        final_equity = INITIAL_CAPITAL
        max_drawdown = 0.0
    else:
        final_equity = equity_curve[-1]
        peak = INITIAL_CAPITAL
        max_drawdown = 0.0

        for equity in equity_curve:
            peak = max(peak, equity)

            if peak > 0:
                drawdown = (peak - equity) / peak
                max_drawdown = max(max_drawdown, drawdown)

    total_return = (
        (final_equity / INITIAL_CAPITAL) - 1
    ) * 100

    win_rate = (
        wins / closed_trades * 100
        if closed_trades else 0.0
    )

    return {
        "strategy": strategy,
        "initial_capital": INITIAL_CAPITAL,
        "final_equity": round(final_equity, 6),
        "return_pct": round(total_return, 3),
        "closed_trades": closed_trades,
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round(win_rate, 2),
        "max_drawdown_pct": round(max_drawdown * 100, 3),
        "trade_log": trade_log,
    }


def run_market(symbol, candles):
    split = int(len(candles) * 0.70)

    # Use the first 70% for the full-history evaluation.
    # The final 30% is separately evaluated from fresh capital.
    # Indicators still have access to earlier candles for warm-up.
    results = []

    print(
        f"\n{symbol}: {len(candles)} candles; "
        f"out-of-sample begins near "
        f"{utc_text(candles[split]['t'])}"
    )

    for strategy in STRATEGIES:
        full = simulate(candles, strategy, 60)
        oos = simulate(candles, strategy, split)

        row = {
            "symbol": symbol,
            "strategy": strategy,
            "full_period": full,
            "out_of_sample": oos,
            "out_of_sample_start": utc_text(candles[split]["t"]),
        }

        results.append(row)

        print(
            f"{strategy:22} "
            f"full {full['return_pct']:+7.3f}% | "
            f"OOS {oos['return_pct']:+7.3f}% | "
            f"trades {full['closed_trades']:4d} | "
            f"max DD {full['max_drawdown_pct']:.2f}%"
        )

    return results


def main():
    print("NOBITEX HISTORICAL BACKTEST — SIMULATION ONLY")
    print(
        f"Lookback: {LOOKBACK_DAYS} days | "
        f"timeframe: 5m | "
        f"fee/side: {FEE_RATE * 100:.3f}% | "
        f"slippage/side: {SLIPPAGE_RATE * 100:.3f}%"
    )
    print(
        f"Stop loss: {STOP_LOSS * 100:.2f}% | "
        f"take profit: {TAKE_PROFIT * 100:.2f}% | "
        "real orders: DISABLED"
    )

    report = {
        "created_utc": datetime.now(
            timezone.utc
        ).isoformat(),
        "simulation_only": True,
        "real_orders_enabled": False,
        "lookback_days_requested": LOOKBACK_DAYS,
        "resolution_minutes": 5,
        "fee_rate_per_side": FEE_RATE,
        "slippage_rate_per_side": SLIPPAGE_RATE,
        "stop_loss_pct": STOP_LOSS * 100,
        "take_profit_pct": TAKE_PROFIT * 100,
        "markets": {},
    }

    for symbol in MARKETS:
        candles = fetch_history(symbol)
        report["markets"][symbol] = {
            "candles": len(candles),
            "first_candle_utc": utc_text(candles[0]["t"]),
            "last_candle_utc": utc_text(candles[-1]["t"]),
            "strategies": run_market(symbol, candles),
        }

    with open(REPORT_FILE, "w", encoding="utf-8") as file:
        json.dump(
            report,
            file,
            ensure_ascii=False,
            indent=2,
        )

    print(f"\nReport saved to {REPORT_FILE}")
    print(
        "Research only. Historical performance does not predict "
        "future returns; no real orders were sent."
    )


if __name__ == "__main__":
    main()
