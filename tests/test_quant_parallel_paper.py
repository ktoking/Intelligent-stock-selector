from dataclasses import replace
import pytest

from quant_agent.config import initialize
from quant_agent.store import Store
from quant_agent.paper_activation import activate_batch


def parallel_store(tmp_path):
    s=Store(tmp_path,initialize(tmp_path,parallel_paper=True,paper_enabled=True,quote_push_enabled=False))
    ids=[]
    for name in ['one','two','three']:
        key=s.propose({'policy':{'name':name}})
        for before,after in [('proposed','backtested'),('backtested','validated'),('validated','shadow')]:
            s.transition(key,before,after,{'passed':True,'frozen_rule':'keep'})
        ids.append(key)
    return s,ids


def test_explicit_early_paper_preserves_evidence_and_total_budget(tmp_path):
    s,ids=parallel_store(tmp_path);alloc=activate_batch(s,ids,'user authorized simulations')
    assert sum(alloc.values())==10000 and len(s.strategies(['paper']))==3
    for r in s.strategies():
        assert r['evidence']['frozen_rule']=='keep'
        assert not r['evidence']['paper_authorization']['forward_validation_passed']
    assert s.orders()==[] and s.portfolio('option')['cash']==1000


def test_parallel_cash_reservations_and_owner_positions(tmp_path):
    s,ids=parallel_store(tmp_path);activate_batch(s,ids,'user authorized')
    a,b,_=ids
    with pytest.raises(ValueError,match='allocation'):
        s.reserve('too-much',a,'stock','US.AAA','BUY',40,100,'2026-10-12')
    s.reserve('a-buy',a,'stock','US.AAA','BUY',20,100,'2026-10-12')
    with pytest.raises(ValueError,match='allocation'):
        s.reserve('double',a,'stock','US.BBB','BUY',20,100,'2026-10-12')
    s.fill('a-buy',20,100,'filled')
    s.reserve('b-buy',b,'stock','US.AAA','BUY',10,120,'2026-10-12')
    s.fill('b-buy',10,120,'filled')
    with pytest.raises(ValueError,match='another strategy'):
        s.reserve('steal',b,'stock','US.AAA','SELL',11,120,'2026-10-12')
    assert s.strategy_portfolio(a)['positions']['US.AAA']['qty']==20
    assert s.strategy_portfolio(b)['positions']['US.AAA']['qty']==10
    assert sum(s.strategy_portfolio(k)['cash'] for k in ids)==pytest.approx(s.portfolio()['cash'])
    for k in ids:
        assert s.strategy_portfolio(k)['nav']<=s.strategy_portfolio(k)['budget']+1000
    with pytest.raises(ValueError):
        activate_batch(s,ids,'new allocation')


def test_option_bucket_is_separate_and_contract_multiplier_is_enforced(tmp_path):
    s,ids=parallel_store(tmp_path);activate_batch(s,ids,'user authorized')
    allocation=s.meta('paper_allocations');allocation['option']={ids[0]:1000};s.set_meta('paper_allocations',allocation)
    before=s.portfolio()['cash']
    with pytest.raises(ValueError):
        s.reserve('too-large-option',ids[0],'option','US.NVDA261030C230000','BUY',2,6,'2026-10-12',multiplier=100)
    with pytest.raises(ValueError):
        s.reserve('bad-multiplier',ids[0],'option','US.NVDA261030C230000','BUY',1,6,'2026-10-12',multiplier=1)
    s.reserve('option',ids[0],'option','US.NVDA261030C230000','BUY',1,6,'2026-10-12',multiplier=100)
    s.fill('option',1,6,'filled');s.fill('option',1,6,'filled')
    assert s.portfolio()['cash']==before
    assert s.portfolio('option')['cash']==pytest.approx(398.2)
    assert s.option_activity()['fill_count']==1


