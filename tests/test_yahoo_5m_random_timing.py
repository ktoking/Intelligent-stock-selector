import numpy as np
import pandas as pd
from tests.test_yahoo_5m_factor_lab import synthetic
from scripts.yahoo_5m_timing_lab import timing_features
from scripts.yahoo_5m_random_timing_control import draw_signals


def test_random_schedule_preserves_symbol_day_and_seed():
    fs=timing_features(synthetic());schedule=pd.DataFrame({'symbol':['SOXL','TQQQ'],'day':['2026-07-13','2026-07-14']})
    a,chosen=draw_signals(fs,schedule,np.random.default_rng(8))
    b,again=draw_signals(fs,schedule,np.random.default_rng(8))
    assert chosen==again
    assert sum(int(f.placebo.sum()) for f in a.values())==2
    for row in chosen:
        local=pd.Timestamp(row['signal_bar']).tz_convert('America/New_York')
        assert str(local.date())==row['day']
        assert '09:50'<=local.strftime('%H:%M')<='11:25'
        assert schedule.loc[schedule.day==row['day'],'symbol'].iloc[0]==row['symbol']
    assert all('placebo' not in f for f in fs.values())
