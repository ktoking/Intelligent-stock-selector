"""Execution gates and OS service evidence; no network requests or order writes."""
import hashlib
import math
import os
import re
from pathlib import Path
import subprocess
import sys
import copy
import threading
import time

from .config import digest
from .store import utcnow

_DISPLAY_CACHE={}
_DISPLAY_LOCK=threading.Lock()


def readiness_view(store,snapshot):
    """Current local gates, independently of the last preflight's timestamp.

    Immutable data pointers and state changes invalidate immediately. A short
    cache keeps dashboard polling from repeatedly loading all history files.
    """
    from .exchange_clock import latest_completed_session
    active=[r for r in snapshot['strategies'] if r['stage'] in ('paper','shadow')]
    paths=[store.meta('execution_data_dir'),store.meta('current_data_dir'),store.meta('option_data_dir'),store.meta('option_data_blocked')]
    paths += [store.meta('execution_cache:'+r['id']) for r in active]
    key=digest([snapshot['paused'],snapshot['pause_reason'],store.settings.sha256,
        latest_completed_session(),paths,[(r['id'],r['stage']) for r in active],
        store.meta('paper_allocations'),store.meta('promotion_policy'),(store.meta('promotion_screen') or {}).get('selection_proof'),[(o['id'],o['status'],o['filled']) for o in snapshot['orders']],
        store.meta('last_tick',{}).get('time')])
    with _DISPLAY_LOCK:
        cached=_DISPLAY_CACHE.get(str(store.home))
        if cached and cached[0]==key and time.monotonic()-cached[1]<30:
            return copy.deepcopy(cached[2])
        value=execution_readiness(store)
        value['broker_reconciled_at']=store.meta('last_tick',{}).get('time')
        value['preflight_observed_at']=(store.meta('execution_readiness') or {}).get('observed_at')
        if len(_DISPLAY_CACHE)>4:_DISPLAY_CACHE.clear()
        _DISPLAY_CACHE[str(store.home)]=(key,time.monotonic(),value)
        return copy.deepcopy(value)


