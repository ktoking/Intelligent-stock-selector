"""Stored evidence for a concise dashboard; no quote or trading requests."""
import pandas as pd
import math

from .presentation import session_timeline


def age_seconds(stamp, now):
    try:
        age=(pd.Timestamp(now)-pd.Timestamp(stamp)).total_seconds()
        return age if math.isfinite(age) and age>=-10 else None
    except (TypeError,ValueError):
        return None


def dashboard_summary(store, calendar, snapshot, strategies, paper, tasks):
    now=calendar['now'];session=session_timeline(calendar)['session']
    health=store.meta('health') or {};scan=store.meta('universe_scan') or {}
    news=store.meta('news_packet') or {};brief=store.meta('daily_brief') or {}
    with store.transaction() as db:
        latest=db.execute('SELECT * FROM paper_nav ORDER BY observed DESC LIMIT 1').fetchone()
        baseline=db.execute('SELECT nav FROM paper_nav WHERE session<? ORDER BY observed DESC LIMIT 1',(session,)).fetchone()
        fill_times=[r[0] for r in db.execute('SELECT f.observed FROM fills f JOIN orders o ON o.id=f.order_id WHERE o.bucket=?',('stock',))]
    observed=latest['observed'] if latest else None
    same_day=bool(latest and latest['session']==session)
    fills=sum(str(pd.Timestamp(t).tz_convert('America/New_York').date())==session for t in fill_times)
    fills_valued=not fill_times or (latest and max(pd.Timestamp(t) for t in fill_times)<=pd.Timestamp(observed))
    valued=same_day and fills_valued and (not calendar['session'] or (age_seconds(observed,now) is not None and age_seconds(observed,now)<=120))
    no_fills=paper['fill_count']==0
    base=baseline['nav'] if baseline else store.settings.stock_budget
    if store.settings.capital_mode=='broker_equity' and baseline:
        # Deposits between the prior close and this mark are not today's P&L.
        with store.transaction() as db:
            previous=db.execute('SELECT observed FROM paper_nav WHERE session<? ORDER BY observed DESC LIMIT 1',(session,)).fetchone()
        end=observed or now
        base+=sum(f['amount'] for f in store.meta('capital_flows',[]) if previous['observed']<f['observed']<=end)
    pnl=latest['nav']-base if valued else 0. if no_fills else None
    orders=snapshot['orders']
    working=[o for o in orders if o['status'] not in ('filled','cancelled','rejected')]
    positions=snapshot['portfolios']['stock']['positions']
    held=sum(r['qty']*r['mark']*r['multiplier'] for r in positions.values())
    active=[r for r in strategies if r['stage'] in ('shadow','paper','live_review')]
    shadow=[r for r in active if r['stage']=='shadow']
    progress=[r['shadow_sessions'] for r in shadow]
    health_age=age_seconds(health.get('time'),now)
    online=bool(health.get('status')) and health_age is not None and health_age<180
    expected=calendar['session'] or calendar['completed_session']
    dates={r.get('data_date') for r in scan.get('hits',[]) if r.get('data_date')}
    market_date=max(dates) if dates else None
    scan_age=age_seconds(scan.get('created_at'),now)
    market_ok=(market_date==expected and (not calendar['session'] or (scan_age is not None and scan_age<=store.settings.broad_scan_seconds+120)))
    news_age=age_seconds(news.get('observed_at'),now)
    fresh=[{'id':'service','name':'服务','tone':'waiting' if online and (snapshot['paused'] or health.get('status')!='ok') else 'good' if online else 'stale','label':'在线 · 执行暂停' if online and snapshot['paused'] else '在线 · 局部降级' if online and health.get('status')!='ok' else '服务在线' if online else '心跳待检查','observed_at':health.get('time')},
           {'id':'market','name':'行情','tone':'good' if market_ok else 'waiting' if not calendar['session'] else 'stale',
            'label':('盘中快照' if calendar['session'] else '休市缓存') if market_ok else '等待最新扫描','asof':market_date,'observed_at':scan.get('created_at')},
           {'id':'news','name':'资讯','tone':'good' if news_age is not None and news_age<=store.settings.news_scan_seconds+120 else 'waiting',
            'label':'已更新' if news_age is not None and news_age<=store.settings.news_scan_seconds+120 else '等待更新' if calendar['session'] else '开盘后更新','observed_at':news.get('observed_at')},
           {'id':'review','name':'复盘','tone':'good' if brief.get('asof')==calendar['completed_session'] else 'waiting',
            'label':'最新收盘复盘' if brief.get('asof')==calendar['completed_session'] else '等待收盘流程','asof':brief.get('asof'),'observed_at':brief.get('generated_at')}]
    attention=[]
    from .recovery import summary
    active_ids={r['full_id'] for r in active}
    for component in summary(store):
        if component['status']=='ok':continue
        if component['component'].startswith('strategy:') and component['component'].split(':',1)[1] not in active_ids:continue
        attention.append({'id':'component:'+component['component'],'tone':'watch','title':component['component']+' 等待恢复',
                          'detail':component.get('reason') or component.get('error_type','查看运行记录与重试时间。'),'target':'runtime'})
    if snapshot['paused']:attention.append({'id':'paused','tone':'critical','title':'新增执行已暂停','detail':snapshot.get('pause_reason') or '请查看运行记录。','target':'runtime'})
    uncertain=[o for o in working if o['status'] in ('unknown','submitting')]
    if uncertain:attention.append({'id':'orders','tone':'critical','title':str(len(uncertain))+' 笔委托等待核对','detail':'成交状态未明确，请查看委托记录。','target':'paper'})
    if not online:attention.append({'id':'health','tone':'critical','title':'服务心跳需要检查','detail':'当前数据可能未继续更新。','target':'runtime'})
    if calendar['session'] and not market_ok:attention.append({'id':'market','tone':'watch','title':'等待本交易日行情','detail':'最新广池扫描尚未覆盖当前交易日，显示报价的日期需单独核对。','target':'market'})
    tick=store.meta('last_tick') or {}
    blocked=[r for r in tick.get('strategy_checks',[]) if r['status'] in ('blocked','partial','missed_window')]
    if calendar['session'] and tick.get('session')==calendar['session'] and blocked:
        attention.append({'id':'execution_gates','tone':'watch','title':str(len(blocked))+' 个策略等待执行条件','detail':'；'.join(r.get('reason') or r.get('error_type','执行条件未满足') for r in blocked),'target':'runtime'})
    execution=store.meta('paper_execution_session') or {}
    missed=[r for r in execution.get('strategies',[]) if r['status']=='missed_window']
    if missed and execution.get('session')==calendar['completed_session'] and not calendar['session']:
        attention.append({'id':'missed_entry','tone':'watch','title':execution['session']+' · '+str(len(missed))+' 个策略存在未执行订单',
            'detail':'入场窗口结束；查看各策略的报价阻断原因。服务在线不代表已下单或成交。','target':'runtime'})
    for task in tasks:
        if task['state'].get('status') in ('failed','blocked') and task['state'].get('asof')==calendar['completed_session']:
            attention.append({'id':'task:'+task['key'],'tone':'watch','title':task['name']+'待重试','detail':task['state'].get('reason') or '查看任务记录和失败原因。','target':'runtime'})
    return {'session':session,'today_pnl':pnl,'today_return_pct':pnl/base*100 if pnl is not None else None,
            'valuation_note':'尚无成交，当前为初始资金状态。' if no_fills else '当日最新账本估值；包含未实现损益。' if valued else '等待本交易日的新估值。',
            'valuation_at':observed,'position_count':len(positions),'exposure_pct':held/paper['nav']*100 if paper['nav'] else 0,
            'today_orders':sum(o['session']==session for o in orders),'today_fills':fills,'working_orders':len(working),
            'active_strategies':len(active),'shadow_strategies':len(shadow),'paper_strategies':sum(r['stage']=='paper' for r in active),
            'shadow_progress_min':min(progress) if progress else None,'shadow_progress_max':max(progress) if progress else None,
            'research_versions':len(strategies),'rejected_versions':sum(r['stage']=='rejected' for r in strategies),
            'freshness':fresh,'attention':attention,'online':online}
