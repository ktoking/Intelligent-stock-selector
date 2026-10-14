"""Cash, costs and attribution must agree across strategy transaction views."""
import json

import pytest

from quant_agent.activity import ledger_activity, strategy_activity
from quant_agent.config import initialize
from quant_agent.store import Store
from quant_agent.web import public_status
from tests.test_quant_agent import paper


def test_daily_transactions_reconcile_partial_sale_and_open_profit():
    curve=[{'date':'2026-10-09','equity':9997.5,'cash':8997.5},
           {'date':'2026-10-12','equity':10084.4,'cash':9436.4}]
    orders=[{'date':'2026-10-09','symbol':'US.AAA','side':'BUY','qty':10,'raw_price':100,'cost':2.5},
            {'date':'2026-10-12','symbol':'US.AAA','side':'SELL','qty':4,'raw_price':110,'cost':1.1}]
    activity=ledger_activity(curve,orders)
    s=activity['summary']
    assert s['order_count']==2 and s['fees']==pytest.approx(3.6)
    assert s['buy_amount']==1000 and s['sell_amount']==440
    assert s['realized_pnl']==pytest.approx(37.9)
    assert s['unrealized_pnl']==pytest.approx(46.5)
    assert activity['daily'][1]['pnl']==pytest.approx(86.9)
    assert activity['daily'][1]['return_pct']==pytest.approx((10084.4/9997.5-1)*100)
    assert activity['daily'][0]['return_pct']==pytest.approx(-.025)
    with pytest.raises(ValueError,match='cash differs'):
        ledger_activity([{**curve[0],'cash':8998}],orders[:1])


def test_no_historical_material_cannot_create_simulated_fills(tmp_path):
    s=Store(tmp_path,initialize(tmp_path));key=s.propose({'policy':{'name':'one'}})
    a=strategy_activity(s,key,'history')
    assert a['trades']==[] and a['daily']==[] and a['unavailable']
    assert strategy_activity(s,key,'paper')['trades']==[]
    assert strategy_activity(s,key,'paper')['curve']==[]
    assert not s.orders()


def test_shadow_only_includes_sealed_actual_observation_days(tmp_path):
    s=Store(tmp_path,initialize(tmp_path));key=s.propose({'policy':{'name':'one'}})
    order={'date':'2026-10-12','symbol':'US.AAA','side':'BUY','qty':10,'raw_price':100,'cost':2.5}
    s.event('shadow_observed_plan',{'strategy':key,'session':'2026-10-12','orders':[order]})
    s.set_meta('shadow_pending:'+key,{'session':'2026-10-12','orders':[order]})
    assert strategy_activity(s,key,'forward')['trades']==[]
    with s.transaction() as db:
        db.execute('INSERT INTO shadow VALUES (?,?,?,?,?)',(key,'2026-10-12','2026-10-12T20:01Z',json.dumps({'nav':9997.5,'cash':8997.5,'order_count':1}),'hash'))
    a=strategy_activity(s,key,'forward')
    assert len(a['trades'])==1 and a['trades'][0]['evidence_kind']=='forward_shadow'
    assert len(a['daily'])==1 and not s.orders()


def test_paper_transactions_are_filtered_and_mixed_nav_is_not_attributed(tmp_path):
    s=Store(tmp_path,initialize(tmp_path));first=paper(s)
    s.reserve('a',first,'stock','US.AAA','BUY',5,100,'2026-10-12');s.claim('a');s.submitted('a','private-broker-a');s.fill('a',5,100,'filled')
    assert public_status(s)['strategies'][0]['paper_return_pct']==pytest.approx(-.015)
    s.transition(first,'paper','rejected',{})
    second=s.propose({'policy':{'name':'second'}})
    for start,end in [('proposed','backtested'),('backtested','validated'),('validated','shadow'),('shadow','paper')]:s.transition(second,start,end,{})
    s.reserve('b',second,'stock','US.BBB','BUY',1,200,'2026-10-12')
    a=strategy_activity(s,first,'paper')
    assert len(a['orders'])==1 and len(a['trades'])==1
    assert 'broker_id' not in a['orders'][0]
    assert a['trades'][0]['symbol']=='US.AAA'
    assert a['summary']['nav'] is None and a['curve']==[]
    assert '多个策略' in a['limitation']
    assert all(r['paper_return_pct'] is None for r in public_status(s)['strategies'])


def test_paper_daily_profit_uses_each_session_final_observation_before_chart_sampling(tmp_path):
    from datetime import datetime, timedelta, timezone
    s=Store(tmp_path,initialize(tmp_path));key=paper(s)
    s.reserve('a',key,'stock','US.AAA','BUY',1,100,'2026-10-12');s.claim('a');s.submitted('a','private-broker-a');s.fill('a',1,100,'filled')
    rows=[]
    for day,end_nav in [('2026-10-12',10100),('2026-10-13',10150)]:
        start=datetime.fromisoformat(day).replace(hour=13,minute=30,tzinfo=timezone.utc)
        for i in range(1002):
            nav=end_nav if i==1001 else 10000
            rows.append(((start+timedelta(seconds=i)).isoformat(),day,nav,9899.7,nav-10000,0))
    with s.transaction() as db:
        db.executemany('INSERT INTO paper_nav VALUES (?,?,?,?,?,?)',rows)
        db.execute('UPDATE positions SET mark=250.3')
        db.execute("UPDATE fills SET observed='2026-10-13T00:01:00+00:00'")
    a=strategy_activity(s,key,'paper')
    assert len(a['curve'])<2004 and all('session' in r for r in a['curve'])
    assert [r['pnl'] for r in a['daily']]==pytest.approx([100,50])
    assert a['daily'][1]['return_pct']==pytest.approx(50/10100*100)
    assert a['daily'][0]['order_count']==1 and a['daily'][0]['buy_amount']==100
    assert a['summary']['nav']==pytest.approx(10150)
    assert a['summary']['unrealized_pnl']==pytest.approx(150)
    assert a['trades'][0]['session']=='2026-10-12'
