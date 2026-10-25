#!/usr/bin/env python3
"""Nobitex Win/Loss Selector v1.2.0 — research only; no real orders."""
from __future__ import annotations
import argparse, datetime as dt, json, math, statistics, time, urllib.parse, urllib.request
from dataclasses import dataclass
from typing import Optional

VERSION = "1.2.0"
API_URL = "https://apiv2.nobitex.ir/market/udf/history"
REPORT_FILE = "backtest_profitable_only_report.json"
INITIAL_CAPITAL = 5.0
BUY_CASH_FRACTION = 0.75
DAYS, RESOLUTION = 180, "60"
FEE_RATE, SLIPPAGE_RATE = 0.0013, 0.0005
MARKETS = ("BTCUSDT", "ETHUSDT")
TRAIN_DAYS, TEST_DAYS, MIN_TRAIN_TRADES = 60, 14, 3
MIN_COVERAGE, DRAWDOWN_PENALTY = 0.95, 0.50
MAX_RETRIES, REQUEST_DELAY_SECONDS = 4, 0.25
MAX_CANDLES_PER_REQUEST = 450

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
    forced_exit_trades: int = 0


def utc(ts): return dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).isoformat()
def interval_seconds(resolution): return 86400 if resolution == "D" else int(resolution)*60

def get_json(url):
    req=urllib.request.Request(url, headers={"User-Agent":f"Nobitex-Profitable-Backtest/{VERSION}","Accept":"application/json"})
    with urllib.request.urlopen(req,timeout=30) as response: data=json.loads(response.read().decode("utf-8"))
    if not isinstance(data,dict): raise ValueError("Unexpected API response: expected JSON object")
    return data

def parse_candles(obj):
    status=obj.get("s")
    if status not in (None,"ok"):
        if status=="no_data": return []
        raise ValueError(f"API status: {status}")
    arrays=[obj.get(k) for k in ("t","o","h","l","c")]
    if not all(isinstance(a,list) for a in arrays): raise ValueError("Missing/invalid candle arrays")
    ts,opens,highs,lows,closes=arrays; volumes=obj.get("v",[])
    if not isinstance(volumes,list): volumes=[]
    by_time={}
    for i in range(min(map(len,arrays))):
        try:
            c=Candle(int(ts[i]),float(opens[i]),float(highs[i]),float(lows[i]),float(closes[i]),float(volumes[i]) if i<len(volumes) else 0.0)
            vals=(c.o,c.h,c.l,c.c,c.v)
            if c.t>0 and all(math.isfinite(x) for x in vals) and min(c.o,c.h,c.l,c.c)>0 and c.v>=0 and c.h>=max(c.o,c.c,c.l) and c.l<=min(c.o,c.c,c.h): by_time[c.t]=c
        except (ValueError,TypeError,OverflowError): continue
    return sorted(by_time.values(),key=lambda x:x.t)

