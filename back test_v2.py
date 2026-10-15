import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# ==========================================
# NOBITEX BACKTEST V2 - SIMULATION ONLY
# ==========================================

API_URL = "https://apiv2.nobitex.ir/market/udf/history"

LOOKBACK_DAYS = 180
RESOLUTION = "60"

INITIAL_CAPITAL = 5.0
FEE_RATE = 0.0013
SLIPPAGE_RATE = 0.0005
MIN_BUY_CASH = 0.50

MARKETS = ["BTCUSDT", "ETHUSDT"]

STOP_LOSSES = [0.015, 0.025, 0.04]
TAKE_PROFITS = [0.03, 0.05, 0.07]

RSI_PERIODS = [7, 14, 21]
OVERSOLD_LEVELS = [20, 25, 30]
OVERBOUGHT_LEVELS = [65, 70, 75]

REPORT_FILE = "backtest_v2_report.json"

MAX_PAGES = 50
REQUEST_DELAY_SECONDS = 2.1
MIN_HISTORY_COVERAGE = 0.95


def utc_text(timestamp):
    return datetime.fromtimestamp(
        timestamp, timezone.utc
    ).strftime("%Y-%m-%d %H:%M UTC")


# ==========================================
# FETCH HISTORICAL CANDLES
# ==========================================

def fetch_chunk(symbol, start_ts, end_ts, page):
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
            "User-Agent": "Nobitex-Paper-Backtest/9.0",
            "Accept": "application/json",
        },
    )

    with urllib.request.urlopen(request, timeout=30) as response:
        data = json.loads(response.read().decode("utf-8"))

    if data.get("s") == "no_data":
        return []

    if data.get("s") != "ok":
        raise RuntimeError(f"API error for {symbol}: {data}")

    fields = [data.get(k, []) for k in ("t", "o", "h", "l", "c")]

    if len({len(x) for x in fields}) != 1:
        raise RuntimeError(f"Inconsistent candle data for {symbol}")

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

        if (
            row["o"] > 0
            and row["h"] > 0
            and row["l"] > 0
            and row["c"] > 0
            and row["h"] >= max(row["o"], row["c"], row["l"])
            and row["l"] <= min(row["o"], row["c"], row["h"])
        ):
            rows.append(row)

    return rows


def fetch_history(symbol):
    now = int(time.time())
    start = now - LOOKBACK_DAYS * 86400
    all_rows = {}

    print(f"\nFetching {symbol}: {LOOKBACK_DAYS} days")

    for page in range(1, MAX_PAGES + 1):
        rows = fetch_chunk(symbol, start, now, page)

        if not rows:
            print(f"{symbol}: pagination complete.")
            break

        before = len(all_rows)

        for row in rows:
            if start <= row["t"] <= now:
                all_rows[row["t"]] = row

        added = len(all_rows) - before

        print(
            f"{symbol}: page {page}, "
            f"received {len(rows)}, new {added}"
        )

        if added == 0:
            print("No new candles; stopping pagination.")
            break

        if page < MAX_PAGES:
            time.sleep(REQUEST_DELAY_SECONDS)

    candles = [all_rows[t] for t in sorted(all_rows)]

    expected = LOOKBACK_DAYS * 24
    coverage = len(candles) / expected if expected else 0

    print(
        f"{symbol}: {len(candles)} candles; "
        f"coverage {coverage * 100:.1f}%"
    )

    if candles:
        span = candles[-1]["t"] - candles[0]["t"]
        time_coverage = span / (LOOKBACK_DAYS * 86400)
        print(
            f"{utc_text(candles[0]['t'])} -> "
            f"{utc_text(candles[-1]['t'])}"
        )
    else:
        time_coverage = 0

    if (
        coverage < MIN_HISTORY_COVERAGE
        or time_coverage < MIN_HISTORY_COVERAGE
    ):
        raise RuntimeError(
            f"{symbol}: insufficient historical coverage. "
            "Backtest stopped."
        )

    return candles


# ==========================================
# RSI INDICATOR
# ==========================================

def calculate_rsi(closes, period):
    result = [None] * len(closes)

    if len(closes) <= period:
        return result

    gains = []
    losses = []

    for i in range(1, period + 1):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    def rsi_value(gain, loss):
        if loss == 0:
            return 100.0 if gain > 0 else 50.0

        rs = gain / loss
        return 100 - 100 / (1 + rs)

    result[period] = rsi_value(avg_gain, avg_loss)

    for i in range(period + 1, len(closes)):
        change = closes[i] - closes[i - 1]

        avg_gain = (
            avg_gain * (period - 1)
            + max(change, 0)
        ) / period

        avg_loss = (
            avg_loss * (period - 1)
            + max(-change, 0)
        ) / period

        result[i] = rsi_value(avg_gain, avg_loss)

    return result


