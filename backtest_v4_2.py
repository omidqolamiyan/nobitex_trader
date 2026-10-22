#!/usr/bin/env python3
"""Nobitex Backtest V4.2.1 — research only; never submits real orders.
Standard-library-only walk-forward research for BTCUSDT and ETHUSDT.
Historical results do not guarantee future returns.
"""
import json, math, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timezone

API_URL = "https://apiv2.nobitex.ir/market/udf/history"
LOOKBACK_DAYS, RESOLUTION_MINUTES = 180, 60
INITIAL_CAPITAL, FEE_RATE, SLIPPAGE_RATE, MIN_BUY_CASH = 5.0, 0.0013, 0.0005, 0.50
MARKETS = ("BTCUSDT", "ETHUSDT")
RSI_PERIODS, OVERSOLD_LEVELS, OVERBOUGHT_LEVELS = (7, 14, 21), (20, 25, 30), (65, 70, 75)
EMA_FAST_PERIODS, EMA_SLOW_PERIODS = (5, 9, 12), (21, 30, 50)
STOP_LOSSES, TAKE_PROFITS = (0.015, 0.025, 0.04), (0.03, 0.05, 0.07)
DRAWDOWN_PENALTY, MIN_TRAIN_TRADES = 0.50, 3
TRAIN_DAYS, TEST_DAYS, MIN_HISTORY_COVERAGE = 60, 14, 0.95
CHUNK_DAYS, REQUEST_DELAY_SECONDS, MAX_FETCH_ATTEMPTS = 7, 2.1, 4
RETRY_BASE_SECONDS, REQUEST_TIMEOUT_SECONDS = 2.0, 30
REPORT_FILE = "backtest_v4_2_report.json"


def utc_text(ts):
    return datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _valid_candle(row):
    vals = (row["o"], row["h"], row["l"], row["c"])
    return (all(math.isfinite(x) and x > 0 for x in vals)
            and row["h"] >= max(row["o"], row["c"], row["l"])
            and row["l"] <= min(row["o"], row["c"], row["h"]))


