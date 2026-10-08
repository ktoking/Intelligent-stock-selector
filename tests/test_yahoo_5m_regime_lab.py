import pandas as pd

from scripts.yahoo_5m_regime_lab import regime_features,configs
from tests.test_yahoo_5m_factor_lab import synthetic


def test_regime_and_volume_confirmations_are_prefix_invariant():
    data=synthetic();changed={k:v.copy() for k,v in data.items()}
    for frame in changed.values():
        frame.iloc[400:,:4]*=4
        frame.iloc[400:,4]*=10
    a,b=regime_features(data),regime_features(changed)
    for symbol in a:
        pd.testing.assert_frame_equal(a[symbol].iloc[:400],b[symbol].iloc[:400])


def test_volume_gate_only_removes_events_and_regimes_are_distinct():
    out=regime_features(synthetic())
    for frame in out.values():
        for family in ('vwap_reclaim','oversold_bounce','range_bounce','trend_retest'):
            assert not (frame[family+'_volume']&~frame[family+'_plain']).any()
        assert not (frame.range_bounce_plain&frame.trend_retest_plain).any()
        assert frame.loc[frame.oversold_bounce_plain,'distance_atr'].lt(-1).all()
    assert len(configs())==len({c['name'] for c in configs()})==16
