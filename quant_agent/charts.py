"""Persist actual retrospective paths; never invent a forward equity curve."""
import json
import math
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from research.daily_factor_lab.core import benchmark, load_panel
from . import engine
from .evolution import compile_strategy as compile_spec
from .pipeline import artifact, engine_hash


@lru_cache(maxsize=256)
def _chart_window(path, modified, size):
    return json.loads(Path(path).read_text()).get('window')


def chart_window(path):
    """Small presentation metadata, cached until the saved artifact changes."""
    try:
        stat=path.stat()
        return _chart_window(str(path),stat.st_mtime_ns,stat.st_size)
    except (OSError,ValueError):
        return None


@lru_cache(maxsize=16)
def _minute_benchmark(path, modified, size, data_hash, start, end, cost_bps):
    # Read the frozen dataset, never current quotes or a new universe. This is
    # a chart projection only; the trading artifacts and engine stay untouched.
    from .intraday_data import load
    data=load(path,data_hash)
    bars=[r for r in data['bars']['US.QQQ'] if start<=r['session']<=end]
    if not bars or not math.isfinite(bars[0]['open']) or bars[0]['open']<=0:
        raise ValueError('QQQ minute benchmark has no opening price')
    fee=1-float(cost_bps)/10000
    points=[]
    for i,r in enumerate(bars):
        if not math.isfinite(r['close']) or r['close']<=0:
            raise ValueError('QQQ minute benchmark has an invalid close')
        at=datetime.fromtimestamp(r['end_ms']/1000,timezone.utc).isoformat()
        equity=10000*r['close']/bars[0]['open']*fee*(fee if i==len(bars)-1 else 1)
        points.append((at,equity))
    if len({r[0] for r in points})!=len(points):
        raise ValueError('QQQ minute benchmark has duplicate timestamps')
    return tuple(points)


def with_minute_benchmark(record,chart):
    """Restore the missing QQQ display only if it matches frozen evidence."""
    if record['spec'].get('execution_timeframe')!='5m' or chart.get('benchmark_curve') or not chart.get('curve'):
        return chart
    try:
        evidence=record['evidence'];path=Path(evidence['minute_data_path']);stat=path.stat()
        start,end=chart['window']
        points=_minute_benchmark(str(path),stat.st_mtime_ns,stat.st_size,
            record['spec']['research_data_sha256'],start,end,chart['cost_bps'])
        if [p[0] for p in points]!=[p['date'] for p in chart['curve']]:
            raise ValueError('QQQ minute timestamps differ from the strategy')
        result=(points[-1][1]/10000-1)*100
        if not math.isclose(result,evidence['benchmark']['return_pct'],abs_tol=1e-8):
            raise ValueError('QQQ minute return differs from frozen evidence')
        return {**chart,'benchmark_curve':[{'date':at,'equity':nav} for at,nav in points],
                'benchmark_metrics':{'return_pct':result},
                'benchmark_source':'Futunn frozen unadjusted QQQ 5m bars; same window; entry/exit costs included'}
    except (OSError,ValueError,KeyError,TypeError,IndexError):
        return {**chart,'benchmark_unavailable':'QQQ 冻结分钟数据或同区间核对未通过，暂不绘制基准。'}


def save_run(store,record,run,base,window,data_hash):
    from .activity import save_activity
    save_activity(store,record,run,data_hash)
    name="chart-"+record["id"]
    path=store.home/"artifacts"/(name+".json")
    if path.exists():
        return str(path)
    payload={"strategy":record["id"],"data_hash":data_hash,"window":window,"kind":"retrospective_test",
             "cost_bps":25,"source":"Futunn adjusted daily OHLCV; hypothetical next-open execution",
             "curve":run["curve"].to_dict("records"),"benchmark_curve":base["curve"].to_dict("records"),
             "metrics":run["metrics"],"benchmark_metrics":base["metrics"]}
    return artifact(store,name,payload)


def build_existing(store):
    """One offline reconstruction of same-version evidence, with metric equality."""
    records=[r for r in store.strategies() if r["evidence"].get("stats") and r['spec'].get('execution_timeframe')!='5m']
    outputs=[]
    for r in records:
        if (store.home/"artifacts"/("chart-"+r["id"]+".json")).exists():
            continue
        evidence=r["evidence"]
        if r["spec"]["engine_sha256"]!=engine_hash(r['spec']):
            continue
        protocol=json.loads(open(evidence["protocol"]).read())
        # The protocol directory is still versioned, even after the current pool grows.
        data_dir=store.meta("current_data_dir",store.settings.data_dir)
        panel=load_panel(data_dir)
        if panel.metadata["input_sha256"]!=r["spec"]["research_data_sha256"]:
            continue
        panel,policy=compile_spec(panel,r["spec"])
        start,end=protocol["windows"]["retrospective_test"]
        run=engine.simulate(panel,policy,start,end,25)
        expected=evidence["stats"]["retrospective_test"]
        for metric in ("return_pct","max_drawdown_pct","cost_dollars","orders","trading_days","avg_exposure_pct"):
            if not math.isclose(run["metrics"][metric],expected[metric],rel_tol=1e-10,abs_tol=1e-8):
                raise ValueError("Reconstructed chart differs from frozen evidence")
        outputs.append(save_run(store,r,run,benchmark(panel,start,end,25),[start,end],panel.metadata["input_sha256"]))
    return outputs
