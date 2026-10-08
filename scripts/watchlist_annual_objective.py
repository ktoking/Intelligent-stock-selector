"""Annual-only objective: fixed pools, compare removing daily profit pause."""
import json,hashlib
from pathlib import Path
from datetime import datetime,timezone
import pandas as pd
import numpy as np
from scripts.yahoo_5m_factor_lab import simulate
from scripts.yahoo_5m_regime_lab import regime_features
from scripts.watchlist_factor_screen import CONFIGS


def run(output):
    if output.exists():raise FileExistsError(output)
    output.mkdir(parents=True);parent=Path('data/factor_lab/20260907_watchlist_v1');old=json.loads((parent/'report.json').read_text());parts=old['manifest']['parts'];pools=old['pools'];source=parent/'eligible_data.pkl'
    m={'created_at':datetime.now(timezone.utc).isoformat(),'objective':'Annual40-110%; user removed daily2% target','profit_pause':[.02,None],'loss_pause':-.02,'pools':pools,'parts':parts,'capital':10000,'cost_bps':[7,15],'minimum_fee':2,'slippage_bps':2,'risk':.01,'daily_entries':1,'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'scope':'Frozen train-selected pools; exits compared without reselection. No-profit-pause still has stops and daily loss pause. Same observed short history; not annual proof.','code':{}}
    for path in (Path(__file__),Path('scripts/yahoo_5m_factor_lab.py'),Path('scripts/yahoo_5m_regime_lab.py'),Path('scripts/equity_factor_lab.py')):
        b=path.read_bytes();(output/path.name).write_bytes(b);m['code'][path.name]=hashlib.sha256(b).hexdigest()
    (output/'manifest.json').write_text(json.dumps(m,indent=2));data=pd.read_pickle(source);symbols=list(dict.fromkeys(sum(pools.values(),[])));fs=regime_features(data,pairs=[(s,'QQQ') for s in symbols])
    for f in fs.values():f['range_morning']=f.range_bounce_volume&(f.index.tz_convert('America/New_York').strftime('%H:%M')<='11:25')
    rows=[]
    for cfg in CONFIGS:
        for pause in m['profit_pause']:
            for fee in m['cost_bps']:
                row={'strategy':cfg['name'],'pause':pause,'fee':fee,'parts':{}}
                for part,days in {**parts,'continuous':sum(parts.values(),[])}.items():
                    r=simulate(data,fs,cfg,days,symbols=pools[cfg['name']],initial_equity=10000,cost_bps=fee,slippage_bps=2,minimum_fee_usd=2,max_daily_entries=1,reward_multiple=None,daily_profit_pause=pause)
                    row['parts'][part]=r['metrics']
                    np.testing.assert_allclose(sum(t['pnl'] for t in r['trades']),r['daily'][-1]['equity']-10000,atol=1e-7,rtol=0)
                    if pause==.02 and part=='validation':
                        expected=next(v[str(fee)] for v in old['validation'] if v['strategy']==cfg['name'])
                        for k in expected:
                            if expected[k] is None:assert r['metrics'][k] is None
                            else:np.testing.assert_allclose(r['metrics'][k],expected[k],atol=1e-9,rtol=0)
                    for kind in ('daily','trades'):pd.DataFrame(r[kind]).to_csv(output/f'{cfg["name"]}_pause{pause}_fee{fee}_{part}_{kind}.csv',index=False)
                rows.append(row);print(cfg['name'],pause,fee,{p:round(v['return_pct'],3) for p,v in row['parts'].items()},flush=True)
    (output/'report.json').write_text(json.dumps({'manifest':m,'rows':rows,'previous_validation_parity':True,'promotion_passed':False,'execution_enabled':False},indent=2))
if __name__=='__main__':run(Path('data/factor_lab/20260907_annual_objective_v2'))
