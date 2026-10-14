"""Broker ownership, cost basis, multipliers and price freshness in holdings UI."""
import pytest

from quant_agent.holdings import holdings_summary,record_marks
from tests.test_quant_parallel_paper import parallel_store
from quant_agent.paper_activation import activate_batch


def views(s,now='2026-10-12T13:31:30Z',session='2026-10-12'):
    return holdings_summary(s,{'now':now,'session':session})


def fill(s,key,owner,symbol,side,qty,price,bucket='stock',multiplier=1):
    s.reserve(key,owner,bucket,symbol,side,qty,price,'2026-10-12',multiplier=multiplier)
    s.claim(key);s.submitted(key,'broker-'+key);s.fill(key,qty,price,'filled')


def test_same_symbol_different_strategy_basis_and_partial_exit(tmp_path):
    s,ids=parallel_store(tmp_path);activate_batch(s,ids,'paper authorized')
    fill(s,'a-buy',ids[0],'US.AAA','BUY',20,100)
    fill(s,'b-buy',ids[1],'US.AAA','BUY',10,120)
    fill(s,'a-sell',ids[0],'US.AAA','SELL',5,110)
    with s.transaction() as db:db.execute('UPDATE positions SET mark=115')
    result=views(s);groups={g['id']:g for g in result['strategies']}
    a=groups[ids[0]]['positions'][0];b=groups[ids[1]]['positions'][0]
    assert a['qty']==15 and b['qty']==10
    assert a['avg_cost']==pytest.approx(100.3) and b['avg_cost']==pytest.approx(120.36)
    assert a['unrealized_pnl']==pytest.approx(220.5)
    assert b['unrealized_pnl']==pytest.approx(-53.6)
    assert a['weight_pct']==pytest.approx(1725/groups[ids[0]]['nav']*100)
    assert sum(g['unrealized_pnl'] for g in result['strategies'])==pytest.approx(result['stock']['unrealized_pnl'])
    assert groups[ids[2]]['position_count']==0 and groups[ids[2]]['cash']==groups[ids[2]]['budget']
    assert result['option']['position_count']==0
    activity=s.paper_activity()
    assert activity['unrealized_pnl']==pytest.approx(result['stock']['unrealized_pnl'])
    assert activity['realized_pnl']+activity['unrealized_pnl']==pytest.approx(activity['total_pnl'])
    # The display aggregation must not mutate the physical ledger's cost basis.
    assert s.portfolio()['positions']['US.AAA']['basis']!=pytest.approx(a['cost_basis']+b['cost_basis'])


def test_option_unit_cost_and_contract_multiplier(tmp_path):
    s,ids=parallel_store(tmp_path);activate_batch(s,ids,'paper authorized')
    alloc=s.meta('paper_allocations');alloc['option']={ids[0]:1000};s.set_meta('paper_allocations',alloc)
    fill(s,'call',ids[0],'US.NVDA261030C230000','BUY',1,6,'option',100)
    with s.transaction() as db:db.execute("UPDATE positions SET mark=7 WHERE bucket='option'")
    result=views(s);option=result['option']['positions'][0]
    assert option['avg_cost']==pytest.approx(6.018)
    assert option['market_value']==700 and option['unrealized_pnl']==pytest.approx(98.2)
    assert option['weight_pct']==pytest.approx(700/1098.2*100)
    assert result['stock']['position_count']==0 and result['stock']['nav']==10000


def test_recorded_prices_deduplicate_and_stale_quotes_are_labeled(tmp_path):
    s,ids=parallel_store(tmp_path);activate_batch(s,ids,'paper authorized')
    fill(s,'buy',ids[0],'US.AAA','BUY',1,100)
    quotes={'US.AAA':{'last_price':101,'update_time':'2026-10-12 09:31:00','data_date':'2026-10-12'},
            'US.UNHELD':{'last_price':1,'update_time':'2026-10-12 09:31:00'}}
    with s.transaction() as db:
        record_marks(db,quotes,'2026-10-12T13:31:10Z');record_marks(db,quotes,'2026-10-12T13:31:20Z')
        db.execute('UPDATE positions SET mark=101')
        assert db.execute('SELECT COUNT(*) FROM position_marks').fetchone()[0]==1
    row=views(s)['stock']['positions'][0]
    assert row['quote_status']=='fresh' and row['quote_time']=='2026-10-12T13:31:00+00:00'
    assert len(row['series'])==1
    assert views(s,'2026-10-12T13:35:00Z')['stock']['positions'][0]['quote_status']=='stale'
    assert views(s,'2026-10-13T02:00:00Z',None)['stock']['positions'][0]['quote_status']=='closed'


