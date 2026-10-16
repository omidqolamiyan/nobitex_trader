import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# ==========================================================
# NOBITEX BACKTEST V3 - WALK-FORWARD
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

# Walk-forward configuration
TRAIN_DAYS = 60
TEST_DAYS = 14
MIN_TRAIN_TRADES = 3

# RSI optimization grid
RSI_PERIODS = [7, 14, 21]
OVERSOLD_LEVELS = [20, 25, 30]
OVERBOUGHT_LEVELS = [65, 70, 75]
STOP_LOSSES = [0.015, 0.025, 0.04]
TAKE_PROFITS = [0.03, 0.05, 0.07]

# Selection score = return - drawdown penalty
DRAWDOWN_PENALTY = 0.50

REPORT_FILE = "backtest_v3_report.json"

MAX_PAGES = 50
REQUEST_DELAY_SECONDS = 2.1
MIN_HISTORY_COVERAGE = 0.95


# ==========================================================
# TIME HELPERS
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
            "User-Agent": "Nobitex-WalkForward-Backtest/3.0",
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
        try:
            rows = fetch_chunk(
                symbol, start, now, page
            )
        except Exception as exc:
            raise RuntimeError(
                f"{symbol}: failed on page {page}: {exc}"
            ) from exc

        if not rows:
            print(
                f"{symbol}: pagination complete."
            )
            break

        before = len(all_rows)

        for row in rows:
            if start <= row["t"] <= now:
                all_rows[row["t"]] = row

        added = len(all_rows) - before

        print(
            f"{symbol}: page {page}, "
            f"received {len(rows)}, new {added}, "
            f"total {len(all_rows)}"
        )

        if added == 0:
            print(
                f"{symbol}: no new candles; "
                "stopping pagination."
            )
            break

        if page < MAX_PAGES:
            time.sleep(REQUEST_DELAY_SECONDS)

    candles = [
        all_rows[timestamp]
        for timestamp in sorted(all_rows)
    ]

    expected = LOOKBACK_DAYS * 24

    coverage = (
        len(candles) / expected
        if expected
        else 0.0
    )

    if candles:
        first_ts = candles[0]["t"]
        last_ts = candles[-1]["t"]

        time_coverage = (
            (last_ts - first_ts)
            / (LOOKBACK_DAYS * 86400)
        )

        print(
            f"{symbol}: {len(candles)} candles; "
            f"coverage {coverage * 100:.1f}%"
        )

        print(
            f"{utc_text(first_ts)} -> "
            f"{utc_text(last_ts)}"
        )

        print(
            f"Timestamp coverage: "
            f"{time_coverage * 100:.1f}%"
        )
    else:
        time_coverage = 0.0

    if (
        coverage < MIN_HISTORY_COVERAGE
        or time_coverage < MIN_HISTORY_COVERAGE
    ):
        raise RuntimeError(
            f"{symbol}: insufficient historical coverage. "
            f"Candle coverage={coverage * 100:.1f}%, "
            f"time coverage={time_coverage * 100:.1f}%. "
            "Backtest stopped."
        )

    if len(candles) < 200:
        raise RuntimeError(
            f"{symbol}: too few candles."
        )

    return candles


# ==========================================================
# RSI INDICATOR
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

    result[period] = rsi_value(
        avg_gain, avg_loss
    )

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

        result[i] = rsi_value(
            avg_gain, avg_loss
        )

    return result


# ==========================================================
# EXECUTION MODEL
# ==========================================================

def execute_buy(cash, price):
    fill_price = price * (1 + SLIPPAGE_RATE)

    spend = cash / (1 + FEE_RATE)

    qty = spend / fill_price

    fee = spend * FEE_RATE
    total_cost = spend + fee

    return 0.0, qty, fill_price, total_cost


def execute_sell(qty, price):
    fill_price = price * (1 - SLIPPAGE_RATE)

    gross = qty * fill_price
    fee = gross * FEE_RATE

    return gross - fee, fill_price