def fetch_chunk(symbol, start_ts, end_ts):
    params = {"symbol": symbol, "resolution": str(RESOLUTION_MINUTES),
              "from": int(start_ts), "to": int(end_ts), "page": 1}
    req = urllib.request.Request(API_URL + "?" + urllib.parse.urlencode(params),
        headers={"User-Agent": "Nobitex-Backtest-V4.2.1/1.0", "Accept": "application/json"})
    last_error = None
    for attempt in range(MAX_FETCH_ATTEMPTS):
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                data = json.loads(response.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code not in (408, 429, 500, 502, 503, 504) or attempt == MAX_FETCH_ATTEMPTS - 1:
                raise
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt == MAX_FETCH_ATTEMPTS - 1:
                raise
        delay = RETRY_BASE_SECONDS * (2 ** attempt)
        print(f"{symbol}: retry {attempt + 1}/{MAX_FETCH_ATTEMPTS} in {delay:.1f}s ({last_error})")
        time.sleep(delay)
    else:
        raise RuntimeError(f"{symbol}: request failed after retries: {last_error}")
    if not isinstance(data, dict):
        raise RuntimeError(f"{symbol}: API returned a non-object response")
    if data.get("s") == "no_data":
        return []
    if data.get("s") != "ok":
        raise RuntimeError(f"{symbol}: Nobitex API response was not ok: {data}")
    arrays = [data.get(k, []) for k in ("t", "o", "h", "l", "c")]
    if not all(isinstance(a, list) for a in arrays) or len({len(a) for a in arrays}) != 1:
        raise RuntimeError(f"{symbol}: inconsistent candle array lengths")
    rows = []
    for t, o, h, l, c in zip(*arrays):
        try:
            row = {"t": int(t), "o": float(o), "h": float(h), "l": float(l), "c": float(c)}
        except (TypeError, ValueError, OverflowError):
            continue
        if _valid_candle(row):
            rows.append(row)
    return rows


def fetch_history(symbol, now=None):
    now = int(time.time()) if now is None else int(now)
    step, start = RESOLUTION_MINUTES * 60, now - LOOKBACK_DAYS * 86400
    chunk_seconds, by_ts, cursor, chunk_no = CHUNK_DAYS * 86400, {}, start, 0
    print(f"\nFetching {symbol}: {LOOKBACK_DAYS} days, {RESOLUTION_MINUTES}m candles")
    while cursor < now:
        chunk_no += 1
        chunk_end = min(cursor + chunk_seconds, now)
        try:
            rows = fetch_chunk(symbol, cursor, chunk_end)
        except Exception as exc:
            raise RuntimeError(f"{symbol}: chunk {chunk_no} [{utc_text(cursor)} -> {utc_text(chunk_end)}] failed: {exc}") from exc
        before = len(by_ts)
        for row in rows:
            if start <= row["t"] <= now:
                by_ts[row["t"]] = row
        print(f"{symbol}: chunk {chunk_no}, received {len(rows)}, new {len(by_ts)-before}, unique {len(by_ts)}")
        if chunk_end >= now:
            break
        next_cursor = chunk_end - step
        if next_cursor <= cursor:
            raise RuntimeError(f"{symbol}: history cursor failed to advance")
        cursor = next_cursor
        time.sleep(REQUEST_DELAY_SECONDS)
    candles = [by_ts[t] for t in sorted(by_ts)]
    expected = LOOKBACK_DAYS * 24 * 60 // RESOLUTION_MINUTES
    count_coverage = len(candles) / expected if expected else 0.0
    span_coverage = (candles[-1]["t"] - candles[0]["t"]) / (LOOKBACK_DAYS * 86400) if len(candles) > 1 else 0.0
    if len(candles) < 2:
        raise RuntimeError(f"{symbol}: fewer than two valid candles")
    irregular = [(a["t"], b["t"]) for a, b in zip(candles, candles[1:]) if b["t"] - a["t"] != step]
    print(f"{symbol}: {len(candles)}/{expected} candles ({count_coverage:.1%}); span {span_coverage:.1%}")
    print(f"{utc_text(candles[0]['t'])} -> {utc_text(candles[-1]['t'])}")
    if count_coverage < MIN_HISTORY_COVERAGE or span_coverage < MIN_HISTORY_COVERAGE or irregular:
        detail = f"; first gap {utc_text(irregular[0][0])} -> {utc_text(irregular[0][1])}" if irregular else ""
        raise RuntimeError(f"{symbol}: insufficient/irregular history (count={count_coverage:.1%}, span={span_coverage:.1%}){detail}; refusing unreliable backtest")
    return candles


def calculate_rsi(closes, period):
    out = [None] * len(closes)
    if period < 1 or len(closes) <= period:
        return out
    gains, losses = [], []
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0)); losses.append(max(-d, 0.0))
    ag, al = sum(gains) / period, sum(losses) / period
    def value(g, l):
        if l == 0: return 100.0 if g > 0 else 50.0
        if g == 0: return 0.0
        return 100.0 - 100.0 / (1.0 + g / l)
    out[period] = value(ag, al)
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (period - 1) + max(d, 0.0)) / period
        al = (al * (period - 1) + max(-d, 0.0)) / period
        out[i] = value(ag, al)
    return out


def calculate_ema(values, period):
    out = [None] * len(values)
    if period < 1 or len(values) < period: return out
    current = sum(values[:period]) / period
    out[period - 1] = current
    alpha = 2.0 / (period + 1)
    for i in range(period, len(values)):
        current = values[i] * alpha + current * (1 - alpha)
        out[i] = current
    return out


