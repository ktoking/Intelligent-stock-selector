"""Dynamic, evidence-bound private briefing and execution review, without orders."""
from copy import deepcopy
from datetime import datetime, timezone
import html
import json
from pathlib import Path
import re

import numpy as np
import pandas as pd
import requests

from .ai import SCHEMA, SYSTEM, ask
from .broker import FutuSim
from .config import canonical, digest
from .store import utcnow

REVIEW_VERSION = 2
TEXT = {'type':'string','maxLength':1200}
IDS = {'type':'array','maxItems':8,'items':{'type':'string'}}

def obj(fields):
    return {'type':'object','additionalProperties':False,'required':list(fields),'properties':fields}

def rows(fields, count=5):
    return {'type':'array','maxItems':count,'items':obj(fields)}

OBSERVATION = {'symbol':TEXT,'reason':TEXT,'reflection':TEXT,'trigger':TEXT,'invalid':TEXT,'risk':TEXT,'evidence_ids':IDS}
REVIEW_SCHEMA = deepcopy(SCHEMA)
REVIEW_SCHEMA['properties']['review']['maxLength'] = 16000
REVIEW_SCHEMA['required'].append('brief')
REVIEW_SCHEMA['properties']['brief'] = obj({
    'headline':TEXT,
    'key_events':rows({'title':TEXT,'judgment':TEXT,'impact':TEXT,'risk':TEXT,'evidence_ids':IDS}),
    'market_temperature':rows({'market':TEXT,'judgment':TEXT,'evidence_ids':IDS},12),
    'themes':rows({'direction':TEXT,'strength':TEXT,'catalyst':TEXT,'trigger':TEXT,'invalid':TEXT,'risk':TEXT,'evidence_ids':IDS},3),
    'us_scan':obj({k:rows(OBSERVATION) for k in ('bottom','catalyst','strong','avoid')}),
    'cn_hk':rows({'direction':TEXT,'reason':TEXT,'trigger':TEXT,'invalid':TEXT,'risk':TEXT,'evidence_ids':IDS},5),
    'conclusions':obj({k:{'type':'array','maxItems':5,'items':TEXT} for k in ('watch','avoid','changes')}),
    'execution_review':TEXT,
    'coverage_gaps':{'type':'array','maxItems':12,'items':TEXT}
})
REVIEW_SYSTEM = SYSTEM + '''
生成一份中文每日投资与AI产业私人助理简报。不要把关注范围写死为固定股票或公司；按当天证据动态筛选，优先质量，不凑数。
六大模块：最重要3-5件事；全球市场温度；1-3条主线；美股短线扫描；A股/港股候选观察方向；对用户有用的结论。另写模拟操作复盘和因子待办。
全球关注A股、港股、美股、纳指、标普500、美元、美债收益率、人民币、原油、黄金、宏观数据和政策。只对有证据的变化给判断；ETF代理不能当原始指数、现货或收益率。
行业关注AI算力、半导体、光模块/CPO、PCB、存储、机器人、软件、云计算、AI应用、数据中心电力、能源、医药、高股息和政策方向；方向由当天事实筛选，不要求逐个覆盖。
美股短线四类必须存在，每类0-5个：bottom短期抄底观察；catalyst资讯利好/催化；strong强势延续；avoid走坏回避。symbol只能用证据里的美股代码（含US.前缀）。同类不重复。
每项写入选原因、利好是否已反映(reflection)、观察触发条件(trigger)、失效条件(invalid)、风险点(risk)，使用实际MA5/MA10/支撑/前低/量比作为观察依据。
基本面/财报全文未核实时明确未核实，不能凭反弹或标题断言基本面未破坏、利好未反映或财报超预期。无资讯时catalyst可以为空。
所有具体行情日期、数字、标的、新闻ID仅取输入。引用用evidence_ids，必须存在于brief_context.evidence中。没有可用数据的市场写证据缺失，不凭记忆补近期事实。资讯按available_at使用，不把周末生成时间当行情日期。
A股/港股若缺个股行情，仅列有资讯支持的板块观察方向；不捏造个股和支撑价。宏观背景和美股策略执行时间分开。
复盘基于实际订单、成交增量、费用、每日账本收益；0成交不能编造买卖经验或执行问题。历史/Shadow交易只能作为研究，不能当模拟实绩。提出可反证新因子，保留淘汰理由，不改冻结运行规则。
只输出研究判断，不输出确定性买卖建议。统一用观察、候选、触发条件、失效条件、风险点，禁止稳赚、必涨、强烈买入。brief结构化字段不要写HTML；review可用中文Markdown，重点用**加粗**。candidates返回空数组。
'''


