"""Regression tests for the observed opening pause and overnight job failures."""
from dataclasses import replace
from http.client import RemoteDisconnected
import json

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pandas as pd
import pytest

from quant_agent import runner
from quant_agent.broker import BrokerError, FutuSim, ReadUnavailable, QuoteUnavailable
from quant_agent.config import initialize, canonical
from quant_agent.store import Store


def make_store(tmp_path):
    return Store(tmp_path,initialize(tmp_path,quote_push_enabled=False,evolution_enabled=False))


def test_transport_read_can_retry_but_order_acknowledgement_stays_uncertain(tmp_path,monkeypatch):
    settings=replace(initialize(tmp_path),paper_enabled=True,stock_account='paper-test')
    b=FutuSim(settings);b.verified=True;b.key=Ed25519PrivateKey.generate()
    def disconnect(*a,**kw):raise RemoteDisconnected('socket closed')
    monkeypatch.setattr('urllib.request.urlopen',disconnect)
    with pytest.raises(ReadUnavailable):b.snapshots(['US.NVDA'])
    with pytest.raises(RemoteDisconnected):
        b.request('POST','/api/v1.0/sim-trade/paper-test/orders',body={'test':1})


def test_supervisor_recovers_reads_but_keeps_integrity_pause(tmp_path,monkeypatch):
    s=make_store(tmp_path);calls=[]
    class Stop:
        cycles=0
        def is_set(self):return self.cycles>=3
        def wait(self,_):self.cycles+=1
    monkeypatch.setattr(runner,'activity',lambda s,boot=False:{'mode':'startup_reconcile'})
    monkeypatch.setattr(runner,'daily_due',lambda s:False)
    monkeypatch.setattr(runner,'FutuSim',lambda _:object())
    monkeypatch.setattr('quant_agent.forecast_tracking.update_tracking',lambda s:None)
    def tick(*a,**kw):
        calls.append(1)
        if len(calls)==1:raise ReadUnavailable('temporary read')
    monkeypatch.setattr(runner,'tick',tick);runner.serve(s,Stop())
    assert not s.meta('paused') and len(calls)==3
    assert s.meta('health')['execution_status']=='ready'
    calls.clear()
    def bad(*a,**kw):
        calls.append(1)
        if len(calls)==1:raise BrokerError('Broker positions differ from allocated ledger')
        raise ReadUnavailable('temporary read')
    monkeypatch.setattr(runner,'tick',bad);runner.serve(s,Stop())
    assert s.meta('paused') and s.meta('pause_reason')=='Supervisor failure: BrokerError'


def test_irrelevant_bad_quote_does_not_block_strategy(monkeypatch):
    def executable(q,s,**kw):
        if q.get('invalid'):raise QuoteUnavailable('Spread gate exceeded')
        return (99,101)
    monkeypatch.setattr(runner,'executable_quote',executable)
    quotes={'US.AAA':{},'US.BAD':{'invalid':True}}
    assert runner.quote_prices(quotes,None,{'US.AAA'})=={'US.AAA':100}
    with pytest.raises(QuoteUnavailable,match='US.BAD'):
        runner.quote_prices(quotes,None,{'US.BAD'})
    with pytest.raises(QuoteUnavailable,match='Required quote missing'):
        runner.quote_prices(quotes,None,{'US.MISSING'})


def test_stock_activation_preserves_independent_option_allocation(tmp_path):
    from tests.test_quant_parallel_paper import parallel_store
    from quant_agent.paper_activation import activate_batch
    s,ids=parallel_store(tmp_path)
    s.set_meta('paper_allocations',{'stock':{},'option':{'frozen-option-agent':1000}})
    activate_batch(s,ids,'existing user paper authorization')
    assert s.meta('paper_allocations')['option']=={'frozen-option-agent':1000}


def test_research_subset_cannot_silently_expand_frozen_membership():
    from tests.test_quant_agent import panel
    from quant_agent.dataset import frozen_view
    p=panel();names=p.symbols[:2]
    subset=frozen_view(p,{'evidence':{'symbols':names}})
    assert subset.symbols==names
    assert all(values.shape[1]==len(names) for values in subset.fields.values())


