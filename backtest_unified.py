#!/usr/bin/env python3
"""Nobitex Unified Backtest v1.0.0 — research only; no real orders."""
from __future__ import annotations
import argparse, datetime as dt, json, math, statistics, time, urllib.parse, urllib.request
from dataclasses import asdict, dataclass
from typing import Optional

VERSION = "1.0.0"
API_URL = "https://apiv2.nobitex.ir/market/udf/history"
REPORT_FILE = "backtest_unified_report.json"
INITIAL_CAPITAL = 5.0
DAYS, RESOLUTION = 180, "60"
FEE_RATE, SLIPPAGE_RATE = 0.0013, 0.0005
MARKETS = ("BTCUSDT", "ETHUSDT")
TRAIN_DAYS, TEST_DAYS, MIN_TRAIN_TRADES = 60, 14, 3
MIN_COVERAGE, DRAWDOWN_PENALTY = 0.95, 0.50
MAX_RETRIES, REQUEST_DELAY_SECONDS = 4, 2.1

@dataclass(frozen=True)
class Candle:
    t: int; o: float; h: float; l: float; c: float; v: float = 0.0

@dataclass(frozen=True)
class Config:
    strategy: str
    params: tuple[tuple[str, float], ...]
    stop_loss: float
    take_profit: float
    def get(self, key, default=0.0): return dict(self.params).get(key, default)
    def label(self):
        p = ",".join(f"{k}={v:g}" for k,v in self.params)
        return f"{self.strategy}[{p};SL={self.stop_loss:g}%;TP={self.take_profit:g}%]"

@dataclass
class Trade:
    entry_time: int; exit_time: int; entry_price: float; exit_price: float
    quantity: float; pnl: float; pnl_pct: float; reason: str

@dataclass
class Result:
    start_equity: float; end_equity: float; return_pct: float; max_drawdown_pct: float
    closed_trades: int; wins: int; losses: int; win_rate_pct: float
    profit_factor: Optional[float]; trades: list; equity_curve: list

def utc(ts):
    return dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).isoformat()

def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent":"Nobitex-Unified-Backtest/1.0","Accept":"application/json"})
    with urllib.request.urlopen(req, timeout=30) as r: data = json.loads(r.read().decode("utf-8"))
    if not isinstance(data, dict): raise ValueError("Unexpected API response")
    return data