def screen_us(store, panel=None):
    from research.daily_factor_lab.core import load_panel
    from daily_direction.reporting import identity
    panel = panel or load_panel(store.meta('current_data_dir', store.settings.data_dir))
    f, output = panel.fields, []
    for j, symbol in enumerate(panel.symbols):
        if not panel.stock_mask[j]: continue
        c, v = f['close'][:,j], f['volume'][:,j]
        if len(c)<21 or not np.isfinite(c[-21:]).all() or not np.isfinite(v[-21:]).all(): continue
        price, prior = float(c[-1]), float(c[-2])
        name, sector = identity(symbol.removeprefix('US.'))
        ma5, ma10, ma20 = [float(c[-n:].mean()) for n in (5,10,20)]
        prior_ma5 = float(c[-6:-1].mean())
        vr = float(v[-1]/v[-21:-1].mean()) if v[-21:-1].mean()>0 else None
        peak = float(f['high'][-20:,j].max()); low = float(f['low'][-20:-1,j].min())
        row={'id':'tech:'+symbol,'symbol':symbol,'name':name or symbol,'sector':sector or '待分类',
             'data_date':panel.metadata['end'],'close':price,'daily_pct':(price/prior-1)*100,
             'ma5':ma5,'ma10':ma10,'ma20':ma20,'low20_previous':low,'drawdown20_pct':(price/peak-1)*100,
             'volume_ratio':vr,'return5_pct':(price/c[-6]-1)*100,'source':panel.metadata.get('source','Futunn cached OHLCV'),
             'fundamentals':'未验证财报全文与基本面变化，仅为技术候选'}
        row['bottom'] = row['drawdown20_pct']<=-8 and price>=ma5 and prior<=prior_ma5 and row['daily_pct']>0
        row['strong'] = price>ma5>ma10>ma20 and row['daily_pct']>0 and vr is not None and vr>=1.0
        row['avoid'] = price<ma20 and (price<low or row['daily_pct']<=-3) and vr is not None and vr>=1.2
        output.append(row)
    groups={k:sorted([r for r in output if r[k]],key=lambda r: (-abs(r['drawdown20_pct']) if k=='bottom' else -r['return5_pct'] if k=='strong' else r['daily_pct']))[:8] for k in ('bottom','strong','avoid')}
    movers=store.meta('universe_scan',{}).get('hits',[])
    lookup={r['symbol']:r for r in output}
    selected=list({r['symbol']:r for g in groups.values() for r in g}.values())
    selected+= [lookup[r['symbol']] for r in movers[:12] if r['symbol'] in lookup and r['symbol'] not in {x['symbol'] for x in selected}]
    return {'asof':panel.metadata['end'],'history_stocks':len(output),'snapshot_universe':store.meta('universe_scan',{}).get('scanned',0),
            'groups':{k:[r['symbol'] for r in g] for k,g in groups.items()},'candidates':selected,
            'coverage_note':'全池快照与完整日线覆盖分开统计；当前成分/自选含选择偏差；基本面未验证。'}


def global_headlines(now):
    from .news import normalize
    groups, errors = [], []
    for keyword in ('美联储','AI','A股','港股'):
        try:
            r=requests.get('https://ai-news-search.futunn.com/news_search',params={'keyword':keyword,'size':8,'news_type':1,'sort_type':2,'lang':'zh-CN'},
                           headers={'User-Agent':'futunn-news-search/0.0.2 (Skill)'},timeout=15)
            r.raise_for_status();value=r.json()
            if value.get('code')!=0:raise ValueError('News search unsuccessful')
            group=[]
            for item in value.get('data',[]):
                value=normalize(item,'主题:'+keyword,now,'Futu news-search Skill API')
                if value and pd.Timestamp(value['published_at'])>=pd.Timestamp(now)-pd.Timedelta(hours=72): group.append(value)
            groups.append(group)
        except Exception as exc: errors.append({'keyword':keyword,'error_type':type(exc).__name__})
    unique={r['id']:r for group in groups for r in group}
    rows=list(unique.values())
    if len(rows)<4:
        # Current Futu keyword results may be empty, off-topic or lack allowed
        # original links; reuse the project's timestamped public market feeds.
        from daily_direction.news import fetch_market_news
        fallback, diagnostics=fetch_market_news(now=now)
        errors.append({'fallback_reason':'Futu global headline coverage insufficient','diagnostics':diagnostics})
        rows += [{'id':'feed:'+digest(r['url'])[:16],'symbol':'主题:'+r['market'],'title':r['title'],'published_at':r['published'],
                  'available_at':now,'url':r['url'],'source':r['publisher'],'content_level':'headline_only'} for r in fallback]
    return list({r['url']:r for r in rows}.values())[:32], errors