def test_daily_review_bounds_nested_option_curve_and_keeps_evidence(tmp_path,monkeypatch):
    from quant_agent.briefing import review_packet
    from tests.test_quant_agent import panel
    s=make_store(tmp_path);p=panel();p.metadata={'end':'2026-10-09'}
    with s.transaction() as db:
        for i in range(1500):
            db.execute('INSERT INTO option_paper_nav VALUES (?,?,?,?,?,?)',(f't{i:04}','2026-10-09',1000,1000,0,0))
    context={'evidence':[{'id':'tech:US.AAA','symbol':'US.AAA','close':123}],
             'technical_scan':{'candidates':[{'id':'tech:US.AAA','symbol':'US.AAA','close':123}]},
             'headlines':[],'markets':[]}
    s.set_meta('brief_context',context)
    result=review_packet(s,panel=p)
    assert len(result['paper_activity']['option']['curve'])==30
    assert result['brief_context']['evidence']==context['evidence']
    assert s.meta('brief_context')==context
    assert len(canonical(result))<180000


def test_invalid_complete_staging_refetches_instead_of_recopying(tmp_path,monkeypatch):
    from quant_agent import market_data
    s=make_store(tmp_path);source=tmp_path/'source';source.mkdir()
    names=['US.AIFR','US.QQQ']
    (source/'universe.json').write_text(canonical({'symbols':names}))
    (source/'security_types.json').write_text(canonical({n:'ETF' for n in names}))
    old={'date':20261009,'open':10,'high':11,'low':9,'close':10,'volume':100,'turnover':1000}
    new={**old,'date':20261012}
    for n in names:pd.DataFrame([old]).to_pickle(source/(n[3:]+'.pkl'))
    s.set_meta('current_data_dir',str(source))
    staged=tmp_path/'market-data'/'2026-10-12-repaired-1';staged.mkdir(parents=True)
    for name in ('universe.json','security_types.json'):(staged/name).write_text((source/name).read_text())
    pd.DataFrame([old,{**new,'close':12}]).to_pickle(staged/'AIFR.pkl')
    pd.DataFrame([old,new]).to_pickle(staged/'QQQ.pkl')
    (staged/'refresh_report.json').write_text(canonical({'status':'complete_new_version','source_cache':str(source.resolve()),'failures':[]}))
    calls=[]
    class Broker:
        def __init__(self,_):pass
        def request(self,*args):
            calls.append(args);return {'ret_code':0,'data':{'kline_list':[old,new]}}
    monkeypatch.setattr(market_data,'FutuSim',Broker)
    monkeypatch.setattr(market_data,'latest_completed_session',lambda:'2026-10-12')
    meta=market_data.refresh_cache(s)
    assert meta['end']=='2026-10-12' and len(calls)==1
    assert s.meta('current_data_dir')==str(staged)
    assert list((tmp_path/'market-data').iterdir())==[staged]
    assert pd.read_pickle(source/'AIFR.pkl').close.tolist()==[10]
    assert json.loads((staged/'refresh_report.json').read_text())['quality_repairs'][0]['symbol']=='US.AIFR'


