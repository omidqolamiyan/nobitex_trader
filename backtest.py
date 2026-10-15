#!/usr/bin/env python3
"""Nobitex paper-trading research backtest. Never submits real orders."""
import json
import math
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

API_URL = "https://apiv2.nobitex.ir"
MARKETS = ("BTCUSDT", "ETHUSDT")
STRATEGIES = ("BUY_HOLD", "EMA_TREND", "RSI_REVERSAL", "BOLLINGER_REVERSION", "MOMENTUM_BREAKOUT")
RESOLUTION_MINUTES = 5
DAYS = 60
INITIAL_CAPITAL = 5.0  # each strategy gets an independent 5 USDT simulation per symbol
FEE_RATE = 0.0013       # each side
SLIPPAGE_RATE = 0.0005  # adverse execution assumption per side
STOP_LOSS = 0.025
TAKE_PROFIT = 0.05
REPORT_FILE = "backtest_report.json"


def api_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Nobitex-Paper-Backtest/2.0"})
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_candles(symbol):
    now = int(time.time())
    step = RESOLUTION_MINUTES * 60
    start = now - DAYS * 86400
    cursor = start
    candles = {}
    print(f"Fetching {symbol} candles for up to {DAYS} days...")
    while cursor < now:
        end = min(now, cursor + step * 180)
        params = urllib.parse.urlencode({"symbol": symbol, "resolution": str(RESOLUTION_MINUTES), "from": str(cursor), "to": str(end)})
        data = api_get(f"{API_URL}/market/udf/history?{params}")
        status = data.get("s")
        if status == "no_data":
            cursor = end
            continue
        if status != "ok":
            raise RuntimeError(f"Nobitex history API error for {symbol}: {status}; {data}")
        arrays = [data.get(k, []) for k in ("t", "o", "h", "l", "c", "v")]
        if not arrays[0] or any(len(a) != len(arrays[0]) for a in arrays[:5]):
            raise RuntimeError(f"Incomplete OHLC data returned for {symbol}")
        for ts, op, hi, lo, cl in zip(*arrays[:5]):
            if ts is None or any(x is None for x in (op, hi, lo, cl)):
                continue
            ts = int(ts)
            if ts + step <= now and min(float(op), float(hi), float(lo), float(cl)) > 0:
                candles[ts] = {"t": ts, "o": float(op), "h": float(hi), "l": float(lo), "c": float(cl)}
        cursor = end
        time.sleep(0.08)
    rows = [candles[k] for k in sorted(candles)]
    if len(rows) < 500:
        raise RuntimeError(f"Not enough candles for {symbol}: {len(rows)}. No result should be trusted.")
    first = datetime.fromtimestamp(rows[0]["t"], timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    last = datetime.fromtimestamp(rows[-1]["t"], timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"{symbol}: {len(rows)} candles, {first} -> {last}")
    return rows


def ema(values, period):
    if len(values) < period:
        return None
    result = sum(values[:period]) / period
    alpha = 2 / (period + 1)
    for x in values[period:]:
        result = alpha * x + (1 - alpha) * result
    return result


def rsi(values, period=14):
    if len(values) <= period:
        return None
    changes = [values[i] - values[i - 1] for i in range(1, len(values))]
    gains = [max(x, 0.0) for x in changes]
    losses = [max(-x, 0.0) for x in changes]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(changes)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0 if avg_gain else 50.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def signal_for(strategy, hist):
    closes = [x["c"] for x in hist]
    if len(closes) < 30:
        return "WAIT"
    price = closes[-1]
    prev = closes[-2]
    fast = ema(closes, 9)
    slow = ema(closes, 21)
    current_rsi = rsi(closes)
    if strategy == "BUY_HOLD":
        return "BUY" if len(closes) == 30 else "WAIT"
    if strategy == "EMA_TREND":
        if fast and slow and fast > slow and price > fast and current_rsi is not None and 52 <= current_rsi <= 68:
            return "BUY"
        if fast and slow and fast < slow and current_rsi is not None and current_rsi < 48:
            return "SELL"
    elif strategy == "RSI_REVERSAL":
        if current_rsi is not None and current_rsi <= 30 and price > prev:
            return "BUY"
        if current_rsi is not None and current_rsi >= 68 and price < prev:
            return "SELL"
    elif strategy == "BOLLINGER_REVERSION":
        window = closes[-20:]
        mean = sum(window) / len(window)
        std = math.sqrt(sum((x - mean) ** 2 for x in window) / len(window))
        lower, upper = mean - 2 * std, mean + 2 * std
        if price < lower and price > prev:
            return "BUY"
        if price > upper and price < prev:
            return "SELL"
    elif strategy == "MOMENTUM_BREAKOUT":
        previous_window = closes[-21:-1]
        if previous_window and price > max(previous_window) and current_rsi is not None and current_rsi < 75:
            return "BUY"
        if previous_window and price < min(previous_window):
            return "SELL"
    return "WAIT"


def net_buy_price(price):
    return price * (1 + SLIPPAGE_RATE)


def net_sell_price(price):
    return price * (1 - SLIPPAGE_RATE)


def simulate(rows, strategy, initial=INITIAL_CAPITAL):
    cash = initial
    qty = 0.0
    entry_price = 0.0
    entry_equity = 0.0
    trades = []
    equity_curve = []
    warmup = 30
    for i in range(warmup, len(rows)):
        row = rows[i]
        # Signals use only candles ending before this candle; execution occurs at this candle's open.
        hist = rows[:i]
        price = row["o"]
        exited_this_bar = False
        if qty > 0:
            stop = entry_price * (1 - STOP_LOSS)
            target = entry_price * (1 + TAKE_PROFIT)
            exit_price = None
            reason = None
            # Conservative assumption if both thresholds are hit in one candle: stop-loss first.
            if row["l"] <= stop:
                exit_price, reason = stop, "STOP_LOSS"
            elif row["h"] >= target:
                exit_price, reason = target, "TAKE_PROFIT"
            else:
                sig = signal_for(strategy, hist)
                if sig == "SELL":
                    exit_price, reason = price, "SIGNAL"
            if exit_price is not None:
                executed = net_sell_price(exit_price)
                proceeds = qty * executed * (1 - FEE_RATE)
                pnl = proceeds - entry_equity
                cash += proceeds
                trades.append({"entry_time": entry_time, "exit_time": row["t"], "entry_price": round(entry_price, 8), "exit_price": round(executed, 8), "pnl_usdt": round(pnl, 8), "reason": reason})
                qty = 0.0
                entry_price = entry_equity = 0.0
                exited_this_bar = True
        if qty == 0 and not exited_this_bar:
            sig = signal_for(strategy, hist)
            if sig == "BUY" and cash > 0.05:
                executed = net_buy_price(price)
                qty = cash / (executed * (1 + FEE_RATE))
                entry_equity = cash
                entry_price = executed
                entry_time = row["t"]
                cash = 0.0
        equity = cash + qty * net_sell_price(row["c"]) * (1 - FEE_RATE)
        equity_curve.append(equity)
    final = cash + qty * net_sell_price(rows[-1]["c"]) * (1 - FEE_RATE)
    peak = initial
    max_dd = 0.0
    for value in equity_curve + [final]:
        peak = max(peak, value)
        if peak > 0:
            max_dd = max(max_dd, (peak - value) / peak)
    wins = sum(1 for t in trades if t["pnl_usdt"] > 0)
    losses = len(trades) - wins
    gross_profit = sum(t["pnl_usdt"] for t in trades if t["pnl_usdt"] > 0)
    gross_loss = abs(sum(t["pnl_usdt"] for t in trades if t["pnl_usdt"] < 0))
    return {"initial_usdt": round(initial, 6), "final_usdt": round(final, 6), "return_pct": round((final / initial - 1) * 100, 4), "closed_trades": len(trades), "wins": wins, "losses": losses, "win_rate_pct": round(wins / len(trades) * 100, 2) if trades else 0.0, "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss else (None if gross_profit == 0 else "inf"), "max_drawdown_pct": round(max_dd * 100, 4), "open_position_at_end": qty > 0, "trades_detail": trades}


def main():
    print("NOBITEX HISTORICAL BACKTEST — SIMULATION ONLY")
    print(f"Lookback: {DAYS} days | timeframe: {RESOLUTION_MINUTES}m | fee/side: {FEE_RATE*100:.3f}% | slippage/side: {SLIPPAGE_RATE*100:.3f}%")
    print(f"Stop loss: {STOP_LOSS*100:.2f}% | take profit: {TAKE_PROFIT*100:.2f}% | real orders: DISABLED\n")
    report = {"version": 2, "generated_utc": datetime.now(timezone.utc).isoformat(), "settings": {"days_requested": DAYS, "resolution_minutes": RESOLUTION_MINUTES, "initial_capital_per_strategy_per_symbol": INITIAL_CAPITAL, "fee_rate_each_side": FEE_RATE, "slippage_each_side": SLIPPAGE_RATE, "stop_loss_pct": STOP_LOSS * 100, "take_profit_pct": TAKE_PROFIT * 100, "live_trading": False}, "markets": {}}
    for symbol in MARKETS:
        rows = fetch_candles(symbol)
        split = int(len(rows) * 0.70)
        train = rows[:split]
        test = rows[split:]
        # Add warm-up context to the test period; report performance starts at the split boundary.
        test_context = rows[max(0, split - 30):]
        report["markets"][symbol] = {"candle_count": len(rows), "start_utc": datetime.fromtimestamp(rows[0]["t"], timezone.utc).isoformat(), "end_utc": datetime.fromtimestamp(rows[-1]["t"], timezone.utc).isoformat(), "strategies": {}}
        print(f"\n{symbol}: {len(rows)} candles; out-of-sample segment begins near {datetime.fromtimestamp(test[0]['t'], timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
        for strategy in STRATEGIES:
            full = simulate(rows, strategy)
            # This is a separate final-segment simulation with independent starting capital.
            out = simulate(test_context, strategy)
            report["markets"][symbol]["strategies"][strategy] = {"full_period": full, "out_of_sample_segment": out}
            print(f"{strategy:22s} full {full['return_pct']:+7.3f}% | OOS {out['return_pct']:+7.3f}% | trades {full['closed_trades']:3d} | max DD {full['max_drawdown_pct']:.2f}%")
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\nReport saved to {REPORT_FILE}")
    print("Research only. Historical performance does not predict future returns; no real orders were sent.")

if __name__ == "__main__":
    main()
