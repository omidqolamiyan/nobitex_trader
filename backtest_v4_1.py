#!/usr/bin/env python3
"""Nobitex Backtest V4.1 — research only, no real orders.

Correctness-focused successor to V4. Uses only the Python standard library.
Run in GitHub Actions or locally: python backtest_v4_1.py
Outputs: backtest_v4_1_report.json
"""
import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# ------------------------------ Configuration ------------------------------
API_URL = "https://apiv2.nobitex.ir/market/udf/history"
LOOKBACK_DAYS = 180
RESOLUTION_MINUTES = 60
INITIAL_CAPITAL = 5.0  # USDT per independent test
FEE_RATE = 0.0013       # per side (0.13%)
SLIPPAGE_RATE = 0.0005  # per side (0.05%)
MIN_BUY_CASH = 0.50
MARKETS = ("BTCUSDT", "ETHUSDT")

# Preserve V4's broad RSI/EMA search space, but avoid selecting on OOS data.
RSI_PERIODS = (7, 14, 21)
OVERSOLD_LEVELS = (20, 25, 30)
OVERBOUGHT_LEVELS = (65, 70, 75)
EMA_FAST_PERIODS = (5, 9, 12)
EMA_SLOW_PERIODS = (21, 30, 50)
STOP_LOSSES = (0.015, 0.025, 0.04)
TAKE_PROFITS = (0.03, 0.05, 0.07)
DRAWDOWN_PENALTY = 0.50
MIN_TRAIN_TRADES = 3
TRAIN_DAYS = 60
TEST_DAYS = 14
MIN_HISTORY_COVERAGE = 0.95
MAX_PAGES = 50
REQUEST_DELAY_SECONDS = 2.1
MAX_FETCH_ATTEMPTS = 4
RETRY_BASE_SECONDS = 2.0
REPORT_FILE = "backtest_v4_1_report.json"


def utc_text(ts):
    return datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _valid_candle(row):
    vals = (row["o"], row["h"], row["l"], row["c"])
    return (all(math.isfinite(x) and x > 0 for x in vals)
            and row["h"] >= max(row["o"], row["c"], row["l"])
            and row["l"] <= min(row["o"], row["c"], row["h"]))


