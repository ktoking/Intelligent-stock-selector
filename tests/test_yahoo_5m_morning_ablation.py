import pandas as pd
from tests.test_yahoo_5m_factor_lab import synthetic
from scripts.yahoo_5m_morning_ablation import ablation_features,COMPONENTS


def test_removing_gate_never_removes_full_signals_and_is_causal():
    data=synthetic();full=ablation_features(data);prefix=ablation_features({s:f.iloc[:350] for s,f in data.items()})
    for s,f in full.items():
        for c in COMPONENTS:assert not (f.full&~f['without_'+c]).any()
        assert (f.full==f.neutral_rank).all()
        pd.testing.assert_frame_equal(f.iloc[:350],prefix[s])