def market_temperature(store, now):
    from daily_direction.quality import latest_completed_session
    labels={'SH.000001':'A股 · 上证综指','HK.800000':'港股 · 恒生指数','US..NDX':'纳斯达克100','US..SPX':'标普500',
            'US.QQQ':'纳指100 ETF','US.SPY':'标普500 ETF','US.SMH':'半导体 ETF','US.IGV':'软件 ETF','US.SKYY':'云计算 ETF',
            'US.XLU':'公用事业 ETF','US.XLE':'能源 ETF','US.IWM':'美股小盘 ETF'}
    rows, gaps = [], []
    try:
        quotes=FutuSim(store.settings).snapshots(list(labels))
        for q in quotes:
            symbol=q['code'];price=float(q.get('last_price',0));prior=float(q.get('prev_close_price',0))
            market='cn' if symbol.startswith('SH.') else 'hk' if symbol.startswith('HK.') else 'us'
            expected=latest_completed_session(market,now=now)
            if symbol not in labels or not np.isfinite([price,prior]).all() or price<=0 or prior<=0:continue
            rows.append({'id':'market:'+symbol,'symbol':symbol,'name':labels[symbol],'value':price,'change_pct':(price/prior-1)*100,
                         'data_date':q.get('data_date'),'expected_session':expected,'current_session':q.get('data_date')==expected,
                         'source':'Futunn REST snapshot','unit':'指数点位' if q.get('stock_type')=='INDEX' else '报价','observed_at':now})
    except Exception as exc: gaps.append('富途跨市场快照：'+type(exc).__name__)
    for symbol,name in labels.items():
        if symbol not in {r['symbol'] for r in rows}:gaps.append(name+'行情缺失')
    # No verified Futu raw macro-series endpoints in this deployment.
    import yfinance as yf
    macro={'DX-Y.NYB':('美元指数','指数点位'),'^TNX':('美国10年期国债收益率','%'),
           'USDCNY=X':('美元兑人民币','CNY/USD'),'CL=F':('WTI原油近月期货','USD/桶'),'GC=F':('黄金近月期货','USD/盎司')}
    try:
        frames=yf.download(list(macro),period='5d',interval='1d',auto_adjust=False,progress=False,threads=True,timeout=12)
        for symbol,(name,unit) in macro.items():
            series=frames['Close'][symbol].dropna()
            if len(series)<2: gaps.append(name+'数据不足');continue
            value,prior=float(series.iloc[-1]),float(series.iloc[-2])
            rows.append({'id':'market:'+symbol,'symbol':symbol,'name':name,'value':value,'change_pct':(value/prior-1)*100,
                         'data_date':str(series.index[-1].date()),'source':'Yahoo Finance via yfinance','unit':unit,'observed_at':now,
                         'limitation':'富途原始宏观序列未验证，备用来源；期货不等于现货，日线不等于实时。'})
    except Exception as exc:gaps.append('宏观备用数据不可用：'+type(exc).__name__)
    return rows,gaps


def refresh_context(store, news=None):
    now=utcnow();technical=screen_us(store)
    markets,gaps=market_temperature(store,now)
    global_news,errors=global_headlines(now)
    n=news or store.meta('news_packet',{})
    headlines=list({r['url']:r for r in list(n.get('items',[]))+global_news}.values())[:60]
    evidence=technical['candidates']+markets+[dict(r,id='news:'+r['id']) for r in headlines]
    value={'version':REVIEW_VERSION,'observed_at':now,'asof':technical['asof'],'technical_scan':technical,
           'markets':markets,'headlines':headlines,'evidence':evidence,'gaps':gaps,'news_errors':errors,
           'scope':'动态全池快照 / 完整日线候选 / 跨市场与行业主题资讯；非完整实时全市场覆盖'}
    store.set_meta('brief_context',value)
    return value


