"""Execution gates and OS service evidence; no network requests or order writes."""
import hashlib
import math
import os
import re
from pathlib import Path
import subprocess
import sys

from .config import digest
from .store import utcnow


def execution_readiness(store,remote=None):
    from .runner import checked_panel
    from research.daily_factor_lab.core import load_panel
    blockers=[];checks={};panel=None
    if store.meta('paused'):blockers.append('执行暂停：'+str(store.meta('pause_reason')))
    if not store.settings.paper_enabled:blockers.append('股票模拟执行未启用')
    if any(o['status'] in ('submitting','unknown') for o in store.orders(active=True)):blockers.append('存在未核对的订单提交结果')
    expected=digest([store.settings.stock_account,store.settings.stock_market])
    checks['bound_simulated_account']=bool(store.settings.stock_account) and store.meta('bound_account')==expected
    if not checks['bound_simulated_account']:blockers.append('模拟账户与持久账本身份不一致或未完成绑定')
    alloc=store.meta('paper_allocations',{})
    if store.settings.parallel_paper:
        checks['stock_allocations']=math.isclose(sum(alloc.get('stock',{}).values()),store.settings.stock_budget,abs_tol=.001)
        owners={r['id'] for r in store.strategies(['paper','live_review'])}
        checks['stock_owners']=owners=={k for k,v in alloc.get('stock',{}).items() if v>0} and bool(owners)
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
            checked_panel(store,record,panel);checked.append(record['id'][:12])
        checks['frozen_strategies']=checked
    except Exception as exc:
        checks['frozen_error_type']=type(exc).__name__
        blockers.append('日线日期、冻结成员、历史数据或引擎校验未通过')
    if store.settings.option_paper_enabled:
        from . import option_paper
        agent=store.meta('option_paper_strategy',{});option=alloc.get('option',{})
        checks['option_allocation']=set(option)=={agent.get('id')} and math.isclose(sum(option.values()),store.settings.option_budget,abs_tol=.001)
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
        'checks':checks,'blockers':blockers,'order_endpoint_tested':False,'live_enabled':False}


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