# ==========================================
# EXECUTION MODEL
# ==========================================

def execute_buy(cash, price):
    fill_price = price * (1 + SLIPPAGE_RATE)
    spend = cash / (1 + FEE_RATE)
    qty = spend / fill_price
    total_cost = spend + spend * FEE_RATE

    return 0.0, qty, fill_price, total_cost


def execute_sell(qty, price):
    fill_price = price * (1 - SLIPPAGE_RATE)
    gross = qty * fill_price
    fee = gross * FEE_RATE

    return gross - fee, fill_price


# ==========================================
# SIMULATE ONE RSI CONFIGURATION
# ==========================================

def simulate(
    candles,
    rsi_values,
    start_index,
    end_index,
    oversold,
    overbought,
    stop_loss,
    take_profit,
):
    cash = INITIAL_CAPITAL
    qty = 0.0
    entry_price = None
    entry_cost = 0.0

    entries = 0
    closed = 0
    wins = 0
    equity_curve = []

    for i in range(start_index, end_index):
        candle = candles[i]
        price_open = candle["o"]

        # Signals use the previous completed candle.
        previous_rsi = rsi_values[i - 1]

        buy_signal = (
            previous_rsi is not None
            and previous_rsi < oversold
        )

        sell_signal = (
            previous_rsi is not None
            and previous_rsi > overbought
        )

        if qty > 0:
            stop_price = entry_price * (1 - stop_loss)
            target_price = entry_price * (1 + take_profit)

            exit_price = None

            # Conservative assumption when both levels are hit.
            if candle["o"] <= stop_price:
                exit_price = candle["o"]
            elif candle["o"] >= target_price:
                exit_price = candle["o"]
            elif candle["l"] <= stop_price:
                exit_price = stop_price
            elif candle["h"] >= target_price:
                exit_price = target_price
            elif sell_signal:
                exit_price = price_open

            if exit_price is not None:
                proceeds, _ = execute_sell(qty, exit_price)

                if proceeds > entry_cost:
                    wins += 1

                cash = proceeds
                qty = 0.0
                entry_price = None
                entry_cost = 0.0
                closed += 1

        if qty == 0 and cash >= MIN_BUY_CASH and buy_signal:
            cash, qty, entry_price, entry_cost = execute_buy(
                cash, price_open
            )
            entries += 1

        equity_curve.append(cash + qty * candle["c"])

    # Liquidate any remaining position at the final close.
    if qty > 0:
        proceeds, _ = execute_sell(qty, candles[end_index - 1]["c"])
        cash = proceeds
        qty = 0.0

        if cash > entry_cost:
            wins += 1

        closed += 1

        equity_curve[-1] = cash

    final_equity = equity_curve[-1] if equity_curve else INITIAL_CAPITAL

    peak = INITIAL_CAPITAL
    max_drawdown = 0.0

    for equity in equity_curve:
        peak = max(peak, equity)

        if peak > 0:
            drawdown = (peak - equity) / peak
            max_drawdown = max(max_drawdown, drawdown)

    return_pct = (final_equity / INITIAL_CAPITAL - 1) * 100

    return {
        "final_equity": round(final_equity, 6),
        "return_pct": round(return_pct, 3),
        "entries": entries,
        "closed_trades": closed,
        "wins": wins,
        "win_rate_pct": round(
            wins / closed * 100, 2
        ) if closed else 0.0,
        "max_drawdown_pct": round(max_drawdown * 100, 3),
    }


# ==========================================
# BUY AND HOLD BENCHMARK
# ==========================================

def buy_and_hold(candles, start_index, end_index):
    start_price = candles[start_index]["o"]
    end_price = candles[end_index - 1]["c"]

    cash, qty, _, _ = execute_buy(
        INITIAL_CAPITAL, start_price
    )

    proceeds, _ = execute_sell(qty, end_price)

    return_pct = (proceeds / INITIAL_CAPITAL - 1) * 100

    return {
        "final_equity": round(proceeds, 6),
        "return_pct": round(return_pct, 3),
    }


# ==========================================
# GRID SEARCH
# ==========================================

