#!/usr/bin/env python3
"""Frozen morning rule: adjacent boundaries and predefined instrument transfer."""
from pathlib import Path
from datetime import datetime,timezone
import hashlib
import json
import numpy as np
import pandas as pd
from scripts.yahoo_5m_factor_lab import load_data,simulate
from scripts.yahoo_5m_regime_lab import regime_features

PAIRS={'SOXL':'SMH','TQQQ':'QQQ','NVDA':'SMH','AMD':'SMH','TSLA':'QQQ','UPRO':'SPY','QLD':'QQQ'}
WINDOWS={'baseline':('09:50','11:25'),'start_later':('10:05','11:25'),'end_earlier':('09:50','11:10'),'end_later':('09:50','11:40'),'shift_later':('10:05','11:40')}

def window_features(fs,start,end):
    result={s:f.copy() for s,f in fs.items()}
    for f in result.values():
        slots=f.index.tz_convert('America/New_York').strftime('%H:%M')
        f['fixed_morning']=f.range_bounce_volume&(slots>=start)&(slots<=end)
    return result

def run(output):
    if output.exists():raise FileExistsError(output)
    parent=Path('data/factor_lab/20260906_yahoo_5m_transfer_v1');source=parent/'data.pkl'
    quality=json.loads((parent/'data_quality_before_returns.json').read_text())
    if list(PAIRS)!=quality['eligible_trade_symbols']:raise ValueError('predeclared universe changed')
    parts=json.loads(Path('data/factor_lab/20260906_yahoo_5m_timing_v1/manifest.json').read_text())['partitions']
    cases=[{'name':f'boundary_{n}','symbols':['SOXL','TQQQ'],'window':w} for n,w in WINDOWS.items()]
    cases += [{'name':f'single_{s}','symbols':[s],'window':WINDOWS['baseline']} for s in PAIRS]
    cases += [{'name':'expanded_shared','symbols':list(PAIRS),'window':WINDOWS['baseline']}]
    output.mkdir(parents=True)
    m={'created_at':datetime.now(timezone.utc).isoformat(),'cases':cases,'partitions':parts,'pairs':PAIRS,'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'fees_bps':[7,15],'slippage_bps':2,'minimum_fee_usd':2,'capital':10000,'risk':.01,'daily_entries':1,'target':None,'selection':'All predeclared cases reported. No parameter or symbol selection. Previously inspected history; transfer is not new temporal holdout.','excluded_before_returns':quality['invalid_symbols'],'code':{}}
    for file in (Path(__file__),Path('scripts/yahoo_5m_factor_lab.py'),Path('scripts/yahoo_5m_regime_lab.py'),Path('scripts/equity_factor_lab.py')):
        payload=file.read_bytes();(output/file.name).write_bytes(payload);m['code'][file.name]=hashlib.sha256(payload).hexdigest()
    (output/'manifest.json').write_text(json.dumps(m,indent=2))
    data=load_data(source,symbols=list(dict.fromkeys(list(PAIRS)+list(PAIRS.values()))));fs=regime_features(data,pairs=list(PAIRS.items()));rows=[]
    for case in cases:
        f=window_features(fs,*case['window'])
        for fee in m['fees_bps']:
            res=simulate(data,f,{'name':case['name'],'family':'fixed_morning','threshold':0,'risk':.01},sum(parts.values(),[]),symbols=case['symbols'],initial_equity=10000,cost_bps=fee,minimum_fee_usd=2,slippage_bps=2,max_daily_entries=1,reward_multiple=None)
            row={'case':case,'fee':fee,'metrics':res['metrics'],'parts':{}}
            for part,days in parts.items():
                row['parts'][part]=float((np.prod([1+d['return'] for d in res['daily'] if d['date'] in days])-1)*100)
            np.testing.assert_allclose(sum(t['pnl'] for t in res['trades']),res['daily'][-1]['equity']-10000,rtol=0,atol=1e-7)
            for kind in ('daily','trades'):pd.DataFrame(res[kind]).to_csv(output/f'{case["name"]}_fee{fee}_{kind}.csv',index=False)
            rows.append(row);print(case['name'],fee,round(res['metrics']['return_pct'],3),flush=True)
    prior=json.loads(Path('data/factor_lab/20260906_yahoo_5m_timing_v1/report.json').read_text())
    for fee in m['fees_bps']:
        old=next(x for x in prior['results'] if x['config']['name']=='morning_base' and x['fee']==fee)['metrics']
        new=next(x for x in rows if x['case']['name']=='boundary_baseline' and x['fee']==fee)['metrics']
        for key in old:np.testing.assert_allclose(new[key],old[key],atol=1e-9,rtol=0)
    (output/'report.json').write_text(json.dumps({'manifest':m,'results':rows,'baseline_parity':True,'promotion_passed':False,'execution_enabled':False},indent=2,allow_nan=False))

if __name__=='__main__':run(Path('data/factor_lab/20260906_yahoo_5m_morning_robustness_v1'))
