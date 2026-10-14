"""Explicit, bounded diagnostics. Never submits orders or calls company MCPs."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
import copy
import json
from pathlib import Path
import threading
import time
import tomllib
from urllib.parse import urlsplit
from urllib.error import HTTPError, URLError

import requests

from .ai import ask
from .broker import FutuSim, BrokerError
from .store import utcnow


def result(status, detail, **evidence):
    return {'status': status, 'detail': detail, 'evidence': evidence}


def safe_failure(exc):
    if isinstance(exc, (TimeoutError, requests.Timeout)) or type(exc).__name__ in ('TimeoutExpired', 'WebSocketTimeoutException'):
        return result('failed', '请求超时；可稍后重试。', error_type=type(exc).__name__)
    if isinstance(exc, BrokerError):
        return result('failed', '富途业务请求未通过，请核对权限或限流。', ret_code=exc.ret_code)
    if isinstance(exc, HTTPError):
        return result('failed', 'HTTP 请求未通过。', http_status=exc.code)
    if isinstance(exc, requests.HTTPError):
        return result('failed', 'HTTP 请求未通过。', http_status=exc.response.status_code if exc.response is not None else None)
    if isinstance(exc, FileNotFoundError):
        return result('unconfigured', '所需本地文件或凭据未配置。')
    if isinstance(exc, (requests.ConnectionError, URLError)):
        return result('failed', '网络、代理或 TLS 连接失败。', error_type=type(exc).__name__)
    # Never expose raw exceptions, response bodies, account IDs, tokens or URLs.
    return result('failed', '校验未通过；请检查对应服务配置与授权。', error_type=type(exc).__name__)


def mcp_inventory():
    p = Path.home()/'.codex/config.toml'
    if not p.exists():
        return []
    config = tomllib.loads(p.read_text()).get('mcp_servers', {})
    return [{'name': n, 'transport': 'http' if c.get('url') else 'stdio',
             'enabled': c.get('enabled', True),
             'project_scope': n == 'futu-mcp',
             'status': 'unverified' if n == 'futu-mcp' and c.get('enabled', True) else 'skipped',
             'detail': '等待服务协议探测' if n == 'futu-mcp' and c.get('enabled', True) else
             '未启用' if not c.get('enabled', True) else '非本项目依赖；不启动、不发送请求'} for n, c in config.items()]


def mcp_probe():
    cfg = tomllib.loads((Path.home()/'.codex/config.toml').read_text()).get('mcp_servers', {}).get('futu-mcp')
    if not cfg or not cfg.get('enabled', True):
        return result('unconfigured', '未配置或未启用富途 MCP。')
    # A configuration entry is not permission to probe arbitrary/private hosts.
    if cfg.get('url') != 'https://mcp.futunn.com/mcp':
        return result('unverified', '配置地址不在已核实的富途 MCP 探测范围。')
    payload = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
        'protocolVersion': '2025-03-26', 'capabilities': {},
        'clientInfo': {'name': 'quant-self-check', 'version': '1'}}}
    with requests.post(cfg['url'], json=payload, headers={'Accept': 'application/json, text/event-stream'},
                       timeout=(5, 8), allow_redirects=False, stream=True) as response:
        status = response.status_code
        if status in (401, 403):
            return result('unverified', '服务返回鉴权/权限响应；此探针未使用 Codex OAuth，登录后的 MCP 工具调用未验证。',
                          reachable=True, http_status=status, authenticated=False)
        if status != 200:
            return result('failed', 'MCP 协议探测未通过。', http_status=status)
        content = bytearray()
        for chunk in response.iter_content(1024):
            content.extend(chunk)
            if len(content) > 32768:
                return result('unverified', '服务响应可达，协议响应超出探测上限。', reachable=True)
            text = content.decode('utf-8', errors='replace')
            try:
                if 'text/event-stream' in response.headers.get('Content-Type', ''):
                    messages = [json.loads(line[6:]) for line in text.splitlines() if line.startswith('data: ')]
                    body = next((m for m in messages if m.get('id') == 1), None)
                    if body is None: continue
                else:
                    body = json.loads(text)
            except (ValueError, StopIteration):
                continue
            if isinstance(body, dict) and isinstance(body.get('result'), dict) and body['result'].get('protocolVersion'):
                return result('unverified', 'MCP initialize 已响应；只证明协议连通，OAuth 与行情工具仍需客户端验证。',
                              reachable=True, protocol_initialized=True, tool_call_verified=False)
            return result('failed', 'MCP 返回了无效的初始化结果。', reachable=True)
    return result('failed', 'MCP 没有返回完整的协议结果。')


def check_jobs(store):
    settings = store.settings
    def runtime():
        health = store.meta('health') or {}
        stamp = datetime.fromisoformat(health.get('time', ''))
        age = (datetime.now(timezone.utc)-stamp).total_seconds()
        with store.transaction() as db:
            valid = db.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
        ok = health.get('status') == 'ok' and 0 <= age < 180 and valid
        return result('ok' if ok else 'failed', '服务心跳与本地账本检查通过。' if ok else '服务心跳或本地账本需要检查。',
                      heartbeat_age_seconds=round(age, 1), database_ok=valid, mode=health.get('mode'))
    def ai():
        if settings.ai_provider == 'configured':
            return result('unverified', '自定义 LLM 后端尚未接入隔离诊断；未自动回退。')
        schema = {'type':'object','additionalProperties':False,'required':['check'],
                  'properties':{'check':{'type':'string','enum':['quant-health-ok']}}}
        value = ask(settings.ai_provider, {'purpose':'connectivity_check','expected':'quant-health-ok'},
                    timeout=60, model_name=settings.ai_model, reasoning_effort=settings.ai_reasoning_effort,
                    output_schema=schema, system_instruction='仅返回 {"check":"quant-health-ok"}；不调用工具。')
        return result('ok', '实际模型请求成功，返回值已通过 JSON schema 校验。', provider=value['provider'],
                      model=value['model'], reasoning_effort=value.get('reasoning_effort'),
                      requested_model=settings.ai_model, requested_reasoning_effort=settings.ai_reasoning_effort)
    def quote():
        rows = FutuSim(settings).snapshots(['US.QQQ'])
        row = next(r for r in rows if r.get('code') == 'US.QQQ')
        if not isinstance(row.get('last_price'), (int, float)) or row['last_price'] <= 0:
            raise ValueError('Invalid quote')
        return result('ok', '签名与行情业务响应通过；行情新鲜度按日期、时段另行判断。',
                      ret_code=0, symbol='US.QQQ', data_date=row.get('data_date'), update_time=row.get('update_time'))
    def account():
        if not settings.stock_account:
            return result('unconfigured', '股票模拟账户尚未配置。')
        broker = FutuSim(settings)
        matches = [a for a in broker.accounts() if str(a['account_id']) == settings.stock_account
                   and a.get('market_id') == 100 and '美股' in a.get('account_title', '')]
        if len(matches) != 1: raise ValueError('Account mismatch')
        state = broker.state()
        root = '/api/v1.0/sim-trade/'+settings.stock_account
        now = datetime.now(timezone.utc)
        history = broker.request('GET',root+'/history-orders',{'market':100,
            'time_begin':int((now-timedelta(days=7)).timestamp()*1e6),'time_end':int(now.timestamp()*1e6),'page_size':1})
        if not isinstance(state['cash'], dict) or not isinstance(history['data']['orders'],list):
            raise ValueError('Invalid account response')
        return result('ok', '已核实美股模拟账户；资产、持仓、委托及历史委托查询通过。未调用下单接口。',
                      account_verified=True, read_endpoints_verified=5, order_endpoint_tested=False, paper_enabled=settings.paper_enabled)
    def watchlist():
        data = FutuSim(settings).request('GET','/api/v1.0/quote/user-security',{'group_name':'US'})['data']['security_list']
        if not isinstance(data,list): raise ValueError('Invalid watchlist')
        return result('ok', '自选列表业务响应通过。', entries=len(data))
    def index(symbol):
        data = FutuSim(settings).request('GET','/api/v1.0/quote/valuation/index-stocks',
            {'symbol':symbol,'limit':1,'valuation_type':1})['data']['stock_list']
        if not isinstance(data,list) or not data: raise ValueError('Invalid index response')
        return result('ok', '指数成分首分页业务响应通过；自检不做全量成分校验。', entries=len(data), complete_universe_verified=False)
    def history_api():
        from .schedule import calendar_state
        end = calendar_state()['completed_session']
        start = str(datetime.fromisoformat(end).date()-timedelta(days=10))
        rows = FutuSim(settings).request('GET','/api/v1.0/quote/US.QQQ/history-kline',
            {'start':start,'end':end,'ktype':2,'num':20,'autype':1})['data']['kline_list']
        if not isinstance(rows,list) or not rows: raise ValueError('Invalid history')
        return result('ok','历史日线接口业务响应通过；未更新研究数据。',bars=len(rows),requested_end=end)
    def rest_news():
        body = FutuSim(settings).request('GET', '/api/v1.0/quote/find-news',
            {'symbol':'NVDA','size':1,'news_type':1,'sort_type':2,'lang':'zh-CN'})
        if not isinstance(body.get('data'), list): raise ValueError('Invalid news')
        return result('ok', '富途资讯业务响应通过。', ret_code=0, items=len(body['data']))
    def websocket_probe():
        from .push import HOST, handshake
        import websocket
        sock = websocket.create_connection(HOST, timeout=6)
        try:
            handshake(sock, ['US.QQQ'])
        finally:
            sock.close()
        return result('ok', 'WebSocket 鉴权与订阅确认通过；未验证持续实时行情。',
                      auth_ok=True, subscribe_ok=True, streaming_freshness_verified=False)
    def news_search():
        r = requests.get('https://ai-news-search.futunn.com/news_search',
            params={'keyword':'NVDA','size':1,'news_type':1,'sort_type':2,'lang':'zh-CN'},
            timeout=(5,10), allow_redirects=False)
        r.raise_for_status(); body = r.json()
        if body.get('code') != 0 or not isinstance(body.get('data'), list): raise ValueError('Invalid news search')
        return result('ok', '备用资讯搜索业务响应通过。', code=0, items=len(body['data']))
    def feed(source):
        from daily_direction.news import fetch_market_feed
        rows, diagnostic = fetch_market_feed(source, 'us' if source == 'wallstreetcn' else 'global')
        status = diagnostic.get('status')
        return result('failed' if status == 'error' else 'ok' if rows else 'warning',
                      '公开资讯解析通过。' if rows else '接口返回有效响应，但无近期可用资讯。' if status != 'error' else '公开资讯请求或业务校验失败。',
                      raw=diagnostic.get('raw',0), accepted=len(rows), error_type=diagnostic.get('error'))
    def historical_data():
        from research.daily_factor_lab.core import load_panel
        panel = load_panel(store.meta('current_data_dir', settings.data_dir))
        from .schedule import calendar_state
        expected = calendar_state()['completed_session']
        end = panel.metadata['end']
        return result('ok' if end == expected else 'warning', '日线缓存已加载；与最近完成交易日核对。',
                      data_date=end, expected_completed_session=expected, symbols=len(panel.symbols))
    def execution():
        from .readiness import execution_readiness
        value=execution_readiness(store)
        return result('ok' if value['ready'] else 'failed','冻结策略、预算与本地执行状态通过；尚未验证真实模拟成交。' if value['ready'] else '执行前置检查未通过。',**value)
    def power():
        from .readiness import power_guard
        value=power_guard()
        return result('ok' if value['active'] else 'warning','服务防闲置休眠保护已实际生效。' if value['active'] else '服务防闲置休眠保护未核实。',**value)
    def option_chain():
        now=datetime.now(timezone.utc).date()
        data=FutuSim(settings).request('GET','/api/v1.0/quote/US.NVDA/option-chain',
            {'start':str(now+timedelta(days=14)),'end':str(now+timedelta(days=28)),'filter_standard':'STANDARD'})['data']['option_chain']
        if not isinstance(data,list) or not data:raise ValueError('No option contracts')
        return result('ok','真实期权链业务响应通过；不代表合约报价足够新鲜。',contracts=len(data))
    def option_capacity():
        broker=FutuSim(settings);broker.verify()
        probe=store.meta('option_capability',{})
        code=probe.get('probe_contract')
        if not code:return result('unverified','尚无经过核实的期权容量探测合约。')
        broker.option_capacity(code,1.)
        return result('ok','模拟账户接受期权数量查询；探测价格1美元仅用于接口验证，不会下单。',
            account_verified=True,contract_verified=True,order_endpoint_tested=False)
    jobs = [('runtime','服务与账本','本地',True,runtime),
            ('ai','AI 模型','研究核心',True,ai),
            ('quote','富途行情 REST','行情',True,quote),
            ('watchlist','富途自选列表','研究池',False,watchlist),
            ('ndx','纳指 100 成分接口','研究池',False,lambda:index('US..NDX')),
            ('spx','标普 500 成分接口','研究池',False,lambda:index('US..SPX')),
            ('history_api','富途历史日线','行情',False,history_api),
            ('paper','富途模拟账户','模拟',bool(settings.paper_enabled),account),
            ('websocket','富途 WebSocket','行情',False,websocket_probe),
            ('news','富途资讯 REST','资讯',False,rest_news),
            ('news_search','富途备用资讯','资讯',False,news_search),
            ('wallstreetcn','华尔街见闻','备用资讯',False,lambda:feed('wallstreetcn')),
            ('eastmoney','东方财富','备用资讯',False,lambda:feed('eastmoney')),
            ('history','日线缓存','数据',True,historical_data),
            ('execution','模拟执行就绪','执行',True,execution),
            ('power','常驻防休眠','本机',False,power),
            ('mcp','富途 MCP','MCP',False,mcp_probe)]
    if settings.option_paper_enabled:
        jobs += [('option_chain','富途期权链','期权',True,option_chain),
                 ('option_capacity','期权模拟账户权限','期权',True,option_capacity)]
    return jobs


class SelfCheck:
    """Single flight, cooldown, asynchronous probes; GET never runs probes."""
    def __init__(self, store):
        self.store, self.lock = store, threading.Lock()
        self.report = store.meta('self_check') or {'status':'idle','items':[],'mcp_servers':[]}
        if self.report.get('status') == 'running':
            self.report = {'status':'interrupted','items':[],'mcp_servers':[],
                           'detail':'上次自检因服务重启中断，请重新检查。'}
        self.last_finished = 0

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.report)

    def start(self):
        with self.lock:
            if self.report['status'] == 'running' or time.monotonic()-self.last_finished < 120:
                return copy.deepcopy(self.report)
            jobs = check_jobs(self.store)
            self.report = {'status':'running','started_at':utcnow(),'finished_at':None,
                           'items':[{'id':i,'name':n,'group':g,'required':r,'status':'pending','detail':'等待检查'} for i,n,g,r,_ in jobs],
                           'mcp_servers':[], 'scope':'本项目使用的服务；无交易操作，无公司 MCP 调用'}
            threading.Thread(target=self._run,args=(jobs,),daemon=True,name='quant-self-check').start()
            return copy.deepcopy(self.report)

    def _run(self, jobs):
        try:
            try: inventory = mcp_inventory()
            except Exception: inventory = [{'name':'配置读取','status':'unverified','detail':'MCP 配置无法读取'}]
            with self.lock: self.report['mcp_servers'] = inventory
            def execute(job):
                start = time.monotonic()
                try: value = job[4]()
                except Exception as exc: value = safe_failure(exc)
                return job[0], value | {'latency_ms':round((time.monotonic()-start)*1000),'checked_at':utcnow()}
            with ThreadPoolExecutor(max_workers=4) as pool:
                for task in as_completed([pool.submit(execute,j) for j in jobs]):
                    ident, value = task.result()
                    with self.lock:
                        next(r for r in self.report['items'] if r['id'] == ident).update(value)
                        if ident == 'mcp':
                            for r in self.report['mcp_servers']:
                                if r['name'] == 'futu-mcp': r.update({'status':value['status'],'detail':value['detail']})
            with self.lock:
                items = self.report['items']
                counts = {s:sum(r['status']==s for r in items) for s in ('ok','failed','warning','unconfigured','unverified')}
                self.report.update(status='completed',finished_at=utcnow(),counts=counts,
                    overall='failed' if any(r['required'] and r['status']!='ok' for r in items) else
                    'warning' if any(r['status']!='ok' for r in items) else 'ok')
                self.last_finished = time.monotonic()
                report = copy.deepcopy(self.report)
            self.store.set_meta('self_check', report)
        except Exception:
            with self.lock:
                self.report.update(status='failed',detail='自检调度失败，请重试。',finished_at=utcnow())
                self.last_finished = time.monotonic()
