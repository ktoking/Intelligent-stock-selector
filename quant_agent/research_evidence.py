"""Read-only lineage from actual trials, artifacts and persisted job outcomes."""
import json
from pathlib import Path

from .promotion import score
from .presentation import expression_formula


def research_pending(store,asof):
    enabled=store.settings.daily_research or store.settings.evolution_enabled
    return enabled and store.meta('research_cycle_completed:'+asof)!=store.meta('current_data_dir',store.settings.data_dir)


def trial_evidence(store,identity):
    record=next((r for r in store.strategies() if r['id']==identity),None)
    if record is None:return None
    materials={}
    for kind,name in [('protocol','protocol-'+identity[:16]),('factor','evolution-'+identity[:16]),('historical_curve','chart-'+identity)]:
        path=store.home/'artifacts'/(name+'.json')
        if path.is_file():materials[kind]=json.loads(path.read_text())
    return {'strategy':identity,'spec':record['spec'],'stage':record['stage'],'evidence':record['evidence'],
        'materials':materials,'kind':'research_evidence','limitation':'历史研究、真实Shadow与券商模拟成交必须分开解释。'}


def research_summary(store,asof):
    directory=Path(store.meta('current_data_dir',store.settings.data_dir));manifest=directory/'completed.json'
    data_hash=json.loads(manifest.read_text()).get('input_sha256') if manifest.exists() else None
    artifacts={};artifact_errors=[]
    for path in (store.home/'artifacts').glob('evolution-*.json'):
        try:value=json.loads(path.read_text())
        except (OSError,ValueError) as exc:
            artifact_errors.append({'file':path.name,'error_type':type(exc).__name__});continue
        if value.get('strategy'):artifacts[value['strategy']]=value
    with store.transaction() as db:
        shadows={r[0]:r[1] for r in db.execute('SELECT strategy,COUNT(*) FROM shadow GROUP BY strategy')}
    queue=store.meta('promotion_queue',[]);screen=store.meta('promotion_screen',{})
    initial=store.meta('strategy_initial_capital',{})
    rows=[]
    for record in reversed(store.strategies()):
        spec,e=record['spec'],record['evidence'];factor=artifacts.get(record['id'],{})
        metrics=e.get('stats',{});test=metrics.get('retrospective_test',{})
        failed=[k for k,v in e.get('gates',{}).items() if not v]
        comparison=next((r for r in queue if r['candidate']==record['id']),None)
        comparisons=[r for r in screen.get('comparisons',[]) if r['candidate']==record['id']]
        comparison=comparison or (max(comparisons,key=lambda r:r['improvement']) if comparisons else None)
        code=Path(__file__).parents[1]/'research/evolved_factors'/('factor_'+record['id'][:12]+'.py')
        rows.append({'id':record['id'][:12],'full_id':record['id'],'name':spec.get('factor_name',spec['policy']['name']),
            'created':record['created'],'stage':record['stage'],'is_factor':bool(spec.get('factor_expression')),
            'origin':spec.get('origin','fixed_catalog'),'hypothesis':spec.get('hypothesis'),
            'falsification':spec.get('falsification'),'formula':expression_formula(spec['factor_expression']) if spec.get('factor_expression') else spec['policy'].get('factor','未设定'),
            'causal_checks':factor.get('checks',{}),'causal_status':factor.get('status'),
            'code_url':'/api/factor/'+record['id'] if code.is_file() and spec.get('factor_expression') else None,
            'evidence_url':'/api/research-evidence/'+record['id'],'data_sha256':spec.get('research_data_sha256'),
            'current_version':spec.get('research_data_sha256')==data_hash,'asof':e.get('seed_asof',e.get('data',{}).get('end')),
            'historical_return_pct':test.get('return_pct'),'max_drawdown_pct':test.get('max_drawdown_pct'),
            'benchmark_return_pct':e.get('benchmark',{}).get('return_pct'),'validation':metrics.get('validation'),
            'stress':e.get('stress',{}).get('retrospective_test'),'delay':e.get('delayed',{}).get('retrospective_test'),
            'ledger_audited':len(e.get('independent_ledger',{}))==3,
            'gates':e.get('gates',{}),'failed_gates':failed,'reason':e.get('reason'),
            'score':score(metrics) if all(k in metrics for k in ('validation','retrospective_test')) else None,
            'shadow_sessions':shadows.get(record['id'],0),'promotion':comparison,
            'paper_allocation':store.meta('paper_allocations',{}).get('stock',{}).get(record['id']),
            'initial_capital':initial.get(record['id']),'retirement':e.get('retirement')})
    steps=[]
    cycle_matches=store.meta('research_cycle_completed:'+asof)==str(directory)
    for key,label in [('review','每日复盘'),('discovery','因子组合假设'),('evolution','新表达式因子'),('backtest','回测与独立核账'),('promotion','晋升比较')]:
        job=store.meta('task:'+key) or {}
        current=job.get('asof')==asof and (job['data_dir']==str(directory) if job.get('data_dir') else cycle_matches)
        steps.append({'key':key,'label':label,'status':job.get('status') if current else 'pending',
            'finished_at':job.get('finished_at') if current else None,
            'last_asof':job.get('asof'),'reason':job.get('reason') or ('当前数据版本尚未完成此步骤；旧记录不作为新版本完成证据。' if not current and job.get('asof')==asof else '本日尚未执行；上次记录 '+job['asof'] if not current and job.get('asof') else None)})
    latest=[r for r in rows if r['current_version']]
    return {'asof':asof,'data_sha256':data_hash,'pending':research_pending(store,asof),'steps':steps,'artifact_errors':artifact_errors,
        'trials':rows,'factor_count':sum(r['is_factor'] for r in rows),'current_trials':len(latest),
        'current_factors':sum(r['is_factor'] for r in latest),'current_rejected':sum(r['stage']=='rejected' for r in latest),
        'queued':len([r for r in queue if r['status'] in ('ready','waiting')]),'promotion_policy':store.meta('promotion_policy',{}),
        'queue':queue,'screen':screen,'recovery_note':store.meta('research_recovery_note'),
        'limitation':'每个行情版本最多1个新表达式、30个研究试验；历史择优不能证明未来Alpha。'}