# ==========================================================
# SIMULATE ONE CONFIGURATION
# end_index IS EXCLUSIVE
# ==========================================================

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

        # Use only the previous completed candle's RSI.
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
            stop_price = (
                entry_price * (1 - stop_loss)
            )

            target_price = (
                entry_price * (1 + take_profit)
            )

            exit_price = None

            # Handle price gaps at the candle open.
            if candle["o"] <= stop_price:
                exit_price = candle["o"]

            elif candle["o"] >= target_price:
                exit_price = candle["o"]

            # If both levels are touched in one candle,
            # conservatively assume stop-loss first.
            elif candle["l"] <= stop_price:
                exit_price = stop_price

            elif candle["h"] >= target_price:
                exit_price = target_price

            elif sell_signal:
                exit_price = price_open

            if exit_price is not None:
                proceeds, _ = execute_sell(
                    qty, exit_price
                )

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

        # No same-candle look-ahead: signal is based
        # on the previous completed candle.
        if (
            qty == 0
            and cash >= MIN_BUY_CASH
            and buy_signal
        ):
            (
                cash,
                qty,
                entry_price,
                entry_cost,
            ) = execute_buy(cash, price_open)

            entries += 1

        equity_curve.append(
            cash + qty * candle["c"]
        )

    # Liquidate any open position at the final close.
    if qty > 0:
        proceeds, _ = execute_sell(
            qty, candles[end_index - 1]["c"]
        )

        profit = proceeds - entry_cost

        if profit > 0:
            wins += 1
        else:
            losses += 1

        cash = proceeds
        qty = 0.0

        closed += 1

        # Record the liquidation in final equity.
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
            drawdown = (
                (peak - equity) / peak
            )

            max_drawdown = max(
                max_drawdown, drawdown
            )

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

    proceeds, _ = execute_sell(
        qty, end_price
    )

    return_pct = (
        proceeds / INITIAL_CAPITAL - 1
    ) * 100

    return {
        "final_equity": round(proceeds, 6),
        "return_pct": round(return_pct, 3),
    }


# ==========================================================
# PARAMETER GRID
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
                            "rsi_period": period,
                            "oversold": oversold,
                            "overbought": overbought,
                            "stop_loss": stop,
                            "take_profit": target,
                        })

    return configurations


def config_for_report(config):
    return {
        "rsi_period": config["rsi_period"],
        "oversold": config["oversold"],
        "overbought": config["overbought"],
        "stop_loss_pct": round(
            config["stop_loss"] * 100, 3
        ),
        "take_profit_pct": round(
            config["take_profit"] * 100, 3
        ),
    }


def score_result(result):
    return (
        result["return_pct"]
        - DRAWDOWN_PENALTY
        * result["max_drawdown_pct"]
    )


# ==========================================================
# WALK-FORWARD TEST
# ==========================================================

