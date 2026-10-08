import pandas as pd
from tests.test_yahoo_5m_factor_lab import synthetic
from scripts.yahoo_5m_regime_lab import regime_features
from scripts.yahoo_5m_morning_robustness import window_features

def test_exact_boundaries_do_not_mutate_inputs():
    fs=regime_features(synthetic());copies={s:f.copy() for s,f in fs.items()}
    # Force signals so the boundary test cannot pass vacuously.
    for f in fs.values():f['range_bounce_volume']=True
    out=window_features(fs,'10:05','11:10')
    for s,f in out.items():
        slot=f.index.tz_convert('America/New_York').strftime('%H:%M')
        assert (f.fixed_morning.to_numpy()==((slot>='10:05')&(slot<='11:10'))).all()
        assert 'fixed_morning' not in fs[s]
        pd.testing.assert_series_equal(copies[s].score,fs[s].score)
