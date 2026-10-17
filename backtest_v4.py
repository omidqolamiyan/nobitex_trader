import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# ==========================================================
# NOBITEX BACKTEST V4
# WALK-FORWARD COMPARISON: RSI vs EMA
# SIMULATION ONLY - NO REAL ORDERS
# ==========================================================

API_URL = "https://apiv2.nobitex.ir/market/udf/history"

LOOKBACK_DAYS = 180
RESOLUTION = "60"

INITIAL_CAPITAL = 5.0
FEE_RATE = 0.0013
SLIPPAGE_RATE = 0.0005
MIN_BUY_CASH = 0.50

MARKETS = ["BTCUSDT", "ETHUSDT"]

TRAIN_DAYS = 60
TEST_DAYS = 14
MIN_TRAIN_TRADES = 3

RSI_PERIODS = [7, 14, 21]
OVERSOLD_LEVELS = [20, 25, 30]
OVERBOUGHT_LEVELS = [65, 70, 75]
STOP_LOSSES = [0.015, 0.025]
TAKE_PROFITS = [0.03, 0.05, 0.07]

EMA_FAST_PERIODS = [8, 12, 20]
EMA_SLOW_PERIODS = [30, 50, 100]

DRAWDOWN_PENALTY = 0.50
MAX_PAGES = 50
REQUEST_DELAY_SECONDS = 2.1
MIN_HISTORY_COVERAGE = 0.95

REPORT_FILE = "backtest_v4_report.json"


# ==========================================================
# TIME
# ==========================================================

def utc_text(timestamp):
    return datetime.fromtimestamp(
        timestamp, timezone.utc
    ).strftime("%Y-%m-%d %H:%M UTC")


# ==========================================================
# HISTORICAL DATA
# ==========================================================

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
            "User-Agent": "Nobitex-Backtest-V4/1.0",
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

    fields = [
        data.get(key, [])
        for key in ("t", "o", "h", "l", "c")
    ]

    if len({len(values) for values in fields}) != 1:
        raise RuntimeError(
            f"{symbol}: inconsistent candle arrays"
        )

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
            and row["h"] >= max(
                row["o"], row["c"], row["l"]
            )
            and row["l"] <= min(
                row["o"], row["c"], row["h"]
            )
        ):
            rows.append(row)

    return rows


def fetch_history(symbol):
    now = int(time.time())
    start = now - LOOKBACK_DAYS * 86400
    all_rows = {}

    print(
        f"\nFetching {symbol}: "
        f"{LOOKBACK_DAYS} days of hourly candles"
    )

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
            f"Page {page}: received {len(rows)}, "
            f"new {added}, total {len(all_rows)}"
        )

        if added == 0:
            break

        if page < MAX_PAGES:
            time.sleep(REQUEST_DELAY_SECONDS)

    candles = [
        all_rows[t]
        for t in sorted(all_rows)
    ]

    expected = LOOKBACK_DAYS * 24
    coverage = len(candles) / expected if expected else 0.0

    if candles:
        time_coverage = (
            candles[-1]["t"] - candles[0]["t"]
        ) / (LOOKBACK_DAYS * 86400)

        print(
            f"{symbol}: {len(candles)} candles; "
            f"count coverage {coverage * 100:.1f}%"
        )

        print(
            f"{utc_text(candles[0]['t'])} -> "
            f"{utc_text(candles[-1]['t'])}"
        )
    else:
        time_coverage = 0.0

    if (
        coverage < MIN_HISTORY_COVERAGE
        or time_coverage < MIN_HISTORY_COVERAGE
    ):
        raise RuntimeError(
            f"{symbol}: insufficient historical coverage. "
            "Backtest stopped to avoid unreliable results."
        )

    if len(candles) < 200:
        raise RuntimeError(
            f"{symbol}: too few candles."
        )

    return candles


# ==========================================================
# INDICATORS
# ==========================================================