def test_market(symbol, candles):
    closes = [c["c"] for c in candles]

    indicators = {
        period: calculate_rsi(closes, period)
        for period in RSI_PERIODS
    }

    configurations = build_configurations()

    train_bars = TRAIN_DAYS * 24
    test_bars = TEST_DAYS * 24

    if len(candles) <= train_bars + 24:
        raise RuntimeError(
            f"{symbol}: insufficient candles "
            "for walk-forward testing."
        )

    print("\n" + "=" * 65)
    print(f"WALK-FORWARD MARKET: {symbol}")
    print(f"Total candles: {len(candles)}")
    print(f"Training window: {TRAIN_DAYS} days")
    print(f"Testing window: {TEST_DAYS} days")
    print(f"Configurations per training window: {len(configurations)}")
    print("=" * 65)

    folds = []

    # Each fold uses the preceding 60 days for selection,
    # then tests the selected configuration on the next
    # 14 days. Test data is never used for selection
    # in the same fold.
    test_start = train_bars
    fold_number = 0

    while test_start < len(candles):
        test_end = min(
            test_start + test_bars,
            len(candles)
        )

        test_length = test_end - test_start

        # Avoid an excessively short final fold.
        if test_length < 7 * 24:
            break

        fold_number += 1

        train_start = max(
            60, test_start - train_bars
        )

        print(
            f"\nFold {fold_number}: "
            f"training {utc_text(candles[train_start]['t'])} "
            f"-> {utc_text(candles[test_start - 1]['t'])}"
        )

        print(
            f"Testing: "
            f"{utc_text(candles[test_start]['t'])} "
            f"-> {utc_text(candles[test_end - 1]['t'])}"
        )

        scored = []

        # Parameter selection uses training data only.
        for config in configurations:
            period = config["rsi_period"]

            train_result = simulate(
                candles,
                indicators[period],
                train_start,
                test_start,
                config["oversold"],
                config["overbought"],
                config["stop_loss"],
                config["take_profit"],
            )

            score = score_result(train_result)

            scored.append({
                "config": config,
                "training": train_result,
                "score": score,
            })

        # Prefer configurations with enough completed trades.
        eligible = [
            row for row in scored
            if row["training"]["closed_trades"]
            >= MIN_TRAIN_TRADES
        ]

        if eligible:
            selection_pool = eligible
            selection_status = "minimum_trade_requirement_met"
        else:
            # Fallback is explicitly marked as weak evidence.
            selection_pool = scored
            selection_status = (
                "WARNING: no configuration met "
                "minimum training trade count"
            )

        selection_pool.sort(
            key=lambda row: row["score"],
            reverse=True,
        )

        best = selection_pool[0]

        selected_config = best["config"]

        # The selected configuration is now frozen
        # and evaluated on the unseen test interval.
        oos_result = simulate(
            candles,
            indicators[selected_config["rsi_period"]],
            test_start,
            test_end,
            selected_config["oversold"],
            selected_config["overbought"],
            selected_config["stop_loss"],
            selected_config["take_profit"],
        )

        benchmark = buy_and_hold(
            candles, test_start, test_end
        )

        print(
            f"Selected RSI={selected_config['rsi_period']} "
            f"OS={selected_config['oversold']} "
            f"OB={selected_config['overbought']} "
            f"SL={selected_config['stop_loss'] * 100:.1f}% "
            f"TP={selected_config['take_profit'] * 100:.1f}%"
        )

        print(
            f"Training return: "
            f"{best['training']['return_pct']:+.3f}% | "
            f"training trades: "
            f"{best['training']['closed_trades']}"
        )

        print(
            f"OOS return: {oos_result['return_pct']:+.3f}% | "
            f"OOS DD: {oos_result['max_drawdown_pct']:.3f}% | "
            f"OOS trades: {oos_result['closed_trades']} | "
            f"Buy & Hold: {benchmark['return_pct']:+.3f}%"
        )

        if not eligible:
            print(
                "WARNING: this fold has insufficient "
                "training trades for reliable selection."
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
            "selection_status": selection_status,
            "training_score": round(best["score"], 3),
            "selected_parameters": config_for_report(
                selected_config
            ),
            "training_result": best["training"],
            "out_of_sample": oos_result,
            "buy_and_hold": benchmark,
        })

        test_start = test_end

    if not folds:
        raise RuntimeError(
            f"{symbol}: no valid walk-forward folds."
        )

    # Each test fold starts with the same virtual capital.
    # The compounded percentage represents sequential
    # reinvestment of the reported fold returns.
    compounded_return = 1.0

    oos_returns = []
    benchmark_returns = []
    oos_drawdowns = []
    oos_trades = []
    positive_folds = 0
    negative_folds = 0

    for fold in folds:
        result = fold["out_of_sample"]
        benchmark = fold["buy_and_hold"]

        factor = 1 + result["return_pct"] / 100
        compounded_return *= max(0.0, factor)

        oos_returns.append(result["return_pct"])
        benchmark_returns.append(
            benchmark["return_pct"]
        )
        oos_drawdowns.append(
            result["max_drawdown_pct"]
        )
        oos_trades.append(
            result["closed_trades"]
        )

        if result["return_pct"] > 0:
            positive_folds += 1
        elif result["return_pct"] < 0:
            negative_folds += 1

    cumulative_return = (
        compounded_return - 1
    ) * 100

    benchmark_compounded = 1.0

    for value in benchmark_returns:
        benchmark_compounded *= max(
            0.0, 1 + value / 100
        )

    benchmark_cumulative = (
        benchmark_compounded - 1
    ) * 100

    summary = {
        "folds": len(folds),
        "positive_folds": positive_folds,
        "negative_folds": negative_folds,
        "zero_return_folds": (
            len(folds) - positive_folds - negative_folds
        ),
        "oos_cumulative_compounded_return_pct": round(
            cumulative_return, 3
        ),
        "buy_and_hold_cumulative_compounded_return_pct": round(
            benchmark_cumulative, 3
        ),
        "oos_average_fold_return_pct": round(
            sum(oos_returns) / len(oos_returns), 3
        ),
        "oos_average_max_drawdown_pct": round(
            sum(oos_drawdowns) / len(oos_drawdowns), 3
        ),
        "oos_total_closed_trades": sum(oos_trades),
        "oos_average_closed_trades_per_fold": round(
            sum(oos_trades) / len(oos_trades), 2
        ),
    }

    print("\n" + "-" * 65)
    print(f"{symbol} WALK-FORWARD SUMMARY")
    print(
        "Compounded OOS return: "
        f"{summary['oos_cumulative_compounded_return_pct']:+.3f}%"
    )
    print(
        "Buy & Hold compounded return: "
        f"{summary['buy_and_hold_cumulative_compounded_return_pct']:+.3f}%"
    )
    print(
        f"Positive folds: {positive_folds} / {len(folds)}"
    )
    print(
        f"Total OOS closed trades: {summary['oos_total_closed_trades']}"
    )
    print("-" * 65)

    return {
        "candles": len(candles),
        "first_candle_utc": utc_text(candles[0]["t"]),
        "last_candle_utc": utc_text(candles[-1]["t"]),
        "training_days": TRAIN_DAYS,
        "testing_days": TEST_DAYS,
        "configurations_per_fold": len(configurations),
        "summary": summary,
        "folds": folds,
    }