def build_configurations():
    configs = []
    for p in RSI_PERIODS:
        for os_level in OVERSOLD_LEVELS:
            for ob_level in OVERBOUGHT_LEVELS:
                if os_level >= ob_level: continue
                for sl in STOP_LOSSES:
                    for tp in TAKE_PROFITS:
                        configs.append({"strategy":"RSI", "rsi_period":p, "oversold":os_level,
                                        "overbought":ob_level, "stop_loss":sl, "take_profit":tp})
    for fast in EMA_FAST_PERIODS:
        for slow in EMA_SLOW_PERIODS:
            if fast >= slow: continue
            for sl in STOP_LOSSES:
                for tp in TAKE_PROFITS:
                    configs.append({"strategy":"EMA", "ema_fast":fast, "ema_slow":slow,
                                    "stop_loss":sl, "take_profit":tp})
    return configs


def signals_for_config(candles, config, indicators):
    """Signals use completed prior candles and execute at the current candle open."""
    n = len(candles); buys, sells = [False]*n, [False]*n
    if config["strategy"] == "RSI":
        vals = indicators[("RSI", config["rsi_period"])]
        for i in range(1, n):
            previous = vals[i-2] if i >= 2 else None
            current = vals[i-1]
            if previous is None or current is None: continue
            buys[i] = previous < config["oversold"] and current >= config["oversold"]
            sells[i] = previous > config["overbought"] and current <= config["overbought"]
    else:
        fast, slow = indicators[("EMA", config["ema_fast"])], indicators[("EMA", config["ema_slow"])]
        for i in range(2, n):
            j = i - 1
            if any(x is None for x in (fast[j], slow[j], fast[j-1], slow[j-1])): continue
            buys[i] = fast[j] > slow[j] and fast[j-1] <= slow[j-1]
            sells[i] = fast[j] < slow[j] and fast[j-1] >= slow[j-1]
    return buys, sells


def execute_buy(cash, reference_price):
    if cash <= 0 or reference_price <= 0: return cash, 0.0, None, 0.0
    fill = reference_price * (1 + SLIPPAGE_RATE)
    notional = cash / (1 + FEE_RATE)
    qty = notional / fill
    total_cost = notional * (1 + FEE_RATE)
    return max(0.0, cash - total_cost), qty, fill, total_cost


def execute_sell(qty, reference_price):
    fill = reference_price * (1 - SLIPPAGE_RATE)
    return max(0.0, qty * fill * (1 - FEE_RATE)), fill