def fetch_candles(symbol, days=DAYS, resolution=RESOLUTION):
    now = int(time.time()); sec = 86400 if resolution == "D" else int(resolution)*60
    query = urllib.parse.urlencode({"symbol":symbol,"resolution":resolution,"from":now-days*86400,"to":now})
    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            obj = get_json(API_URL+"?"+query)
            if obj.get("s") not in (None,"ok"): raise ValueError(f"API status: {obj.get('s')}")
            arrays = [obj.get(k) for k in ("t","o","h","l","c")]
            if not all(isinstance(a,list) for a in arrays): raise ValueError("Missing candle arrays")
            ts,oo,hh,ll,cc = arrays; vv=obj.get("v",[])
            by_time={}
            for i in range(min(map(len,arrays))):
                try:
                    c=Candle(int(ts[i]),float(oo[i]),float(hh[i]),float(ll[i]),float(cc[i]),float(vv[i]) if i<len(vv) else 0.0)
                    vals=(c.o,c.h,c.l,c.c,c.v)
                    if c.t>0 and all(math.isfinite(x) for x in vals) and min(c.o,c.h,c.l,c.c)>0 and c.h>=max(c.o,c.c,c.l) and c.l<=min(c.o,c.c,c.h):
                        by_time[c.t]=c
                except (ValueError,TypeError,OverflowError): pass
            candles=sorted((c for c in by_time.values() if c.t < (now//sec)*sec),key=lambda c:c.t)
            if len(candles)<100: raise ValueError(f"Only {len(candles)} valid candles")
            return candles
        except Exception as e:
            last_error=e
            if attempt+1<MAX_RETRIES: time.sleep(1.5*(attempt+1))
    raise RuntimeError(f"Fetching {symbol} failed: {last_error}")

def sma(vals, period, end):
    if period<1 or end+1<period: return None
    return sum(vals[end-period+1:end+1])/period

def rsi(vals, period, end):
    if period<1 or end<period: return None
    gains=losses=0.0
    for i in range(end-period+1,end+1):
        d=vals[i]-vals[i-1]
        if d>0: gains+=d
        elif d<0: losses-=d
    ag,al=gains/period,losses/period
    if al==0: return 100.0 if ag>0 else 50.0
    return 100-100/(1+ag/al)

def ema_series(vals, period):
    out=[None]*len(vals)
    if period<1 or len(vals)<period: return out
    prev=sum(vals[:period])/period; out[period-1]=prev; alpha=2/(period+1)
    for i in range(period,len(vals)):
        prev=alpha*vals[i]+(1-alpha)*prev; out[i]=prev
    return out

def build_configs():
    out=[]
    for sl in (1.5,2.5,4.0):
      for tp in (3.0,5.0,7.0):
        for p in (7,14,21):
          for os in (20,25,30):
            for ob in (65,70,75):
              out.append(Config("RSI",(("period",p),("oversold",os),("overbought",ob)),sl,tp))
        for fast in (5,9,12):
          for slow in (21,30,50):
            if fast<slow: out.append(Config("EMA",(("fast",fast),("slow",slow)),sl,tp))
        for width in (1.5,2.0,2.5):
          out.append(Config("BOLLINGER",(("period",20),("width",width)),sl,tp))
        for p in (10,20,30): out.append(Config("BREAKOUT",(("period",p),),sl,tp))
    return list({(x.strategy,x.params,x.stop_loss,x.take_profit):x for x in out}.values())

def signal_at(candles, index, cfg):
    # Uses only the prior completed candle; executes at the current candle's open.
    j=index-1
    if j<1:return 0
    closes=[c.c for c in candles]
    if cfg.strategy=="RSI":
        x=rsi(closes,int(cfg.get("period")),j)
        if x is None:return 0
        return 1 if x<=cfg.get("oversold") else (-1 if x>=cfg.get("overbought") else 0)
    if cfg.strategy=="EMA":
        f=ema_series(closes[:j+1],int(cfg.get("fast")))[-1]
        s=ema_series(closes[:j+1],int(cfg.get("slow")))[-1]
        return 1 if f is not None and s is not None and f>s else (-1 if f is not None and s is not None and f<s else 0)
    if cfg.strategy=="BOLLINGER":
        p=int(cfg.get("period")); mid=sma(closes,p,j)
        if mid is None:return 0
        sd=statistics.pstdev(closes[j-p+1:j+1]); x=closes[j]
        return 1 if x<mid-cfg.get("width")*sd else (-1 if x>mid+cfg.get("width")*sd else 0)
    if cfg.strategy=="BREAKOUT":
        p=int(cfg.get("period"))
        if j<p:return 0
        prior=candles[j-p:j]
        return 1 if candles[j].c>max(c.h for c in prior) else (-1 if candles[j].c<min(c.l for c in prior) else 0)
    return 0

def simulate(candles, cfg, initial_cash=INITIAL_CAPITAL, fee=FEE_RATE, slip=SLIPPAGE_RATE, start_index=0):
    if len(candles)<2: raise ValueError("Need at least two candles")
    if initial_cash<=0 or fee<0 or slip<0: raise ValueError("Invalid capital/costs")
    cash=float(initial_cash); qty=0.0; entry=entry_fee=0.0; entry_t=0; stop=target=0.0
    trades=[]; curve=[]; peak=initial_cash; maxdd=0.0
    def sell(raw,t,reason):
        nonlocal cash,qty,entry,entry_fee,entry_t,stop,target
        if qty<=0:return
        fill=raw*(1-slip); proceeds=qty*fill*(1-fee)
        pnl=proceeds-(qty*entry+entry_fee); invested=qty*entry+entry_fee
        trades.append(Trade(entry_t,t,entry,fill,qty,pnl,(pnl/invested*100 if invested else 0),reason))
        cash=proceeds; qty=0; entry=entry_fee=0; entry_t=0; stop=target=0
    def buy(raw,t,sl_pct,tp_pct):
        nonlocal cash,qty,entry,entry_fee,entry_t,stop,target
        fill=raw*(1+slip); qty=cash/(fill*(1+fee))
        if qty<=0:return
        entry=fill; entry_fee=qty*fill*fee; entry_t=t; cash=0
        stop=fill*(1-sl_pct/100); target=fill*(1+tp_pct/100)
    for i,c in enumerate(candles):
        if i < start_index:
            continue
        # Signal is known at the open from prior completed candle. Execute before
        # intrabar checks, avoiding the impossible "stop out then buy at same open".
        if cfg is not None and i>0:
            sig=signal_at(candles,i,cfg)
            if qty>0 and sig<0: sell(c.o,c.t,"strategy_exit")
            elif qty<=0 and sig>0: buy(c.o,c.t,cfg.stop_loss,cfg.take_profit)
        if qty>0:
            adverse=qty*c.l
            maxdd=max(maxdd,(peak-adverse)/peak if peak else 0)
            hs=c.l<=stop; ht=c.h>=target
            if hs and ht: sell(stop,c.t,"stop_and_target_same_candle_stop_first")
            elif hs: sell(min(stop,c.o),c.t,"stop_loss_gap_or_touch")
            elif ht: sell(max(target,c.o),c.t,"take_profit_gap_or_touch")
        equity=cash+qty*c.c
        curve.append(equity); peak=max(peak,equity)
        if peak:maxdd=max(maxdd,(peak-equity)/peak)
    if qty>0:
        sell(candles[-1].c,candles[-1].t,"end_of_test_liquidation"); curve[-1]=cash
    eligible=[t for t in trades if t.reason!="end_of_test_liquidation"]
    wins=sum(t.pnl>0 for t in eligible); losses=sum(t.pnl<0 for t in eligible)
    gp=sum(t.pnl for t in eligible if t.pnl>0); gl=-sum(t.pnl for t in eligible if t.pnl<0)
    pf=gp/gl if gl>0 else (math.inf if gp>0 else None)
    return Result(initial_cash,cash,(cash/initial_cash-1)*100,maxdd*100,len(eligible),wins,losses,
                  wins/len(eligible)*100 if eligible else 0,pf,trades,curve)

def buy_hold(candles,capital=INITIAL_CAPITAL,fee=FEE_RATE,slip=SLIPPAGE_RATE):
    if len(candles)<2:raise ValueError("Need at least two candles")
    fill=candles[0].o*(1+slip); qty=capital/(fill*(1+fee)); curve=[]; peak=capital; dd=0
    for c in candles:
        equity=qty*c.c; curve.append(equity); peak=max(peak,equity)
        if peak:dd=max(dd,(peak-qty*c.l)/peak)
    exit_price=candles[-1].c*(1-slip); end=qty*exit_price*(1-fee); curve[-1]=end
    pnl=end-capital; trade=Trade(candles[0].t,candles[-1].t,fill,exit_price,qty,pnl,pnl/capital*100,"buy_and_hold")
    return Result(capital,end,(end/capital-1)*100,dd*100,1,int(pnl>0),int(pnl<0),100 if pnl>0 else 0,
                  math.inf if pnl>0 else None,[trade],curve)

def score(r):
    return r.return_pct-DRAWDOWN_PENALTY*r.max_drawdown_pct if r.closed_trades>=MIN_TRAIN_TRADES else -1e9

def split_folds(candles, resolution=RESOLUTION):
    per_day=1440//int(resolution) if resolution!="D" else 1
    train_n=TRAIN_DAYS*per_day; test_n=TEST_DAYS*per_day; out=[]; start=train_n
    while start+2<=len(candles):
        end=min(start+test_n,len(candles))
        if start>=100 and end-start>=2:out.append((candles[:start],candles[start:end]))
        if end==len(candles):break
        start=end
    return out

def summary(r):
    pf=r.profit_factor
    return {"start_equity":round(r.start_equity,8),"end_equity":round(r.end_equity,8),
      "return_pct":round(r.return_pct,6),"max_drawdown_pct":round(r.max_drawdown_pct,6),
      "closed_trades":r.closed_trades,"wins":r.wins,"losses":r.losses,"win_rate_pct":round(r.win_rate_pct,4),
      "profit_factor":round(pf,6) if pf is not None and math.isfinite(pf) else ("infinity" if pf==math.inf else None)}

def run_market(symbol,candles,configs,capital,fee,slip,resolution):
    fold_rows=[]
    for no,(train,test) in enumerate(split_folds(candles,resolution),1):
        best=None; best_score=-1e9
        for cfg in configs:
            r=simulate(train,cfg,capital,fee,slip); s=score(r)
            if s>best_score:best,best_score=cfg,s
        if best is None or best_score<=-1e8:
            fold_rows.append({"fold":no,"status":"no_eligible_config","oos_return_pct":0.0,"oos_trades":0})
            continue
        # Use trailing training candles only as indicator context. The OOS portfolio
        # still starts with fresh capital at the first test candle.
        prefix=train[-100:]
        combined=prefix+test
        out=simulate(combined,best,capital,fee,slip,start_index=len(prefix))
        fold_rows.append({"fold":no,"status":"ok","train_start_utc":utc(train[0].t),
          "train_end_utc":utc(train[-1].t),"test_start_utc":utc(test[0].t),"test_end_utc":utc(test[-1].t),
          "selected_config":best.label(),"train_score":round(best_score,6),
          "oos_return_pct":round(out.return_pct,6),"oos_max_drawdown_pct":round(out.max_drawdown_pct,6),
          "oos_trades":out.closed_trades,"oos_summary":summary(out)})
    valid=[x for x in fold_rows if x["status"]=="ok"]; equity=1.0
    for x in valid:equity*=max(0,1+x["oos_return_pct"]/100)
    returns=[x["oos_return_pct"] for x in valid]
    bh=buy_hold(candles,capital,fee,slip)
    return {"symbol":symbol,"candles":len(candles),"data_start_utc":utc(candles[0].t),"data_end_utc":utc(candles[-1].t),
      "buy_and_hold_full_period":summary(bh),"walk_forward":{"folds":len(valid),
      "compounded_oos_return_pct":round((equity-1)*100,6) if valid else None,
      "mean_oos_return_pct":round(statistics.mean(returns),6) if returns else None,
      "median_oos_return_pct":round(statistics.median(returns),6) if returns else None,
      "positive_folds":sum(x>0 for x in returns),"negative_folds":sum(x<0 for x in returns),
      "total_oos_trades":sum(x["oos_trades"] for x in valid)},"fold_details":fold_rows}

def self_test():
    base=1700000000
    assert ema_series([1,2,3,4],3)[1] is None
    assert rsi([1,2,3,4,5],3,4)==100.0
    flat=[Candle(base+i*3600,100,101,99,100) for i in range(20)]
    free=buy_hold(flat,5,0,0).end_equity; cost=buy_hold(flat,5,.0013,.0005).end_equity
    assert cost<free
    # Explicitly check conservative both-hit behavior with an entry on bar 1.
    candles=[Candle(base,100,101,99,100),Candle(base+3600,100,101,99,100),
             Candle(base+7200,100,105,90,100),Candle(base+10800,100,101,99,100)]
    cfg=Config("RSI",(("period",1),("oversold",100),("overbought",101)),2,2)
    r=simulate(candles,cfg,5,0,0)
    both=[t for t in r.trades if "same_candle" in t.reason]
    assert len(both) == 1 and both[0].reason == "stop_and_target_same_candle_stop_first"
    assert abs(both[0].exit_price - 98.0) < 1e-9
    # Core smoke/accounting invariants.
    assert all(math.isfinite(x) for x in r.equity_curve)
    assert len(r.equity_curve)==len(candles)
    return {"status":"passed","checks":["EMA warm-up","RSI monotonic rise","fee/slippage reduce flat buy-and-hold","finite equity curve","equity curve length","same-candle stop/target conservatism"],"conservative_stop_rule":"stop wins if stop and target both touched in one candle"}

def main():
    ap=argparse.ArgumentParser(description="Nobitex historical backtest; research only")
    ap.add_argument("--days",type=int,default=DAYS); ap.add_argument("--resolution",default=RESOLUTION)
    ap.add_argument("--capital",type=float,default=INITIAL_CAPITAL); ap.add_argument("--fee",type=float,default=FEE_RATE)
    ap.add_argument("--slippage",type=float,default=SLIPPAGE_RATE); ap.add_argument("--symbols",nargs="+",default=list(MARKETS))
    ap.add_argument("--report",default=REPORT_FILE); ap.add_argument("--self-test",action="store_true")
    a=ap.parse_args()
    if a.self_test:print(json.dumps(self_test(),indent=2));return 0
    if a.days<30 or a.capital<=0 or a.fee<0 or a.slippage<0:ap.error("days >= 30, capital > 0, costs >= 0 required")
    if a.resolution not in {"1","5","15","30","60","120","240","D"}:ap.error("Unsupported resolution")
    configs=build_configs(); markets={}
    print(f"Unified Backtest {VERSION} | research only | no real orders | {len(configs)} configurations")
    for n,symbol in enumerate(a.symbols):
        try:
            cs=fetch_candles(symbol,a.days,a.resolution)
            per_day=1 if a.resolution=="D" else 1440//int(a.resolution)
            expected=max(1,a.days*per_day); coverage=min(1,len(cs)/expected)
            if coverage<MIN_COVERAGE:raise RuntimeError(f"Insufficient coverage: {coverage:.1%} < {MIN_COVERAGE:.0%}")
            markets[symbol]=run_market(symbol,cs,configs,a.capital,a.fee,a.slippage,a.resolution)
            markets[symbol]["coverage_pct"]=round(coverage*100,3)
            print(symbol,markets[symbol]["walk_forward"])
        except Exception as e:
            markets[symbol]={"error":str(e)};print(f"ERROR {symbol}: {e}")
        if n+1<len(a.symbols):time.sleep(REQUEST_DELAY_SECONDS)
    report={"version":VERSION,"generated_at_utc":dt.datetime.now(dt.timezone.utc).isoformat(),
      "research_only":True,"real_orders_enabled":False,
      "settings":{"days":a.days,"resolution":a.resolution,"initial_capital":a.capital,
      "fee_per_side":a.fee,"slippage_per_side":a.slippage,"symbols":a.symbols,
      "train_days":TRAIN_DAYS,"test_days":TEST_DAYS,"min_training_trades":MIN_TRAIN_TRADES,
      "drawdown_penalty":DRAWDOWN_PENALTY,"config_count":len(configs),"coverage_threshold":MIN_COVERAGE,
      "selection_metric":"training net return minus 0.5 * max drawdown (%)",
      "limitations":["OHLC intrabar order unknown; stop assumed first if stop and target both touched",
      "fresh capital per OOS fold; trailing training candles provide indicator warm-up only",
      "no partial fills, spread model, funding, or exchange outages"]},
      "self_test":self_test(),"markets":markets}
    with open(a.report,"w",encoding="utf-8") as f:json.dump(report,f,ensure_ascii=False,indent=2,allow_nan=False)
    print("Report saved:",a.report)
    return 0 if any("error" not in x for x in markets.values()) else 1

if __name__=="__main__":raise SystemExit(main())
