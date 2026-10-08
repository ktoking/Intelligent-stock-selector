#!/usr/bin/env python3
"""Independent one-trade-per-day execution ledger, conditional on frozen signals."""
import json
import hashlib
from datetime import datetime,timezone
from pathlib import Path
import numpy as np
import pandas as pd
from scripts.yahoo_5m_factor_lab import load_data,simulate
from scripts.yahoo_5m_timing_lab import timing_features


def reconstruct(data,fs,days,fee_bps=7,slip_bps=2,delay=0,whole_shares=False,
                size_multiplier=1.,maintenance_margin=.30,financing_apr=0.):
    if size_multiplier<1 or not 0<maintenance_margin<1 or financing_apr<0:
        raise ValueError("invalid leverage assumptions")
    capital=10000.;daily=[];trades=[];bars=[]
    index=data['SOXL'].index;labels=index.tz_convert('America/New_York').strftime('%Y-%m-%d')
    fee=fee_bps/10000;slip=slip_bps/10000
    for day in days:
        if capital<=0:
            daily.append(capital);bars.extend([capital]*sum(labels==day));continue
        start=capital;indices=np.flatnonzero(labels==day);entry=None
        # Locate the first executable morning signal without consuming prior trade output.
        for k in indices[1+delay:]:
            signal_index=k-1-delay
            eligible=[s for s in ('SOXL','TQQQ') if bool(fs[s].morning_base.iloc[signal_index]) and bool(fs[s].entry_window.iloc[signal_index]) and pd.notna(fs[s].stop_fraction.iloc[signal_index]) and fs[s].score.iloc[signal_index]>=0]
            if not eligible:continue
            s=sorted(eligible,key=lambda s:(fs[s].score.iloc[signal_index],s))[-1]
            stop_fraction=fs[s].stop_fraction.iloc[signal_index];limit=data[s].close.iloc[signal_index]*1.03
            qty=min(start/(limit*(1+fee)),max(0,start-2)/limit,start*.01/(limit*stop_fraction))
            qty*=size_multiplier
            if whole_shares:qty=np.floor(qty)
            fill=data[s].open.iloc[k]*(1+slip)
            if qty<=1e-8 or fill>limit:continue
            entry_fee=max(qty*fill*fee,2)
            if (start-entry_fee)/(qty*fill)<maintenance_margin:continue
            entry=(k,s,qty,fill,entry_fee,fill*(1-stop_fraction));break
        if entry is None:
            bars.extend([capital]*len(indices));daily.append(capital);continue
        k,s,qty,buy,buy_fee,stop=entry
        cash=start-qty*buy-buy_fee
        borrowed=max(0.,-cash);financing=borrowed*financing_apr/365
        cash-=financing;pending=False;exit_index=None
        margin_price=max(0.,-cash)/(qty*(1-maintenance_margin))
        barrier=max(stop,margin_price)
        barrier_reason="margin_liquidation" if margin_price>stop else "stop"
        for j in indices:
            if j<k:bars.append(start);continue
            if exit_index is not None:bars.append(capital);continue
            bar=data[s].iloc[j];sell=None
            if pending:sell=bar.open;reason='daily_limit_next_open'
            elif bar.open<=barrier:sell=bar.open;reason='gap_'+barrier_reason
            elif bar.low<=barrier:sell=barrier;reason=barrier_reason
            elif j==indices[-1]:sell=bar.close;reason='session_close'
            if sell is not None:
                sell*=1-slip;sell_fee=max(qty*sell*fee,2);capital=cash+qty*sell-sell_fee;exit_index=j
                trades.append({'symbol':s,'entry_index':int(k),'exit_index':int(j),'qty':float(qty),'entry':float(buy),'exit_price':float(sell),'pnl':float(capital-start),'reason':reason,'borrowed':float(borrowed),'financing':float(financing),'entry_gross_leverage':float(qty*buy/start)})
                bars.append(capital)
            else:
                mark=cash+qty*bar.close;bars.append(mark)
                pending=(mark/start>=1.02 or mark/start<=.98)
        daily.append(capital)
    returns=np.array(daily)/np.r_[10000,daily[:-1]]-1
    curve=np.r_[10000,bars]
    return {'daily_equity':daily,'bar_equity':bars,'trades':trades,'metrics':{'return_pct':(capital/10000-1)*100,'trades':len(trades),'financing_usd':float(sum(t['financing'] for t in trades)),'max_entry_gross_leverage':float(max([t['entry_gross_leverage'] for t in trades],default=0)),'margin_exits':sum('margin' in t['reason'] for t in trades),'worst_day_pct':float(returns.min()*100),'days_ge_2pct':int((returns>=.02).sum()),'max_drawdown_5m_pct':float((1-curve/np.maximum.accumulate(curve)).max()*100)}}