def simulate(candles, buys, sells, start, end, stop_loss, take_profit, liquidate=True):
    """Long-only simulation. DD uses close peaks and only price levels reached before/at exit."""
    start, end = max(1, int(start)), min(int(end), len(candles))
    cash, qty, entry_price, entry_cost, entry_time = INITIAL_CAPITAL, 0.0, None, 0.0, None
    entries = closed = wins = losses = selection_trades = 0
    trades, curve = [], []
    if start >= end:
        return {"initial_capital":INITIAL_CAPITAL, "final_equity":INITIAL_CAPITAL, "return_pct":0.0,
                "entries":0, "closed_trades":0, "selection_trades":0, "wins":0, "losses":0,
                "win_rate_pct":0.0, "max_drawdown_pct":0.0, "trade_log":[]}
    peak, max_dd = INITIAL_CAPITAL, 0.0
    for i in range(start, end):
        c = candles[i]; open_px = c["o"]
        low_candidates = [cash + qty*c["l"] if qty > 0 else cash]
        exited_this_bar = False
        exit_px = reason = None
        if qty > 0:
            stop, target = entry_price*(1-stop_loss), entry_price*(1+take_profit)
            if open_px <= stop: exit_px, reason = open_px, "STOP_GAP"
            elif open_px >= target: exit_px, reason = open_px, "TARGET_GAP"
            elif c["l"] <= stop and c["h"] >= target: exit_px, reason = stop, "STOP_AND_TARGET_SAME_BAR_STOP_FIRST"
            elif c["l"] <= stop: exit_px, reason = stop, "STOP_LOSS"
            elif c["h"] >= target: exit_px, reason = target, "TAKE_PROFIT"
            elif sells[i]: exit_px, reason = open_px, "SIGNAL"
            if exit_px is not None:
                proceeds, fill = execute_sell(qty, exit_px); pnl = proceeds-entry_cost
                closed += 1; selection_trades += 1; wins += int(pnl > 0); losses += int(pnl <= 0)
                trades.append({"entry_time":entry_time,"exit_time":c["t"],"entry_price":round(entry_price,8),
                    "exit_price":round(fill,8),"reason":reason,"entry_cost":round(entry_cost,8),
                    "proceeds":round(proceeds,8),"pnl":round(pnl,8)})
                # For an open-gap or signal exit at the open, do not count this candle's later low.
                if reason in ("STOP_GAP", "TARGET_GAP", "SIGNAL"):
                    low_candidates = [proceeds]
                else:
                    # Stop/target was reached intrabar; use the modeled exit proceeds, not any later extreme.
                    low_candidates = [proceeds]
                cash, qty, entry_price, entry_cost, entry_time = proceeds, 0.0, None, 0.0, None
                exited_this_bar = True
        if qty == 0 and not exited_this_bar and cash >= MIN_BUY_CASH and buys[i]:
            cash, qty, entry_price, entry_cost = execute_buy(cash, open_px)
            if qty > 0:
                entries += 1; entry_time = c["t"]
                low_candidates.append(cash + qty*c["l"])
                stop, target = entry_price*(1-stop_loss), entry_price*(1+take_profit)
                if c["l"] <= stop and c["h"] >= target: exit_px, reason = stop, "ENTRY_BAR_STOP_AND_TARGET_STOP_FIRST"
                elif c["l"] <= stop: exit_px, reason = stop, "ENTRY_BAR_STOP_LOSS"
                elif c["h"] >= target: exit_px, reason = target, "ENTRY_BAR_TAKE_PROFIT"
                else: exit_px = reason = None
                if exit_px is not None:
                    proceeds, fill = execute_sell(qty, exit_px); pnl = proceeds-entry_cost
                    closed += 1; selection_trades += 1; wins += int(pnl > 0); losses += int(pnl <= 0)
                    trades.append({"entry_time":entry_time,"exit_time":c["t"],"entry_price":round(entry_price,8),
                        "exit_price":round(fill,8),"reason":reason,"entry_cost":round(entry_cost,8),
                        "proceeds":round(proceeds,8),"pnl":round(pnl,8)})
                    low_candidates = [proceeds]
                    cash, qty, entry_price, entry_cost, entry_time = proceeds, 0.0, None, 0.0, None
        if qty > 0: low_candidates.append(cash + qty*c["l"])
        close_equity = cash + qty*c["c"]
        low_equity = min(low_candidates + [close_equity])
        peak = max(peak, close_equity)
        if peak > 0: max_dd = max(max_dd, (peak-low_equity)/peak)
        curve.append({"t":c["t"], "close":close_equity, "low":low_equity})
    if qty > 0 and liquidate:
        last = candles[end-1]; proceeds, fill = execute_sell(qty, last["c"]); pnl = proceeds-entry_cost
        closed += 1; wins += int(pnl > 0); losses += int(pnl <= 0)
        # Administrative end-of-window liquidation is intentionally NOT counted as a selection trade.
        trades.append({"entry_time":entry_time,"exit_time":last["t"],"entry_price":round(entry_price,8),
            "exit_price":round(fill,8),"reason":"END_OF_SEGMENT","entry_cost":round(entry_cost,8),
            "proceeds":round(proceeds,8),"pnl":round(pnl,8)})
        cash, qty = proceeds, 0.0
        if curve:
            curve[-1]["close"] = cash; curve[-1]["low"] = min(curve[-1]["low"], cash)
            if peak > 0: max_dd = max(max_dd, (peak-curve[-1]["low"])/peak)
    final_equity = curve[-1]["close"] if curve else INITIAL_CAPITAL
    return {"initial_capital":INITIAL_CAPITAL,"final_equity":round(final_equity,8),
        "return_pct":round((final_equity/INITIAL_CAPITAL-1)*100,4),"entries":entries,
        "closed_trades":closed,"selection_trades":selection_trades,"wins":wins,"losses":losses,
        "win_rate_pct":round(wins/closed*100,2) if closed else 0.0,
        "max_drawdown_pct":round(max_dd*100,4),"trade_log":trades}


