"""Loopback dashboard: stored evidence plus explicit read-only service probes."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import threading
from urllib.parse import urlsplit, parse_qs

from .schedule import calendar_state
from .config import ROOT
from .presentation import session_timeline, strategy_logic, focus_quote_view
from .activity import strategy_activity

STATIC = Path(__file__).with_name('static')


def public_status(store):
    snapshot=store.snapshot()
    with store.transaction() as db:
        events=[dict(r) for r in db.execute('SELECT seq,created,kind,payload FROM events ORDER BY seq DESC LIMIT 100')]
        shadow_counts={r['strategy']:r['n'] for r in db.execute('SELECT strategy,COUNT(*) n FROM shadow GROUP BY strategy')}
        shadow_latest={r['strategy']:json.loads(r['state']) for r in db.execute('SELECT strategy,state FROM shadow s WHERE session=(SELECT MAX(session) FROM shadow WHERE strategy=s.strategy)')}
        paper_owners=[r[0] for r in db.execute('SELECT DISTINCT strategy FROM orders')]
        paper_filled={r[0] for r in db.execute("SELECT DISTINCT o.strategy FROM orders o JOIN fills f ON f.order_id=o.id WHERE o.bucket='stock'")}
    paper_activity=store.paper_activity()
    for e in events:
        e['payload']=json.loads(e['payload'])
        if e['kind'] in ('ai_discovery','ai_review','stage'):
            e['payload']={k:v for k,v in e['payload'].items() if k in ('id','from','to','provider','model')}
    calendar=calendar_state()
    from .holdings import holdings_summary
    holdings=holdings_summary(store,calendar)
    asof=calendar['completed_session']
    records=snapshot['strategies']
    strategies=[]
    for r in records:
        evidence=r['evidence']; stats=evidence.get('stats',{})
        allocation=store.meta('paper_allocations',{}).get('stock',{}).get(r['id'])
        owner=store.strategy_portfolio(r['id']) if allocation is not None and store.settings.parallel_paper else None
        strategies.append({'id':r['id'][:12],'full_id':r['id'],'name':r['spec'].get('factor_name',r['spec']['policy']['name']),'stage':r['stage'],
            'origin':r['spec'].get('origin'),'hypothesis':r['spec'].get('hypothesis'),'policy':r['spec']['policy'],
            'factor_expression':r['spec'].get('factor_expression'),'falsification':r['spec'].get('falsification'),
            'factor_weights':r['spec'].get('factor_weights'),'logic':strategy_logic(r['spec']),
            'forward_return_pct':(shadow_latest[r['id']]['nav']/10000-1)*100 if r['id'] in shadow_latest else None,
            'paper_return_pct':((owner['performance_nav']/owner['initial_capital']-1)*100 if owner and owner['initial_capital']>0 else paper_activity['return_pct']) if (owner or paper_owners==[r['id']]) and r['id'] in paper_filled else None,
            'paper_allocation':allocation,'paper_authorization':evidence.get('paper_authorization'),
            'data_hash':r['spec'].get('research_data_sha256'),'created':r['created'],
            'stats':stats,'gates':evidence.get('gates',{}),'stress':evidence.get('stress',{}),'delayed':evidence.get('delayed',{}),
            'benchmark':evidence.get('benchmark'),'shadow_sessions':shadow_counts.get(r['id'],0),
            'chart_available':(store.home/'artifacts'/('chart-'+r['id']+'.json')).exists(),
            'failed_gates':[k for k,v in evidence.get('gates',{}).items() if not v] or ([evidence['reason']] if evidence.get('reason') else [])})
    focus=next((r for r in reversed(strategies) if r['stage'] in ('paper','shadow')),strategies[-1] if strategies else {})
    report=store.home/'reports'/(asof+'.md')
    review=report.read_text() if report.exists() else None
    jobs=[{'key':'refresh','name':'历史数据更新','trigger':'收盘 15 分钟后，每个交易日一次','command':'refresh'},
          {'key':'scan','name':'指数、自选与异动入池','trigger':'收盘刷新成分股；每次最多补 3 只新异动标的历史','command':'scan --enrich'},
          {'key':'news','name':'资讯证据更新','trigger':'收盘一次；盘中每 30 分钟更新重点标的标题','command':'news'},
          {'key':'discovery','name':'Codex 假设与因子组合','trigger':'每个数据版本最多 30 个试验，累计记录保留','command':'discover --ai'},
          {'key':'backtest','name':'回测 · 压力测试 · 核账','trigger':'候选注册后，冻结规则并验证','command':'research'},
          {'key':'review','name':'Codex 每日操作复盘','trigger':'研究结束后，每日一次；失败一小时后重试','command':'tick --daily'},
          {'key':'evolution','name':'新因子代码与独立验证','trigger':'每个新数据版本最多 1 个表达式；因果检查后回测','command':'evolve'}]
    jobs.append({'key':'promotion','name':'优秀策略模拟晋升','trigger':'同区间比较；仅在休市替换空仓、无未完成委托且已交收的槽位','command':'自动比较与排队'})
    completed=store.meta('daily_completed:'+asof,False)
    for job in jobs:
        job['state']=store.meta('task:'+job['key']) or {'status':'recorded' if completed else 'pending','asof':asof}
        job['enabled']={'refresh':store.settings.auto_refresh,'discovery':store.settings.daily_research,'evolution':store.settings.evolution_enabled,'promotion':store.meta('promotion_policy',{}).get('enabled',False)}.get(job['key'],True)
    evolutions=[]
    for path in sorted((store.home/'artifacts').glob('evolution-*.json')):
        value=json.loads(path.read_text())
        if not value.get('strategy'):continue
        record=next((r for r in strategies if r['full_id']==value['strategy']),None)
        code_path=ROOT/'research/evolved_factors'/('factor_'+value['strategy'][:12]+'.py')
        evolutions.append({k:v for k,v in value.items() if k!='code_path'}|{'stage':record['stage'] if record else 'proposed',
            'code_url':'/api/factor/'+value['strategy'] if code_path.is_file() else None,'failed_gates':record['failed_gates'] if record else []})
    orders=[{k:v for k,v in o.items() if k not in ('broker_id',)} for o in snapshot['orders'][-100:]]
    from .dashboard import dashboard_summary
    dashboard=dashboard_summary(store,calendar,snapshot,strategies,paper_activity,jobs)
    from .research_evidence import research_summary
    return {'dashboard':dashboard,'research_evidence':research_summary(store,asof),'forecast_tracking':store.meta('forecast_tracking'),
        'ai_provider':store.settings.ai_provider,'health':store.meta('health'),'last_tick':store.meta('last_tick'),
        'execution_readiness':store.meta('execution_readiness'),
        'paper_execution_session':store.meta('paper_execution_session'),
        'paper_authorization':store.meta('paper_authorization'),'parallel_paper':store.settings.parallel_paper,
        'ai_requested':{'model':store.settings.ai_model,'reasoning_effort':store.settings.ai_reasoning_effort},'ai_runtime':store.meta('ai_runtime'),
        'paused':snapshot['paused'],'pause_reason':snapshot['pause_reason'],'portfolios':snapshot['portfolios'],
        'order_counts':{s:sum(o['status']==s for o in snapshot['orders']) for s in set(o['status'] for o in snapshot['orders'])},
        'strategies':strategies,'focus_id':focus.get('full_id'),'focus_data_hash':focus.get('data_hash'),
        'events':events,'live_enabled':False,'universe_scan':store.meta('universe_scan'),
        'index_universes':store.meta('index_universes'),'news_packet':store.meta('news_packet'),
        'push_status':store.meta('push_status'),'push_probe':store.meta('push_probe'),'focus_quotes':focus_quote_view(store,calendar),
        'evolutions':sorted(evolutions,key=lambda r:r['created_at'],reverse=True),
        'orders':orders,'paper_activity':paper_activity,'holdings':holdings,
        'intervals':{'orders':store.settings.poll_seconds,'broad_scan':store.settings.broad_scan_seconds,'news':store.settings.news_scan_seconds,'push_focus_limit':40},
        'strategy_references':store.meta('strategy_references',[]),'calendar':calendar,'timeline':session_timeline(calendar),'schedule':store.meta('schedule'),
        'tasks':jobs,'daily_completed':completed,'daily_asof':asof,'review_markdown':review,
        'daily_brief':store.meta('daily_brief'),
        'max_trials':store.settings.max_trials,'min_shadow_sessions':store.settings.min_shadow_sessions,
        'options_status':('期权前向模拟已授权：仅买入Call/Put及卖出平仓，预算$1,000；历史期权回测未验证。' if store.settings.option_paper_enabled else '期权历史链、模拟账户与行权交割待验证，执行关闭'),
        'limitations':['历史样本已查看；当前自选股有幸存者与选择偏差','现金股息未计入；历史结果不代表未来收益','预算由程序控制，券商模拟账户尚未物理隔离']}


def chart_data(store,strategy,mode):
    record=next((r for r in store.strategies() if r['id']==strategy),None)
    if not record:
        return None
    if mode=='paper':
        activity=strategy_activity(store,strategy,'paper')
        return {'kind':'paper','curve':activity['curve'],'benchmark_curve':[],
                'initial_capital':activity['summary'].get('initial_capital',10000),
                'source':activity['source'],'cost_bps':store.settings.fee_reserve_bps}
    if mode=='forward':
        with store.transaction() as db:
            rows=[dict(r) for r in db.execute('SELECT session,state FROM shadow WHERE strategy=? ORDER BY session',(strategy,))]
        return {'kind':'forward_shadow','curve':[{'date':r['session'],'equity':json.loads(r['state'])['nav'],
            'cash':json.loads(r['state'])['cash']} for r in rows],'benchmark_curve':[],
            'source':'未来实际观察报价；假设成交，非券商成交','cost_bps':25}
    path=store.home/'artifacts'/('chart-'+strategy+'.json')
    return json.loads(path.read_text()) if path.exists() else {'kind':'retrospective_test','curve':[],'benchmark_curve':[]}


def start_web(store,port=8791):
    from .self_check import SelfCheck
    diagnostics = SelfCheck(store)
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path != '/api/self-check':
                self.send_error(404);return
            allowed = {f'http://127.0.0.1:{self.server.server_port}', f'http://localhost:{self.server.server_port}'}
            if self.headers.get('Host') not in {urlsplit(u).netloc for u in allowed} or (
                self.headers.get('Origin') and self.headers['Origin'] not in allowed) or self.headers.get('Sec-Fetch-Site') == 'cross-site':
                self.send_error(403);return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 64 or self.headers.get('Content-Type','').split(';')[0] != 'application/json':
                    raise ValueError()
                if json.loads(self.rfile.read(length)) != {}: raise ValueError()
            except (ValueError,TypeError):
                self.send_error(400);return
            body = json.dumps(diagnostics.start(),ensure_ascii=False,allow_nan=False).encode()
            self.send_response(202);self.send_header('Content-Type','application/json; charset=utf-8')
            self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(body)))
            self.end_headers();self.wfile.write(body)

        def do_GET(self):
            parsed=urlsplit(self.path)
            if parsed.path=='/':
                body,mime=(STATIC/'dashboard.html').read_bytes(),'text/html; charset=utf-8'
            elif parsed.path in ('/assets/dashboard.css','/assets/dashboard.js'):
                asset=parsed.path.rsplit('/',1)[1]
                body=(STATIC/asset).read_bytes()
                mime='text/css; charset=utf-8' if asset.endswith('.css') else 'text/javascript; charset=utf-8'
            elif parsed.path=='/api/status':
                body,mime=json.dumps(public_status(store),ensure_ascii=False,allow_nan=False).encode(),'application/json; charset=utf-8'
            elif parsed.path=='/api/self-check':
                body,mime=json.dumps(diagnostics.snapshot(),ensure_ascii=False,allow_nan=False).encode(),'application/json; charset=utf-8'
            elif re.fullmatch(r'/api/chart/[a-f0-9]{64}',parsed.path):
                data=chart_data(store,parsed.path.rsplit('/',1)[1],parse_qs(parsed.query).get('mode',['history'])[0])
                if data is None:
                    self.send_error(404);return
                body,mime=json.dumps(data,ensure_ascii=False,allow_nan=False).encode(),'application/json; charset=utf-8'
            elif re.fullmatch(r'/api/activity/[a-f0-9]{64}',parsed.path):
                mode=parse_qs(parsed.query).get('mode',['history'])[0]
                if mode not in ('history','forward','paper'):
                    self.send_error(400);return
                data=strategy_activity(store,parsed.path.rsplit('/',1)[1],mode)
                if data is None:
                    self.send_error(404);return
                body,mime=json.dumps(data,ensure_ascii=False,allow_nan=False).encode(),'application/json; charset=utf-8'
            elif re.fullmatch(r'/api/forecast/[a-f0-9]{64}',parsed.path):
                from .forecast import forecast_data
                data=forecast_data(store,parsed.path.rsplit('/',1)[1],calendar_state())
                if data is None:
                    self.send_error(404);return
                body,mime=json.dumps(data,ensure_ascii=False,allow_nan=False).encode(),'application/json; charset=utf-8'
            elif re.fullmatch(r'/api/holdings/[a-f0-9]{64}',parsed.path):
                from .holdings import historical_holdings
                query=parse_qs(parsed.query);day=query.get('asof',[None])[0]
                if query.get('mode',['history'])[0]!='history' or (day and not re.fullmatch(r'\d{4}-\d{2}-\d{2}',day)):
                    self.send_error(400);return
                data=historical_holdings(store,parsed.path.rsplit('/',1)[1],day)
                if data is None:self.send_error(404);return
                body,mime=json.dumps(data,ensure_ascii=False,allow_nan=False).encode(),'application/json; charset=utf-8'
            elif re.fullmatch(r'/api/factor/[a-f0-9]{64}',parsed.path):
                key=parsed.path.rsplit('/',1)[1]
                record=next((r for r in store.strategies() if r['id']==key and r['spec'].get('factor_expression')),None)
                path=ROOT/'research/evolved_factors'/('factor_'+key[:12]+'.py')
                if record is None or not path.is_file() or path.is_symlink():
                    self.send_error(404);return
                body,mime=path.read_bytes(),'text/plain; charset=utf-8'
            elif re.fullmatch(r'/api/research-evidence/[a-f0-9]{64}',parsed.path):
                from .research_evidence import trial_evidence
                data=trial_evidence(store,parsed.path.rsplit('/',1)[1])
                if data is None:self.send_error(404);return
                body,mime=json.dumps(data,ensure_ascii=False,allow_nan=False).encode(),'application/json; charset=utf-8'
            else:
                self.send_error(404);return
            self.send_response(200)
            self.send_header('Content-Type',mime)
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'")
            self.send_header('Content-Length',str(len(body)))
            self.end_headers();self.wfile.write(body)
        def log_message(self,*args):
            pass
    server=ThreadingHTTPServer(('127.0.0.1',port),Handler)
    threading.Thread(target=server.serve_forever,daemon=True,name='quant-status').start()
    return server