# ==========================================================
# MAIN
# ==========================================================

def main():
    print("=" * 65)
    print("NOBITEX BACKTEST V3 - WALK-FORWARD")
    print("SIMULATION ONLY - NO REAL ORDERS")
    print("=" * 65)

    print(f"Lookback: {LOOKBACK_DAYS} days")
    print(f"Timeframe: {RESOLUTION} minutes")
    print(f"Initial capital per test: {INITIAL_CAPITAL:.2f} USDT")
    print(f"Fee per side: {FEE_RATE * 100:.3f}%")
    print(f"Slippage per side: {SLIPPAGE_RATE * 100:.3f}%")
    print(f"Training window: {TRAIN_DAYS} days")
    print(f"Testing window: {TEST_DAYS} days")
    print(f"Minimum training trades: {MIN_TRAIN_TRADES}")
    print("Real orders: DISABLED")

    report = {
        "version": 3,
        "created_utc": datetime.now(
            timezone.utc
        ).isoformat(),
        "simulation_only": True,
        "real_orders_enabled": False,
        "lookback_days": LOOKBACK_DAYS,
        "resolution_minutes": int(RESOLUTION),
        "initial_capital_per_test": INITIAL_CAPITAL,
        "fee_rate_per_side": FEE_RATE,
        "slippage_rate_per_side": SLIPPAGE_RATE,
        "training_days": TRAIN_DAYS,
        "testing_days": TEST_DAYS,
        "minimum_training_trades": MIN_TRAIN_TRADES,
        "drawdown_penalty": DRAWDOWN_PENALTY,
        "markets": {},
    }

    for symbol in MARKETS:
        candles = fetch_history(symbol)

        report["markets"][symbol] = test_market(
            symbol, candles
        )

    with open(
        REPORT_FILE, "w", encoding="utf-8"
    ) as file:
        json.dump(
            report,
            file,
            ensure_ascii=False,
            indent=2,
        )

    print("\n" + "=" * 65)
    print(f"Report saved to {REPORT_FILE}")
    print("Historical performance does not guarantee future profits.")
    print("No real orders were sent.")
    print("=" * 65)


if __name__ == "__main__":
    main()
