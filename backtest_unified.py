#!/usr/bin/env python3
"""Nobitex Unified Backtest v1.1.0 — research only; no real orders."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import statistics
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Optional

VERSION = "1.1.1"
API_URL = "https://apiv2.nobitex.ir/market/udf/history"
REPORT_FILE = "backtest_unified_report.json"
INITIAL_CAPITAL = 5.0
DAYS, RESOLUTION = 180, "60"
FEE_RATE, SLIPPAGE_RATE = 0.0013, 0.0005
MARKETS = ("BTCUSDT", "ETHUSDT")
TRAIN_DAYS, TEST_DAYS, MIN_TRAIN_TRADES = 60, 14, 3
MIN_COVERAGE, DRAWDOWN_PENALTY = 0.95, 0.50
MAX_RETRIES, REQUEST_DELAY_SECONDS = 4, 0.25
# Keep requests below the exchange endpoint's observed per-response candle limit.
MAX_CANDLES_PER_REQUEST = 450


@dataclass(frozen=True)
class Candle:
    t: int
    o: float
    h: float
    l: float
    c: float
    v: float = 0.0


@dataclass(frozen=True)
class Config:
    strategy: str
    params: tuple[tuple[str, float], ...]
    stop_loss: float
    take_profit: float

    def get(self, key, default=0.0):
        return dict(self.params).get(key, default)

    def label(self):
        p = ",".join(f"{k}={v:g}" for k, v in self.params)
        return f"{self.strategy}[{p};SL={self.stop_loss:g}%;TP={self.take_profit:g}%]"


@dataclass
class Trade:
    entry_time: int
    exit_time: int
    entry_price: float
    exit_price: float
    quantity: float
    pnl: float
    pnl_pct: float
    reason: str


@dataclass
class Result:
    start_equity: float
    end_equity: float
    return_pct: float
    max_drawdown_pct: float
    closed_trades: int
    wins: int
    losses: int
    win_rate_pct: float
    profit_factor: Optional[float]
    trades: list
    equity_curve: list


def utc(ts):
    return dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).isoformat()


def interval_seconds(resolution):
    return 86400 if resolution == "D" else int(resolution) * 60


def get_json(url):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": f"Nobitex-Unified-Backtest/{VERSION}",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        data = json.loads(response.read().decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Unexpected API response: expected a JSON object")
    return data


def parse_candles(obj):
    status = obj.get("s")
    if status not in (None, "ok"):
        if status == "no_data":
            return []
        raise ValueError(f"API status: {status}")
    arrays = [obj.get(k) for k in ("t", "o", "h", "l", "c")]
    if not all(isinstance(a, list) for a in arrays):
        raise ValueError("Missing or invalid candle arrays in API response")
    ts, opens, highs, lows, closes = arrays
    volumes = obj.get("v", [])
    if not isinstance(volumes, list):
        volumes = []
    by_time = {}
    for i in range(min(map(len, arrays))):
        try:
            candle = Candle(
                int(ts[i]), float(opens[i]), float(highs[i]),
                float(lows[i]), float(closes[i]),
                float(volumes[i]) if i < len(volumes) else 0.0,
            )
            values = (candle.o, candle.h, candle.l, candle.c, candle.v)
            if (
                candle.t > 0
                and all(math.isfinite(value) for value in values)
                and min(candle.o, candle.h, candle.l, candle.c) > 0
                and candle.v >= 0
                and candle.h >= max(candle.o, candle.c, candle.l)
                and candle.l <= min(candle.o, candle.c, candle.h)
            ):
                by_time[candle.t] = candle
        except (ValueError, TypeError, OverflowError):
            continue
    return list(by_time.values())


def fetch_candles(symbol, days=DAYS, resolution=RESOLUTION):
    """Fetch history in bounded time windows; do not depend on an unsupported page parameter."""
    if days < 1:
        raise ValueError("days must be positive")
    sec = interval_seconds(resolution)
    now = int(time.time())
    # Align boundaries to candle opens so adjacent requests cannot skip an interval.
    end_exclusive = (now // sec) * sec
    start = ((now - days * 86400) // sec) * sec
    if end_exclusive <= start:
        raise ValueError("Invalid history time range")

    all_by_time = {}
    chunk_span = MAX_CANDLES_PER_REQUEST * sec
    cursor = start
    request_count = 0
    while cursor < end_exclusive:
        chunk_end_exclusive = min(cursor + chunk_span, end_exclusive)
        # UDF from/to are inclusive timestamps; request through the final candle open
        # in this chunk, excluding the currently forming candle.
        chunk_to = chunk_end_exclusive - sec
        query = urllib.parse.urlencode({
            "symbol": symbol,
            "resolution": resolution,
            "from": cursor,
            "to": chunk_to,
        })
        last_error = None
        batch = None
        for attempt in range(MAX_RETRIES):
            try:
                obj = get_json(API_URL + "?" + query)
                batch = parse_candles(obj)
                last_error = None
                break
            except Exception as exc:
                last_error = exc
                if attempt + 1 < MAX_RETRIES:
                    time.sleep(1.0 * (attempt + 1))
        if last_error is not None:
            raise RuntimeError(
                f"Fetching {symbol} failed for {utc(cursor)} to {utc(chunk_to)}: {last_error}"
            )
        for candle in batch or []:
            if cursor <= candle.t < chunk_end_exclusive and candle.t < end_exclusive:
                all_by_time[candle.t] = candle
        request_count += 1
        cursor = chunk_end_exclusive
        if cursor < end_exclusive:
            time.sleep(REQUEST_DELAY_SECONDS)

    candles = sorted(all_by_time.values(), key=lambda candle: candle.t)
    if len(candles) < 100:
        raise RuntimeError(f"Only {len(candles)} valid candles received for {symbol}")
    # Reject future/out-of-range data and identify obvious interval alignment issues.
    if any(c.t < start or c.t >= end_exclusive for c in candles):
        raise RuntimeError("API returned candles outside the requested time range")
    # Do not assume candle opens must be divisible by the interval relative to
    # Unix epoch. Some UDF feeds use a consistent offset (for example :30 for
    # hourly candles). Validate the grid relative to the first returned candle,
    # and separately report gaps instead of rejecting a consistent offset.
    phase = candles[0].t % sec
    misaligned = [c.t for c in candles if (c.t - candles[0].t) % sec != 0]
    if misaligned:
        examples = ", ".join(utc(ts) for ts in misaligned[:3])
        raise RuntimeError(
            f"{len(misaligned)} candles do not follow a consistent {resolution}-interval grid; "
            f"examples: {examples}"
        )
    gaps = sum(
        max(0, (candles[i].t - candles[i - 1].t) // sec - 1)
        for i in range(1, len(candles))
    )
    print(
        f"{symbol}: fetched {len(candles)} candles using {request_count} time-window requests; "
        f"grid_offset={phase}s; missing_intervals={gaps}"
    )
    return candles


def sma(values, period, end):
    if period < 1 or end + 1 < period:
        return None
    return sum(values[end - period + 1:end + 1]) / period


def rsi(values, period, end):
    if period < 1 or end < period:
        return None
    gains = losses = 0.0
    for i in range(end - period + 1, end + 1):
        change = values[i] - values[i - 1]
        if change > 0:
            gains += change
        elif change < 0:
            losses -= change
    avg_gain, avg_loss = gains / period, losses / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    return 100 - 100 / (1 + avg_gain / avg_loss)


def ema_series(values, period):
    out = [None] * len(values)
    if period < 1 or len(values) < period:
        return out
    previous = sum(values[:period]) / period
    out[period - 1] = previous
    alpha = 2 / (period + 1)
    for i in range(period, len(values)):
        previous = alpha * values[i] + (1 - alpha) * previous
        out[i] = previous
    return out


def build_configs():
    out = []
    for stop_loss in (1.5, 2.5, 4.0):
        for take_profit in (3.0, 5.0, 7.0):
            for period in (7, 14, 21):
                for oversold in (20, 25, 30):
                    for overbought in (65, 70, 75):
                        out.append(Config("RSI", (("period", period), ("oversold", oversold), ("overbought", overbought)), stop_loss, take_profit))
            for fast in (5, 9, 12):
                for slow in (21, 30, 50):
                    if fast < slow:
                        out.append(Config("EMA", (("fast", fast), ("slow", slow)), stop_loss, take_profit))
            for width in (1.5, 2.0, 2.5):
                out.append(Config("BOLLINGER", (("period", 20), ("width", width)), stop_loss, take_profit))
            for period in (10, 20, 30):
                out.append(Config("BREAKOUT", (("period", period),), stop_loss, take_profit))
    # Preserve order while removing any accidental duplicates.
    unique = {}
    for config in out:
        unique[(config.strategy, config.params, config.stop_loss, config.take_profit)] = config
    return list(unique.values())


def prepare_signals(candles, config):
    """Prepare one signal per bar in linear time; bar i uses only bar i-1 and earlier."""
    n = len(candles)
    signals = [0] * n
    if config is None or n < 2:
        return signals
    closes = [c.c for c in candles]

    if config.strategy == "EMA":
        fast_values = ema_series(closes, int(config.get("fast")))
        slow_values = ema_series(closes, int(config.get("slow")))
        for i in range(1, n):
            j = i - 1
            fast, slow = fast_values[j], slow_values[j]
            if fast is not None and slow is not None:
                signals[i] = 1 if fast > slow else (-1 if fast < slow else 0)
        return signals

    for i in range(1, n):
        j = i - 1
        if config.strategy == "RSI":
            value = rsi(closes, int(config.get("period")), j)
            if value is not None:
                signals[i] = 1 if value <= config.get("oversold") else (-1 if value >= config.get("overbought") else 0)
        elif config.strategy == "BOLLINGER":
            period = int(config.get("period"))
            middle = sma(closes, period, j)
            if middle is not None:
                deviation = statistics.pstdev(closes[j - period + 1:j + 1])
                if closes[j] < middle - config.get("width") * deviation:
                    signals[i] = 1
                elif closes[j] > middle + config.get("width") * deviation:
                    signals[i] = -1
        elif config.strategy == "BREAKOUT":
            period = int(config.get("period"))
            if j >= period:
                prior = candles[j - period:j]
                if candles[j].c > max(c.h for c in prior):
                    signals[i] = 1
                elif candles[j].c < min(c.l for c in prior):
                    signals[i] = -1
    return signals


def signal_at(candles, index, config):
    """Convenience helper for tests/debugging; simulator uses precomputed signals."""
    if index < 1:
        return 0
    return prepare_signals(candles[:index + 1], config)[index]


def simulate(candles, config, initial_cash=INITIAL_CAPITAL, fee=FEE_RATE, slip=SLIPPAGE_RATE, start_index=0):
    if len(candles) < 2:
        raise ValueError("Need at least two candles")
    if not math.isfinite(initial_cash) or initial_cash <= 0:
        raise ValueError("Initial capital must be finite and positive")
    if not math.isfinite(fee) or not math.isfinite(slip) or fee < 0 or slip < 0 or fee >= 1 or slip >= 1:
        raise ValueError("Fee and slippage must be finite and in [0, 1)")
    if not 0 <= start_index < len(candles):
        raise ValueError("start_index must be within candle range")

    signals = prepare_signals(candles, config)
    cash = float(initial_cash)
    quantity = 0.0
    entry_price = entry_fee = 0.0
    entry_time = 0
    stop_price = target_price = 0.0
    trades, equity_curve = [], []
    peak_equity = float(initial_cash)
    max_drawdown = 0.0

    def sell(raw_price, timestamp, reason):
        nonlocal cash, quantity, entry_price, entry_fee, entry_time, stop_price, target_price
        if quantity <= 0:
            return
        fill_price = raw_price * (1 - slip)
        proceeds = quantity * fill_price * (1 - fee)
        pnl = proceeds - (quantity * entry_price + entry_fee)
        invested = quantity * entry_price + entry_fee
        trades.append(Trade(entry_time, timestamp, entry_price, fill_price, quantity, pnl,
                            pnl / invested * 100 if invested else 0.0, reason))
        cash = proceeds
        quantity = 0.0
        entry_price = entry_fee = 0.0
        entry_time = 0
        stop_price = target_price = 0.0

    def buy(raw_price, timestamp, sl_pct, tp_pct):
        nonlocal cash, quantity, entry_price, entry_fee, entry_time, stop_price, target_price
        fill_price = raw_price * (1 + slip)
        quantity = cash / (fill_price * (1 + fee))
        if quantity <= 0 or not math.isfinite(quantity):
            quantity = 0.0
            return
        entry_price = fill_price
        entry_fee = quantity * fill_price * fee
        entry_time = timestamp
        cash = 0.0
        stop_price = fill_price * (1 - sl_pct / 100)
        target_price = fill_price * (1 + tp_pct / 100)

    for i, candle in enumerate(candles):
        if i < start_index:
            continue
        # Signal uses only completed prior candles; orders fill at this bar's open.
        if config is not None and i > 0:
            signal = signals[i]
            if quantity > 0 and signal < 0:
                sell(candle.o, candle.t, "strategy_exit")
            elif quantity <= 0 and signal > 0:
                buy(candle.o, candle.t, config.stop_loss, config.take_profit)

        if quantity > 0:
            adverse_equity = quantity * candle.l
            if peak_equity:
                max_drawdown = max(max_drawdown, (peak_equity - adverse_equity) / peak_equity)
            hit_stop = candle.l <= stop_price
            hit_target = candle.h >= target_price
            if hit_stop and hit_target:
                sell(stop_price, candle.t, "stop_and_target_same_candle_stop_first")
            elif hit_stop:
                # If the market opens below the stop, use the worse open price.
                sell(min(stop_price, candle.o), candle.t, "stop_loss_gap_or_touch")
            elif hit_target:
                # If the market opens above the target, use the better open price.
                sell(max(target_price, candle.o), candle.t, "take_profit_gap_or_touch")

        equity = cash + quantity * candle.c
        equity_curve.append(equity)
        peak_equity = max(peak_equity, equity)
        if peak_equity:
            max_drawdown = max(max_drawdown, (peak_equity - equity) / peak_equity)

    if quantity > 0:
        sell(candles[-1].c, candles[-1].t, "end_of_test_liquidation")
        equity_curve[-1] = cash

    eligible_trades = [trade for trade in trades if trade.reason != "end_of_test_liquidation"]
    wins = sum(trade.pnl > 0 for trade in eligible_trades)
    losses = sum(trade.pnl < 0 for trade in eligible_trades)
    gross_profit = sum(trade.pnl for trade in eligible_trades if trade.pnl > 0)
    gross_loss = -sum(trade.pnl for trade in eligible_trades if trade.pnl < 0)
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else (math.inf if gross_profit > 0 else None)
    return Result(
        initial_cash, cash, (cash / initial_cash - 1) * 100, max_drawdown * 100,
        len(eligible_trades), wins, losses,
        wins / len(eligible_trades) * 100 if eligible_trades else 0.0,
        profit_factor, trades, equity_curve,
    )


def buy_hold(candles, capital=INITIAL_CAPITAL, fee=FEE_RATE, slip=SLIPPAGE_RATE):
    if len(candles) < 2:
        raise ValueError("Need at least two candles")
    if capital <= 0 or fee < 0 or slip < 0 or fee >= 1 or slip >= 1:
        raise ValueError("Invalid buy-and-hold capital or costs")
    entry_fill = candles[0].o * (1 + slip)
    quantity = capital / (entry_fill * (1 + fee))
    curve, peak, drawdown = [], capital, 0.0
    for candle in candles:
        equity = quantity * candle.c
        curve.append(equity)
        peak = max(peak, equity)
        if peak:
            drawdown = max(drawdown, (peak - quantity * candle.l) / peak)
    exit_fill = candles[-1].c * (1 - slip)
    end_equity = quantity * exit_fill * (1 - fee)
    curve[-1] = end_equity
    pnl = end_equity - capital
    trade = Trade(candles[0].t, candles[-1].t, entry_fill, exit_fill, quantity, pnl,
                  pnl / capital * 100, "buy_and_hold")
    return Result(capital, end_equity, (end_equity / capital - 1) * 100, drawdown * 100,
                  1, int(pnl > 0), int(pnl < 0), 100.0 if pnl > 0 else 0.0,
                  math.inf if pnl > 0 else None, [trade], curve)


def score(result):
    if result.closed_trades < MIN_TRAIN_TRADES:
        return -1e9
    return result.return_pct - DRAWDOWN_PENALTY * result.max_drawdown_pct


def split_folds(candles, resolution=RESOLUTION):
    per_day = 1 if resolution == "D" else 1440 // int(resolution)
    train_count = TRAIN_DAYS * per_day
    test_count = TEST_DAYS * per_day
    folds = []
    start = train_count
    while start + test_count <= len(candles):
        folds.append((candles[:start], candles[start:start + test_count]))
        start += test_count
    # Do not silently score a short final fold as if it were a full 14-day test.
    return folds


def summary(result):
    pf = result.profit_factor
    return {
        "start_equity": round(result.start_equity, 8),
        "end_equity": round(result.end_equity, 8),
        "return_pct": round(result.return_pct, 6),
        "max_drawdown_pct": round(result.max_drawdown_pct, 6),
        "closed_trades": result.closed_trades,
        "wins": result.wins,
        "losses": result.losses,
        "win_rate_pct": round(result.win_rate_pct, 4),
        "profit_factor": round(pf, 6) if pf is not None and math.isfinite(pf) else ("infinity" if pf == math.inf else None),
    }


def run_market(symbol, candles, configs, capital, fee, slip, resolution):
    folds = split_folds(candles, resolution)
    fold_rows = []
    for number, (train, test) in enumerate(folds, 1):
        best_config, best_score = None, -1e9
        for config in configs:
            training_result = simulate(train, config, capital, fee, slip)
            candidate_score = score(training_result)
            if candidate_score > best_score:
                best_config, best_score = config, candidate_score
        if best_config is None or best_score <= -1e8:
            fold_rows.append({"fold": number, "status": "no_eligible_config", "oos_return_pct": None, "oos_trades": 0})
            continue

        # Training candles are indicator warm-up only; test equity starts fresh.
        prefix = train[-100:]
        combined = prefix + test
        out = simulate(combined, best_config, capital, fee, slip, start_index=len(prefix))
        fold_rows.append({
            "fold": number,
            "status": "ok",
            "train_start_utc": utc(train[0].t),
            "train_end_utc": utc(train[-1].t),
            "test_start_utc": utc(test[0].t),
            "test_end_utc": utc(test[-1].t),
            "selected_config": best_config.label(),
            "train_score": round(best_score, 6),
            "oos_return_pct": round(out.return_pct, 6),
            "oos_max_drawdown_pct": round(out.max_drawdown_pct, 6),
            "oos_trades": out.closed_trades,
            "oos_summary": summary(out),
        })

    valid = [row for row in fold_rows if row["status"] == "ok"]
    compounded_equity = 1.0
    for row in valid:
        compounded_equity *= max(0.0, 1 + row["oos_return_pct"] / 100)
    returns = [row["oos_return_pct"] for row in valid]
    hold_result = buy_hold(candles, capital, fee, slip)
    return {
        "symbol": symbol,
        "candles": len(candles),
        "data_start_utc": utc(candles[0].t),
        "data_end_utc": utc(candles[-1].t),
        "buy_and_hold_full_period": summary(hold_result),
        "walk_forward": {
            "folds": len(valid),
            "folds_skipped": len(fold_rows) - len(valid),
            "compounded_oos_return_pct": round((compounded_equity - 1) * 100, 6) if valid else None,
            "mean_oos_return_pct": round(statistics.mean(returns), 6) if returns else None,
            "median_oos_return_pct": round(statistics.median(returns), 6) if returns else None,
            "positive_folds": sum(value > 0 for value in returns),
            "negative_folds": sum(value < 0 for value in returns),
            "total_oos_trades": sum(row["oos_trades"] for row in valid),
        },
        "fold_details": fold_rows,
    }


def self_test():
    base = 1700000000
    assert ema_series([1, 2, 3, 4], 3)[1] is None
    assert rsi([1, 2, 3, 4, 5], 3, 4) == 100.0
    flat = [Candle(base + i * 3600, 100, 101, 99, 100) for i in range(20)]
    free = buy_hold(flat, 5, 0, 0).end_equity
    cost = buy_hold(flat, 5, 0.0013, 0.0005).end_equity
    assert cost < free

    candles = [
        Candle(base, 100, 101, 99, 100),
        Candle(base + 3600, 100, 101, 99, 100),
        Candle(base + 7200, 100, 105, 90, 100),
        Candle(base + 10800, 100, 101, 99, 100),
    ]
    config = Config("RSI", (("period", 1), ("oversold", 100), ("overbought", 101)), 2, 2)
    result = simulate(candles, config, 5, 0, 0)
    both = [trade for trade in result.trades if "same_candle" in trade.reason]
    assert len(both) == 1 and both[0].reason == "stop_and_target_same_candle_stop_first"
    assert abs(both[0].exit_price - 98.0) < 1e-9
    assert all(math.isfinite(value) for value in result.equity_curve)
    assert len(result.equity_curve) == len(candles)

    # Verify that the optimized signal builder matches the convenience helper.
    for cfg in (config, Config("EMA", (("fast", 2), ("slow", 3)), 2, 4)):
        signals = prepare_signals(flat, cfg)
        assert all(signals[i] == signal_at(flat, i, cfg) for i in range(len(flat)))

    # Candle timestamps may have a consistent offset from Unix-epoch boundaries.
    shifted_hourly = [Candle(base + 1800 + i * 3600, 100, 101, 99, 100) for i in range(20)]
    assert all((c.t - shifted_hourly[0].t) % 3600 == 0 for c in shifted_hourly)
    assert any(c.t % 3600 != 0 for c in shifted_hourly)

    # A full test fold is required; an incomplete trailing fold must be ignored.
    hourly = [Candle(base + i * 3600, 100, 101, 99, 100) for i in range(60 * 24 + 5)]
    assert len(split_folds(hourly, "60")) == 0
    return {
        "status": "passed",
        "checks": [
            "EMA warm-up", "RSI monotonic rise", "fees/slippage reduce flat buy-and-hold",
            "finite equity curve", "equity curve length", "same-candle stop/target conservatism",
            "precomputed signals match direct signals", "consistent offset candle grid accepted",
            "incomplete OOS fold is ignored",
        ],
        "conservative_stop_rule": "stop wins if stop and target both touched in one candle",
    }


def main():
    parser = argparse.ArgumentParser(description="Nobitex historical backtest; research only")
    parser.add_argument("--days", type=int, default=DAYS)
    parser.add_argument("--resolution", default=RESOLUTION)
    parser.add_argument("--capital", type=float, default=INITIAL_CAPITAL)
    parser.add_argument("--fee", type=float, default=FEE_RATE)
    parser.add_argument("--slippage", type=float, default=SLIPPAGE_RATE)
    parser.add_argument("--symbols", nargs="+", default=list(MARKETS))
    parser.add_argument("--report", default=REPORT_FILE)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        print(json.dumps(self_test(), indent=2, ensure_ascii=False, allow_nan=False))
        return 0
    if args.days < 30 or not math.isfinite(args.capital) or args.capital <= 0:
        parser.error("days >= 30 and finite capital > 0 are required")
    if args.resolution not in {"1", "5", "15", "30", "60", "120", "240", "D"}:
        parser.error("Unsupported resolution")
    if any(not math.isfinite(value) or value < 0 or value >= 1 for value in (args.fee, args.slippage)):
        parser.error("fee and slippage must be finite values in [0, 1)")
    if not args.symbols or any(not isinstance(symbol, str) or not symbol.strip() for symbol in args.symbols):
        parser.error("At least one valid symbol is required")

    configs = build_configs()
    markets = {}
    print(f"Unified Backtest {VERSION} | research only | no real orders | {len(configs)} configurations")
    for number, symbol in enumerate(args.symbols):
        try:
            candles = fetch_candles(symbol, args.days, args.resolution)
            per_day = 1 if args.resolution == "D" else 1440 // int(args.resolution)
            expected = args.days * per_day
            coverage = min(1.0, len(candles) / expected) if expected else 0.0
            if coverage < MIN_COVERAGE:
                raise RuntimeError(f"Insufficient coverage: {coverage:.1%} < {MIN_COVERAGE:.0%}")
            market_report = run_market(symbol, candles, configs, args.capital, args.fee, args.slippage, args.resolution)
            market_report["coverage_pct"] = round(coverage * 100, 3)
            market_report["expected_candles"] = expected
            market_report["missing_candles_estimate"] = max(0, expected - len(candles))
            markets[symbol] = market_report
            print(symbol, json.dumps(market_report["walk_forward"], ensure_ascii=False))
        except Exception as exc:
            markets[symbol] = {"error": str(exc)}
            print(f"ERROR {symbol}: {exc}")
        if number + 1 < len(args.symbols):
            time.sleep(REQUEST_DELAY_SECONDS)

    report = {
        "version": VERSION,
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "research_only": True,
        "real_orders_enabled": False,
        "settings": {
            "days": args.days,
            "resolution": args.resolution,
            "initial_capital": args.capital,
            "fee_per_side": args.fee,
            "slippage_per_side": args.slippage,
            "symbols": args.symbols,
            "train_days": TRAIN_DAYS,
            "test_days": TEST_DAYS,
            "min_training_trades": MIN_TRAIN_TRADES,
            "drawdown_penalty": DRAWDOWN_PENALTY,
            "config_count": len(configs),
            "coverage_threshold": MIN_COVERAGE,
            "max_candles_per_request": MAX_CANDLES_PER_REQUEST,
            "selection_metric": "training net return minus 0.5 * max drawdown (%)",
            "limitations": [
                "OHLC intrabar order is unknown; stop is assumed first if stop and target both touch in one candle",
                "Fresh capital is used for each OOS fold; trailing training candles provide indicator warm-up only",
                "No partial fills, bid/ask spread model, funding, or exchange outage model",
                "Only complete 14-day OOS folds are included; any trailing partial fold is omitted",
                "Coverage is an overall candle-count estimate and does not itself prove there are no internal gaps",
            ],
        },
        "self_test": self_test(),
        "markets": markets,
    }
    with open(args.report, "w", encoding="utf-8") as report_file:
        json.dump(report, report_file, ensure_ascii=False, indent=2, allow_nan=False)
    print("Report saved:", args.report)
    # Fail the workflow if any requested market failed, rather than hiding a partial run.
    return 0 if markets and all("error" not in item for item in markets.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
