#!/usr/bin/env python3
"""Historical paper backtest for Nobitex USDT markets. No real orders are sent."""
import json
import math
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

API_URL = "https://apiv2.nobitex.ir"
SYMBOLS = {"BTCUSDT": ("btc", "usdt"), "ETHUSDT": ("eth", "usdt")}
RESOLUTION = "5"
DAYS_BACK = 30
INITIAL_CAPITAL = 5.0
FEE_RATE = 0.0013       # estimated fee on each side
SLIPPAGE_RATE = 0.0005  # conservative assumed slippage on each side
STOP_LOSS = 0.025
TAKE_PROFIT = 0.05
MIN_CANDLES = 100
REPORT_FILE = "backtest_report.json"


def fetch_candles(symbol, days=DAYS_BACK):
    """Fetch 5-minute candles from Nobitex UDF history endpoint."""
    now = int(time.time())
    start = now - days * 24 * 60 * 60
    params = urllib.parse.urlencode({
        "symbol": symbol,
        "resolution": RESOLUTION,
        "from": start,
        "to": now,
    })
    url = f"{API_URL}/market/udf/history?{params}"
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Nobitex-Paper-Backtester/1.0", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=25) as response:
        data = json.loads(response.read().decode("utf-8"))
    if data.get("s") != "ok":
        raise RuntimeError(f"Nobitex candle API returned status={data.get('s')}: {data}")
    times = data.get("t", [])
    closes = data.get("c", [])
    highs = data.get("h", [])
    lows = data.get("l", [])
    candles = []
    for i in range(min(len(times), len(closes), len(highs), len(lows))):
        try:
            candle = {
                "time": int(times[i]),
                "close": float(closes[i]),
                "high": float(highs[i]),
                "low": float(lows[i]),
            }
            if all(math.isfinite(candle[k]) and candle[k] > 0 for k in ("close", "high", "low")):
                candles.append(candle)
        except (TypeError, ValueError):
            continue
    candles.sort(key=lambda x: x["time"])
    # Drop duplicate timestamps if the API returns any.
    unique = {}
    for candle in candles:
        unique[candle["time"]] = candle
    return [unique[t] for t in sorted(unique)]


def ema(values, period):
    out = [None] * len(values)
    if len(values) < period:
        return out
    alpha = 2.0 / (period + 1.0)
    current = sum(values[:period]) / period
    out[period - 1] = current
    for i in range(period, len(values)):
        current = alpha * values[i] + (1.0 - alpha) * current
        out[i] = current
    return out


