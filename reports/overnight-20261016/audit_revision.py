"""Read-only OHLC revision audit; never submits orders or modifies caches."""
from pathlib import Path
import json
import numpy as np
import pandas as pd
from quant_agent.config import DEFAULT_HOME,Settings,canonical
from quant_agent.store import Store,utcnow
from quant_agent.broker import FutuSim
from quant_agent.market_data import full_symbol
from quant_agent.execution_cache import append_observed,price_revision_hash
from research.daily_factor_lab.core import load_panel
s=Store(DEFAULT_HOME,Settings.load());src=Path(s.meta('execution_data_dir'));dst=Path(s.meta('current_data_dir'))
a=load_panel(src);b=load_panel(dst);report={'observed_at':utcnow(),'source_cache':str(src),'research_cache':str(dst),'source_sha256':a.metadata['input_sha256'],'research_sha256':b.metadata['input_sha256'],'held':list(s.portfolio()['positions']),'revisions':{},'unchanged_or_small_close':[]}
for symbol in a.symbols:
 old=pd.read_pickle(src/(symbol[3:]+'.pkl'));new=pd.read_pickle(dst/(symbol[3:]+'.pkl'))
 try:append_observed(old,new);report['unchanged_or_small_close'].append(symbol);continue
 except ValueError as exc:reason=str(exc)
 assert symbol not in report['held']
 x=old.set_index('date');y=new.set_index('date').loc[x.index];keys=['open','high','low','close'];xx=x[keys].to_numpy().ravel();yy=y[keys].to_numpy().ravel()
 scale,offset=np.linalg.lstsq(np.column_stack([xx,np.ones(len(xx))]),yy,rcond=None)[0]
 report['revisions'][symbol]={'reason':reason,'price_hash':price_revision_hash(old,new),'rows':len(old),'through_date':int(old.date.max()),'max_relative_change':float(((y[keys]-x[keys])/x[keys]).abs().max().max()),'affine_scale':float(scale),'affine_offset':float(offset),'affine_max_error':float(np.max(np.abs(yy-(scale*xx+offset))))}
 append_observed(old,new,reviewed_price_hash=report['revisions'][symbol]['price_hash'])
 if symbol=='US.ABBV':
  fresh=full_symbol(FutuSim(s.settings),symbol,'2023-09-01','2026-10-15')
  check=fresh.set_index('date')[keys];cache=new.set_index('date')[keys]
  assert set(cache.index)==set(check.index) and np.allclose(cache,check,rtol=1e-9,atol=1e-7)
  report['revisions'][symbol]['full_futunn_adjusted_readback_matches']=True
  fresh.to_json('reports/overnight-20261016/abbv-full-readback.json',orient='records')
recent=json.loads(Path('reports/overnight-20261016/revision-readback.json').read_text())
for symbol in report['revisions']:
 raw=recent[symbol]['0']['data']['kline_list'];adj=recent[symbol]['1']['data']['kline_list']
 old=pd.read_pickle(src/(symbol[3:]+'.pkl')).set_index('date');new=pd.read_pickle(dst/(symbol[3:]+'.pkl')).set_index('date');keys=['open','high','low','close']
 for row in adj:assert np.allclose([row[k] for k in keys],new.loc[row['date'],keys].to_numpy(dtype=float),rtol=1e-9,atol=1e-7)
 last=raw[-1];assert np.allclose([last[k] for k in keys],new.loc[last['date'],keys].to_numpy(dtype=float),rtol=1e-9,atol=1e-7)
 report['revisions'][symbol]['latest_raw_adjusted_match']=True
 if symbol=='US.ABBV':
  for row in raw[:-1]:assert np.allclose([row[k] for k in keys],old.loc[row['date'],keys].to_numpy(dtype=float),rtol=1e-9,atol=1e-7)
  report['revisions'][symbol]['overlap_raw_preserved']=True
Path('reports/overnight-20261016/revision-audit.json').write_text(canonical(report))
print(canonical({k:v for k,v in report.items() if k!='unchanged_or_small_close'}));print('other_symbols_checked',len(report['unchanged_or_small_close']))
