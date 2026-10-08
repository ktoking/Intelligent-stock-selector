import pandas as pd
from scripts.yahoo_5m_price_audit import compare,aggregate


def test_comparison_detects_missing_price_and_volume_changes():
    ix=pd.date_range('2026-07-13 13:30',periods=3,freq='5min',tz='UTC')
    a=pd.DataFrame({'open':100.,'high':101.,'low':99.,'close':100.,'volume':10.},index=ix)
    b=a.iloc[1:].copy();b.loc[ix[1],'close']=101.;b.loc[ix[2],'volume']=11.
    r=compare(a,b)
    assert r['missing_bars']==1 and r['price_bars_over_1bp']==1
    assert r['volume_changed_bars']==1 and r['price_max_abs_difference_bps']==100.


def test_aggregation_uses_first_open_last_close_and_extremes():
    ix=pd.date_range('2026-07-13 13:30',periods=3,freq='5min',tz='UTC')
    f=pd.DataFrame({'open':[100,101,102],'high':[102,104,103],'low':[99,100,101],'close':[101,102,102.5],'volume':[10,20,30]},index=ix)
    a=aggregate(f).iloc[0]
    assert a.to_dict()=={'open':100.,'high':104.,'low':99.,'close':102.5,'volume':60.}
