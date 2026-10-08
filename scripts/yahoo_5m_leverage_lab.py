"""User-authorized leverage research, not live brokerage execution."""
import json,hashlib
from pathlib import Path
from datetime import datetime,timezone
import numpy as np
import pandas as pd
from scripts.yahoo_5m_factor_lab import load_data
from scripts.yahoo_5m_morning_ablation import ablation_features
from scripts.yahoo_5m_morning_execution_audit import reconstruct


def run(output):
    if output.exists():raise FileExistsError(output)
    output.mkdir(parents=True);source=Path('data/factor_lab/20260906_minute_pilot_v1/public_5m.pkl');prior=json.loads(Path('data/factor_lab/20260906_yahoo_5m_morning_ablation_v1/report.json').read_text());parts=prior['manifest']['parts'];days=sum(parts.values(),[])
    m={'created_at':datetime.now(timezone.utc).isoformat(),'rules':['full','without_efficiency'],'multipliers':[1,1.5,2,3],'fees_bps':[7,15],'slippage_bps':2,'minimum_fee_usd':2,'financing_apr':.12,'maintenance_margin':.30,'capital':10000,'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'scope':'Multiply original risk-sized quantity, not returns. Stop price and daily +/-2% pause unchanged. Actual notional/equity may be lower than multiplier. Conservative one full day12% APR charge on borrowed cash per trade; hypothetical30% maintenance barrier, not verified Futu eligibility or margin. Both leverage and original signals researched on observed history.','code':{}}
    for path in (Path(__file__),Path('scripts/yahoo_5m_morning_execution_audit.py'),Path('scripts/yahoo_5m_morning_ablation.py'),Path('scripts/yahoo_5m_regime_lab.py'),Path('scripts/yahoo_5m_factor_lab.py')):
        b=path.read_bytes();(output/path.name).write_bytes(b);m['code'][path.name]=hashlib.sha256(b).hexdigest()
    (output/'manifest.json').write_text(json.dumps(m,indent=2));data=load_data(source);fs=ablation_features(data);rows=[]
    for rule in m['rules']:
        f={s:x.assign(morning_base=x[rule]) for s,x in fs.items()}
        for mult in m['multipliers']:
            for fee in m['fees_bps']:
                res=reconstruct(data,f,days,fee_bps=fee,size_multiplier=mult,maintenance_margin=.3,financing_apr=.12)
                if mult==1:
                    old=next(x['metrics']['return_pct'] for x in prior['rows'] if x['case']==rule and x['fee']==fee)
                    np.testing.assert_allclose(res['metrics']['return_pct'],old,atol=1e-9,rtol=0)
                np.testing.assert_allclose(sum(t['pnl'] for t in res['trades']),res['daily_equity'][-1]-10000,atol=1e-7,rtol=0)
                rows.append({'rule':rule,'multiplier':mult,'fee':fee,'metrics':res['metrics']})
                for kind in ('daily_equity','trades'):pd.DataFrame(res[kind]).to_csv(output/f'{rule}_mult{mult}_fee{fee}_{kind}.csv',index=False)
                print(rule,mult,fee,res['metrics'],flush=True)
    (output/'report.json').write_text(json.dumps({'manifest':m,'rows':rows,'promotion_passed':False,'execution_enabled':False},indent=2))
if __name__=='__main__':run(Path('data/factor_lab/20260906_yahoo_5m_leverage_v1'))
