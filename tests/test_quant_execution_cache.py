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
    assert result['execution_blocked'] and not s.meta('paused')
    assert s.meta('execution_data_blocked')
    assert load_panel(s.meta('current_data_dir')).metadata['end']=='2026-10-13'
    assert load_panel(s.meta('execution_data_dir')).metadata['end']=='2026-10-12'


def _price_hash(old,new):
    from quant_agent.config import digest
    a,b=old.set_index('date'),new.set_index('date');overlap=a.index.intersection(b.index)
    keys=['open','high','low','close']
    return digest({'dates':[int(d) for d in overlap],
        'observed':a.loc[overlap,keys].to_numpy().tolist(),
        'revised':b.loc[overlap,keys].to_numpy().tolist()})


def test_review_only_accepts_exact_bounded_revision_and_keeps_original_prefix():
    old=bars();new=old.copy();new[['open','high','low','close']]*=.9998
    new=pd.concat([new,new.assign(date=20261013)],ignore_index=True)
    frame,audit=append_observed(old,new,reviewed_price_hash=_price_hash(old,new))
    pd.testing.assert_frame_equal(frame.iloc[:1],old)
    assert len(frame)==2 and set(audit[0]['fields'])=={'open','high','low','close'}
    revised=new.copy();revised.loc[0,'open']+=.001
    with pytest.raises(ValueError,match='basis revised'):
        append_observed(old,revised,reviewed_price_hash=_price_hash(old,new))
    split=new.copy();split[['open','high','low','close']]*=.5
    with pytest.raises(ValueError,match='split/basis migration'):
        append_observed(old,split,reviewed_price_hash=_price_hash(old,split))


def test_post_close_finalization_runs_once_after_initial_daily_completion(tmp_path,monkeypatch):
    from dataclasses import replace
    from tests.test_quant_supervisor_mock import rehearsal_store
    from quant_agent.market_data import finalization_due
    from quant_agent.schedule import daily_due
    s=rehearsal_store(tmp_path);s.settings=replace(s.settings,auto_refresh=True)
    s.set_meta('daily_completed:2026-10-13',True)
    monkeypatch.setattr('quant_agent.research_evidence.research_pending',lambda *a:False)
    assert not finalization_due(s,'2026-10-13T20:59Z')
    assert finalization_due(s,'2026-10-13T21:01Z')
    assert daily_due(s,'2026-10-13T21:01Z')
    s.set_meta('data_finalized:2026-10-13',{'data_sha256':'checked'})
    assert not finalization_due(s,'2026-10-13T21:02Z')
    assert not daily_due(s,'2026-10-13T21:02Z')


def test_finalization_only_retires_unobserved_same_session_shadow(tmp_path):
    from tests.test_quant_supervisor_mock import rehearsal_store
    from quant_agent.market_data import retire_provisional_shadows
    import shutil,json
    s=rehearsal_store(tmp_path);records=s.strategies(['paper'])
    # Fixture converts two unused candidates back to Shadow; the paper owner
    # must be retained even when its frozen source has been revised.
    with s.transaction() as db:
        for r in records[:2]:db.execute("UPDATE strategies SET stage='shadow' WHERE id=?",(r['id'],))
        db.execute('INSERT INTO shadow VALUES (?,?,?,?,?)',(records[1]['id'],'2026-10-12','test','{}','hash'))
    revised=tmp_path/'finalized';shutil.copytree(s.settings.data_dir,revised)
    frame=pd.read_pickle(revised/'AAA.pkl');frame.loc[len(frame)-1,'close']*=.999;frame.to_pickle(revised/'AAA.pkl')
    retire_provisional_shadows(s,revised)
    actual={r['id']:r for r in s.strategies()}
    assert actual[records[0]['id']]['stage']=='rejected'
    assert actual[records[1]['id']]['stage']=='shadow'
    assert actual[records[2]['id']]['stage']=='paper'
    assert actual[records[0]['id']]['evidence']['seed_hash']==records[0]['evidence']['seed_hash']


def test_inherited_review_cannot_approve_a_new_tail_or_changed_old_revision(tmp_path):
    import json
    from types import SimpleNamespace
    from quant_agent.execution_cache import inherited_review,price_revision_hash
    source=tmp_path/'source';latest=tmp_path/'latest';source.mkdir();latest.mkdir()
    old=bars();corrected=old.copy();corrected[['open','high','low','close']]*=.9998
    observed=pd.concat([old,old.assign(date=20261013)],ignore_index=True)
    vendor=pd.concat([corrected,old.assign(date=20261013),old.assign(date=20261014)],ignore_index=True)
    observed.to_pickle(source/'AAA.pkl');vendor.to_pickle(latest/'AAA.pkl')
    review={'source_sha256':'old','research_sha256':'first','reviewed_at':'reviewed','evidence':['readback'],
        'regions':{'US.AAA':{'through_date':20261012,'rows':1,'price_hash':price_revision_hash(old,corrected)}}}
    (source/'revision_audit.json').write_text(json.dumps({'price_review':review}))
    prev=SimpleNamespace(metadata={'input_sha256':'next-source'});new=SimpleNamespace(metadata={'input_sha256':'next-research'})
    inherited=inherited_review(source,latest,prev,new)
    assert inherited['source_sha256']=='next-source' and inherited['research_sha256']=='next-research'
    append_observed(observed,vendor,reviewed_price_hash=inherited['symbols']['US.AAA'])
    changed=vendor.copy();changed.loc[1,'high']+=.01;changed.to_pickle(latest/'AAA.pkl')
    assert inherited_review(source,latest,prev,new) is None
    changed=vendor.copy();changed.loc[0,'low']+=.01;changed.to_pickle(latest/'AAA.pkl')
    assert inherited_review(source,latest,prev,new) is None