def buy_and_hold(candles, start, end):
    start, end = max(0,int(start)), min(int(end),len(candles))
    if start >= end: return {"final_equity":INITIAL_CAPITAL,"return_pct":0.0,"max_drawdown_pct":0.0}
    _, qty, _, _ = execute_buy(INITIAL_CAPITAL,candles[start]["o"])
    peak, max_dd = INITIAL_CAPITAL, 0.0
    for i in range(start,end):
        c = candles[i]; close_equity, low_equity = qty*c["c"], qty*c["l"]
        peak = max(peak,close_equity)
        if peak > 0: max_dd = max(max_dd,(peak-low_equity)/peak)
    proceeds,_ = execute_sell(qty,candles[end-1]["c"])
    return {"final_equity":round(proceeds,8),"return_pct":round((proceeds/INITIAL_CAPITAL-1)*100,4),
            "max_drawdown_pct":round(max_dd*100,4)}


def score_result(result):
    # End-of-segment liquidation is excluded from selection_trades.
    if result.get("selection_trades", 0) < MIN_TRAIN_TRADES: return -1e9
    return result["return_pct"] - DRAWDOWN_PENALTY*result["max_drawdown_pct"]


def config_for_report(c):
    out = {"strategy":c["strategy"],"stop_loss_pct":c["stop_loss"]*100,"take_profit_pct":c["take_profit"]*100}
    for k in ("rsi_period","oversold","overbought","ema_fast","ema_slow"):
        if k in c: out[k] = c[k]
    return out


def make_indicators(candles):
    closes = [x["c"] for x in candles]; out = {}
    for p in RSI_PERIODS: out[("RSI",p)] = calculate_rsi(closes,p)
    for p in set(EMA_FAST_PERIODS+EMA_SLOW_PERIODS): out[("EMA",p)] = calculate_ema(closes,p)
    return out


def evaluate_config(candles, config, indicators, start, end):
    buys,sells = signals_for_config(candles,config,indicators)
    return simulate(candles,buys,sells,start,end,config["stop_loss"],config["take_profit"])


