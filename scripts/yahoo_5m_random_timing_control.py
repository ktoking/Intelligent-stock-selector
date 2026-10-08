#!/usr/bin/env python3
"""Conditional timing placebo; does not test date/symbol selection or formal significance."""
import json
import hashlib
from datetime import datetime,timezone
from pathlib import Path
import numpy as np
import pandas as pd
from scripts.yahoo_5m_factor_lab import load_data,simulate
from scripts.yahoo_5m_timing_lab import timing_features


def draw_signals(fs,trades,rng):
    out={s:f.copy() for s,f in fs.items()};chosen=[]
    for f in out.values():f['placebo']=False
    for t in trades.itertuples():
        f=out[t.symbol];local=f.index.tz_convert('America/New_York')
        eligible=(local.strftime('%Y-%m-%d')==t.day)&(local.strftime('%H:%M')>='09:50')&(local.strftime('%H:%M')<='11:25')&f.stop_fraction.notna().to_numpy()
        pool=np.flatnonzero(eligible)
        if not len(pool):raise ValueError('no eligible signal times')
        i=int(rng.choice(pool));f.loc[f.index[i],'placebo']=True
        chosen.append({'day':t.day,'symbol':t.symbol,'signal_bar':str(f.index[i])})
    return out,chosen


def run(output):
    if output.exists():raise FileExistsError(output)
    output.mkdir(parents=True)
    parent=Path('data/factor_lab/20260906_yahoo_5m_timing_v1');source=Path('data/factor_lab/20260906_minute_pilot_v1/public_5m.pkl')
    trades=pd.read_csv(parent/'morning_base_fee7_trades.csv');parts=json.loads((parent/'manifest.json').read_text())['partitions'];days=sum(parts.values(),[])
    assert trades.day.is_unique
    m={'created_at':datetime.now(timezone.utc).isoformat(),'seed':20260906,'draws':128,'fees_bps':[7,15],'slippage_bps':2,'minimum_fee_usd':2,'capital':10000,'risk':.01,'daily_entries':1,'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'trade_schedule_sha256':hashlib.sha256((parent/'morning_base_fee7_trades.csv').read_bytes()).hexdigest(),'parts':parts,'design':'Uniform random signal bar09:50-11:25 on each actual traded day and same symbol; previous-completed-bar ATR at randomized time; all other days flat; same draws paired across fee cases.','interpretation':'Conditional exploratory timing comparison on repeatedly observed history, not a valid significance p-value and not a test of date/symbol selection. Each draw reruns shared cash including minimum fees.','code':{}}
    for file in (Path(__file__),Path('scripts/yahoo_5m_factor_lab.py'),Path('scripts/yahoo_5m_timing_lab.py'),Path('scripts/yahoo_5m_regime_lab.py'),Path('scripts/equity_factor_lab.py')):
        b=file.read_bytes();(output/file.name).write_bytes(b);m['code'][file.name]=hashlib.sha256(b).hexdigest()
    (output/'manifest.json').write_text(json.dumps(m,indent=2))
    data=load_data(source);fs=timing_features(data);rng=np.random.default_rng(m['seed']);results=[];schedules=[]
    baseline={}
    for fee in m['fees_bps']:
        b=simulate(data,fs,{'family':'morning_base','threshold':0,'risk':.01},days,cost_bps=fee,slippage_bps=2,minimum_fee_usd=2,initial_equity=10000,max_daily_entries=1,reward_multiple=None)
        baseline[str(fee)]=b['metrics']
    for draw in range(m['draws']):
        placebo,chosen=draw_signals(fs,trades,rng);schedules.append({'draw':draw,'signals':chosen})
        for fee in m['fees_bps']:
            res=simulate(data,placebo,{'family':'placebo','threshold':0,'risk':.01},days,cost_bps=fee,slippage_bps=2,minimum_fee_usd=2,initial_equity=10000,max_daily_entries=1,reward_multiple=None)
            np.testing.assert_allclose(sum(t['pnl'] for t in res['trades']),res['daily'][-1]['equity']-10000,rtol=0,atol=1e-7)
            results.append({'draw':draw,'fee':fee,**res['metrics']})
        if (draw+1)%32==0:print('Completed',draw+1,flush=True)
    table=pd.DataFrame(results);table.to_csv(output/'draw_metrics.csv',index=False);(output/'schedules.json').write_text(json.dumps(schedules,indent=2))
    summary={}
    for fee in m['fees_bps']:
        group=table[table.fee==fee];x=group.return_pct.to_numpy();actual=baseline[str(fee)]['return_pct']
        summary[str(fee)]={'actual_return_pct':actual,'control_median_pct':float(np.median(x)),'control_5_95_pct':np.quantile(x,[.05,.95]).tolist(),'fraction_controls_at_least_actual':float((x>=actual).mean()),'controls_at_least_actual':int((x>=actual).sum()),'trade_count_range':[int(group.trades.min()),int(group.trades.max())]}
    (output/'report.json').write_text(json.dumps({'manifest':m,'baseline':baseline,'summary':summary,'promotion_passed':False,'execution_enabled':False},indent=2));print(json.dumps(summary,indent=2))

if __name__=='__main__':run(Path('data/factor_lab/20260906_yahoo_5m_random_timing_v1'))
