import pandas as pd
from scripts.yahoo_5m_collision_lab import apply_policy,concentration


def test_collision_skip_preserves_later_signal_and_priority_is_explicit():
    fs={'SOXL':pd.DataFrame({'full':[True,True],'score':[4.,2.]}),'TQQQ':pd.DataFrame({'full':[True,False],'score':[1.,3.]})}
    skip=apply_policy(fs,'full','skip_collision')
    assert skip['SOXL'].full.tolist()==[False,True]
    assert not skip['TQQQ'].full.any()
    priority=apply_policy(fs,'full','prefer_TQQQ')
    assert (priority['TQQQ'].score>priority['SOXL'].score).all()
    assert fs['SOXL'].full.tolist()==[True,True]
    assert abs(concentration([{'return':.1},{'return':-.05},{'return':0}])['1']+5)<1e-10