def test_market(symbol, candles):
    configs, indicators = build_configurations(), make_indicators(candles)
    bars_per_day = 24*60//RESOLUTION_MINUTES
    train_bars, test_bars = TRAIN_DAYS*bars_per_day, TEST_DAYS*bars_per_day
    if len(candles) <= train_bars+test_bars:
        raise RuntimeError(f"{symbol}: not enough candles for initial {TRAIN_DAYS}-day training + {TEST_DAYS}-day test")
    folds, selected_oos = [], []; fold_no = 0
    while train_bars+fold_no*test_bars < len(candles):
        test_start = train_bars+fold_no*test_bars; test_end = min(test_start+test_bars,len(candles))
        train_start = 1; ranked = []
        for cfg in configs:
            train = evaluate_config(candles,cfg,indicators,train_start,test_start)
            ranked.append((score_result(train),cfg,train))
        ranked.sort(key=lambda x:x[0],reverse=True)
        eligible = [x for x in ranked if x[0] > -1e8]
        base = {"fold":fold_no+1,"train_start_utc":utc_text(candles[train_start]["t"]),
            "train_end_utc":utc_text(candles[test_start-1]["t"]),"training_method":"expanding_window",
            "training_days_actual":round((candles[test_start-1]["t"]-candles[train_start]["t"])/86400,2),
            "test_start_index":test_start,"test_start_utc":utc_text(candles[test_start]["t"]),
            "test_end_utc":utc_text(candles[test_end-1]["t"]),"test_days_actual":round((test_end-test_start)/bars_per_day,2),
            "buy_and_hold_oos_fold":buy_and_hold(candles,test_start,test_end)}
        if not eligible:
            base.update({"selected_config":None,"selection_note":"No configuration met minimum non-administrative training trade threshold; strategy remains in cash.","out_of_sample":None})
        else:
            _,chosen,train_result = eligible[0]
            oos = evaluate_config(candles,chosen,indicators,test_start,test_end)
            base.update({"selected_config":config_for_report(chosen),
                "training_metrics":{k:v for k,v in train_result.items() if k!="trade_log"},
                "out_of_sample":{k:v for k,v in oos.items() if k!="trade_log"},
                "out_of_sample_trade_log":oos["trade_log"]})
            selected_oos.append({"start":test_start,"result":oos})
        folds.append(base); fold_no += 1
    selected_by_start = {x["start"]:x["result"] for x in selected_oos}
    compounded, fold_benchmark = INITIAL_CAPITAL, INITIAL_CAPITAL
    oos_trades = oos_wins = 0
    for f in folds:
        result = selected_by_start.get(f["test_start_index"])
        if result is not None:
            compounded *= result["final_equity"]/INITIAL_CAPITAL
            oos_trades += result["closed_trades"]; oos_wins += result["wins"]
        fold_benchmark *= f["buy_and_hold_oos_fold"]["final_equity"]/INITIAL_CAPITAL
    # Continuous buy-and-hold over the full OOS span, rather than selling/rebuying each fold.
    continuous_benchmark = buy_and_hold(candles,train_bars,len(candles))
    summary = {"folds":len(folds),"selected_folds":len(selected_oos),
        "oos_compounded_final_equity":round(compounded,8),
        "oos_compounded_return_pct":round((compounded/INITIAL_CAPITAL-1)*100,4),
        "oos_closed_trades_including_end_liquidations":oos_trades,"oos_wins":oos_wins,
        "oos_win_rate_pct":round(oos_wins/oos_trades*100,2) if oos_trades else 0.0,
        "buy_and_hold_continuous_oos_final_equity":continuous_benchmark["final_equity"],
        "buy_and_hold_continuous_oos_return_pct":continuous_benchmark["return_pct"],
        "buy_and_hold_fold_rebalanced_final_equity":round(fold_benchmark,8),
        "buy_and_hold_fold_rebalanced_return_pct":round((fold_benchmark/INITIAL_CAPITAL-1)*100,4)}
    return {"candles":len(candles),"first_candle_utc":utc_text(candles[0]["t"]),"last_candle_utc":utc_text(candles[-1]["t"]),
        "count_coverage_expected":LOOKBACK_DAYS*24*60//RESOLUTION_MINUTES,"configurations_tested_per_fold":len(configs),
        "walk_forward":{"initial_train_days":TRAIN_DAYS,"training_method":"expanding_window",
            "test_days":TEST_DAYS,"minimum_train_non_administrative_trades":MIN_TRAIN_TRADES,
            "drawdown_penalty":DRAWDOWN_PENALTY},"summary":summary,"folds":folds}


