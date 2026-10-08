import pandas as pd
from tests.test_yahoo_5m_factor_lab import synthetic
from scripts.yahoo_5m_timing_lab import timing_features,bootstrap

def test_timing_masks_partition_and_strength_nested():
    for f in timing_features(synthetic()).values():
        assert (f.all_base==(f.morning_base|f.midday_base|f.afternoon_base)).all()
        assert not (f.all_deep_vwap&~f.all_below_vwap).any()
        assert not (f.all_below_vwap&~f.all_base).any()

def test_prefix_invariance_and_zero_bootstrap():
    data=synthetic();full=timing_features(data)
    partial=timing_features({s:f.iloc[:350] for s,f in data.items()})
    for s in full:pd.testing.assert_frame_equal(full[s].iloc[:350],partial[s])
    assert bootstrap([0.]*36)['return_pct_95_interval']==[0.,0.]