def test_option_broker_requires_verified_capacity_and_simulated_path(tmp_path):
    from quant_agent.broker import FutuSim,BrokerError
    settings=replace(initialize(tmp_path),stock_account='simulation-test',paper_enabled=True,parallel_paper=True,option_paper_enabled=True)
    calls=[]
    def transport(method,path,query,body):
        calls.append((method,path,query,body))
        if path.endswith('max-buy-sell'):return {'ret_code':0,'data':{'max_cash_buy_qty_round_lot':'1','max_sell_qty_round_lot':'0'}}
        return {'ret_code':0,'data':{'order_id':'test-option'}}
    b=FutuSim(settings,transport);b.verified=True
    order={'id':'intent','bucket':'option','symbol':'US.NVDA261030C230000','side':'BUY','qty':1,'price':6,'multiplier':100}
    with pytest.raises(BrokerError):b.submit(order)
    assert not calls
    b.option_capacity(order['symbol'],6);assert b.submit(order)=='test-option'
    assert all('/api/v1.0/sim-trade/simulation-test/' in c[1] for c in calls)
    assert calls[-1][3]['symbol']=='NVDA261030C230000' and calls[-1][3]['order_side']==1


def test_option_candidates_reject_missing_prices_and_excess_premium(monkeypatch):
    from quant_agent.option_paper import candidates
    from quant_agent import option_paper
    from quant_agent.config import Settings
    monkeypatch.setattr(option_paper,'executable_quote',lambda q,s:(q['bid_price'],q['ask_price']))
    c={'code':'US.NVDA261030C230000','strike_time':'2026-10-30','stock_owner':'US.NVDA','option_type':'CALL','lot_size':100,'option_standard_type':'STANDARD'}
    q={'option_valid':True,'option_contract_multiplier':100,'option_contract_size':100,'last_price':6,'bid_price':6,'ask_price':6.1,'delta':.45,'option_open_interest':1000}
    sig={'symbol':'US.NVDA','type':'CALL'}
    assert len(candidates([c],{c['code']:q},sig,'2026-10-12',Settings()))==1
    assert candidates([c],{c['code']:q|{'last_price':0}},sig,'2026-10-12',Settings())==[]
    assert candidates([c],{c['code']:q|{'ask_price':10}},sig,'2026-10-12',Settings())==[]
    assert candidates([c],{c['code']:q|{'option_open_interest':1}},sig,'2026-10-12',Settings())==[]


def test_independent_option_agent_does_not_change_stock_strategy_rules(tmp_path):
    from quant_agent.option_paper import activate_option_agent
    settings=replace(initialize(tmp_path,parallel_paper=True,paper_enabled=True),option_paper_enabled=True)
    s=Store(tmp_path,settings)
    key=activate_option_agent(s,'explicit user option simulation authorization')
    assert s.strategies()==[]
    agent=s.meta('option_paper_strategy')
    assert not agent['spec']['historical_option_backtest_verified']
    s.reserve('option-intent',key,'option','US.NVDA261030C230000','BUY',1,6,'2026-10-12',multiplier=100)
    assert len(s.orders())==1 and s.portfolio()['cash']==10000


def test_per_strategy_pnl_uses_owner_cost_not_pooled_account_average(tmp_path):
    s,ids=parallel_store(tmp_path);activate_batch(s,ids,'user authorized')
    a,b,_=ids
    s.reserve('a',a,'stock','US.AAA','BUY',20,100,'2026-10-12');s.fill('a',20,100,'filled')
    s.reserve('b',b,'stock','US.AAA','BUY',10,120,'2026-10-12');s.fill('b',10,120,'filled')
    s.reserve('sell',a,'stock','US.AAA','SELL',10,110,'2026-10-12');s.fill('sell',10,110,'filled')
    from quant_agent.activity import strategy_activity
    activity=strategy_activity(s,a,'paper')
    assert activity['summary']['realized_pnl']==pytest.approx(93.7)
    assert s.strategy_portfolio(a)['realized_pnl']==pytest.approx(93.7)
    assert activity['summary']['nav']==pytest.approx(s.strategy_portfolio(a)['nav'])
    assert sum(s.strategy_portfolio(k)['cash'] for k in ids)==pytest.approx(s.portfolio()['cash'])