def review_packet(store, quotes=None, panel=None):
    from research.daily_factor_lab.core import load_panel
    from .news import evidence_at
    packet=store.snapshot()
    packet['capital_authorization']={'stock':store.settings.stock_budget,'option':store.settings.option_budget,
        'mode':store.settings.capital_mode,'option_funding_mode':store.settings.option_funding_mode,
        'note':'账本本金；执行额度另受最新券商净资产、现金及未交收资金约束，无融资。'}
    packet['orders']=[{k:v for k,v in o.items() if k!='broker_id'} for o in packet['orders'][-100:]]
    packet['quotes']={s:{k:q.get(k) for k in ('data_date','last_price','prev_close_price')} for s,q in (quotes or {}).items()}
    packet['news']=evidence_at(store)
    panel=panel or load_panel(store.meta('current_data_dir',store.settings.data_dir))
    packet['data']=panel.metadata
    packet['universe_scan']=store.meta('universe_scan');packet['strategy_references']=store.meta('strategy_references',[])
    records=packet['strategies']
    selected={r['id']:r for r in records[-8:]+[r for r in records if r['stage']=='shadow'][-6:]
              +[r for r in records if r['stage'] in ('paper','live_review')]}
    packet['strategy_archive']={'total':len(records),'summaries':len(selected),'omitted':len(records)-len(selected),
        'note':'摘要包含全部模拟/实盘待审版本、最近6个Shadow及最近8个试验；其余完整证据保留在本地策略档案。'}
    packet['strategies']=[{'id':r['id'][:12],'spec':r['spec'],'stage':r['stage'],
        'evidence':{k:r['evidence'].get(k) for k in ('stats','gates','stress','delayed','benchmark','reason','shadow_sessions','shadow_nav','shadow_dd_pct')}} for r in selected.values()]
    metrics=('return_pct','max_drawdown_pct','orders','trading_days','cost_dollars','sharpe_zero_rf')
    for record in packet['strategies']:
        evidence=record['evidence']
        for key in ('stats','stress','delayed'):
            if isinstance(evidence.get(key),dict):
                evidence[key]={window:{k:v for k,v in values.items() if k in metrics}
                    for window,values in evidence[key].items()
                    if isinstance(values,dict) and (key=='stats' or window!='train')}
    packet['paper_activity']=store.paper_activity();packet['paper_activity']['curve']=packet['paper_activity']['curve'][-30:]
    if 'option' in packet['paper_activity']:
        packet['paper_activity']['option']['curve']=packet['paper_activity']['option']['curve'][-30:]
    # Fill rows retain the historical pooled calculation. Owner-attributed
    # account totals are authoritative after strategies trade the same symbol.
    for fill in packet['paper_activity'].get('fills',[]):
        fill['pooled_realized_pnl']=fill.pop('realized_pnl',0)
    packet['paper_activity']['realized_pnl_basis']='总计按策略归属平均成本核算；逐笔 pooled_realized_pnl 为历史账户池成本口径，两者不应直接相加比较。'
    packet['protocol_gates']={'cost_bps':25,'stress_bps':50,'baseline_signal_lag_sessions':1,'stress_signal_lag_sessions':2,
                              'additional_delay_sessions':1,'max_drawdown_pct':15,'min_test_sessions':52,'min_test_orders':10,'benchmark':'QQQ'}
    context=store.meta('brief_context') or {'version':REVIEW_VERSION,'technical_scan':screen_us(store,panel),
        'evidence':[],'markets':[],'headlines':[],'gaps':['跨市场与资讯上下文尚未采集，不可编造全球行情。']}
    context=deepcopy(context)
    # Facts remain in the evidence registry once, with stable IDs. The original
    # full context and all ledger observations remain stored for audit/UI use.
    if context.get('evidence'):
        context['headlines']=[{'id':'news:'+r['id']} for r in context.get('headlines',[])]
        technical=context.get('technical_scan',{})
        technical['candidates']=[{'symbol':r['symbol'],'evidence_id':r['id']} for r in technical.get('candidates',[])]
    packet['brief_context']=context;packet['review_version']=REVIEW_VERSION
    packet['execution_diagnostics']={
        'health':store.meta('health'), 'last_tick':{k:v for k,v in (store.meta('last_tick') or {}).items() if k!='quotes'},
        'option_status':store.meta('option_paper_status'),
        'recent_errors':[{k:v for k,v in r.items() if k!='frames'} for r in _recent_execution_errors(store)]}
    if len(canonical(packet))>140000:
        for record in packet['strategies']:
            record['spec']=deepcopy(record['spec'])
            record['spec_sha256']=digest(record['spec'])
            for field in ('hypothesis','falsification'):
                if isinstance(record['spec'].get(field),str):record['spec'][field]=record['spec'][field][:800]
            if isinstance(record['evidence'].get('stats'),dict):record['evidence']['stats'].pop('train',None)
        packet['paper_activity']['curve']=packet['paper_activity']['curve'][-5:]
        if 'option' in packet['paper_activity']:packet['paper_activity']['option']['curve']=packet['paper_activity']['option']['curve'][-5:]
        packet['strategy_archive']['additional_compaction']='仅缩短假设文字并省略训练指标/盘中估值曲线；验证/回顾/成本/延迟指标与执行证据保留，完整档案不变。'
    return packet