def calculate_rsi(closes, period):
    result = [None] * len(closes)

    if len(closes) <= period:
        return result

    gains = []
    losses = []

    for i in range(1, period + 1):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    def rsi_value(gain, loss):
        if loss == 0:
            return 100.0 if gain > 0 else 50.0

        rs = gain / loss
        return 100.0 - 100.0 / (1.0 + rs)

    result[period] = rsi_value(avg_gain, avg_loss)

    for i in range(period + 1, len(closes)):
        change = closes[i] - closes[i - 1]

        avg_gain = (
            avg_gain * (period - 1)
            + max(change, 0.0)
        ) / period

        avg_loss = (
            avg_loss * (period - 1)
            + max(-change, 0.0)
        ) / period

        result[i] = rsi_value(avg_gain, avg_loss)

    return result


def calculate_ema(values, period):
    result = [None] * len(values)

    if len(values) < period:
        return result

    current = sum(values[:period]) / period
    result[period - 1] = current

    multiplier = 2.0 / (period + 1)

    for i in range(period, len(values)):
        current = (
            values[i] - current
        ) * multiplier + current

        result[i] = current

    return result


# ==========================================================
# EXECUTION MODEL
# ==========================================================

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


# ==========================================================
# SIMULATION
# Signals are based on the previous completed candle.
# Orders are simulated at the current candle open.
# ==========================================================