def run(output):
    if output.exists():raise FileExistsError(output)
    output.mkdir(parents=True)
    source=Path('data/factor_lab/20260906_minute_pilot_v1/public_5m.pkl');prior=Path('data/factor_lab/20260906_yahoo_5m_timing_v1/manifest.json')
    parts=json.loads(prior.read_text())['partitions'];days=sum(parts.values(),[])
    cases=[{'name':'baseline','fee_bps':7,'slip_bps':2},{'name':'fee_stress','fee_bps':15,'slip_bps':2},{'name':'slip10','fee_bps':7,'slip_bps':10},{'name':'slip20','fee_bps':7,'slip_bps':20},{'name':'delay_one_bar','fee_bps':7,'slip_bps':2,'delay':1},{'name':'whole_shares','fee_bps':7,'slip_bps':2,'whole_shares':True}]
    m={'created_at':datetime.now(timezone.utc).isoformat(),'cases':cases,'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'partitions':parts,'scope':'Independent execution/cash implementation using same frozen factors; not independent data/factor validation. No optimization. Same previously observed history.','code':{}}
    for file in (Path(__file__),Path('scripts/yahoo_5m_factor_lab.py'),Path('scripts/yahoo_5m_timing_lab.py'),Path('scripts/yahoo_5m_regime_lab.py')):
        b=file.read_bytes();(output/file.name).write_bytes(b);m['code'][file.name]=hashlib.sha256(b).hexdigest()
    (output/'manifest.json').write_text(json.dumps(m,indent=2))
    data=load_data(source);fs=timing_features(data);rows=[]
    for c in cases:
        independent=reconstruct(data,fs,days,**{k:v for k,v in c.items() if k!='name'})
        if c['name'] in ('baseline','fee_stress'):
            production=simulate(data,fs,{'name':'morning_base','family':'morning_base','threshold':0,'risk':.01},days,cost_bps=c['fee_bps'],slippage_bps=c['slip_bps'],initial_equity=10000,minimum_fee_usd=2,max_daily_entries=1,reward_multiple=None)
            np.testing.assert_allclose(independent['daily_equity'],[d['equity'] for d in production['daily']],rtol=0,atol=1e-8)
            np.testing.assert_allclose(independent['bar_equity'],[d['equity'] for d in production['bars']],rtol=0,atol=1e-8)
            assert len(independent['trades'])==len(production['trades'])
            for a,b in zip(independent['trades'],production['trades']):
                for key in ('symbol','entry_index','reason'):assert a[key]==b[key]
                for key in ('qty','entry','exit_price','pnl'):np.testing.assert_allclose(a[key],b[key],rtol=0,atol=1e-8)
                assert str(data['SOXL'].index[a['exit_index']])==b['exit_time']
        row={'case':c,'metrics':independent['metrics']};rows.append(row)
        for name in ('daily_equity','bar_equity','trades'):pd.DataFrame(independent[name]).to_csv(output/f'{c["name"]}_{name}.csv',index=False)
        print(c['name'],row['metrics'],flush=True)
    (output/'report.json').write_text(json.dumps({'manifest':m,'rows':rows,'baseline_and_fee_stress_parity':True,'promotion_passed':False,'execution_enabled':False},indent=2))

if __name__=='__main__':run(Path('data/factor_lab/20260906_yahoo_5m_execution_audit_v1'))