def append_research_review(store,asof):
    summary=research_summary(store,asof);path=store.home/'reports'/(asof+'.md')
    if not path.exists():return
    heading='## 因子研究与模拟晋升（系统核验）'
    content=path.read_text().split(heading)[0].rstrip()
    lines=['',heading,'',f"本版本 **{summary['current_trials']}个试验、{summary['current_factors']}个新表达式、{summary['current_rejected']}个淘汰**。行情版本 `{str(summary['data_sha256'])[:12]}`。",'']
    for trial in summary['trials']:
        if not trial['current_version']:continue
        lines.append('### '+trial['name']+' · '+trial['stage'])
        if trial['hypothesis']:lines.append(trial['hypothesis'])
        lines.append('公式：'+trial['formula'])
        if trial['historical_return_pct'] is not None:
            lines.append(f"回顾收益 {trial['historical_return_pct']:.2f}%；最大回撤 {trial['max_drawdown_pct']:.2f}%；独立核账{'已通过' if trial['ledger_audited'] else '未验证'}。")
        if trial['failed_gates']:lines.append('淘汰门槛：'+', '.join(trial['failed_gates']))
        if trial['promotion']:
            q=trial['promotion'];lines.append(f"同区间评分 {q['candidate_score']:.3f}，原策略 {q['incumbent_score']:.3f}；"+str(q.get('reason') or '等待空仓资金交接'))
        lines.append('实验材料：http://127.0.0.1:8791'+trial['evidence_url'])
        if trial['code_url']:lines.append('独立因子代码：http://127.0.0.1:8791'+trial['code_url'])
        lines.append('')
    if store.meta('promotion_policy',{}).get('mode')=='account_champion':
        lines.append(f'账户优胜策略模式：候选并行观察，通过统一区间检验后收益择优；撤旧委托、退出淘汰持仓并交收后交接。股票授权本金 {store.settings.stock_budget:,.2f} 美元，期权预留 {store.settings.option_budget:,.2f} 美元。资金流不计盈利，历史选优不是未来保证。')
    else:lines.append('自动晋升只替换空仓、无未完成订单且资金已交收的槽位；保留原始规则和历史记录，股票总额度10000美元、期权1000美元独立。模拟等待期按用户授权豁免；历史筛选及因果检查不等于未来Alpha验证。')
    from .files import atomic_write
    atomic_write(path,content+'\n'+'\n'.join(lines))
