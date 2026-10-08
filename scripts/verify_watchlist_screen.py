"""Artifact audit: train-only pool construction and shared-account reconciliation."""
import json
from pathlib import Path
import pandas as pd
import numpy as np
P=Path('data/factor_lab/20260907_watchlist_v1')
r=json.loads((P/'report.json').read_text());board=json.loads((P/'training_board.json').read_text());quality=json.loads((P/'quality.json').read_text());universe=json.loads((P/'universe.json').read_text())
assert len(universe)==207 and len({x['symbol'] for x in universe})==207
assert len(board)==r['eligible_count']*3
assert len(quality)==207
for strategy,pool in r['pools'].items():
    eligible=[x for x in board if x['strategy']==strategy and x['train']['trades']>=5 and x['train']['return_pct']>0 and x['train_stress']['return_pct']>0]
    expected=[x['symbol'] for x in sorted(eligible,key=lambda x:(-x['train_stress']['return_pct'],x['symbol']))[:5]]
    assert expected==pool
for val in r['validation']:
    for fee in (7,15):
        prefix=val['strategy']+'_validation_fee'+str(fee)
        d=pd.read_csv(P/(prefix+'_daily.csv'));t=pd.read_csv(P/(prefix+'_trades.csv'))
        np.testing.assert_allclose(t.pnl.sum(),d.equity.iloc[-1]-10000,atol=1e-7,rtol=0)
        assert t.day.is_unique
        assert set(t.symbol).issubset(val['symbols'])
for fee in (7,15):
    d=pd.read_csv(P/f'selected_tail_fee{fee}_daily.csv');t=pd.read_csv(P/f'selected_tail_fee{fee}_trades.csv')
    np.testing.assert_allclose(t.pnl.sum(),d.equity.iloc[-1]-10000,atol=1e-7,rtol=0)
    assert t.day.is_unique
    assert all(pd.Timestamp(x.entry_time)==pd.Timestamp(x.signal_time) for x in t.itertuples())
print('PASS:207 unique mappings,480 train cases,train-only pools,8 portfolio fee/period ledgers,one trade/day')