def rsi(values, period=14):
    out = [None] * len(values)
    if len(values) <= period:
        return out
    gains = []
    losses = []
    for i in range(1, period + 1):
        delta = values[i] - values[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    def calc(g, l):
        if l == 0:
            return 100.0 if g > 0 else 50.0
        return 100.0 - 100.0 / (1.0 + g / l)

    out[period] = calc(avg_gain, avg_loss)
    for i in range(period + 1, len(values)):
        delta = values[i] - values[i - 1]
        gain = max(delta, 0.0)
        loss = max(-delta, 0.0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out[i] = calc(avg_gain, avg_loss)
    return out


def indicators(candles):
    closes = [c["close"] for c in candles]
    ema9 = ema(closes, 9)
    ema21 = ema(closes, 21)
    rsi14 = rsi(closes, 14)
    lower = [None] * len(closes)
    upper = [None] * len(closes)
    period = 20
    for i in range(period - 1, len(closes)):
        window = closes[i - period + 1:i + 1]
        mean = sum(window) / period
        variance = sum((x - mean) ** 2 for x in window) / period
        sd = math.sqrt(variance)
        lower[i] = mean - 2 * sd
        upper[i] = mean + 2 * sd
    return closes, ema9, ema21, rsi14, lower, upper


def signal_for(strategy, i, candles, ind):
    closes, ema9, ema21, rsi14, lower, upper = ind
    price = closes[i]
    prev = closes[i - 1]
    if strategy == "BUY_HOLD":
        return "BUY" if i == 60 else "HOLD"
    if strategy == "EMA_TREND":
        if ema9[i] is not None and ema21[i] is not None and rsi14[i] is not None:
            if ema9[i] > ema21[i] and rsi14[i] >= 50:
                return "BUY"
            if ema9[i] < ema21[i] or rsi14[i] > 75:
                return "SELL"
    elif strategy == "RSI_REVERSAL":
        if rsi14[i] is not None:
            if rsi14[i] < 30:
                return "BUY"
            if rsi14[i] > 55:
                return "SELL"
    elif strategy == "BOLLINGER_REVERSION":
        if lower[i] is not None and upper[i] is not None:
            if price < lower[i]:
                return "BUY"
            if price > upper[i]:
                return "SELL"
    elif strategy == "MOMENTUM_BREAKOUT":
        lookback = 20
        if i >= lookback:
            previous_high = max(c["high"] for c in candles[i - lookback:i])
            previous_low = min(c["low"] for c in candles[i - lookback:i])
            if price > previous_high:
                return "BUY"
            if price < previous_low:
                return "SELL"
    return "HOLD"


def backtest(strategy, candles, initial_capital=INITIAL_CAPITAL):
    if len(candles) < MIN_CANDLES:
        raise ValueError(f"Only {len(candles)} candles available; need at least {MIN_CANDLES}.")
    ind = indicators(candles)
    cash = float(initial_capital)
    qty = 0.0
    entry_price = 0.0
    entry_value = 0.0
    trades = []
    equity_curve = []
    # Start at index 60 to warm up indicators. Signals use the current completed candle;
    # trades are filled at that candle's close with estimated fees and slippage.
    for i in range(60, len(candles)):
        price = candles[i]["close"]
        signal = signal_for(strategy, i, candles, ind)
        if qty > 0:
            change = price / entry_price - 1.0
            if change <= -STOP_LOSS:
                signal = "SELL"
            elif change >= TAKE_PROFIT:
                signal = "SELL"
        if signal == "BUY" and qty == 0 and cash >= 0.5:
            fill = price * (1.0 + SLIPPAGE_RATE)
            spend = cash
            fee = spend * FEE_RATE
            qty = max(0.0, (spend - fee) / fill)
            cash = 0.0
            entry_price = fill
            entry_value = spend
        elif signal == "SELL" and qty > 0:
            fill = price * (1.0 - SLIPPAGE_RATE)
            gross = qty * fill
            fee = gross * FEE_RATE
            proceeds = gross - fee
            pnl = proceeds - entry_value
            trades.append({
                "entry_price": round(entry_price, 8),
                "exit_price": round(fill, 8),
                "pnl_usdt": round(pnl, 8),
                "return_pct": round((proceeds / entry_value - 1.0) * 100.0, 4) if entry_value else 0.0,
            })
            cash = proceeds
            qty = 0.0
            entry_price = 0.0
            entry_value = 0.0
        equity_curve.append(cash + qty * price)
    last_price = candles[-1]["close"]
    final_equity = cash + qty * last_price
    # Mark-to-market open position, including an estimated sell fee/slippage for fair comparison.
    if qty > 0:
        liquidation_value = qty * last_price * (1.0 - SLIPPAGE_RATE) * (1.0 - FEE_RATE)
        final_equity = liquidation_value
    peak = initial_capital
    max_drawdown = 0.0
    for value in equity_curve:
        peak = max(peak, value)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - value) / peak)
    wins = sum(1 for t in trades if t["pnl_usdt"] > 0)
    losses = sum(1 for t in trades if t["pnl_usdt"] < 0)
    gross_profit = sum(t["pnl_usdt"] for t in trades if t["pnl_usdt"] > 0)
    gross_loss = abs(sum(t["pnl_usdt"] for t in trades if t["pnl_usdt"] < 0))
    return {
        "initial_capital_usdt": round(initial_capital, 4),
        "final_equity_usdt": round(final_equity, 4),
        "profit_loss_usdt": round(final_equity - initial_capital, 4),
        "return_pct": round((final_equity / initial_capital - 1.0) * 100.0, 3),
        "closed_trades": len(trades),
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round(wins / len(trades) * 100.0, 2) if trades else 0.0,
        "profit_factor": round(gross_profit / gross_loss, 3) if gross_loss > 0 else (None if gross_profit == 0 else "infinite"),
        "max_drawdown_pct": round(max_drawdown * 100.0, 3),
        "open_position_at_end": qty > 0,
        "last_price": last_price,
        "trades": trades,
    }


def main():
    report = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "HISTORICAL_BACKTEST_ONLY_NO_REAL_ORDERS",
        "settings": {
            "days_requested": DAYS_BACK,
            "candle_resolution_minutes": 5,
            "initial_capital_per_test_usdt": INITIAL_CAPITAL,
            "fee_per_side_pct": FEE_RATE * 100,
            "assumed_slippage_per_side_pct": SLIPPAGE_RATE * 100,
            "stop_loss_pct": STOP_LOSS * 100,
            "take_profit_pct": TAKE_PROFIT * 100,
            "note": "Past results do not predict future results. Fees, slippage and candle data may differ from live execution.",
        },
        "markets": {},
    }
    for symbol in SYMBOLS:
        print(f"Fetching {symbol} candles...")
        try:
            candles = fetch_candles(symbol)
            if len(candles) < MIN_CANDLES:
                raise RuntimeError(f"Not enough candles received: {len(candles)}")
            first = datetime.fromtimestamp(candles[0]["time"], timezone.utc).isoformat()
            last = datetime.fromtimestamp(candles[-1]["time"], timezone.utc).isoformat()
            market_result = {"candles": len(candles), "first_candle_utc": first, "last_candle_utc": last, "strategies": {}}
            for strategy in ("BUY_HOLD", "EMA_TREND", "RSI_REVERSAL", "BOLLINGER_REVERSION", "MOMENTUM_BREAKOUT"):
                result = backtest(strategy, candles)
                # Exclude detailed individual trades from the console summary; retain them in JSON.
                market_result["strategies"][strategy] = result
                print(f"{symbol} | {strategy}: {result['return_pct']:+.3f}% | final ${result['final_equity_usdt']:.4f} | trades {result['closed_trades']} | max DD {result['max_drawdown_pct']:.2f}%")
            report["markets"][symbol] = market_result
        except Exception as exc:
            report["markets"][symbol] = {"error": str(exc)}
            print(f"ERROR {symbol}: {exc}")
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\nReport saved to {REPORT_FILE}")
    if all("error" in report["markets"].get(s, {}) for s in SYMBOLS):
        raise SystemExit("Backtest failed for all markets; inspect errors above.")


if __name__ == "__main__":
    main()
