"""Incomplete-history names: complete-session diagnostics, never price filled or pool selected."""
from pathlib import Path
import json
import pandas as pd
import numpy as np
from scripts.yahoo_5m_regime_lab import regime_features
from scripts.yahoo_5m_factor_lab import simulate
from scripts.watchlist_factor_screen import CONFIGS,P

if __name__=='__main__':
    if (P/'partial_report.json').exists():raise FileExistsError('already complete')
    quality=json.loads((P/'quality.json').read_text());parts=json.loads((P/'screen_manifest.json').read_text())['parts'];ref=pd.read_pickle(P/'raw/QQQ.pkl')[['open','high','low','close','volume']];labels=ref.index.tz_convert('America/New_York').strftime('%Y-%m-%d');rows=[]
    (P/'partial_manifest.json').write_text(json.dumps({'scope':'All45 incomplete-coverage names evaluated only on fully observed sessions. Missing bars remain NaN, never filled. Observability known retrospectively: diagnostic, not tradable filter. Different day counts not directly ranked. No change to train-frozen main pools.','configs':CONFIGS,'parts':parts,'cost_bps':[7,15],'capital':10000,'minimum_fee_usd':2,'slippage_bps':2},indent=2))
    for q in quality:
        if q['status']!='excluded_from_common_window':continue
        s=q['symbol'];raw=pd.read_pickle(P/'raw'/f'{s}.pkl')[['open','high','low','close','volume']].astype(float)
        f=raw.reindex(ref.index);finite=np.isfinite(f.to_numpy()).all(axis=1);valid=finite&(f[['open','high','low','close']]>0).all(axis=1)&(f.volume>=0)&(f.high>=f[['open','low','close']].max(axis=1))&(f.low<=f[['open','high','close']].min(axis=1))
        complete=[d for d in dict.fromkeys(labels) if valid[labels==d].all()]
        data={s:f,'QQQ':ref};fs=regime_features(data,pairs=[(s,'QQQ')]);slot=f.index.tz_convert('America/New_York').strftime('%H:%M');fs[s]['range_morning']=fs[s].range_bounce_volume&(slot<='11:25')
        for cfg in CONFIGS:
            record={'symbol':s,'name':q['name'],'strategy':cfg['name'],'complete_sessions':len(complete),'parts':{}}
            for part,days in parts.items():
                ds=[d for d in days if d in complete];record['parts'][part]={'sessions':len(ds)}
                if not ds:continue
                for fee in (7,15):
                    result=simulate(data,fs,cfg,ds,symbols=[s],initial_equity=10000,cost_bps=fee,minimum_fee_usd=2,slippage_bps=2,max_daily_entries=1,reward_multiple=None)
                    np.testing.assert_allclose(sum(t['pnl'] for t in result['trades']),result['daily'][-1]['equity']-10000,atol=1e-7,rtol=0)
                    record['parts'][part][str(fee)]=result['metrics']
            rows.append(record)
        print(s,len(complete),flush=True)
    (P/'partial_report.json').write_text(json.dumps(rows,indent=2))