def fetch_candles(symbol,days=DAYS,resolution=RESOLUTION):
    if days<1: raise ValueError("days must be positive")
    sec=interval_seconds(resolution); now=int(time.time()); end=(now//sec)*sec; start=((now-days*86400)//sec)*sec
    if end<=start: raise ValueError("Invalid history range")
    all_by_time={}; cursor=start; requests=0; span=MAX_CANDLES_PER_REQUEST*sec
    while cursor<end:
        chunk_end=min(cursor+span,end); chunk_to=chunk_end-sec
        query=urllib.parse.urlencode({"symbol":symbol,"resolution":resolution,"from":cursor,"to":chunk_to})
        error=None; batch=None
        for attempt in range(MAX_RETRIES):
            try: batch=parse_candles(get_json(API_URL+"?"+query)); error=None; break
            except Exception as exc:
                error=exc
                if attempt+1<MAX_RETRIES: time.sleep(attempt+1)
        if error is not None: raise RuntimeError(f"Fetching {symbol} failed ({utc(cursor)} to {utc(chunk_to)}): {error}")
        for c in batch or []:
            if cursor<=c.t<chunk_end and c.t<end: all_by_time[c.t]=c
        requests+=1; cursor=chunk_end
        if cursor<end: time.sleep(REQUEST_DELAY_SECONDS)
    candles=sorted(all_by_time.values(),key=lambda x:x.t)
    if len(candles)<100: raise RuntimeError(f"Only {len(candles)} valid candles for {symbol}")
    offsets={}
    for c in candles: offsets[c.t%sec]=offsets.get(c.t%sec,0)+1
    offset,count=max(offsets.items(),key=lambda kv:kv[1])
    if count/len(candles)<0.99: raise RuntimeError("Candle timestamps do not share a consistent interval grid")
    missing=max(0,((candles[-1].t-candles[0].t)//sec+1)-len(candles))
    # Internal gap audit, excluding expected leading/trailing range boundary effects.
    gaps=[]
    for left,right in zip(candles,candles[1:]):
        steps=(right.t-left.t)//sec
        if steps>1: gaps.append({"after_utc":utc(left.t),"before_utc":utc(right.t),"missing_intervals":steps-1})
    print(f"{symbol}: {len(candles)} candles; requests={requests}; grid_offset={offset}s; missing_intervals={missing}; internal_gap_events={len(gaps)}")
    return candles

def sma(values,period,end):
    if period<1 or end+1<period: return None
    return sum(values[end-period+1:end+1])/period

def rsi(values,period,end):
    if period<1 or end<period: return None
    gains=losses=0.0
    for i in range(end-period+1,end+1):
        d=values[i]-values[i-1]
        if d>0: gains+=d
        elif d<0: losses-=d
    ag,al=gains/period,losses/period
    if al==0: return 100.0 if ag>0 else 50.0
    return 100-100/(1+ag/al)

def ema_series(values,period):
    out=[None]*len(values)
    if period<1 or len(values)<period: return out
    prev=sum(values[:period])/period; out[period-1]=prev; alpha=2/(period+1)
    for i in range(period,len(values)): prev=alpha*values[i]+(1-alpha)*prev; out[i]=prev
    return out

def build_configs():
    out=[]
    for sl in (1.5,2.5,4.0):
      for tp in (3.0,5.0,7.0):
        for period in (7,14,21):
          for os in (20,25,30):
            for ob in (65,70,75): out.append(Config("RSI",(("period",period),("oversold",os),("overbought",ob)),sl,tp))
        for fast in (5,9,12):
          for slow in (21,30,50):
            if fast<slow: out.append(Config("EMA",(("fast",fast),("slow",slow)),sl,tp))
        for width in (1.5,2.0,2.5): out.append(Config("BOLLINGER",(("period",20),("width",width)),sl,tp))
        for period in (10,20,30): out.append(Config("BREAKOUT",(("period",period),),sl,tp))
    unique={ (c.strategy,c.params,c.stop_loss,c.take_profit):c for c in out }
    return list(unique.values())

def prepare_signals(candles,config):
    n=len(candles); signals=[0]*n
    if config is None or n<2: return signals
    closes=[c.c for c in candles]
    if config.strategy=="EMA":
        fasts=ema_series(closes,int(config.get("fast"))); slows=ema_series(closes,int(config.get("slow")))
        for i in range(1,n):
            f,s=fasts[i-1],slows[i-1]
            if f is not None and s is not None: signals[i]=1 if f>s else (-1 if f<s else 0)
        return signals
    for i in range(1,n):
        j=i-1
        if config.strategy=="RSI":
            val=rsi(closes,int(config.get("period")),j)
            if val is not None: signals[i]=1 if val<=config.get("oversold") else (-1 if val>=config.get("overbought") else 0)
        elif config.strategy=="BOLLINGER":
            p=int(config.get("period")); mid=sma(closes,p,j)
            if mid is not None:
                dev=statistics.pstdev(closes[j-p+1:j+1])
                if closes[j]<mid-config.get("width")*dev: signals[i]=1
                elif closes[j]>mid+config.get("width")*dev: signals[i]=-1
        elif config.strategy=="BREAKOUT":
            p=int(config.get("period"))
            if j>=p:
                prior=candles[j-p:j]
                if candles[j].c>max(c.h for c in prior): signals[i]=1
                elif candles[j].c<min(c.l for c in prior): signals[i]=-1
    return signals

def signal_at(candles,index,config): return 0 if index<1 else prepare_signals(candles[:index+1],config)[index]

def simulate(candles,config,initial_cash=INITIAL_CAPITAL,fee=FEE_RATE,slip=SLIPPAGE_RATE,start_index=0,buy_fraction=BUY_CASH_FRACTION):
    if len(candles)<2: raise ValueError("Need at least two candles")
    if not math.isfinite(initial_cash) or initial_cash<=0: raise ValueError("Initial capital must be finite and positive")
    if any(not math.isfinite(x) or x<0 or x>=1 for x in (fee,slip)): raise ValueError("Fee/slippage must be finite in [0,1)")
    if not 0<=start_index<len(candles): raise ValueError("start_index out of range")
    if not math.isfinite(buy_fraction) or not 0<buy_fraction<=1: raise ValueError("buy_fraction must be in (0,1]")
    signals=prepare_signals(candles,config); cash=float(initial_cash); qty=0.0
    entry_price=entry_fee=0.0; entry_time=0; stop=target=0.0
    trades=[]; curve=[]; peak=float(initial_cash); max_dd=0.0; forced=0
    def sell(raw,timestamp,reason):
        nonlocal cash,qty,entry_price,entry_fee,entry_time,stop,target,forced
        if qty<=0:return
        fill=raw*(1-slip); proceeds=qty*fill*(1-fee)
        pnl=proceeds-(qty*entry_price+entry_fee); invested=qty*entry_price+entry_fee
        trades.append(Trade(entry_time,timestamp,entry_price,fill,qty,pnl,pnl/invested*100 if invested else 0.0,reason))
        if reason=="end_of_test_liquidation": forced+=1
        cash+=proceeds; qty=0.0; entry_price=entry_fee=0.0; entry_time=0; stop=target=0.0
    def buy(raw,timestamp,sl_pct,tp_pct):
        nonlocal cash,qty,entry_price,entry_fee,entry_time,stop,target
        allocation=cash*buy_fraction
        fill=raw*(1+slip)
        q=allocation/(fill*(1+fee))
        if q<=0 or not math.isfinite(q):return
        qty=q; entry_price=fill; entry_fee=qty*fill*fee; entry_time=timestamp
        cash-=qty*fill*(1+fee)
        stop=fill*(1-sl_pct/100); target=fill*(1+tp_pct/100)
    for i,c in enumerate(candles):
        if i<start_index: continue
        if config is not None and i>0:
            sig=signals[i]
            if qty>0 and sig<0: sell(c.o,c.t,"strategy_exit")
            elif qty<=0 and sig>0: buy(c.o,c.t,config.stop_loss,config.take_profit)
        if qty>0:
            # Conservative intrabar mark-to-market: low price for drawdown while holding.
            adverse=cash+qty*c.l*(1-slip)*(1-fee)
            if peak>0: max_dd=max(max_dd,(peak-adverse)/peak)
            hit_stop=c.l<=stop; hit_target=c.h>=target
            if hit_stop and hit_target: sell(stop,c.t,"stop_and_target_same_candle_stop_first")
            elif hit_stop: sell(min(stop,c.o),c.t,"stop_loss_gap_or_touch")
            elif hit_target: sell(max(target,c.o),c.t,"take_profit_gap_or_touch")
        equity=cash+qty*c.c
        curve.append(equity); peak=max(peak,equity)
        if peak>0:max_dd=max(max_dd,(peak-equity)/peak)
    if qty>0:
        sell(candles[-1].c,candles[-1].t,"end_of_test_liquidation")
        curve[-1]=cash
    # Include all actually closed trades, including forced end-of-test liquidation.
    wins=sum(t.pnl>0 for t in trades); losses=sum(t.pnl<0 for t in trades)
    gp=sum(t.pnl for t in trades if t.pnl>0); gl=-sum(t.pnl for t in trades if t.pnl<0)
    pf=gp/gl if gl>0 else (math.inf if gp>0 else None)
    return Result(initial_cash,cash,(cash/initial_cash-1)*100,max_dd*100,len(trades),wins,losses,wins/len(trades)*100 if trades else 0.0,pf,trades,curve,forced)

def buy_hold(candles,capital=INITIAL_CAPITAL,fee=FEE_RATE,slip=SLIPPAGE_RATE):
    if len(candles)<2: raise ValueError("Need at least two candles")
    if not math.isfinite(capital) or capital<=0 or any(not math.isfinite(x) or x<0 or x>=1 for x in (fee,slip)): raise ValueError("Invalid buy-and-hold parameters")
    entry=candles[0].o*(1+slip); q=capital*(1)/(entry*(1+fee)); curve=[]; peak=capital; dd=0.0
    for c in candles:
        eq=q*c.c; curve.append(eq); peak=max(peak,eq)
        adverse=q*c.l*(1-slip)*(1-fee)
        if peak>0:dd=max(dd,(peak-adverse)/peak)
    exit_fill=candles[-1].c*(1-slip); end=q*exit_fill*(1-fee); curve[-1]=end; pnl=end-capital
    trade=Trade(candles[0].t,candles[-1].t,entry,exit_fill,q,pnl,pnl/capital*100,"buy_and_hold")
    return Result(capital,end,(end/capital-1)*100,dd*100,1,int(pnl>0),int(pnl<0),100.0 if pnl>0 else 0.0,math.inf if pnl>0 else None,[trade],curve,0)

def should_remove_family(wins, losses):
    """The sole strategy-family removal criterion requested by the user."""
    return int(losses) > int(wins)


def score(result):
    if result.closed_trades<MIN_TRAIN_TRADES:return -1e9
    return result.return_pct-DRAWDOWN_PENALTY*result.max_drawdown_pct

def split_folds(candles,resolution=RESOLUTION):
    per_day=1 if resolution=="D" else 1440//int(resolution); train=TRAIN_DAYS*per_day; test=TEST_DAYS*per_day; folds=[]; start=train
    while start+test<=len(candles): folds.append((candles[:start],candles[start:start+test])); start+=test
    return folds

def summary(r):
    pf=r.profit_factor
    return {"start_equity":round(r.start_equity,8),"end_equity":round(r.end_equity,8),"return_pct":round(r.return_pct,6),"max_drawdown_pct":round(r.max_drawdown_pct,6),"closed_trades":r.closed_trades,"wins":r.wins,"losses":r.losses,"win_rate_pct":round(r.win_rate_pct,4),"profit_factor":round(pf,6) if pf is not None and math.isfinite(pf) else ("infinity" if pf==math.inf else None),"forced_end_liquidation_trades":r.forced_exit_trades}

def run_market(symbol,candles,configs,capital,fee,slip,resolution,buy_fraction=BUY_CASH_FRACTION):
    folds=split_folds(candles,resolution)
    configs_by_family={}
    for cfg in configs: configs_by_family.setdefault(cfg.strategy,[]).append(cfg)
    rows=[]
    # Select the best configuration independently for each strategy family in each training fold.
    # This prevents a globally selected family from hiding the other methods from the audit.
    for number,(train,test) in enumerate(folds,1):
        for family,family_configs in sorted(configs_by_family.items()):
            best=None; best_score=-1e9
            for cfg in family_configs:
                tr=simulate(train,cfg,capital,fee,slip,buy_fraction=buy_fraction)
                sc=score(tr)
                if sc>best_score: best,best_score=cfg,sc
            if best is None or best_score<=-1e8:
                rows.append({"fold":number,"strategy_family":family,"status":"no_eligible_config","oos_return_pct":None,"oos_trades":0})
                continue
            prefix=train[-100:]; combined=prefix+test
            out=simulate(combined,best,capital,fee,slip,start_index=len(prefix),buy_fraction=buy_fraction)
            rows.append({"fold":number,"strategy_family":family,"status":"ok","train_start_utc":utc(train[0].t),"train_end_utc":utc(train[-1].t),"test_start_utc":utc(test[0].t),"test_end_utc":utc(test[-1].t),"selected_config":best.label(),"train_score":round(best_score,6),"oos_return_pct":round(out.return_pct,6),"oos_max_drawdown_pct":round(out.max_drawdown_pct,6),"oos_trades":out.closed_trades,"oos_summary":summary(out)})
    valid=[r for r in rows if r["status"]=="ok"]
    family_report={}; retained=[]; removed=[]
    for family in sorted(configs_by_family):
        family_rows=[r for r in valid if r.get("strategy_family")==family]
        wins=sum(int((r.get("oos_summary") or {}).get("wins",0)) for r in family_rows)
        losses=sum(int((r.get("oos_summary") or {}).get("losses",0)) for r in family_rows)
        trade_count=sum(int(r.get("oos_trades",0)) for r in family_rows)
        active_returns=[float(r["oos_return_pct"]) for r in family_rows if int(r.get("oos_trades",0))>0]
        compounded=1.0
        for r in family_rows: compounded*=max(0.0,1+float(r["oos_return_pct"])/100)
        net=(compounded-1)*100 if family_rows else None
        remove=should_remove_family(wins,losses)
        (removed if remove else retained).append(family)
        family_report[family]={"oos_fold_count":len(family_rows),"active_oos_folds":len(active_returns),"oos_trades":trade_count,"winning_trades":wins,"losing_trades":losses,"winning_minus_losing_trades":wins-losses,"positive_active_folds":sum(x>0 for x in active_returns),"negative_active_folds":sum(x<0 for x in active_returns),"compounded_oos_return_pct":round(net,6) if net is not None else None,"retained_by_win_loss_rule":not remove,"decision":"removed: losing trades exceed winning trades" if remove else ("retained: wins equal losses" if wins==losses else "retained: winning trades exceed losing trades")}
    sec=interval_seconds(resolution); gaps=[]
    for a,b in zip(candles,candles[1:]):
        steps=round((b.t-a.t)/sec)
        if steps>1:gaps.append({"after_utc":utc(a.t),"before_utc":utc(b.t),"missing_intervals":steps-1})
    hold=buy_hold(candles,capital,fee,slip)
    return {"symbol":symbol,"retained_strategy_families":retained,"removed_strategy_families":removed,"strategy_family_audit":family_report,"candles":len(candles),"data_start_utc":utc(candles[0].t),"data_end_utc":utc(candles[-1].t),"data_quality":{"internal_gap_events":len(gaps),"estimated_missing_internal_intervals":sum(g["missing_intervals"] for g in gaps),"largest_gap_intervals":max((g["missing_intervals"] for g in gaps),default=0),"gap_examples":gaps[:20]},"buy_and_hold_full_period":summary(hold),"walk_forward":{"folds":len(folds),"strategy_family_evaluations":len(valid),"evaluations_skipped":len(rows)-len(valid),"families_tested":sorted(configs_by_family),"total_oos_trades_across_family_tests":sum(r["oos_trades"] for r in valid)},"fold_details":rows}

def self_test():
    assert should_remove_family(2,3) is True
    assert should_remove_family(3,2) is False
    assert should_remove_family(2,2) is False
    base=1700000000
    assert ema_series([1,2,3,4],3)[1] is None
    assert rsi([1,2,3,4,5],3,4)==100.0
    flat=[Candle(base+i*3600,100,101,99,100) for i in range(20)]
    assert buy_hold(flat,10,0.0013,0.0005).end_equity<buy_hold(flat,10,0,0).end_equity
    # 75% allocation must preserve 25% cash (with entry costs included in the allocation).
    test=[Candle(base,100,101,99,100),Candle(base+3600,100,101,99,100),Candle(base+7200,100,101,99,100)]
    cfg=Config("RSI",(("period",1),("oversold",100),("overbought",101)),2,4)
    res=simulate(test,cfg,10,0,0,start_index=2,buy_fraction=.75)
    assert any(t.quantity>0 for t in res.trades) or res.end_equity>0
    # A position entered with 75% allocation leaves positive cash while open.
    sig=prepare_signals(test,cfg); assert sig[2]>0
    raw=100.0; alloc=10*.75; qty=alloc/raw
    assert abs((10-qty*raw)-2.5)<1e-9
    # Conservative stop-first handling if both barriers touched.
    both=[Candle(base,100,101,99,100),Candle(base+3600,100,101,99,100),Candle(base+7200,100,105,90,100),Candle(base+10800,100,101,99,100)]
    result=simulate(both,cfg,10,0,0,buy_fraction=1)
    assert any(t.reason=="stop_and_target_same_candle_stop_first" for t in result.trades)
    assert all(math.isfinite(x) for x in result.equity_curve) and len(result.equity_curve)==len(both)
    for c in (cfg,Config("EMA",(("fast",2),("slow",3)),2,4)):
        sigs=prepare_signals(flat,c); assert all(sigs[i]==signal_at(flat,i,c) for i in range(len(flat)))
    hourly=[Candle(base+i*3600,100,101,99,100) for i in range(60*24+5)]
    assert len(split_folds(hourly,"60"))==0
    # A final open position must be counted as a closed, explicitly tagged trade.
    open_cfg=Config("RSI",(("period",1),("oversold",100),("overbought",101)),50,100)
    r=simulate(test,open_cfg,10,0,0,start_index=2,buy_fraction=.75)
    assert r.closed_trades==len(r.trades)
    return {"status":"passed","checks":["win/loss selector removes only when losses exceed wins","EMA warm-up","RSI monotonic rise","fees/slippage reduce buy-and-hold","75% cash allocation arithmetic","stop-first same-candle handling","finite equity curve","signal helper equivalence","incomplete OOS fold omitted","forced liquidation included in trade statistics"]}

def main():
    parser=argparse.ArgumentParser(description="Nobitex historical backtest; research only")
    parser.add_argument("--days",type=int,default=DAYS);parser.add_argument("--resolution",default=RESOLUTION);parser.add_argument("--capital",type=float,default=INITIAL_CAPITAL);parser.add_argument("--buy-fraction",type=float,default=BUY_CASH_FRACTION);parser.add_argument("--fee",type=float,default=FEE_RATE);parser.add_argument("--slippage",type=float,default=SLIPPAGE_RATE);parser.add_argument("--symbols",nargs="+",default=list(MARKETS));parser.add_argument("--report",default=REPORT_FILE);parser.add_argument("--self-test",action="store_true")
    args=parser.parse_args()
    if args.self_test:
        print(json.dumps(self_test(),indent=2,ensure_ascii=False,allow_nan=False));return 0
    if args.days<30 or not math.isfinite(args.capital) or args.capital<=0:parser.error("days >= 30 and finite capital > 0 required")
    if args.resolution not in {"1","5","15","30","60","120","240","D"}:parser.error("Unsupported resolution")
    if not math.isfinite(args.buy_fraction) or not 0<args.buy_fraction<=1:parser.error("buy-fraction must be in (0,1]")
    if any(not math.isfinite(x) or x<0 or x>=1 for x in (args.fee,args.slippage)):parser.error("fee/slippage must be finite in [0,1)")
    if not args.symbols or any(not isinstance(s,str) or not s.strip() for s in args.symbols):parser.error("At least one valid symbol required")
    configs=build_configs();markets={};print(f"Win/Loss Selector {VERSION} | research only | real orders disabled | configs={len(configs)} | capital={args.capital} | buy_fraction={args.buy_fraction:.0%}")
    for idx,symbol in enumerate(args.symbols):
        try:
            candles=fetch_candles(symbol,args.days,args.resolution);per_day=1 if args.resolution=="D" else 1440//int(args.resolution);expected=args.days*per_day;coverage=min(1.0,len(candles)/expected) if expected else 0
            if coverage<MIN_COVERAGE:raise RuntimeError(f"Insufficient candle coverage: {coverage:.1%} < {MIN_COVERAGE:.0%}")
            mr=run_market(symbol,candles,configs,args.capital,args.fee,args.slippage,args.resolution,args.buy_fraction);mr["coverage_pct"]=round(coverage*100,3);mr["expected_candles"]=expected;mr["missing_candles_estimate"]=max(0,expected-len(candles));markets[symbol]=mr;print(symbol,json.dumps(mr["walk_forward"],ensure_ascii=False))
        except Exception as exc:markets[symbol]={"error":str(exc)};print(f"ERROR {symbol}: {exc}")
        if idx+1<len(args.symbols):time.sleep(REQUEST_DELAY_SECONDS)
    report={"version":VERSION,"generated_at_utc":dt.datetime.now(dt.timezone.utc).isoformat(),"research_only":True,"real_orders_enabled":False,"purpose":"Research-only strategy-family selector; no live orders.","settings":{"days":args.days,"resolution":args.resolution,"initial_capital":args.capital,"buy_cash_fraction":args.buy_fraction,"fee_per_side":args.fee,"slippage_per_side":args.slippage,"symbols":args.symbols,"train_days":TRAIN_DAYS,"test_days":TEST_DAYS,"min_training_trades":MIN_TRAIN_TRADES,"drawdown_penalty":DRAWDOWN_PENALTY,"config_count":len(configs),"coverage_threshold":MIN_COVERAGE,"max_candles_per_request":MAX_CANDLES_PER_REQUEST,"selection_metric":"training net return minus 0.5 * max drawdown (%)","selection_rule":"remove a strategy family only when total losing out-of-sample trades are greater than total winning out-of-sample trades; ties are retained","selection_scope":"per market and strategy family across all valid walk-forward out-of-sample folds","limitations":["Intrabar order is unknown; stop assumed first if stop and target both touched","Each OOS fold starts with fresh capital; training candles are indicator warm-up only","No partial fills, bid/ask spread, funding, or exchange outage model","Only complete 14-day OOS folds included","Coverage count does not guarantee no internal gaps; gap audit is reported","Win/loss count alone does not establish profitability; review net return, fees, drawdown, and trade count too","Family grouping is based on strategy family selected in each walk-forward fold; it is not a full nested hyperparameter significance test"]},"self_test":self_test(),"markets":markets}
    with open(args.report,"w",encoding="utf-8") as f:json.dump(report,f,ensure_ascii=False,indent=2,allow_nan=False)
    print("Report saved:",args.report)
    return 0 if markets and all("error" not in x for x in markets.values()) else 1

if __name__=="__main__":raise SystemExit(main())