def fetch_chunk(symbol, start_ts, end_ts, page):
    params = {"symbol": symbol, "resolution": str(RESOLUTION_MINUTES),
              "from": int(start_ts), "to": int(end_ts), "page": int(page)}
    req = urllib.request.Request(
        API_URL + "?" + urllib.parse.urlencode(params),
        headers={"User-Agent": "Nobitex-Backtest-V4.1.2/1.0", "Accept": "application/json"})
    # Temporary Nobitex/CDN failures (for example HTTP 504) should not
    # abort a multi-minute history download on the first attempt.
    last_error = None
    for attempt in range(MAX_FETCH_ATTEMPTS):
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                data = json.loads(response.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as exc:
            last_error = exc
            retryable = exc.code in (408, 429, 500, 502, 503, 504)
            if not retryable or attempt == MAX_FETCH_ATTEMPTS - 1:
                raise
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt == MAX_FETCH_ATTEMPTS - 1:
                raise
        time.sleep(RETRY_BASE_SECONDS * (2 ** attempt))
    else:
        raise RuntimeError(f"{symbol}: history request failed after retries: {last_error}")
    if data.get("s") == "no_data":
        return []
    if data.get("s") != "ok":
        raise RuntimeError(f"{symbol}: Nobitex API response was not ok: {data}")
    arrays = [data.get(k, []) for k in ("t", "o", "h", "l", "c")]
    if len({len(a) for a in arrays}) != 1:
        raise RuntimeError(f"{symbol}: inconsistent candle array lengths")
    rows=[]
    for t,o,h,l,c in zip(*arrays):
        try:
            row={"t":int(t),"o":float(o),"h":float(h),"l":float(l),"c":float(c)}
        except (TypeError, ValueError, OverflowError):
            continue
        if _valid_candle(row): rows.append(row)
    return rows


def fetch_history(symbol, now=None):
    """Fetch history in time chunks to avoid unsupported/ignored page pagination."""
    now = int(time.time()) if now is None else int(now)
    start = now - LOOKBACK_DAYS * 86400
    step = RESOLUTION_MINUTES * 60
    # Keep each request below common UDF response limits; 7 days = 168 hourly bars.
    chunk_seconds = 7 * 86400
    by_ts = {}
    cursor = start
    chunk_no = 0
    print(f"\nFetching {symbol}: {LOOKBACK_DAYS} days, {RESOLUTION_MINUTES}m candles")
    while cursor < now:
        chunk_no += 1
        chunk_end = min(cursor + chunk_seconds, now)
        try:
            rows = fetch_chunk(symbol, cursor, chunk_end, 1)
        except Exception as exc:
            raise RuntimeError(f"{symbol}: failed to fetch time chunk {chunk_no}: {exc}") from exc
        before = len(by_ts)
        for row in rows:
            if start <= row["t"] <= now:
                by_ts[row["t"]] = row
        added = len(by_ts) - before
        print(f"{symbol}: chunk {chunk_no}, received {len(rows)}, new {added}, unique {len(by_ts)}")
        # Re-fetch one candle at each boundary. This protects against inclusive vs.
        # exclusive API endpoints; duplicate timestamps are deduplicated in by_ts.
        cursor = chunk_end - step
        if cursor < start:
            cursor = start
        if cursor < now:
            time.sleep(REQUEST_DELAY_SECONDS)
    candles = [by_ts[t] for t in sorted(by_ts)]
    expected = LOOKBACK_DAYS * 24 * 60 // RESOLUTION_MINUTES
    count_coverage = len(candles) / expected if expected else 0.0
    span_coverage = ((candles[-1]["t"] - candles[0]["t"]) / (LOOKBACK_DAYS * 86400)) if len(candles) > 1 else 0.0
    if len(candles) < 2:
        raise RuntimeError(f"{symbol}: API returned fewer than two valid candles")
    ts_set = set(by_ts)
    expected_in_span = max(1, (candles[-1]["t"] - candles[0]["t"]) // step + 1)
    observed_in_span = sum(1 for t in range(candles[0]["t"], candles[-1]["t"] + 1, step) if t in ts_set)
    gap_coverage = observed_in_span / expected_in_span
    print(f"{symbol}: {len(candles)}/{expected} candles ({count_coverage:.1%}); span {span_coverage:.1%}; in-span continuity {gap_coverage:.1%}")
    print(f"{utc_text(candles[0]['t'])} -> {utc_text(candles[-1]['t'])}")
    if count_coverage < MIN_HISTORY_COVERAGE or span_coverage < MIN_HISTORY_COVERAGE or gap_coverage < MIN_HISTORY_COVERAGE:
        raise RuntimeError(f"{symbol}: insufficient history (count={count_coverage:.1%}, span={span_coverage:.1%}, continuity={gap_coverage:.1%}); refusing unreliable backtest")
    # Reject irregular timestamps rather than silently treating missing hours as consecutive bars.
    gaps = [b["t"] - a["t"] for a, b in zip(candles, candles[1:]) if b["t"] - a["t"] != step]
    if gaps:
        raise RuntimeError(f"{symbol}: found {len(gaps)} missing/irregular candle intervals; refusing unreliable backtest")
    return candles


def calculate_rsi(closes, period):
    out=[None]*len(closes)
    if period < 1 or len(closes) <= period: return out
    gains=[]; losses=[]
    for i in range(1, period+1):
        d=closes[i]-closes[i-1]; gains.append(max(d,0.0)); losses.append(max(-d,0.0))
    ag=sum(gains)/period; al=sum(losses)/period
    def val(g,l):
        if l == 0: return 100.0 if g > 0 else 50.0
        if g == 0: return 0.0
        return 100.0 - 100.0/(1.0+g/l)
    out[period]=val(ag,al)
    for i in range(period+1,len(closes)):
        d=closes[i]-closes[i-1]
        ag=(ag*(period-1)+max(d,0.0))/period
        al=(al*(period-1)+max(-d,0.0))/period
        out[i]=val(ag,al)
    return out


def calculate_ema(values, period):
    out=[None]*len(values)
    if period < 1 or len(values)<period: return out
    current=sum(values[:period])/period; out[period-1]=current
    alpha=2.0/(period+1)
    for i in range(period,len(values)):
        current=values[i]*alpha+current*(1-alpha); out[i]=current
    return out


def build_configurations():
    configs=[]
    for p in RSI_PERIODS:
        for os_level in OVERSOLD_LEVELS:
            for ob_level in OVERBOUGHT_LEVELS:
                if os_level >= ob_level: continue
                for sl in STOP_LOSSES:
                    for tp in TAKE_PROFITS:
                        configs.append({"strategy":"RSI","rsi_period":p,"oversold":os_level,"overbought":ob_level,"stop_loss":sl,"take_profit":tp})
    for fast in EMA_FAST_PERIODS:
        for slow in EMA_SLOW_PERIODS:
            if fast >= slow: continue
            for sl in STOP_LOSSES:
                for tp in TAKE_PROFITS:
                    configs.append({"strategy":"EMA","ema_fast":fast,"ema_slow":slow,"stop_loss":sl,"take_profit":tp})
    return configs


def signals_for_config(candles, config, indicators):
    """Signals use only completed prior candles; fills occur at the current candle open."""
    n = len(candles)
    buys = [False] * n
    sells = [False] * n
    if config["strategy"] == "RSI":
        vals = indicators[("RSI", config["rsi_period"])]
        for i in range(1, n):
            previous, current = vals[i - 2] if i >= 2 else None, vals[i - 1]
            if previous is None or current is None:
                continue
            # Mean-reversion entry: RSI recovers back above oversold.
            buys[i] = previous < config["oversold"] and current >= config["oversold"]
            # Exit: RSI falls back below overbought.
            sells[i] = previous > config["overbought"] and current <= config["overbought"]
    else:
        fast = indicators[("EMA", config["ema_fast"])]
        slow = indicators[("EMA", config["ema_slow"])]
        for i in range(1, n):
            j = i - 1
            if j < 1 or fast[j] is None or slow[j] is None or fast[j-1] is None or slow[j-1] is None:
                continue
            buys[i] = fast[j] > slow[j] and fast[j-1] <= slow[j-1]
            sells[i] = fast[j] < slow[j] and fast[j-1] >= slow[j-1]
    return buys, sells


def execute_buy(cash, reference_price):
    if cash <= 0 or reference_price <= 0: return cash,0.0,None,0.0
    fill=reference_price*(1+SLIPPAGE_RATE)
    # All available cash includes buy fee; prevents negative cash from fee rounding.
    notional=cash/(1+FEE_RATE)
    qty=notional/fill
    total_cost=notional*(1+FEE_RATE)
    return max(0.0,cash-total_cost),qty,fill,total_cost


def execute_sell(qty, reference_price):
    fill=reference_price*(1-SLIPPAGE_RATE)
    gross=qty*fill
    fee=gross*FEE_RATE
    return max(0.0,gross-fee),fill


def simulate(candles, buys, sells, start, end, stop_loss, take_profit, liquidate=True):
    """Simulate one isolated segment, including entry-bar stops and conservative drawdown."""
    start = max(1, int(start))
    end = min(int(end), len(candles))
    cash = INITIAL_CAPITAL
    qty = 0.0
    entry_price = None
    entry_cost = 0.0
    entry_time = None
    entries = closed = wins = losses = 0
    curve = []
    trades = []
    if start >= end:
        return {"initial_capital": INITIAL_CAPITAL, "final_equity": INITIAL_CAPITAL, "return_pct": 0.0,
                "entries": 0, "closed_trades": 0, "wins": 0, "losses": 0, "win_rate_pct": 0.0,
                "max_drawdown_pct": 0.0, "trade_log": []}

    peak = INITIAL_CAPITAL
    max_dd = 0.0
    for i in range(start, end):
        c = candles[i]
        open_px = c["o"]
        # Mark low-water equity from any position carried into this candle before processing exits.
        low_candidates = [cash + qty * c["l"] if qty > 0 else cash]
        exited_this_bar = False

        if qty > 0:
            stop = entry_price * (1 - stop_loss)
            target = entry_price * (1 + take_profit)
            exit_px = None
            reason = None
            if open_px <= stop:
                exit_px, reason = open_px, "STOP_GAP"
            elif open_px >= target:
                exit_px, reason = open_px, "TARGET_GAP"
            elif c["l"] <= stop and c["h"] >= target:
                # Unknown intrabar path: assume adverse stop was hit first.
                exit_px, reason = stop, "STOP_AND_TARGET_SAME_BAR_STOP_FIRST"
            elif c["l"] <= stop:
                exit_px, reason = stop, "STOP_LOSS"
            elif c["h"] >= target:
                exit_px, reason = target, "TAKE_PROFIT"
            elif sells[i]:
                exit_px, reason = open_px, "SIGNAL"
            if exit_px is not None:
                proceeds, fill = execute_sell(qty, exit_px)
                pnl = proceeds - entry_cost
                closed += 1
                wins += int(pnl > 0)
                losses += int(pnl <= 0)
                trades.append({"entry_time": entry_time, "exit_time": c["t"], "entry_price": round(entry_price, 8),
                               "exit_price": round(fill, 8), "reason": reason, "entry_cost": round(entry_cost, 8),
                               "proceeds": round(proceeds, 8), "pnl": round(pnl, 8)})
                cash, qty, entry_price, entry_cost, entry_time = proceeds, 0.0, None, 0.0, None
                exited_this_bar = True

        if qty == 0 and not exited_this_bar and cash >= MIN_BUY_CASH and buys[i]:
            cash, qty, entry_price, entry_cost = execute_buy(cash, open_px)
            if qty > 0:
                entries += 1
                entry_time = c["t"]
                # Evaluate stop/target on the entry candle too. Entry is at the open;
                # if both thresholds are touched, conservatively assume stop first.
                stop = entry_price * (1 - stop_loss)
                target = entry_price * (1 + take_profit)
                exit_px = None
                reason = None
                if c["l"] <= stop and c["h"] >= target:
                    exit_px, reason = stop, "ENTRY_BAR_STOP_AND_TARGET_STOP_FIRST"
                elif c["l"] <= stop:
                    exit_px, reason = stop, "ENTRY_BAR_STOP_LOSS"
                elif c["h"] >= target:
                    exit_px, reason = target, "ENTRY_BAR_TAKE_PROFIT"
                if exit_px is not None:
                    proceeds, fill = execute_sell(qty, exit_px)
                    pnl = proceeds - entry_cost
                    closed += 1
                    wins += int(pnl > 0)
                    losses += int(pnl <= 0)
                    trades.append({"entry_time": entry_time, "exit_time": c["t"], "entry_price": round(entry_price, 8),
                                   "exit_price": round(fill, 8), "reason": reason, "entry_cost": round(entry_cost, 8),
                                   "proceeds": round(proceeds, 8), "pnl": round(pnl, 8)})
                    cash, qty, entry_price, entry_cost, entry_time = proceeds, 0.0, None, 0.0, None

        # Low-water equity includes intrabar low while any position was open, including entry bars.
        if qty > 0:
            low_candidates.append(cash + qty * c["l"])
        close_equity = cash + qty * c["c"]
        low_equity = min(low_candidates + [close_equity])
        peak = max(peak, close_equity)
        if peak > 0:
            max_dd = max(max_dd, (peak - low_equity) / peak)
        curve.append({"t": c["t"], "close": close_equity, "low": low_equity})

    if qty > 0 and liquidate:
        last = candles[end - 1]
        proceeds, fill = execute_sell(qty, last["c"])
        pnl = proceeds - entry_cost
        closed += 1
        wins += int(pnl > 0)
        losses += int(pnl <= 0)
        trades.append({"entry_time": entry_time, "exit_time": last["t"], "entry_price": round(entry_price, 8),
                       "exit_price": round(fill, 8), "reason": "END_OF_SEGMENT", "entry_cost": round(entry_cost, 8),
                       "proceeds": round(proceeds, 8), "pnl": round(pnl, 8)})
        cash, qty = proceeds, 0.0
        if curve:
            curve[-1]["close"] = cash
            curve[-1]["low"] = min(curve[-1]["low"], cash)
            peak = max(peak, cash)
            if peak > 0:
                max_dd = max(max_dd, (peak - curve[-1]["low"]) / peak)

    final_equity = curve[-1]["close"] if curve else INITIAL_CAPITAL
    return {"initial_capital": INITIAL_CAPITAL, "final_equity": round(final_equity, 8),
            "return_pct": round((final_equity / INITIAL_CAPITAL - 1) * 100, 4), "entries": entries,
            "closed_trades": closed, "wins": wins, "losses": losses,
            "win_rate_pct": round(wins / closed * 100, 2) if closed else 0.0,
            "max_drawdown_pct": round(max_dd * 100, 4), "trade_log": trades}


def buy_and_hold(candles, start, end):
    start = max(0, int(start))
    end = min(int(end), len(candles))
    if start >= end:
        return {"final_equity": INITIAL_CAPITAL, "return_pct": 0.0, "max_drawdown_pct": 0.0}
    cash, qty, entry, entry_cost = execute_buy(INITIAL_CAPITAL, candles[start]["o"])
    peak = INITIAL_CAPITAL
    max_dd = 0.0
    for i in range(start, end):
        c = candles[i]
        close_equity = cash + qty * c["c"]
        # Candle low is a conservative mark; on the entry candle it may precede the open,
        # so report it as conservative rather than claiming exact intrabar chronology.
        low_equity = cash + qty * c["l"]
        peak = max(peak, close_equity)
        if peak > 0:
            max_dd = max(max_dd, (peak - low_equity) / peak)
    proceeds, _ = execute_sell(qty, candles[end - 1]["c"])
    final_equity = proceeds
    return {"final_equity": round(final_equity, 8),
            "return_pct": round((final_equity / INITIAL_CAPITAL - 1) * 100, 4),
            "max_drawdown_pct": round(max_dd * 100, 4)}


def score_result(result):
    # Avoid ranking zero-trade/under-sampled results as if they were evidence.
    if result["closed_trades"] < MIN_TRAIN_TRADES: return -1e9
    return result["return_pct"] - DRAWDOWN_PENALTY*result["max_drawdown_pct"]


def config_for_report(c):
    out={"strategy":c["strategy"],"stop_loss_pct":c["stop_loss"]*100,"take_profit_pct":c["take_profit"]*100}
    for k in ("rsi_period","oversold","overbought","ema_fast","ema_slow"):
        if k in c: out[k]=c[k]
    return out


def make_indicators(candles):
    closes=[x["c"] for x in candles]; out={}
    for p in RSI_PERIODS: out[("RSI",p)]=calculate_rsi(closes,p)
    for p in set(EMA_FAST_PERIODS+EMA_SLOW_PERIODS): out[("EMA",p)]=calculate_ema(closes,p)
    return out


def evaluate_config(candles,config,indicators,start,end):
    buys,sells=signals_for_config(candles,config,indicators)
    return simulate(candles,buys,sells,start,end,config["stop_loss"],config["take_profit"])


def test_market(symbol,candles):
    configs=build_configurations(); indicators=make_indicators(candles)
    bars_per_day=24*60//RESOLUTION_MINUTES
    train_bars=TRAIN_DAYS*bars_per_day; test_bars=TEST_DAYS*bars_per_day
    # Reserve an initial warm-up window, then repeated expanding-window walk-forward tests.
    first_test=train_bars
    folds=[]; selected_oos=[]
    if len(candles) <= first_test+test_bars:
        raise RuntimeError(f"{symbol}: not enough candles for at least one {TRAIN_DAYS}-day train + {TEST_DAYS}-day test fold")
    fold_no=0
    while first_test + fold_no*test_bars < len(candles):
        test_start=first_test+fold_no*test_bars
        test_end=min(test_start+test_bars,len(candles))
        train_start=max(1,0)  # train on history available before this fold
        ranked=[]
        for cfg in configs:
            train=evaluate_config(candles,cfg,indicators,train_start,test_start)
            ranked.append((score_result(train),cfg,train))
        ranked.sort(key=lambda x:x[0],reverse=True)
        eligible=[x for x in ranked if x[0] > -1e8]
        if not eligible:
            # No strategy has enough trades; do not manufacture a "winner".
            fold={"fold":fold_no+1,"train_start_utc":utc_text(candles[train_start]["t"]),"train_end_utc":utc_text(candles[test_start-1]["t"]),"test_start_utc":utc_text(candles[test_start]["t"]),"test_end_utc":utc_text(candles[test_end-1]["t"]),"selected_config":None,"selection_note":"No configuration met minimum closed-trade threshold; OOS strategy skipped.","out_of_sample":None,"buy_and_hold_oos":buy_and_hold(candles,test_start,test_end)}
        else:
            _,chosen,train_result=eligible[0]
            oos=evaluate_config(candles,chosen,indicators,test_start,test_end)
            fold={"fold":fold_no+1,"train_start_utc":utc_text(candles[train_start]["t"]),"train_end_utc":utc_text(candles[test_start-1]["t"]),"test_start_utc":utc_text(candles[test_start]["t"]),"test_end_utc":utc_text(candles[test_end-1]["t"]),"selected_config":config_for_report(chosen),"training_metrics":{k:v for k,v in train_result.items() if k!="trade_log"},"out_of_sample":{k:v for k,v in oos.items() if k!="trade_log"},"out_of_sample_trade_log":oos["trade_log"],"buy_and_hold_oos":buy_and_hold(candles,test_start,test_end)}
            selected_oos.append({"start":test_start,"end":test_end,"result":oos})
        folds.append(fold); fold_no+=1
    # Concatenate fold OOS returns by compounding segment returns, not summing percentages.
    compounded=INITIAL_CAPITAL
    oos_trades=0; oos_wins=0; fold_returns=[]
    selected_by_start={item["start"]:item["result"] for item in selected_oos}
    for f in folds:
        # Every OOS fold contributes the same calendar interval. If no model qualified,
        # strategy capital remains in cash for that fold (0% return), not omitted.
        ts = next((i for i in range(1, len(candles)) if utc_text(candles[i]["t"]) == f["test_start_utc"]), None)
        result = selected_by_start.get(ts) if ts is not None else None
        if result is not None:
            compounded *= result["final_equity"]/INITIAL_CAPITAL
            oos_trades += result["closed_trades"]
            oos_wins += result["wins"]
            fold_returns.append(result["return_pct"])
    benchmark=INITIAL_CAPITAL
    for f in folds:
        b=f["buy_and_hold_oos"]
        benchmark *= b["final_equity"]/INITIAL_CAPITAL
    summary={"folds":len(folds),"selected_folds":len(selected_oos),"oos_compounded_final_equity":round(compounded,8),"oos_compounded_return_pct":round((compounded/INITIAL_CAPITAL-1)*100,4),"oos_closed_trades":oos_trades,"oos_wins":oos_wins,"oos_win_rate_pct":round(oos_wins/oos_trades*100,2) if oos_trades else 0.0,"buy_and_hold_compounded_final_equity":round(benchmark,8),"buy_and_hold_compounded_return_pct":round((benchmark/INITIAL_CAPITAL-1)*100,4)}
    return {"candles":len(candles),"first_candle_utc":utc_text(candles[0]["t"]),"last_candle_utc":utc_text(candles[-1]["t"]),"count_coverage_expected":LOOKBACK_DAYS*24*60//RESOLUTION_MINUTES,"configurations_tested_per_fold":len(configs),"walk_forward":{"train_days":TRAIN_DAYS,"test_days":TEST_DAYS,"minimum_train_closed_trades":MIN_TRAIN_TRADES,"drawdown_penalty":DRAWDOWN_PENALTY},"summary":summary,"folds":folds}


def run_self_tests():
    # Deterministic checks for indicator bounds, fee/slippage arithmetic, and conservative stop handling.
    assert len(calculate_rsi([1,2,3,2,4],2)) == 5
    r=calculate_rsi([1,2,3,4,5],2); assert all(x is None or 0 <= x <= 100 for x in r)
    assert calculate_ema([1,2,3],2)[1] == 1.5
    cash,qty,fill,cost=execute_buy(5.0,100.0)
    assert cash >= 0 and qty > 0 and abs(cost-5.0) < 1e-9 and fill > 100
    proceeds,sfill=execute_sell(qty,100.0)
    assert proceeds < cost and sfill < 100
    # Round-trip cost must include both sides' fees and slippage.
    expected_buy_fill = 100.0 * (1 + SLIPPAGE_RATE)
    expected_qty = (5.0 / (1 + FEE_RATE)) / expected_buy_fill
    assert abs(qty - expected_qty) < 1e-12
    assert abs(cost - 5.0) < 1e-9
    flat_rsi = calculate_rsi([10.0] * 8, 3)
    assert flat_rsi[3] == 50.0
    assert len(build_configurations()) == 324
    cs=[{"t":i*3600,"o":100.0,"h":101.0,"l":99.0,"c":100.0} for i in range(5)]
    buys=[False]*5; sells=[False]*5; buys[1]=True
    # Position must close by end of segment and cash must be finite.
    out=simulate(cs,buys,sells,1,5,0.015,0.03)
    assert math.isfinite(out["final_equity"]) and out["final_equity"] > 0
    # A stop touched in the entry candle must close on that same candle.
    spiky=[{"t":i*3600,"o":100.0,"h":101.0,"l":99.0,"c":100.0} for i in range(5)]
    spiky[1]={"t":3600,"o":100.0,"h":104.0,"l":97.0,"c":101.0}
    b=[False]*5; se=[False]*5; b[1]=True
    out2=simulate(spiky,b,se,1,2,0.02,0.03)
    assert out2["closed_trades"] == 1 and out2["trade_log"][0]["reason"] == "ENTRY_BAR_STOP_AND_TARGET_STOP_FIRST"
    # RSI buy signal must occur on recovery above oversold, not on the drop below it.
    ind={("RSI", 7): [None, 35.0, 25.0, 19.0, 22.0, 28.0]}
    cfg={"strategy":"RSI","rsi_period":7,"oversold":20,"overbought":70}
    b2,s2=signals_for_config([{"t":i,"o":1,"h":1,"l":1,"c":1} for i in range(6)],cfg,ind)
    assert b2[5] is True and b2[4] is False
    print("Self-tests: PASS")


def main():
    run_self_tests()
    report={"created_utc":datetime.now(timezone.utc).isoformat(),"version":"4.1.2","history_fetch_method":"7-day time chunks with one-candle overlap, deduplication, and transient-error retries","simulation_only":True,"real_orders_enabled":False,"lookback_days":LOOKBACK_DAYS,"resolution_minutes":RESOLUTION_MINUTES,"initial_capital_per_independent_test":INITIAL_CAPITAL,"fee_rate_per_side":FEE_RATE,"slippage_rate_per_side":SLIPPAGE_RATE,"markets":{}}
    print("NOBITEX BACKTEST V4.1 — SIMULATION ONLY; NO REAL ORDERS")
    print(f"Capital={INITIAL_CAPITAL:.2f} USDT | fee/side={FEE_RATE:.3%} | slippage/side={SLIPPAGE_RATE:.3%}")
    for symbol in MARKETS:
        candles=fetch_history(symbol)
        report["markets"][symbol]=test_market(symbol,candles)
        s=report["markets"][symbol]["summary"]
        print(f"{symbol}: OOS compounded {s['oos_compounded_return_pct']:+.3f}% ({s['selected_folds']} selected folds); Buy&Hold {s['buy_and_hold_compounded_return_pct']:+.3f}%")
    with open(REPORT_FILE,"w",encoding="utf-8") as f: json.dump(report,f,ensure_ascii=False,indent=2)
    print(f"Report saved: {REPORT_FILE}")
    print("Research only: historical results do not guarantee future performance.")

if __name__ == "__main__":
    main()
