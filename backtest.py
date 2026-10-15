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
MIN_BUY_CASH = 0.50
MARKETS = ["BTCUSDT", "ETHUSDT"]
STRATEGIES = [
    "BUY_HOLD",
    "EMA_TREND",
    "RSI_REVERSAL",
    "BOLLINGER_REVERSION",
    "MOMENTUM_BREAKOUT",
]
REPORT_FILE = "backtest_report.json"
MAX_PAGES = 50
REQUEST_DELAY_SECONDS = 2.1
MIN_HISTORY_COVERAGE = 0.95


def utc_text(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def fetch_chunk(symbol, start_ts, end_ts, page=1):
    """Fetch one page of Nobitex UDF candles for the requested time range."""
    params = {
        "symbol": symbol,
        "resolution": RESOLUTION,
        "from": int(start_ts),
        "to": int(end_ts),
        "page": int(page),
    }
    url = API_URL + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Nobitex-Paper-Backtest/7.0",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        data = json.loads(response.read().decode("utf-8"))

    if data.get("s") == "no_data":
        return []
    if data.get("s") != "ok":
        raise RuntimeError(f"{symbol}: API error: {data}")

    fields = [data.get(k, []) for k in ("t", "o", "h", "l", "c")]
    lengths = [len(x) for x in fields]
    if len(set(lengths)) != 1:
        raise RuntimeError(f"{symbol}: inconsistent candle arrays: {lengths}")

    timestamps, opens, highs, lows, closes = fields
    rows = []
    for i in range(len(timestamps)):
        row = {
            "t": int(timestamps[i]),
            "o": float(opens[i]),
            "h": float(highs[i]),
            "l": float(lows[i]),
            "c": float(closes[i]),
        }
        if row["o"] > 0 and row["c"] > 0 and row["h"] > 0 and row["l"] > 0:
            rows.append(row)
    return rows


def fetch_history(symbol):
    """Fetch paginated historical candles and reject incomplete lookbacks."""
    now = int(time.time())
    start = now - LOOKBACK_DAYS * 86400
    all_rows = {}
    print(f"\nFetching {symbol}: {LOOKBACK_DAYS} days of {RESOLUTION}-minute candles")

    for page in range(1, MAX_PAGES + 1):
        try:
            rows = fetch_chunk(symbol, start, now, page=page)
        except Exception as exc:
            raise RuntimeError(f"{symbol}: failed on page {page}: {exc}") from exc

        if not rows:
            print(f"{symbol}: page {page} returned no data; pagination complete.")
            break

        before = len(all_rows)
        for row in rows:
            if start <= row["t"] <= now:
                all_rows[row["t"]] = row
        added = len(all_rows) - before
        print(
            f"{symbol}: page {page}, received {len(rows)}, "
            f"new {added}, unique total {len(all_rows)}"
        )

        # Some APIs can return a short page before the final page. Continue
        # until the response is empty or pagination repeats existing candles.
        if added == 0:
            print(
                f"{symbol}: page {page} added no new candles; "
                "stopping to avoid an infinite loop."
            )
            break

        if page < MAX_PAGES:
            time.sleep(REQUEST_DELAY_SECONDS)
    else:
        print(f"{symbol}: reached MAX_PAGES={MAX_PAGES}.")

    result = [all_rows[t] for t in sorted(all_rows)]
    expected = LOOKBACK_DAYS * 24 * 60 // int(RESOLUTION)
    coverage = (len(result) / expected * 100) if expected else 0.0
    print(
        f"{symbol}: {len(result)} unique candles; expected about {expected}; "
        f"coverage {coverage:.1f}%"
    )

    if result:
        first_ts, last_ts = result[0]["t"], result[-1]["t"]
        time_coverage = max(0, last_ts - first_ts) / (LOOKBACK_DAYS * 86400) * 100
        print(f"{symbol}: {utc_text(first_ts)} -> {utc_text(last_ts)}")
        print(f"{symbol}: timestamp-span coverage {time_coverage:.1f}%")
    else:
        time_coverage = 0.0

    if len(result) < expected * MIN_HISTORY_COVERAGE or time_coverage < MIN_HISTORY_COVERAGE * 100:
        raise RuntimeError(
            f"{symbol}: insufficient history for a reliable {LOOKBACK_DAYS}-day test. "
            f"Expected about {expected} candles, received {len(result)} "
            f"({coverage:.1f}% candle coverage; {time_coverage:.1f}% time-span coverage). "
            "Backtest stopped rather than presenting an incomplete period as a full test."
        )

    if len(result) < 200:
        raise RuntimeError(f"{symbol}: too few candles for a meaningful test.")
    return result


def ema(values, period):
    output = [None] * len(values)
    if len(values) < period:
        return output
    current = sum(values[:period]) / period
    output[period - 1] = current
    alpha = 2.0 / (period + 1)
    for i in range(period, len(values)):
        current = values[i] * alpha + current * (1 - alpha)
        output[i] = current
    return output


def rsi(values, period=14):
    output = [None] * len(values)
    if len(values) <= period:
        return output
    gains, losses = [], []
    for i in range(1, period + 1):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    def value(gain, loss):
        if loss == 0:
            return 100.0 if gain > 0 else 50.0
        rs = gain / loss
        return 100.0 - 100.0 / (1.0 + rs)

    output[period] = value(avg_gain, avg_loss)
    for i in range(period + 1, len(values)):
        change = values[i] - values[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(change, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-change, 0.0)) / period
        output[i] = value(avg_gain, avg_loss)
    return output


def make_signals(candles, strategy):
    closes = [c["c"] for c in candles]
    fast = ema(closes, 9)
    slow = ema(closes, 21)
    rsi_values = rsi(closes, 14)
    signals = ["WAIT"] * len(candles)

    for i in range(60, len(candles)):
        j = i - 1  # use only data available before the current candle opens
        if strategy == "BUY_HOLD":
            continue  # the simulator enters at the start of each evaluated segment
        if strategy == "EMA_TREND":
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
            if j < 19:
                continue
            window = closes[j - 19:j + 1]
            mean = sum(window) / len(window)
            variance = sum((x - mean) ** 2 for x in window) / len(window)
            lower = mean - 2 * variance ** 0.5
            if closes[j] < lower:
                signals[i] = "BUY"
            elif closes[j] > mean:
                signals[i] = "SELL"
        elif strategy == "MOMENTUM_BREAKOUT":
            if j < 20:
                continue
            previous_high = max(x["h"] for x in candles[j - 20:j])
            previous_low = min(x["l"] for x in candles[j - 20:j])
            if closes[j] > previous_high:
                signals[i] = "BUY"
            elif closes[j] < previous_low:
                signals[i] = "SELL"
    return signals


def execute_buy(cash, price):
    fill_price = price * (1 + SLIPPAGE_RATE)
    spend = cash / (1 + FEE_RATE)
    qty = spend / fill_price
    fee = spend * FEE_RATE
    return 0.0, qty, fill_price, spend + fee


def execute_sell(qty, price):
    fill_price = price * (1 - SLIPPAGE_RATE)
    gross = qty * fill_price
    fee = gross * FEE_RATE
    return gross - fee, fill_price


def simulate(candles, strategy, start_index=60):
    signals = make_signals(candles, strategy)
    cash = INITIAL_CAPITAL
    qty = 0.0
    entry_price = None
    entry_cost = 0.0
    closed_trades = wins = losses = 0
    equity_curve = []
    trade_log = []
    start_index = min(max(start_index, 60), len(candles) - 1)

    for i in range(start_index, len(candles)):
        candle = candles[i]
        price_open = candle["o"]

        # Buy-and-hold is a true hold baseline: no stop-loss or take-profit exits.
        if qty > 0 and strategy != "BUY_HOLD":
            stop_price = entry_price * (1 - STOP_LOSS)
            target_price = entry_price * (1 + TAKE_PROFIT)
            exit_price = exit_reason = None
            # If both are hit within one candle, conservatively assume stop-loss first.
            if candle["l"] <= stop_price:
                exit_price, exit_reason = stop_price, "STOP_LOSS"
            elif candle["h"] >= target_price:
                exit_price, exit_reason = target_price, "TAKE_PROFIT"
            elif signals[i] == "SELL":
                exit_price, exit_reason = price_open, "SIGNAL"
            if exit_price is not None:
                proceeds, fill_price = execute_sell(qty, exit_price)
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
                qty, entry_price, entry_cost = 0.0, None, 0.0

        should_buy = (strategy == "BUY_HOLD" and i == start_index) or signals[i] == "BUY"
        if qty == 0 and cash >= MIN_BUY_CASH and should_buy:
            cash, qty, entry_price, entry_cost = execute_buy(cash, price_open)

        equity_curve.append(cash + qty * candle["c"])

    final_equity = equity_curve[-1] if equity_curve else INITIAL_CAPITAL
    peak = INITIAL_CAPITAL
    max_drawdown = 0.0
    for equity in equity_curve:
        peak = max(peak, equity)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - equity) / peak)
    total_return = (final_equity / INITIAL_CAPITAL - 1) * 100
    win_rate = wins / closed_trades * 100 if closed_trades else 0.0
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
    print(f"\n{symbol}: {len(candles)} candles; out-of-sample begins near {utc_text(candles[split]['t'])}")
    results = []
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
            f"{strategy:22} full {full['return_pct']:+7.3f}% | "
            f"OOS {oos['return_pct']:+7.3f}% | trades {full['closed_trades']:4d} | "
            f"max DD {full['max_drawdown_pct']:.2f}%"
        )
    return results