@pytest.mark.parametrize('status,kind',[(400,'http_request'),(401,'authentication'),(403,'authentication'),(429,'transient_http'),(502,'transient_http')])
def test_read_http_failure_has_safe_diagnostics_and_blocks_only_current_tick(tmp_path,monkeypatch,status,kind):
    from urllib.error import HTTPError
    settings=replace(initialize(tmp_path),paper_enabled=True,stock_account='private-account')
    b=FutuSim(settings);b.key=Ed25519PrivateKey.generate();b.verified=True
    def fail(*a,**kw):raise HTTPError('https://example.invalid/?secret=PRIVATE',status,'PRIVATE',{},None)
    monkeypatch.setattr('urllib.request.urlopen',fail)
    with pytest.raises(ReadUnavailable) as caught:b.state()
    info=runner.error_evidence(caught.value)
    assert info['http_status']==status and info['read_failure_kind']==kind
    assert info['request_route']=='/api/v1.0/sim-trade/{account}/cash-info'
    assert 'PRIVATE' not in canonical(info) and 'private-account' not in canonical(info)
    from quant_agent.broker import OrderRejected
    with pytest.raises(OrderRejected if status in (400,401,403,429) else HTTPError) as caught:
        b.request('POST','/api/v1.0/sim-trade/private-account/orders',body={})
    assert not isinstance(caught.value,ReadUnavailable)
    assert bool(getattr(caught.value,'submission_rejected',False))==(status in (400,401,403,429))
    info=runner.error_evidence(caught.value)
    assert info['http_status']==status and info['request_method']=='POST'
    assert 'PRIVATE' not in canonical(info)


def test_review_selection_stays_bounded_without_dropping_paper_or_evidence(tmp_path,monkeypatch):
    from quant_agent.briefing import review_packet,REVIEW_SCHEMA,REVIEW_SYSTEM
    from tests.test_quant_agent import panel
    from dataclasses import asdict
    from research.daily_factor_lab.core import catalog
    s=make_store(tmp_path);p=panel();p.metadata={'end':'2026-10-15'}
    metrics={'return_pct':2,'max_drawdown_pct':1,'orders':12,'trading_days':60,'cost_dollars':10,'sharpe_zero_rf':1,'annualized_vol_pct':15}
    strategies=[{'id':str(i),'stage':'paper' if i==0 else 'shadow','spec':{'policy':{'name':'demo'}},
        'evidence':{k:{w:metrics for w in ('train','validation','retrospective_test')} for k in ('stats','stress','delayed')}} for i in range(120)]
    monkeypatch.setattr(s,'strategies',lambda *a:strategies)
    context={'evidence':[{'id':'tech:US.AAA','symbol':'US.AAA','close':123}], 'technical_scan':{'candidates':[]},'headlines':[]}
    s.set_meta('brief_context',context)
    packet=review_packet(s,panel=p)
    assert len(packet['strategies'])<=15 and any(r['id']=='0' for r in packet['strategies'])
    assert packet['strategy_archive']['total']==120
    assert all('train' not in r['evidence']['stress'] for r in packet['strategies'])
    assert all('validation' in r['evidence']['stress'] for r in packet['strategies'])
    assert packet['brief_context']['evidence']==context['evidence']
    prompt=canonical({'instruction':REVIEW_SYSTEM,'schema':REVIEW_SCHEMA,'evidence':packet,'policy_catalog':{k:asdict(v) for k,v in catalog().items()}})
    assert len(prompt)<180000
    assert strategies[0]['evidence']['stress']['train']==metrics


def test_failed_daily_retry_preserves_successful_report(tmp_path,monkeypatch):
    from quant_agent import briefing
    from tests.test_quant_promotion import slot
    from research.daily_factor_lab.core import load_panel
    s,*_=slot(tmp_path);p=load_panel(s.settings.data_dir)
    s.set_meta('universe_scanned:2026-10-12',True);s.set_meta('news_completed:2026-10-12',True)
    monkeypatch.setattr(runner,'latest_completed_session',lambda:'2026-10-12')
    monkeypatch.setattr(briefing,'review_packet',lambda *a,**kw:{'created':'offline','strategies':[],'orders':[],'paused':False,'news':[],'data':p.metadata})
    def fail(*a,**kw):raise ValueError('AI evidence packet too large')
    monkeypatch.setattr(runner,'ask',fail)
    directory=s.home/'reports';directory.mkdir();path=directory/'2026-10-12.md';path.write_text('previous successful review')
    runner.daily_jobs(s,{})
    assert path.read_text()=='previous successful review'
    assert len(list(directory.glob('*-failed.md')))==1
    assert s.meta('task:review')['status']=='failed' and not s.meta('daily_completed:2026-10-12')