def run_self_tests():
    assert len(calculate_rsi([1,2,3,2,4],2)) == 5
    r = calculate_rsi([1,2,3,4,5],2); assert all(x is None or 0 <= x <= 100 for x in r)
    assert calculate_ema([1,2,3],2)[1] == 1.5
    cash,qty,fill,cost = execute_buy(5.0,100.0)
    assert cash >= 0 and qty > 0 and abs(cost-5.0) < 1e-9 and fill > 100
    proceeds,sfill = execute_sell(qty,100.0); assert proceeds < cost and sfill < 100
    assert calculate_rsi([10.0]*8,3)[3] == 50.0
    assert len(build_configurations()) == 324
    candles = [{"t":i*3600,"o":100.0,"h":101.0,"l":99.0,"c":100.0} for i in range(5)]
    buys,sells = [False]*5,[False]*5; buys[1] = True
    out = simulate(candles,buys,sells,1,5,0.015,0.03)
    assert math.isfinite(out["final_equity"]) and out["final_equity"] > 0
    # A position closed administratively must not qualify toward the selection threshold.
    assert out["closed_trades"] == 1 and out["selection_trades"] == 0
    spiky = [dict(x) for x in candles]; spiky[1] = {"t":3600,"o":100.0,"h":104.0,"l":97.0,"c":101.0}
    b,se = [False]*5,[False]*5; b[1] = True
    out2 = simulate(spiky,b,se,1,2,0.02,0.03)
    assert out2["closed_trades"] == 1 and out2["trade_log"][0]["reason"] == "ENTRY_BAR_STOP_AND_TARGET_STOP_FIRST"
    ind = {("RSI",7):[None,35.0,25.0,19.0,22.0,28.0]}
    cfg = {"strategy":"RSI","rsi_period":7,"oversold":20,"overbought":70}
    b2,_ = signals_for_config([{"t":i,"o":1,"h":1,"l":1,"c":1} for i in range(6)],cfg,ind)
    assert b2[5] is True and b2[4] is False
    print("Self-tests: PASS")


def main():
    run_self_tests()
    report = {"created_utc":datetime.now(timezone.utc).isoformat(),"version":"4.2.1",
        "simulation_only":True,"real_orders_enabled":False,
        "history_fetch_method":f"{CHUNK_DAYS}-day chunks, one-candle overlap, deduplication, bounded retries, final-chunk termination",
        "research_notes":[
            "Research only; real orders are disabled.",
            "Training uses an expanding window beginning with an initial 60-day training period.",
            "Administrative END_OF_SEGMENT exits affect equity/trade statistics but do not qualify toward minimum training trade count.",
            "Drawdown uses close-based peaks and modeled low/exit levels; OHLC bars cannot reveal exact intrabar ordering.",
            "Continuous buy-and-hold and fold-rebalanced buy-and-hold are reported separately.",
            "Configurations are selected on prior training data and evaluated on the next OOS fold.",
            "All results include configured fees and slippage; historical results do not guarantee future returns."],
        "lookback_days":LOOKBACK_DAYS,"resolution_minutes":RESOLUTION_MINUTES,
        "initial_capital_per_independent_test":INITIAL_CAPITAL,"fee_rate_per_side":FEE_RATE,
        "slippage_rate_per_side":SLIPPAGE_RATE,"markets":{}}
    print("NOBITEX BACKTEST V4.2.1 — SIMULATION ONLY; NO REAL ORDERS")
    print(f"Capital={INITIAL_CAPITAL:.2f} USDT | fee/side={FEE_RATE:.3%} | slippage/side={SLIPPAGE_RATE:.3%}")
    for symbol in MARKETS:
        candles = fetch_history(symbol); market_report = test_market(symbol,candles)
        report["markets"][symbol] = market_report; s = market_report["summary"]
        print(f"{symbol}: OOS compounded {s['oos_compounded_return_pct']:+.3f}% ({s['selected_folds']} selected folds); continuous B&H {s['buy_and_hold_continuous_oos_return_pct']:+.3f}%")
    with open(REPORT_FILE,"w",encoding="utf-8") as f: json.dump(report,f,ensure_ascii=False,indent=2)
    print(f"Report saved: {REPORT_FILE}")
    print("Research only: historical results do not guarantee future performance.")

if __name__ == "__main__":
    main()
