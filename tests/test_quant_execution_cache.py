import pandas as pd
import pytest

from quant_agent.execution_cache import append_observed
from research.daily_factor_lab.refresh import merge_daily


def bars():
    return pd.DataFrame([{'date':20261012,'open':743.,'high':746.,'low':740.,'close':744.,
        'volume':24642662.,'turnover':18397826899.72}])


def test_vendor_volume_finalization_preserves_execution_prefix_and_updates_research():
    old=bars();revised=old.copy();revised['volume']=25147726.;revised['turnover']=18775323074.
    new=pd.concat([revised,revised.assign(date=20261013)],ignore_index=True)
    execution,audit=append_observed(old,new)
    pd.testing.assert_frame_equal(execution.iloc[:1],old)
    assert len(execution)==2 and audit[0]['fields']==['volume','turnover']
    research=merge_daily(old,new.to_dict('records'),'2026-10-13',allow_flow_revisions=True)
    assert research.iloc[0].volume==25147726. and execution.iloc[0].volume==24642662.


@pytest.mark.parametrize('field',['open','high','low','close'])
def test_price_revision_remains_strict_for_both_datasets(field):
    old=bars();new=old.copy();new[field]*=.5
    with pytest.raises(ValueError,match='basis revised'):append_observed(old,new)
    with pytest.raises(ValueError,match='revised'):
        merge_daily(old,new.to_dict('records'),'2026-10-12',allow_flow_revisions=True)


def test_disappearing_observed_bar_is_not_silently_discarded():
    with pytest.raises(ValueError,match='lost'):
        append_observed(bars(),bars().assign(date=20261013))


def test_small_close_only_correction_is_audited_without_rewriting_observation():
    old=bars();new=old.copy();new['close']=744.01
    execution,audit=append_observed(old,new)
    pd.testing.assert_frame_equal(execution,old)
    assert audit[0]['fields']==['close']
    research=merge_daily(old,new.to_dict('records'),'2026-10-12',allow_flow_revisions=True,allow_close_corrections=True)
    assert research.iloc[0].close==744.01


def test_valid_research_cache_publishes_even_when_execution_basis_needs_review(tmp_path,monkeypatch):
    import json,shutil
    from pathlib import Path
    from types import SimpleNamespace
    from tests.test_quant_supervisor_mock import rehearsal_store
    from quant_agent import market_data
    from research.daily_factor_lab.core import load_panel
    s=rehearsal_store(tmp_path);s.set_meta('current_data_dir',s.settings.data_dir)
    def refresh(source,output,asof,**kwargs):
        source,output=Path(source),Path(output);shutil.copytree(source,output)
        symbols=json.loads((source/'universe.json').read_text())['symbols']
        for symbol in symbols:
            file=output/(symbol[3:]+'.pkl');frame=pd.read_pickle(file)
            latest=frame.iloc[[-1]].copy();latest['date']=20261013
            frame=pd.concat([frame,latest],ignore_index=True)
            if symbol=='US.AAA':frame.loc[0,'open']+=.01
            frame.to_pickle(file)
        (output/'refresh_report.json').write_text(json.dumps({'status':'complete','source_cache':str(source),'failures':[]}))
    monkeypatch.setattr(market_data,'latest_completed_session',lambda:'2026-10-13')
    monkeypatch.setattr(market_data,'refresh',refresh)
    monkeypatch.setattr(market_data,'FutuSim',lambda _:SimpleNamespace(request=lambda *a,**kw:None))
    result=market_data._refresh_cache(s)
    assert result['execution_blocked'] and s.meta('paused')
    assert load_panel(s.meta('current_data_dir')).metadata['end']=='2026-10-13'
    assert load_panel(s.meta('execution_data_dir')).metadata['end']=='2026-10-12'
