"""Broad watchlist discovery: train-only selection then shared-account evaluation."""
from pathlib import Path
from datetime import datetime,timezone
import json,hashlib
import pandas as pd
import numpy as np
from scripts.yahoo_5m_factor_lab import simulate
from scripts.yahoo_5m_regime_lab import regime_features
P=Path('data/factor_lab/20260907_watchlist_v1')
CONFIGS=[{'name':n,'family':n,'threshold':0,'risk':.01} for n in ('range_morning','vwap_reclaim_volume','trend_retest_volume')]


def prepare():
    universe=json.loads((P/'universe.json').read_text());reference=pd.read_pickle(P/'raw/QQQ.pkl');expected=reference.index;data={};quality=[]
    for u in universe:
        s=u['symbol'];meta=json.loads((P/'raw'/f'{s}.json').read_text());q={**u,'provider_name':meta.get('metadata',{}).get('longName'),'status':meta['status']}
        if meta['status']!='downloaded':q['reason']=meta.get('error');quality.append(q);continue
        f=pd.read_pickle(P/'raw'/f'{s}.pkl')[['open','high','low','close','volume']].astype(float)
        missing=expected.difference(f.index);extra=f.index.difference(expected)
        valid=np.isfinite(f.to_numpy()).all() and (f[['open','high','low','close']]>0).all().all() and (f.volume>=0).all() and (f.high>=f[['open','close','low']].max(axis=1)).all() and (f.low<=f[['open','close','high']].min(axis=1)).all()
        q.update(bars=len(f),missing=len(missing),extra=len(extra),first=str(f.index[0]),last=str(f.index[-1]))
        if len(missing) or len(extra) or f.index.has_duplicates or not valid:q.update(status='excluded_from_common_window',reason='incomplete regular-session grid or invalid OHLCV; no price filling')
        else:q['status']='eligible';data[s]=f
        quality.append(q)
    (P/'quality.json').write_text(json.dumps(quality,indent=2,ensure_ascii=False));pd.DataFrame(quality).to_csv(P/'coverage.csv',index=False);pd.to_pickle(data,P/'eligible_data.pkl');return data,quality


def run():
    if (P/'screen_manifest.json').exists():raise FileExistsError('screen already started')
    parts=json.loads(Path('data/factor_lab/20260906_yahoo_5m_timing_v1/manifest.json').read_text())['partitions']
    m={'created_at':datetime.now(timezone.utc).isoformat(),'configs':CONFIGS,'parts':parts,'initial_equity':10000,'cost_bps':[7,15],'minimum_fee':2,'slippage_bps':2,'daily_entries':1,'target':None,'reference':'QQQ common broad-market reference for every instrument; no sector tuning','selection':'For each strategy, require >=5 train trades and positive train return at both fees; pick up to5 symbols by train stressed return, tie ticker. Freeze pools before validation. Choose strategy by validation stressed return only if >=3 validation trades and positive at both fees. Inspect final only after selection.','history':'Current user-supplied watchlist introduces selection/survivorship bias. Dates previously observed in other studies; final is chronological diagnostic, not untouched history. New listings/incomplete grids retained in coverage, excluded from common-window ranking.','code':{}}
    for file in (Path(__file__),Path('scripts/yahoo_5m_factor_lab.py'),Path('scripts/yahoo_5m_regime_lab.py'),Path('scripts/equity_factor_lab.py')):
        b=file.read_bytes();(P/('snapshot_'+file.name)).write_bytes(b);m['code'][file.name]=hashlib.sha256(b).hexdigest()
    (P/'screen_manifest.json').write_text(json.dumps(m,indent=2))
    data,quality=prepare();fs=regime_features(data,pairs=[(s,'QQQ') for s in data]);rows=[]
    for f in fs.values():
        slot=f.index.tz_convert('America/New_York').strftime('%H:%M');f['range_morning']=f.range_bounce_volume&(slot<='11:25')
    def bt(cfg,symbols,days,fee):return simulate(data,fs,cfg,days,symbols=symbols,initial_equity=10000,cost_bps=fee,minimum_fee_usd=2,slippage_bps=2,max_daily_entries=1,reward_multiple=None)
    for n,s in enumerate(data,1):
        for cfg in CONFIGS:
            a=bt(cfg,[s],parts['train'],7)['metrics'];b=bt(cfg,[s],parts['train'],15)['metrics']
            rows.append({'symbol':s,'strategy':cfg['name'],'train':a,'train_stress':b})
        if n%25==0:print('training',n,'/',len(data),flush=True)
    (P/'training_board.json').write_text(json.dumps(rows,indent=2));pools={}
    for cfg in CONFIGS:
        eligible=[x for x in rows if x['strategy']==cfg['name'] and x['train']['trades']>=5 and x['train']['return_pct']>0 and x['train_stress']['return_pct']>0]
        pools[cfg['name']]=[x['symbol'] for x in sorted(eligible,key=lambda x:(-x['train_stress']['return_pct'],x['symbol']))[:5]]
    (P/'pools_before_validation.json').write_text(json.dumps(pools,indent=2));print('frozen pools',pools,flush=True)
    validation=[]
    for cfg in CONFIGS:
        pool=pools[cfg['name']]
        if not pool:continue
        row={'strategy':cfg['name'],'symbols':pool}
        for fee in (7,15):
            res=bt(cfg,pool,parts['validation'],fee);row[str(fee)]=res['metrics']
            for k in ('daily','trades'):pd.DataFrame(res[k]).to_csv(P/f'{cfg["name"]}_validation_fee{fee}_{k}.csv',index=False)
        validation.append(row)
    valid=[x for x in validation if x['7']['trades']>=3 and x['7']['return_pct']>0 and x['15']['return_pct']>0]
    selected=max(valid,key=lambda x:(x['15']['return_pct'],x['strategy'])) if valid else None
    (P/'selection_before_tail.json').write_text(json.dumps({'validation':validation,'selected':selected},indent=2))
    tail={}
    if selected:
        cfg=next(c for c in CONFIGS if c['name']==selected['strategy'])
        for fee in (7,15):
            res=bt(cfg,selected['symbols'],parts['final'],fee);tail[str(fee)]=res['metrics']
            for k in ('daily','trades'):pd.DataFrame(res[k]).to_csv(P/f'selected_tail_fee{fee}_{k}.csv',index=False)
    # A diagnostic table for every train-frozen finalist, not for post-hoc reselection.
    finalists=[]
    for cfg in CONFIGS:
        for s in pools[cfg['name']]:
            v=bt(cfg,[s],parts['validation'],7)['metrics'];t=bt(cfg,[s],parts['final'],7)['metrics']
            a=next(x for x in rows if x['symbol']==s and x['strategy']==cfg['name'])
            finalists.append({'symbol':s,'strategy':cfg['name'],'train_pct':a['train']['return_pct'],'train_trades':a['train']['trades'],'train_stress_pct':a['train_stress']['return_pct'],'validation_pct':v['return_pct'],'validation_trades':v['trades'],'tail_pct':t['return_pct'],'tail_trades':t['trades']})
    pd.DataFrame(finalists).to_csv(P/'finalists.csv',index=False)
    (P/'report.json').write_text(json.dumps({'manifest':m,'universe_count':len(quality),'eligible_count':len(data),'pools':pools,'validation':validation,'selected':selected,'tail':tail,'finalists':finalists,'promotion_passed':False,'execution_enabled':False},indent=2));print('complete',len(data),'selected',selected,'tail',tail,flush=True)
if __name__=='__main__':run()