def main():
    print("NOBITEX HISTORICAL BACKTEST — SIMULATION ONLY")
    print(
        f"Lookback: {LOOKBACK_DAYS} days | timeframe: {RESOLUTION}m | "
        f"fee/side: {FEE_RATE * 100:.3f}% | slippage/side: {SLIPPAGE_RATE * 100:.3f}%"
    )
    print(
        f"Stop loss: {STOP_LOSS * 100:.2f}% | take profit: {TAKE_PROFIT * 100:.2f}% | "
        "real orders: DISABLED"
    )
    report = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "simulation_only": True,
        "real_orders_enabled": False,
        "lookback_days_requested": LOOKBACK_DAYS,
        "resolution_minutes": int(RESOLUTION),
        "fee_rate_per_side": FEE_RATE,
        "slippage_rate_per_side": SLIPPAGE_RATE,
        "stop_loss_pct": STOP_LOSS * 100,
        "take_profit_pct": TAKE_PROFIT * 100,
        "markets": {},
    }
    for symbol in MARKETS:
        candles = fetch_history(symbol)
        split = int(len(candles) * 0.70)
        report["markets"][symbol] = {
            "candles": len(candles),
            "first_candle_utc": utc_text(candles[0]["t"]),
            "last_candle_utc": utc_text(candles[-1]["t"]),
            "out_of_sample_start_utc": utc_text(candles[split]["t"]),
            "strategies": run_market(symbol, candles),
        }
    with open(REPORT_FILE, "w", encoding="utf-8") as file:
        json.dump(report, file, ensure_ascii=False, indent=2)
    print(f"\nReport saved to {REPORT_FILE}")
    print("Research only. Historical performance does not predict future returns; no real orders were sent.")


if __name__ == "__main__":
    main()
