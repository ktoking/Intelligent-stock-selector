"""Evidence boundaries of dynamic short-term research and the private brief."""
from copy import deepcopy

import pytest

from quant_agent.briefing import validate_brief, screen_us, persist_brief
from quant_agent.config import initialize
from quant_agent.store import Store
from tests.test_quant_agent import panel


def response():
    return {'model':'test','result':{'candidates':[],'review':'复盘','factor_ideas':['**因子观察**'], 'brief': {
        'headline':'有证据的观察','key_events':[],'market_temperature':[],'themes':[],
        'us_scan':{k:[] for k in ('bottom','catalyst','strong','avoid')},'cn_hk':[],
        'conclusions':{'watch':[],'avoid':[],'changes':[]},'execution_review':'0成交，无法归因执行质量。','coverage_gaps':['资讯缺失']}}}


def packet():
    return {'brief_context':{'evidence':[{'id':'tech:US.AAA','symbol':'US.AAA'}],
                             'technical_scan':{'candidates':[{'symbol':'US.AAA'}]}},'news':[],
            'universe_scan':{'hits':[]},'paper_activity':{'nav':10000,'fill_count':0}}


def candidate(symbol='US.AAA'):
    return {'symbol':symbol,'reason':'观察','reflection':'未核实','trigger':'站回MA5','invalid':'跌破前低','risk':'缺新闻','evidence_ids':['tech:US.AAA']}


def test_brief_rejects_fabricated_symbols_sources_and_news_catalysts():
    r=response();r['result']['brief']['us_scan']['bottom']=[candidate()]
    validate_brief(r,packet())
    r['result']['brief']['us_scan']['bottom'][0]['symbol']='US.INVENTED'
    with pytest.raises(ValueError,match='candidate'):validate_brief(r,packet())
    r=response();r['result']['brief']['us_scan']['strong']=[candidate()]
    r['result']['brief']['us_scan']['strong'][0]['evidence_ids']=['fake:1']
    with pytest.raises(ValueError,match='evidence'):validate_brief(r,packet())
    r=response();r['result']['brief']['us_scan']['catalyst']=[candidate()]
    with pytest.raises(ValueError,match='news evidence'):validate_brief(r,packet())


def test_technical_scan_uses_dynamic_stock_mask_and_explicit_fundamental_gap(tmp_path):
    s=Store(tmp_path,initialize(tmp_path));p=panel();p.metadata={'end':'2026-10-09','source':'Futunn cached'}
    result=screen_us(s,p)
    assert result['history_stocks']==2  # QQQ is not a stock candidate.
    assert all(r['symbol']!='US.QQQ' for r in result['candidates'])
    assert all('未验证' in r['fundamentals'] for r in result['candidates'])


def test_brief_persists_structured_and_markdown_without_orders(tmp_path):
    s=Store(tmp_path,initialize(tmp_path));r=response()
    r['result']['brief']['key_events']=[{'title':'动态变化','judgment':'只观察','impact':'科技方向','risk':'证据不足','evidence_ids':['tech:US.AAA']}]
    value=persist_brief(s,r,packet(),'2026-10-09')
    assert value['asof']=='2026-10-09' and s.meta('daily_brief')['version']==2
    text=(tmp_path/'reports/2026-10-09.md').read_text()
    assert '## 美股短线扫描' in text and '短期抄底观察' in text and '走坏 / 回避' in text
    assert '**有证据的观察**' in text and '0成交' in text
    assert '**判断**' in text and '**影响方向**' in text and '**来源**' in text
    assert '**judgment**' not in text and '**impact**' not in text
    assert not s.orders()


def test_daily_job_rejects_invalid_brief_and_records_retry_instead_of_persisting_it(tmp_path,monkeypatch):
    from tests.test_quant_agent import daily_fixture
    s=Store(tmp_path,initialize(tmp_path));runner=daily_fixture(s,monkeypatch)
    r=response();r['provider']='codex';r['result']['brief']['us_scan']['catalyst']=[candidate('US.INVENTED')]
    monkeypatch.setattr(runner,'ask',lambda *a,**kw:r)
    runner.daily_jobs(s,{})
    assert not s.meta('daily_brief') and not s.meta('daily_completed:2026-10-09')
    assert s.meta('daily_retry_after:2026-10-09')
    assert s.meta('task:review')['status']=='failed'
    assert not s.orders()


def test_report_capital_labels_follow_evidence_version_not_legacy_constants(tmp_path):
    from quant_agent.briefing import render_brief_markdown
    s=Store(tmp_path,initialize(tmp_path));evidence=packet()
    evidence['capital_authorization']={'stock':900000,'option':100000,'mode':'broker_equity'}
    value=persist_brief(s,response(),evidence,'2026-10-15')
    text=render_brief_markdown(value)
    assert '$900,000' in text and '$100,000' in text
    assert '股票 $10,000 / 期权 $1,000' not in text
    assert value['capital_authorization']==evidence['capital_authorization']
    value.pop('capital_authorization')
    assert '对应版本授权记录' in render_brief_markdown(value)