def _recent_execution_errors(store):
    with store.transaction() as db:
        rows=db.execute("SELECT created,kind,payload FROM events WHERE kind IN ('pause','supervisor_failed','supervisor_deferred','strategy_quote_deferred','refresh_failed') ORDER BY seq DESC LIMIT 12").fetchall()
    return [{'created':r['created'],'kind':r['kind'],**json.loads(r['payload'])} for r in rows]


def validate_brief(response, packet):
    brief=response['result'].get('brief')
    if brief is None:return
    allowed={r['id'] for r in packet['brief_context'].get('evidence',[])}
    symbols={r['symbol'] for r in packet['brief_context'].get('technical_scan',{}).get('candidates',[])}
    symbols.update(r['symbol'] for r in (packet.get('universe_scan') or {}).get('hits',[]))
    symbols.update(r['symbol'] for r in packet['news'] if r['symbol'].startswith('US.'))
    def walk(value):
        if isinstance(value,dict):
            if 'evidence_ids' in value and any(i not in allowed for i in value['evidence_ids']):raise ValueError('Unsupported brief evidence')
            for child in value.values():walk(child)
        elif isinstance(value,list):
            for child in value:walk(child)
    walk(brief)
    for category,items in brief['us_scan'].items():
        if len({r['symbol'] for r in items})!=len(items) or any(r['symbol'] not in symbols for r in items):
            raise ValueError('Unsupported or duplicate short-term candidate')
        if category=='catalyst' and any(not any(i.startswith('news:') for i in r['evidence_ids']) for r in items):
            raise ValueError('Catalyst lacks news evidence')


def persist_brief(store,response,packet,asof):
    validate_brief(response,packet)
    if 'brief' not in response['result']:return
    coverage=deepcopy(packet['brief_context'].get('technical_scan',{}))
    registry={r['id']:r for r in packet['brief_context'].get('evidence',[])}
    coverage['candidates']=[registry.get(r.get('evidence_id'),r) for r in coverage.get('candidates',[])]
    value={'version':REVIEW_VERSION,'asof':asof,'generated_at':response.get('completed_at',utcnow()),'model':response['model'],
           'capital_authorization':packet.get('capital_authorization'),
           'reasoning_effort':response.get('reasoning_effort'),'brief':response['result']['brief'],
           'factor_ideas':response['result']['factor_ideas'],'evidence':packet['brief_context'].get('evidence',[]),
           'coverage':coverage,'markets':packet['brief_context'].get('markets',[]),
           'data_gaps':packet['brief_context'].get('gaps',[]),'paper_activity':{k:v for k,v in packet['paper_activity'].items() if k not in ('curve','fills')}}
    from .files import atomic_write
    directory=store.home/'reports';directory.mkdir(exist_ok=True,mode=0o700)
    atomic_write(directory/(asof+'.brief.json'),canonical(value))
    atomic_write(directory/(asof+'.md'),render_brief_markdown(value))
    store.set_meta('daily_brief',value)
    return value