def test_transient_read_failure_repairs_incrementally_without_full_reload(monkeypatch):
    from types import SimpleNamespace
    from quant_agent import market_data
    old=bars();new=pd.concat([old,old.assign(date=20261013)],ignore_index=True)
    calls=[]
    def request(*a):calls.append(a);return {'data':{'kline_list':new.to_dict('records')}}
    def forbidden(*a):raise AssertionError('Transient read should not reload years of data')
    monkeypatch.setattr(market_data,'full_symbol',forbidden)
    result,mode=market_data.repair_symbol(SimpleNamespace(request=request),'US.AAA',old,'2026-10-13','ReadUnavailable')
    assert mode=='incremental_retry' and len(result)==2 and len(calls)==1


def test_reviewed_uniform_cash_adjustment_uses_recent_reference_not_ancient_low():
    old=pd.concat([bars()*1,bars().assign(date=20261013)],ignore_index=True)
    old.loc[0,['open','high','low','close']]/=4
    new=old.copy();new[['open','high','low','close']]-=5
    # 5/744 <1% today, but 5/185 >1% of the oldest low.
    frame,audit=append_observed(old,new,reviewed_price_hash=_price_hash(old,new))
    pd.testing.assert_frame_equal(frame,old)
    assert audit
    new.loc[0,'open']+=.1
    with pytest.raises(ValueError,match='split/basis migration'):
        append_observed(old,new,reviewed_price_hash=_price_hash(old,new))


def test_reviewed_raw_append_bridges_adjusted_bar_without_rewriting_history(tmp_path):
    import shutil
    from pathlib import Path
    from tests.test_quant_supervisor_mock import rehearsal_store
    from quant_agent.execution_cache import prepare_execution_cache,price_revision_hash
    from research.daily_factor_lab.core import load_panel
    from research.daily_factor_lab.paper import data_prefix_hash
    s=rehearsal_store(tmp_path);source=Path(s.settings.data_dir);newdir=tmp_path/'vendor';shutil.copytree(source,newdir)
    raw=None
    for file in newdir.glob('*.pkl'):
        old=pd.read_pickle(file);latest=old.iloc[[-1]].copy();latest['date']=20261013
        frame=pd.concat([old,latest],ignore_index=True)
        if file.stem=='AAA':
            raw={k:float(latest.iloc[0][k]) for k in ['open','high','low','close']};raw['date']=20261013
            frame[['open','high','low','close']]-=.5
        frame.to_pickle(file)
    previous,updated=load_panel(source),load_panel(newdir)
    review={'source_sha256':previous.metadata['input_sha256'],'research_sha256':updated.metadata['input_sha256'],
        'reviewed_at':'reviewed','evidence':['raw REST corroboration'],
        'symbols':{'US.AAA':price_revision_hash(pd.read_pickle(source/'AAA.pkl'),pd.read_pickle(newdir/'AAA.pkl'))},
        'append_ohlc':{'US.AAA':[{**raw,'close':raw['close']+.1}]}}
    with pytest.raises(ValueError,match='differs from reviewed affine'):
        prepare_execution_cache(s,newdir,review=review)
    review['append_ohlc']['US.AAA']=[raw]
    directory,panel=prepare_execution_cache(s,newdir,review=review)
    result=pd.read_pickle(directory/'AAA.pkl');old=pd.read_pickle(source/'AAA.pkl')
    pd.testing.assert_frame_equal(result.iloc[:len(old)],old)
    assert result.iloc[-1]['close']==pytest.approx(raw['close'])
    for record in s.strategies(['paper']):
        assert data_prefix_hash(panel,record['evidence']['seed_asof'])==record['evidence']['seed_hash']


def test_research_step_completion_is_bound_to_data_version_not_only_date(tmp_path):
    from tests.test_quant_supervisor_mock import rehearsal_store
    from quant_agent.research_evidence import research_summary
    s=rehearsal_store(tmp_path);s.set_meta('current_data_dir',s.settings.data_dir)
    s.set_meta('task:review',{'asof':'2026-10-12','status':'completed','data_dir':'old-version'})
    assert next(r for r in research_summary(s,'2026-10-12')['steps'] if r['key']=='review')['status']=='pending'
    s.set_meta('task:review',{'asof':'2026-10-12','status':'completed','data_dir':s.settings.data_dir})
    assert next(r for r in research_summary(s,'2026-10-12')['steps'] if r['key']=='review')['status']=='completed'


def test_published_panel_cache_reuses_and_invalidates_without_mutable_arrays(tmp_path):
    from tests.test_quant_supervisor_mock import rehearsal_store
    from quant_agent.panel_cache import load_panel
    s=rehearsal_store(tmp_path);a=load_panel(s.settings.data_dir);b=load_panel(s.settings.data_dir)
    assert a is b and not a.fields['close'].flags.writeable
    from pathlib import Path
    file=Path(s.settings.data_dir)/'AAA.pkl';frame=pd.read_pickle(file);frame['volume']*=1.01;frame.to_pickle(file)
    c=load_panel(s.settings.data_dir)
    assert c is not a and c.metadata['input_sha256']!=a.metadata['input_sha256']