def test_market(symbol, candles):
    split = int(len(candles) * 0.70)

    print(f"\n{'=' * 55}")
    print(f"MARKET: {symbol}")
    print(f"Training candles: {split}")
    print(f"Out-of-sample candles: {len(candles) - split}")
    print(f"OOS starts: {utc_text(candles[split]['t'])}")

    closes = [c["c"] for c in candles]
    results = []

    # Indicators are calculated once for each RSI period.
    indicators = {
        period: calculate_rsi(closes, period)
        for period in RSI_PERIODS
    }

    total_tests = (
        len(RSI_PERIODS)
        * len(OVERSOLD_LEVELS)
        * len(OVERBOUGHT_LEVELS)
        * len(STOP_LOSSES)
        * len(TAKE_PROFITS)
    )

    print(f"Configurations to test: {total_tests}")

    for period in RSI_PERIODS:
        rsi_values = indicators[period]

        for oversold in OVERSOLD_LEVELS:
            for overbought in OVERBOUGHT_LEVELS:
                if oversold >= overbought:
                    continue

                for stop in STOP_LOSSES:
                    for target in TAKE_PROFITS:

                        train = simulate(
                            candles,
                            rsi_values,
                            60,
                            split,
                            oversold,
                            overbought,
                            stop,
                            target,
                        )

                        # Rank on training data only.
                        # Penalize large drawdowns.
                        score = (
                            train["return_pct"]
                            - 0.5 * train["max_drawdown_pct"]
                        )

                        results.append({
                            "rsi_period": period,
                            "oversold": oversold,
                            "overbought": overbought,
                            "stop_loss_pct": stop * 100,
                            "take_profit_pct": target * 100,
                            "training": train,
                            "training_score": round(score, 3),
                        })

    # OOS data is not used to select the parameters.
    results.sort(
        key=lambda x: x["training_score"],
        reverse=True,
    )

    # Evaluate the top training configurations on unseen data.
    for row in results:
        period = row["rsi_period"]

        row["out_of_sample"] = simulate(
            candles,
            indicators[period],
            split,
            len(candles),
            row["oversold"],
            row["overbought"],
            row["stop_loss_pct"] / 100,
            row["take_profit_pct"] / 100,
        )

    benchmark_train = buy_and_hold(candles, 60, split)
    benchmark_oos = buy_and_hold(candles, split, len(candles))

    print("\nBUY AND HOLD")
    print(
        f"Training: {benchmark_train['return_pct']:+.3f}% | "
        f"OOS: {benchmark_oos['return_pct']:+.3f}%"
    )

    print("\nTOP 10 CONFIGURATIONS (ranked on training data)")

    for number, row in enumerate(results[:10], 1):
        train = row["training"]
        oos = row["out_of_sample"]

        print(
            f"{number:2}. RSI={row['rsi_period']} "
            f"OS={row['oversold']} OB={row['overbought']} "
            f"SL={row['stop_loss_pct']:.1f}% "
            f"TP={row['take_profit_pct']:.1f}% | "
            f"Train={train['return_pct']:+.2f}% "
            f"OOS={oos['return_pct']:+.2f}% "
            f"OOS DD={oos['max_drawdown_pct']:.2f}% "
            f"Trades={oos['closed_trades']}"
        )

    return {
        "candles": len(candles),
        "first_candle_utc": utc_text(candles[0]["t"]),
        "last_candle_utc": utc_text(candles[-1]["t"]),
        "training_candles": split,
        "oos_start_utc": utc_text(candles[split]["t"]),
        "buy_and_hold_training": benchmark_train,
        "buy_and_hold_oos": benchmark_oos,
        "configurations_tested": len(results),
        "top_10": results[:10],
    }


# ==========================================
# MAIN
# ==========================================

def main():
    print("=" * 55)
    print("NOBITEX BACKTEST V2")
    print("SIMULATION ONLY - NO REAL ORDERS")
    print("=" * 55)

    print(f"Lookback: {LOOKBACK_DAYS} days")
    print(f"Timeframe: {RESOLUTION} minutes")
    print(f"Initial capital per test: {INITIAL_CAPITAL:.2f} USDT")
    print(f"Fee per side: {FEE_RATE * 100:.3f}%")
    print(f"Slippage per side: {SLIPPAGE_RATE * 100:.3f}%")
    print("Training/OOS split: 70% / 30%")

    report = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "simulation_only": True,
        "real_orders_enabled": False,
        "lookback_days": LOOKBACK_DAYS,
        "resolution_minutes": int(RESOLUTION),
        "initial_capital_per_test": INITIAL_CAPITAL,
        "fee_rate_per_side": FEE_RATE,
        "slippage_rate_per_side": SLIPPAGE_RATE,
        "training_fraction": 0.70,
        "markets": {},
    }

    for symbol in MARKETS:
        candles = fetch_history(symbol)
        report["markets"][symbol] = test_market(symbol, candles)

    with open(REPORT_FILE, "w", encoding="utf-8") as file:
        json.dump(report, file, ensure_ascii=False, indent=2)

    print(f"\nReport saved to {REPORT_FILE}")
    print("Historical results do not guarantee future profits.")
    print("No real orders were sent.")


if __name__ == "__main__":
    main()
