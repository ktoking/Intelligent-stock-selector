import pandas as pd
from tests.test_yahoo_5m_factor_lab import synthetic
from scripts.yahoo_5m_morning_ablation import ablation_features
from scripts.yahoo_5m_atr_audit import stop_features


def test_session_warmup_and_entry_factors_unchanged():
    d=synthetic();fs=ablation_features(d);out=stop_features(d,fs,'session_only_14bars')
    for s,f in out.items():
        assert f.stop_fraction.iloc[:13].isna().all()
        assert pd.notna(f.stop_fraction.iloc[13])
        pd.testing.assert_series_equal(f.full,fs[s].full)
        pd.testing.assert_series_equal(f.score,fs[s].score)
    prefix=stop_features({s:f.iloc[:350] for s,f in d.items()},{s:f.iloc[:350] for s,f in fs.items()},'exclude_overnight_gap')
    full=stop_features(d,fs,'exclude_overnight_gap')
    for s in full:pd.testing.assert_frame_equal(full[s].iloc[:350],prefix[s])