def render_brief_markdown(value):
    """Render the saved analysis without regenerating it or changing its date."""
    capital=value.get('capital_authorization')
    budget=(f"股票账本本金 ${capital['stock']:,.0f} / 期权账本本金 ${capital['option']:,.0f}；执行额度另受最新净资产与现金约束。"
        if capital else '账本本金以对应版本授权记录为准。')
    b=value['brief'];lines=['# 每日投资与 AI 产业私人助理简报 '+value['asof'],'','**'+b['headline']+'**','',
        '生成：'+value['generated_at']+'；行情各市场交易日期分别列示。'+budget+'实盘关闭。','']
    labels={'judgment':'判断','impact':'影响方向','risk':'风险点','strength':'强度与持续性',
            'catalyst':'催化','trigger':'触发条件','invalid':'失效条件'}
    evidence={r['id']:r for r in value['evidence']}
    def sources(item):
        result=[]
        for key in item.get('evidence_ids',[]):
            r=evidence[key]
            title=str(r.get('title',r.get('name',r.get('symbol',key)))).replace('[','').replace(']','').replace('\n',' ')
            url=r.get('url','')
            title='['+title+']('+url+')' if url.startswith('https://') and not any(c in url for c in '\n\r()') else title
            result.append(title+' · '+str(r.get('source',''))+' · '+str(r.get('data_date',r.get('published_at',''))))
        return ['**来源**：'+'；'.join(result)] if result else []
    for title,key in [('今天最重要的变化','key_events'),('市场温度','market_temperature'),('主线判断','themes')]:
        lines+=['## '+title]
        if key=='market_temperature':
            for r in value.get('markets',[]):
                lines+=['- **'+r['name']+'**：'+format(r['value'],'.3f' if r['unit']=='%' else '.2f')+' '+r['unit']+'；变化 '+format(r['change_pct'],'+.2f')+'%；'+str(r['data_date'])+'；'+r['source']]
        for item in b[key]:lines+=['### '+item.get('title',item.get('market',item.get('direction',''))),*['**'+labels.get(k,k)+'**：'+str(v) for k,v in item.items() if k not in ('title','market','direction','evidence_ids')],*sources(item)]
    lines+=['## 美股短线扫描']
    for key,title in [('bottom','短期抄底观察'),('catalyst','资讯利好 / 催化'),('strong','强势延续'),('avoid','走坏 / 回避')]:
        lines+=['### '+title]
        for r in b['us_scan'][key]:
            fact=evidence.get('tech:'+r['symbol'],{})
            lines+=['**'+r['symbol']+' · '+fact.get('name',r['symbol'])+'**',
                    '**所属方向**：'+fact.get('sector','待核实'),
                    *['**'+label+'**：'+r[field] for field,label in [('reason','入选原因'),('reflection','是否已反映'),('trigger','触发条件'),('invalid','失效条件'),('risk','风险点')]],*sources(r)]
        if not b['us_scan'][key]:lines+=['暂无足够证据支持的候选。']
    lines+=['## A股 / 港股候选观察方向']
    for r in b['cn_hk']:lines+=['### '+r['direction'],*['**'+label+'**：'+r[field] for field,label in [('reason','入选原因'),('trigger','触发条件'),('invalid','失效条件'),('risk','风险点')]],*sources(r)]
    lines+=['## 对我有用的结论']
    for key,label in [('watch','重点观察'),('avoid','避免'),('changes','改变判断的信息')]:lines+=['**'+label+'**',*['- '+v for v in b['conclusions'][key]]]
    lines+=['## 模拟操作复盘',b['execution_review'],'## 因子待办',*['- '+v for v in value['factor_ideas']],
            '## 证据与覆盖缺口',*['- '+v for v in list(dict.fromkeys(b['coverage_gaps']+value['data_gaps']))],
            '','所有投资内容仅供研究，不构成投资建议。']
    return '\n\n'.join(lines)


def generate_brief(store,refresh=False):
    from .pipeline import artifact
    from .schedule import calendar_state
    if refresh:
        from .news import refresh_news
        refresh_news(store)
    packet=review_packet(store);asof=calendar_state()['completed_session']
    from .runner import job_trace
    with job_trace(store,'review',asof):
        response=ask(store.settings.ai_provider,{'purpose':'daily_private_brief','asof':asof,'evidence':packet},
                 model_name=store.settings.ai_model,reasoning_effort=store.settings.ai_reasoning_effort,
                 output_schema=REVIEW_SCHEMA,system_instruction=REVIEW_SYSTEM)
        validate_brief(response,packet)
    store.event('ai_review',{'provider':response['provider'],'model':response['model']})
    artifact(store,'private-brief-'+asof+'-'+digest(response)[:16],{'packet':packet,'ai':response})
    value=persist_brief(store,response,packet,asof)
    store.set_meta('ai_runtime',{'model':response['model'],'reasoning_effort':response.get('reasoning_effort'),'observed_at':utcnow()})
    return {'asof':asof,'model':value['model'],'short_term_candidates':sum(len(v) for v in value['brief']['us_scan'].values()),'report':str(store.home/'reports'/(asof+'.md'))}