def execution_readiness(store,remote=None):
    from .runner import checked_panel
    from .panel_cache import load_panel
    blockers=[];checks={};panel=None;warnings=[]
    if store.meta('paused'):blockers.append('执行暂停：'+str(store.meta('pause_reason')))
    if not store.settings.paper_enabled:blockers.append('股票模拟执行未启用')
    from .account_selection import strict,authorized_screen
    if strict(store):
        checks['strict_account_selection']=authorized_screen(store,store.meta('promotion_screen',{}))
        if not checks['strict_account_selection']:blockers.append('日K/分钟K统一比较尚未通过或已过期；禁止新增股票买入，研究与退出监控继续')
    if any(o['status'] in ('submitting','unknown') for o in store.orders(active=True)):blockers.append('存在未核对的订单提交结果')
    expected=digest([store.settings.stock_account,store.settings.stock_market])
    checks['bound_simulated_account']=bool(store.settings.stock_account) and store.meta('bound_account')==expected
    if not checks['bound_simulated_account']:blockers.append('模拟账户与持久账本身份不一致或未完成绑定')
    alloc=store.meta('paper_allocations',{})
    if store.settings.parallel_paper:
        checks['stock_allocations']=math.isclose(sum(alloc.get('stock',{}).values()),store.settings.stock_budget,abs_tol=.001)
        owners={r['id'] for r in store.strategies(['paper','live_review'])}
        from .capital import TREASURY
        funded={k for k,v in alloc.get('stock',{}).items() if v>0 and k!=TREASURY}
        checks['stock_owners']=owners==funded and bool(owners)
        if store.settings.capital_mode=='broker_equity' and not owners and not funded and alloc.get('stock',{}).get(TREASURY,0)>0:
            checks['stock_owners']=True
        if store.settings.capital_mode=='broker_equity':
            from .champion import pending
            rotation=pending(store)
            checks['account_champion']=store.meta('account_champion')
            checks['rotation_status']=rotation['status'] if rotation else 'completed'
            if rotation:blockers.append('账户换仓：'+rotation.get('reason','等待交接'))
            elif owners!=({store.meta('account_champion')} if store.meta('account_champion') else set()):blockers.append('账户优胜策略与交易权限不一致')
    else:checks['stock_allocations']=checks['stock_owners']=len(store.strategies(['paper']))==1
    if not checks['stock_allocations'] or not checks['stock_owners']:blockers.append('股票策略成员或资金分配不一致')
    if store.settings.parallel_paper:
        attributed=sum(store.strategy_portfolio(k)['cash'] for k in alloc.get('stock',{}))
        checks['strategy_cash_reconciled']=math.isclose(attributed,store.portfolio()['cash'],abs_tol=.001)
        if not checks['strategy_cash_reconciled']:blockers.append('策略资金交接与账户现金不一致')
    try:
        panel=load_panel(store.meta('execution_data_dir',store.meta('current_data_dir',store.settings.data_dir)))
        checked=[]
        for record in store.strategies(['shadow','paper']):
            try:checked_panel(store,record,panel);checked.append(record['id'][:12])
            except Exception:
                if record['stage']=='paper':raise
                warnings.append('Shadow '+record['id'][:12]+' 数据待恢复；不授予交易权限。')
        checks['frozen_strategies']=checked
    except Exception as exc:
        checks['frozen_error_type']=type(exc).__name__
        blockers.append('日线日期、冻结成员、历史数据或引擎校验未通过')
    if store.settings.option_paper_enabled:
        from . import option_paper
        agent=store.meta('option_paper_strategy',{});option=alloc.get('option',{})
        checks['option_allocation']={k for k,v in option.items() if v>0}=={agent.get('id')} and math.isclose(sum(option.values()),store.settings.option_budget,abs_tol=.001)
        checks['option_freeze']=agent.get('authorized') is True and agent.get('spec',{}).get('engine_sha256')==hashlib.sha256(Path(option_paper.__file__).read_bytes()).hexdigest()
        if not checks['option_allocation'] or not checks['option_freeze']:blockers.append('期权实验版本或预算校验未通过')
    if remote is not None:
        from .runner import normalize_symbol,integer_quantity
        actual={normalize_symbol(p['symbol']):integer_quantity(p['qty']) for p in remote['positions'] if float(p['qty'])}
        expected_positions={s:p['qty'] for bucket in ('stock','option') for s,p in store.portfolio(bucket)['positions'].items()}
        checks['positions_reconciled']=actual==expected_positions
        balance=float(remote['cash']['balance'])
        checks['broker_cash_nonnegative']=math.isfinite(balance) and balance>=0
        if not checks['positions_reconciled'] or not checks['broker_cash_nonnegative']:blockers.append('模拟账户持仓或现金核对不通过')
    from .broker import session_now
    current=session_now();tick=store.meta('last_tick') or {}
    if current and tick.get('session')==current:
        blocked=[r['id'] for r in tick.get('strategy_checks',[]) if r['stage']=='paper' and r['status'] in ('blocked','partial','missed_window')]
        checks['blocked_stock_strategies']=blocked
        if blocked:blockers.append('盘中有股票模拟策略等待报价或信号条件；其余策略仍独立检查。')
    return {'ready':not blockers,'observed_at':utcnow(),'data_asof':panel.metadata['end'] if panel is not None else None,
        'checks':checks,'blockers':blockers,'warnings':warnings,
        'order_endpoint_tested':any(o['broker_id'] and o['filled']>0 for o in store.orders()),'live_enabled':False}


def power_guard():
    if sys.platform!='darwin':return {'active':False,'supported':False}
    parent=os.getppid();pid=os.getpid()
    assertions=subprocess.run(['pmset','-g','assertions'],capture_output=True,text=True,timeout=3).stdout
    for match in re.finditer(r'^\s*pid (\d+)\(caffeinate\):[^\n]*(?:PreventUserIdleSystemSleep|NoIdleSleepAssertion)[\s\S]*?(?=^\s*pid |\Z)',assertions,re.M):
        owner=int(match.group(1));block=match.group(0)
        # macOS can exec Python in the original PID and move the assertion into
        # a child. pmset's "on behalf of ... (pid N)" is the actual binding.
        if owner==parent or '(pid '+str(pid)+')' in block:
            return {'active':True,'supported':True,'guard_pid':owner,'service_pid':pid,
                'scope':'服务生命周期内防闲置系统休眠；不阻止显示器休眠或手动休眠。'}
    return {'active':False,'supported':True}
