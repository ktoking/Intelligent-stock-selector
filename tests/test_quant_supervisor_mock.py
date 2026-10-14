"""Actual supervisor + signed REST/JSON over a local fake brokerage."""
from dataclasses import asdict
import json

import pandas as pd
import pytest

from quant_agent.config import initialize
from quant_agent.store import Store
from quant_agent.pipeline import engine_hash
from quant_agent.paper_activation import activate_batch
from quant_agent.option_paper import activate_option_agent
from research.daily_factor_lab.core import load_panel,Policy
from research.daily_factor_lab.paper import data_prefix_hash
from scripts.mock_quant_opening import run_case,Wire
from types import SimpleNamespace
from tests.test_quant_agent import panel


def rehearsal_store(tmp_path):
    data=tmp_path/'data';data.mkdir();p=panel()
    p.dates=pd.bdate_range(end='2026-10-12',periods=len(p.dates))
    for j,symbol in enumerate(p.symbols):
        frame=pd.DataFrame({k:p.fields[k][:,j] for k in ('open','high','low','close','volume','turnover')})
        frame['date']=[int(d.strftime('%Y%m%d')) for d in p.dates]
        frame.to_pickle(data/(symbol[3:]+'.pkl'))
    (data/'universe.json').write_text(json.dumps({'symbols':p.symbols}))
    (data/'security_types.json').write_text(json.dumps({s:'ETF' if s=='US.QQQ' else 'STOCK' for s in p.symbols}))
    p=load_panel(data)
    settings=initialize(tmp_path,parallel_paper=True,paper_enabled=True,option_paper_enabled=True,
        data_dir=str(data),quote_push_enabled=False,evolution_enabled=False)
    store=Store(tmp_path,settings);ids=[]
    for n in range(3):
        policy=Policy('fixture-'+str(n),positions=2,rank_buffer=2,annual_vol_target=1,weight_cap=.4)
        ident=store.propose({'policy':asdict(policy),'engine_sha256':engine_hash(),'factor_weights':[],
            'research_data_sha256':p.metadata['input_sha256']})
        evidence={'passed':True,'symbols':p.symbols,'seed_asof':p.metadata['end'],'seed_hash':data_prefix_hash(p,p.metadata['end'])}
        for before,after in [('proposed','backtested'),('backtested','validated'),('validated','shadow')]:store.transition(ident,before,after,evidence)
        ids.append(ident)
    activate_batch(store,ids,'Offline test fixture only');activate_option_agent(store,'Offline option fixture only')
    return store


@pytest.mark.parametrize('case',['option_eligible','held_option_stale','partial_fill_restart','accepted_ack_lost','invalid_json_recovery'])
def test_real_supervisor_and_signed_http_recover_or_stop_explicitly(tmp_path,case):
    store=rehearsal_store(tmp_path)
    result=run_case(tmp_path,store.settings,case,http_mock=True)
    assert result['result']=='PASS' and result['signed_loopback_http']
    assert result['seed_unchanged'] and result['budgets_checked']


@pytest.mark.parametrize('mode',['original','premarket-age','opening-fresh'])
def test_captured_quote_replay_preserves_provenance_and_gates(tmp_path,mode):
    store=rehearsal_store(tmp_path);p=load_panel(store.settings.data_dir)
    captured_at=pd.Timestamp('2026-10-13T10:00:00Z')
    rows=[]
    for j,s in enumerate(p.symbols):
        close=float(p.fields['close'][-1,j])
        rows.append({'code':s,'last_price':close,'prev_close_price':close*.97,
            'pre_price':close*1.004,'bid_price':close*1.0035,'ask_price':close*1.0045,
            'data_date':'2026-10-13','update_time':int((captured_at-pd.Timedelta(minutes=10)).timestamp()*1000)})
    capture={'source':'TEST captured input','captured_at':captured_at.isoformat(),'snapshots':rows}
    original=json.dumps(capture,sort_keys=True)
    result=run_case(tmp_path,store.settings,'normal',http_mock=True,quote_capture=capture,quote_mode=mode)
    assert result['result']=='PASS'
    assert (result['stock_orders']>0)==(mode=='opening-fresh')
    assert result['option_orders']==0
    assert json.dumps(capture,sort_keys=True)==original
    assert result['quote_input']['mode']==mode
    if mode=='opening-fresh':
        assert any(o['price']>float(p.fields['close'][-1,p.symbols.index(o['symbol'])]) for o in store.orders())


def test_missing_capture_is_not_fabricated_and_mismatched_basis_is_rejected():
    p=panel();clock=SimpleNamespace(now=pd.Timestamp('2026-10-13T13:30:10Z'))
    settings=SimpleNamespace()
    capture={'source':'TEST','captured_at':'2026-10-13T10:00:00Z','snapshots':[]}
    wire=Wire(settings,p,clock,'normal',capture,'opening-fresh')
    assert wire.quote(p.symbols[0]) is None
    capture['snapshots']=[{'code':p.symbols[0],'last_price':float(p.fields['close'][-1,0])*2}]
    with pytest.raises(AssertionError,match='does not match frozen daily basis'):
        Wire(settings,p,clock,'normal',capture,'opening-fresh')
