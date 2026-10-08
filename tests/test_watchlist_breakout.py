from pathlib import Path
import pandas as pd
from research.watchlist_breakout import audit,events,simulate


def test_events_use_past_only_on_two_real_series():
    pool=audit()
    selected={key:pool[key] for key in ('US.QQQ','US.TQQQ')}
    full=events(selected,'opening30')
    last='2026-09-14'
    prefix={key:f.loc[:last] for key,f in selected.items()}
    partial=events(prefix,'opening30')
    assert [x for x in full if x[0]<=last]==partial


def test_account_ledger_reconciles():
    pool=audit()
    selected={key:pool[key] for key in ('US.QQQ','US.TQQQ')}
    ev=events(selected,'opening30')
    days=['2026-09-15','2026-09-16','2026-09-17']
    result,trades,daily=simulate(selected,ev,days)
    assert abs(10000+sum(x['pnl'] for x in trades)-daily[-1]['equity'])<1e-6
    assert all(x['entry_time']<x['exit_time'] for x in trades)
    assert all(x['day']==x['entry_time'][:10]==x['exit_time'][:10] for x in trades)
