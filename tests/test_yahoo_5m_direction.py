import pandas as pd
from tests.test_yahoo_5m_factor_lab import synthetic
from scripts.yahoo_5m_direction_lab import directional_features

def test_prefix_invariance():
    data=synthetic();pairs={'SOXL':('SMH',1),'TQQQ':('QQQ',-1)}
    full=directional_features(data,pairs)
    for cut in (315,319,340,401):
        partial=directional_features({s:f.iloc[:cut] for s,f in data.items()},pairs)
        for s in pairs:pd.testing.assert_frame_equal(full[s].iloc[:cut],partial[s])

def test_bearish_reference_is_not_bullish_reference():
    data=synthetic()
    bullish=directional_features(data,{'SOXL':('SMH',1)})['SOXL']
    bearish=directional_features(data,{'SOXL':('SMH',-1)})['SOXL']
    for name in ('opening_break_1.0','vwap_cross_1.0'):
        assert not (bullish[name]&bearish[name]).any()
