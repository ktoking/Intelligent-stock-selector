"""Read-only seven-session return scenarios; never Shadow or execution evidence."""
from functools import lru_cache
import json

import exchange_calendars as xc
import numpy as np
import pandas as pd

from .exchange_clock import us_calendar

from .config import canonical, digest

MODEL = 'shrunk-market-block-bootstrap-v1'
WINDOW, MIN_RETURNS, HORIZON, PATHS, BLOCK = 63, 40, 7, 4096, 3


def future_sessions(origin, horizon=HORIZON):
    cal = us_calendar()
    day = pd.Timestamp(origin)
    sessions = cal.sessions_in_range(day+pd.Timedelta(days=1), day+pd.Timedelta(days=40))
    return [str(s.date()) for s in sessions[:horizon]]


def project(chart, origin, today):
    """Use completed, aligned daily equity observations through origin only."""
    curves = []
    for key in ('curve', 'benchmark_curve'):
        rows = [r for r in chart.get(key, []) if r['date'] <= origin]
        if not rows or rows[-1]['date'] != origin:
            return {'available':False, 'reason':'该版本曲线未覆盖最新收盘日 '+origin+'，不把旧数据预测显示为今天的预测。'}
        if len({r['date'] for r in rows}) != len(rows):
            return {'available':False, 'reason':'日线日期重复，预测暂不可用。'}
        curves.append({r['date']:float(r['equity']) for r in rows})
    dates = sorted(set(curves[0]) & set(curves[1]))[-(WINDOW+1):]
    if len(dates)-1 < MIN_RETURNS:
        return {'available':False, 'reason':'至少需要40个完整且对齐的策略与QQQ日收益。'}
    expected = us_calendar().sessions_in_range(dates[0], origin)
    if dates != [str(s.date()) for s in expected]:
        return {'available':False, 'reason':'训练窗口存在缺失交易日，不能把多日变化当单日收益。'}
    nav = np.array([[curve[d] for d in dates] for curve in curves])
    if not np.isfinite(nav).all() or np.any(nav <= 0):
        return {'available':False, 'reason':'净值数据无效，预测暂不可用。'}
    returns = np.diff(np.log(nav), axis=1)
    y, x = returns
    variance = np.var(x)
    beta = float(np.mean((x-x.mean())*(y-y.mean()))/(variance*1.1)) if variance > 1e-12 else 0.
    alpha = float(y.mean()-beta*x.mean())
    residuals = y-beta*x-alpha
    # Shared seed and market paths make the QQQ projection identical across
    # strategies with the same benchmark observations, irrespective of IDs.
    seed = int(digest({'model':MODEL, 'dates':dates, 'benchmark':nav[1].tolist()})[:16],16)
    rng = np.random.default_rng(seed)
    indexes = np.concatenate([(rng.integers(0,len(x),size=(PATHS,1))+np.arange(BLOCK)) % len(x)
                              for _ in range((HORIZON+BLOCK-1)//BLOCK)],axis=1)[:,:HORIZON]
    drift = float(x.mean())*.25
    momentum = float(x[-10:].mean()-x.mean())*.2
    q_steps = (x-x.mean())[indexes] + drift + momentum*(.75**np.arange(HORIZON))
    s_steps = beta*q_steps + alpha*.25 + residuals[indexes]
    days = future_sessions(origin)
    def summarize(steps, start):
        paths = start*np.exp(np.cumsum(steps,axis=1))
        bands = np.quantile(paths,[.1,.5,.9],axis=0)
        rows=[]
        for i, day in enumerate([origin]+days):
            low, mid, high = [float(start)]*3 if i==0 else bands[:,i-1].tolist()
            rows.append({'date':day, 'equity':mid, 'low_equity':low, 'high_equity':high,
                         'return_pct':(mid/start-1)*100, 'low_return_pct':(low/start-1)*100,
                         'high_return_pct':(high/start-1)*100})
        return rows
    return {'available':True, 'kind':'statistical_forecast', 'model':MODEL,
            'origin_date':origin, 'today':today, 'horizon_sessions':HORIZON, 'future_dates':days,
            'training_start':dates[0], 'training_end':dates[-1], 'training_returns':len(x),
            'paths':PATHS, 'interval':{'lower_quantile':.1,'upper_quantile':.9,'label':'80%模拟情景区间，未校准'},
            'parameters':{'beta_to_qqq':beta, 'shrunk_alpha_daily':alpha*.25, 'shrunk_qqq_drift_daily':drift},
            'strategy_curve':summarize(s_steps,nav[0,-1]), 'benchmark_curve':summarize(q_steps,nav[1,-1]),
            'source':chart.get('source','Futunn 冻结日线回测净值'), 'data_hash':chart.get('data_hash'),
            'method':'最近最多63个交易日日收益；QQQ趋势与策略Alpha向0收缩；按3日块联合重采样4096条路径。',
            'limitation':'统计情景尚未经未来样本验证；当前选股和已查看回测含选择偏差。预测未重演未来选股、调仓与额外交易成本；不计入收益排名、Shadow或模拟实绩。'}


@lru_cache(maxsize=64)
def _cached(payload, origin, today):
    return canonical(project(json.loads(payload),origin,today))


def forecast_data(store, strategy, calendar):
    if not any(r['id']==strategy for r in store.strategies()):
        return None
    path = store.home/'artifacts'/('chart-'+strategy+'.json')
    if not path.exists():
        return {'available':False, 'reason':'该策略暂无可核验历史曲线，无法生成收益预测。'}
    today = str(pd.Timestamp(calendar['now']).tz_convert('Asia/Shanghai').date())
    # Return a new object: callers cannot mutate the cached forecast or chart.
    return json.loads(_cached(path.read_text(),calendar['completed_session'],today))