def simulate(
    candles,
    buy_signals,
    sell_signals,
    start_index,
    end_index,
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
    losses = 0

    equity_curve = []

    start_index = max(1, start_index)
    end_index = min(end_index, len(candles))

    if start_index >= end_index:
        return {
            "final_equity": INITIAL_CAPITAL,
            "return_pct": 0.0,
            "entries": 0,
            "closed_trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate_pct": 0.0,
            "max_drawdown_pct": 0.0,
        }

    for i in range(start_index, end_index):
        candle = candles[i]
        price_open = candle["o"]

        buy_signal = bool(buy_signals[i - 1])
        sell_signal = bool(sell_signals[i - 1])

        if qty > 0:
            stop_price = entry_price * (1 - stop_loss)
            target_price = entry_price * (1 + take_profit)

            exit_price = None

            # Conservative handling of gaps and intrabar levels.
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
                profit = proceeds - entry_cost

                if profit > 0:
                    wins += 1
                else:
                    losses += 1

                cash = proceeds
                qty = 0.0
                entry_price = None
                entry_cost = 0.0
                closed += 1

        # No same-candle look-ahead.
        if (
            qty == 0
            and cash >= MIN_BUY_CASH
            and buy_signal
        ):
            cash, qty, entry_price, entry_cost = execute_buy(
                cash, price_open
            )
            entries += 1

        equity_curve.append(
            cash + qty * candle["c"]
        )

    # Close any remaining position at the final close.
    if qty > 0:
        proceeds, _ = execute_sell(
            qty, candles[end_index - 1]["c"]
        )

        if proceeds > entry_cost:
            wins += 1
        else:
            losses += 1

        cash = proceeds
        closed += 1

        if equity_curve:
            equity_curve[-1] = cash

    final_equity = (
        equity_curve[-1]
        if equity_curve
        else INITIAL_CAPITAL
    )

    peak = INITIAL_CAPITAL
    max_drawdown = 0.0

    for equity in equity_curve:
        peak = max(peak, equity)

        if peak > 0:
            drawdown = (peak - equity) / peak
            max_drawdown = max(max_drawdown, drawdown)

    return_pct = (
        final_equity / INITIAL_CAPITAL - 1
    ) * 100

    return {
        "final_equity": round(final_equity, 6),
        "return_pct": round(return_pct, 3),
        "entries": entries,
        "closed_trades": closed,
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round(
            wins / closed * 100, 2
        ) if closed else 0.0,
        "max_drawdown_pct": round(
            max_drawdown * 100, 3
        ),
    }


# ==========================================================
# BUY AND HOLD BENCHMARK
# ==========================================================

def buy_and_hold(candles, start_index, end_index):
    if start_index >= end_index:
        return {
            "final_equity": INITIAL_CAPITAL,
            "return_pct": 0.0,
        }

    start_price = candles[start_index]["o"]
    end_price = candles[end_index - 1]["c"]

    _, qty, _, _ = execute_buy(
        INITIAL_CAPITAL, start_price
    )

    proceeds, _ = execute_sell(qty, end_price)

    return {
        "final_equity": round(proceeds, 6),
        "return_pct": round(
            (proceeds / INITIAL_CAPITAL - 1) * 100,
            3,
        ),
    }


# ==========================================================
# CONFIGURATIONS
# ==========================================================

def build_configurations():
    configurations = []

    for period in RSI_PERIODS:
        for oversold in OVERSOLD_LEVELS:
            for overbought in OVERBOUGHT_LEVELS:
                if oversold >= overbought:
                    continue

                for stop in STOP_LOSSES:
                    for target in TAKE_PROFITS:
                        configurations.append({
                            "strategy": "RSI",
                            "rsi_period": period,
                            "oversold": oversold,
                            "overbought": overbought,
                            "stop_loss": stop,
                            "take_profit": target,
                        })

    for fast in EMA_FAST_PERIODS:
        for slow in EMA_SLOW_PERIODS:
            if fast >= slow:
                continue

            for stop in STOP_LOSSES:
                for target in TAKE_PROFITS:
                    configurations.append({
                        "strategy": "EMA",
                        "ema_fast": fast,
                        "ema_slow": slow,
                        "stop_loss": stop,
                        "take_profit": target,
                    })

    return configurations


def score_result(result):
    return (
        result["return_pct"]
        - DRAWDOWN_PENALTY
        * result["max_drawdown_pct"]
    )


def config_report(config):
    output = {
        "strategy": config["strategy"],
        "stop_loss_pct": config["stop_loss"] * 100,
        "take_profit_pct": config["take_profit"] * 100,
    }

    if config["strategy"] == "RSI":
        output.update({
            "rsi_period": config["rsi_period"],
            "oversold": config["oversold"],
            "overbought": config["overbought"],
        })
    else:
        output.update({
            "ema_fast": config["ema_fast"],
            "ema_slow": config["ema_slow"],
        })

    return output


# ==========================================================
# WALK-FORWARD TEST
# ==========================================================

def test_market(symbol, candles):
    closes = [c["c"] for c in candles]

    rsi_indicators = {
        p: calculate_rsi(closes, p)
        for p in RSI_PERIODS
    }

    ema_indicators = {}

    for period in set(EMA_FAST_PERIODS + EMA_SLOW_PERIODS):
        ema_indicators[period] = calculate_ema(
            closes, period
        )

    configurations = build_configurations()

    train_bars = TRAIN_DAYS * 24
    test_bars = TEST_DAYS * 24

    if len(candles) <= train_bars + 24:
        raise RuntimeError(
            f"{symbol}: insufficient data for walk-forward."
        )

    print("\n" + "=" * 65)
    print(f"BACKTEST V4: {symbol}")
    print(f"Candles: {len(candles)}")
    print(f"Configurations: {len(configurations)}")
    print("=" * 65)

    # Build signals once. All signals use completed candles.
    signal_cache = {}

    for config in configurations:
        if config["strategy"] == "RSI":
            key = (
                "RSI",
                config["rsi_period"],
                config["oversold"],
                config["overbought"],
            )

            if key not in signal_cache:
                values = rsi_indicators[config["rsi_period"]]
                buy = [False] * len(candles)
                sell = [False] * len(candles)

                for i, value in enumerate(values):
                    if value is not None:
                        buy[i] = value < config["oversold"]
                        sell[i] = value > config["overbought"]

                signal_cache[key] = (buy, sell)

        else:
            key = (
                "EMA",
                config["ema_fast"],
                config["ema_slow"],
            )

            if key not in signal_cache:
                fast_values = ema_indicators[config["ema_fast"]]
                slow_values = ema_indicators[config["ema_slow"]]

                buy = [False] * len(candles)
                sell = [False] * len(candles)

                for i in range(1, len(candles)):
                    prev_fast = fast_values[i - 1]
                    prev_slow = slow_values[i - 1]
                    curr_fast = fast_values[i]
                    curr_slow = slow_values[i]

                    if (
                        prev_fast is not None
                        and prev_slow is not None
                        and curr_fast is not None
                        and curr_slow is not None
                    ):
                        buy[i] = (
                            prev_fast <= prev_slow
                            and curr_fast > curr_slow
                        )

                        sell[i] = (
                            prev_fast >= prev_slow
                            and curr_fast < curr_slow
                        )

                signal_cache[key] = (buy, sell)

    folds = []
    test_start = train_bars
    fold_number = 0

    while test_start < len(candles):
        test_end = min(
            test_start + test_bars,
            len(candles),
        )

        if test_end - test_start < 7 * 24:
            break

        fold_number += 1
        train_start = max(1, test_start - train_bars)

        print(
            f"\nFold {fold_number}: "
            f"train {utc_text(candles[train_start]['t'])} "
            f"to {utc_text(candles[test_start - 1]['t'])}"
        )

        strategy_results = {}

        # Select the best parameters separately for each strategy.
        for strategy in ("RSI", "EMA"):
            scored = []

            for config in configurations:
                if config["strategy"] != strategy:
                    continue

                if strategy == "RSI":
                    key = (
                        "RSI",
                        config["rsi_period"],
                        config["oversold"],
                        config["overbought"],
                    )
                else:
                    key = (
                        "EMA",
                        config["ema_fast"],
                        config["ema_slow"],
                    )

                buy_signals, sell_signals = signal_cache[key]

                training_result = simulate(
                    candles,
                    buy_signals,
                    sell_signals,
                    train_start,
                    test_start,
                    config["stop_loss"],
                    config["take_profit"],
                )

                scored.append({
                    "config": config,
                    "training": training_result,
                    "score": score_result(training_result),
                })

            eligible = [
                row for row in scored
                if row["training"]["closed_trades"]
                >= MIN_TRAIN_TRADES
            ]

            if eligible:
                pool = eligible
                selection_status = "minimum_trades_met"
            else:
                pool = scored
                selection_status = "WARNING: insufficient training trades"

            pool.sort(
                key=lambda row: row["score"],
                reverse=True,
            )

            best = pool[0]
            config = best["config"]

            if strategy == "RSI":
                key = (
                    "RSI",
                    config["rsi_period"],
                    config["oversold"],
                    config["overbought"],
                )
            else:
                key = (
                    "EMA",
                    config["ema_fast"],
                    config["ema_slow"],
                )

            buy_signals, sell_signals = signal_cache[key]

            # Test the selected configuration on unseen data.
            oos = simulate(
                candles,
                buy_signals,
                sell_signals,
                test_start,
                test_end,
                config["stop_loss"],
                config["take_profit"],
            )

            strategy_results[strategy] = {
                "selection_status": selection_status,
                "selected_parameters": config_report(config),
                "training_result": best["training"],
                "out_of_sample": oos,
            }

            print(
                f"{strategy}: "
                f"training={best['training']['return_pct']:+.3f}% | "
                f"OOS={oos['return_pct']:+.3f}% | "
                f"trades={oos['closed_trades']} | "
                f"DD={oos['max_drawdown_pct']:.3f}%"
            )

        benchmark = buy_and_hold(
            candles, test_start, test_end
        )

        print(
            f"Buy & Hold: {benchmark['return_pct']:+.3f}%"
        )

        folds.append({
            "fold": fold_number,
            "training_start_utc": utc_text(
                candles[train_start]["t"]
            ),
            "training_end_utc": utc_text(
                candles[test_start - 1]["t"]
            ),
            "test_start_utc": utc_text(
                candles[test_start]["t"]
            ),
            "test_end_utc": utc_text(
                candles[test_end - 1]["t"]
            ),
            "strategies": strategy_results,
            "buy_and_hold": benchmark,
        })

        test_start = test_end

    if not folds:
        raise RuntimeError(
            f"{symbol}: no valid test folds."
        )

    summary = {}

    for strategy in ("RSI", "EMA"):
        compound = 1.0
        positive = 0
        negative = 0
        zero = 0
        total_trades = 0
        returns = []
        drawdowns = []

        for fold in folds:
            result = fold["strategies"][strategy]["out_of_sample"]
            value = result["return_pct"]

            compound *= max(0.0, 1 + value / 100)
            returns.append(value)
            drawdowns.append(result["max_drawdown_pct"])
            total_trades += result["closed_trades"]

            if value > 0:
                positive += 1
            elif value < 0:
                negative += 1
            else:
                zero += 1

        summary[strategy] = {
            "compounded_oos_return_pct": round(
                (compound - 1) * 100, 3
            ),
            "average_fold_return_pct": round(
                sum(returns) / len(returns), 3
            ),
            "average_max_drawdown_pct": round(
                sum(drawdowns) / len(drawdowns), 3
            ),
            "positive_folds": positive,
            "negative_folds": negative,
            "zero_return_folds": zero,
            "total_oos_closed_trades": total_trades,
        }

    benchmark_compound = 1.0

    for fold in folds:
        value = fold["buy_and_hold"]["return_pct"]
        benchmark_compound *= max(
            0.0, 1 + value / 100
        )

    summary["BUY_AND_HOLD"] = {
        "compounded_oos_return_pct": round(
            (benchmark_compound - 1) * 100, 3
        ),
        "folds": len(folds),
    }

    print("\n" + "-" * 65)
    print(f"SUMMARY: {symbol}")

    for strategy in ("RSI", "EMA", "BUY_AND_HOLD"):
        print(
            f"{strategy}: "
            f"{summary[strategy]['compounded_oos_return_pct']:+.3f}%"
        )

    print("-" * 65)

    return {
        "candles": len(candles),
        "first_candle_utc": utc_text(candles[0]["t"]),
        "last_candle_utc": utc_text(candles[-1]["t"]),
        "training_days": TRAIN_DAYS,
        "testing_days": TEST_DAYS,
        "configurations_tested": len(configurations),
        "summary": summary,
        "folds": folds,
    }


# ==========================================================
# MAIN
# ==========================================================

def main():
    print("=" * 65)
    print("NOBITEX BACKTEST V4 - RSI vs EMA")
    print("WALK-FORWARD | SIMULATION ONLY")
    print("REAL ORDERS DISABLED")
    print("=" * 65)

    print(f"Lookback: {LOOKBACK_DAYS} days")
    print(f"Timeframe: {RESOLUTION} minutes")
    print(f"Initial capital per test: {INITIAL_CAPITAL:.2f} USDT")
    print(f"Fee per side: {FEE_RATE * 100:.3f}%")
    print(f"Slippage per side: {SLIPPAGE_RATE * 100:.3f}%")
    print(f"Training: {TRAIN_DAYS} days")
    print(f"Testing: {TEST_DAYS} days")

    report = {
        "version": 4,
        "simulation_only": True,
        "real_orders_enabled": False,
        "lookback_days": LOOKBACK_DAYS,
        "resolution_minutes": int(RESOLUTION),
        "initial_capital_usdt": INITIAL_CAPITAL,
        "fee_rate_per_side": FEE_RATE,
        "slippage_rate_per_side": SLIPPAGE_RATE,
        "method": "walk-forward RSI vs EMA vs Buy and Hold",
        "markets": {},
    }

    for symbol in MARKETS:
        candles = fetch_history(symbol)
        report["markets"][symbol] = test_market(
            symbol, candles
        )

    with open(REPORT_FILE, "w", encoding="utf-8") as file:
        json.dump(
            report,
            file,
            indent=2,
            ensure_ascii=False,
        )

    print("\n" + "=" * 65)
    print(f"Report saved to {REPORT_FILE}")
    print("Historical performance does not guarantee future profits.")
    print("No real orders were sent.")
    print("=" * 65)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"\nBACKTEST FAILED: {exc}")
        raise