def test_empty_allocated_strategy_is_cash_not_backtest_holdings(tmp_path):
    s,ids=parallel_store(tmp_path);activate_batch(s,ids,'paper authorized')
    s.set_meta('shadow_state:'+ids[0],{'holdings':{'US.FAKE':100}})
    result=views(s)
    assert len(result['strategies'])==3
    assert all(g['positions']==[] and g['exposure_pct']==0 for g in result['strategies'])
    assert result['stock']['cash']==10000 and result['option']['cash']==1000
    from quant_agent.activity import strategy_activity
    from quant_agent.web import chart_data
    activity=strategy_activity(s,ids[0],'paper')
    assert activity['summary']['order_count']==0 and activity['summary']['fill_count']==0
    assert activity['summary']['nav']==s.strategy_portfolio(ids[0])['budget']
    chart=chart_data(s,ids[0],'paper')
    assert chart['initial_capital']==s.strategy_portfolio(ids[0])['budget']
    assert chart['curve']==[]


def test_fill_price_fallback_is_never_called_live_price(tmp_path):
    s,ids=parallel_store(tmp_path);activate_batch(s,ids,'paper authorized')
    fill(s,'buy',ids[0],'US.AAA','BUY',1,100)
    row=views(s)['stock']['positions'][0]
    assert row['mark']==100 and row['quote_status']=='unverified'
    assert row['quote_time'] is None and row['series']==[]


def test_historical_positions_reconcile_selected_date_without_future_trades():
    import numpy as np
    import pandas as pd
    from research.daily_factor_lab.core import Panel
    from quant_agent.activity import ledger_activity
    from quant_agent.holdings import replay_historical_holdings
    curve=[{'date':'2026-10-09','equity':9997.5,'cash':8997.5},
           {'date':'2026-10-12','equity':10084.4,'cash':9436.4}]
    orders=[{'date':'2026-10-09','symbol':'US.AAA','side':'BUY','qty':10,'raw_price':100,'cost':2.5},
            {'date':'2026-10-12','symbol':'US.AAA','side':'SELL','qty':4,'raw_price':110,'cost':1.1}]
    panel=Panel(pd.DatetimeIndex(['2026-10-09','2026-10-12']),['US.AAA'],np.array([True]),{'close':np.array([[100.],[108.]])},{})
    activity=ledger_activity(curve,orders)
    first=replay_historical_holdings(activity,panel,'2026-10-09')
    assert first['positions'][0]['qty']==10 and first['cash']==8997.5
    assert len(first['positions'][0]['series'])==1  # No later prices/orders.
    last=replay_historical_holdings(activity,panel,'2026-10-12')
    row=last['positions'][0]
    assert row['qty']==6 and row['avg_cost']==pytest.approx(100.25)
    assert row['market_value']==648 and row['unrealized_pnl']==pytest.approx(46.5)
    assert row['quote_status']=='historical' and row['mark_date']=='2026-10-12'
    assert last['budget']==10000 and last['nav']==pytest.approx(last['cash']+last['held_value'])
    panel.fields['close'][-1,0]=999
    with pytest.raises(ValueError,match='differ from frozen'):
        replay_historical_holdings(activity,panel,'2026-10-12')


def test_historical_valuation_refuses_missing_frozen_data(tmp_path):
    from quant_agent.holdings import historical_holdings
    from quant_agent.config import canonical
    s,ids=parallel_store(tmp_path)
    assert historical_holdings(s,ids[0])['available'] is False
    directory=tmp_path/'artifacts';directory.mkdir()
    (directory/('activity-'+ids[0]+'.json')).write_text(canonical({'data_hash':'missing-original-data',
        'daily':[{'date':'2026-10-12'}],'trades':[]}))
    result=historical_holdings(s,ids[0],'2026-10-12')
    assert not result['available'] and '原始冻结行情' in result['reason']
    assert s.orders()==[] and s.portfolio()['positions']=={}
