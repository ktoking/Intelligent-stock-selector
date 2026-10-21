"""Historical navigation metadata and frozen QQQ chart reconstruction."""
from copy import deepcopy
from datetime import datetime
import json

import pytest

from quant_agent.charts import with_minute_benchmark
from quant_agent.config import digest,initialize
from quant_agent.store import Store
from quant_agent.web import chart_data,public_status


def minute_chart(tmp_path):
    dates=['2026-10-16T13:35:00+00:00','2026-10-16T13:40:00+00:00','2026-10-19T20:00:00+00:00']
    bars=[{'open':100,'close':close,'session':at[:10],'end_ms':int(datetime.fromisoformat(at).timestamp()*1000)}
          for at,close in zip(dates,[101,103,99])]
    data={'bars':{'US.QQQ':bars}}
    path=tmp_path/'minute.json';path.write_text(json.dumps(data))
    record={'spec':{'execution_timeframe':'5m','research_data_sha256':digest(data)},
            'evidence':{'minute_data_path':str(path),'benchmark':{'return_pct':(99/100*.997**2-1)*100}}}
    chart={'curve':[{'date':at,'equity':10000} for at in dates], 'benchmark_curve':[],
           'window':['2026-10-16','2026-10-19'],'cost_bps':30}
    return record,chart,path


def test_minute_qqq_is_same_window_same_timestamps_costs_and_frozen_return(tmp_path):
    record,chart,path=minute_chart(tmp_path);before=deepcopy(chart);raw=path.read_bytes()
    result=with_minute_benchmark(record,chart)
    assert [p['date'] for p in result['benchmark_curve']]==[p['date'] for p in chart['curve']]
    assert result['benchmark_curve'][0]['equity']==pytest.approx(10000*1.01*.997)
    assert result['benchmark_metrics']['return_pct']==pytest.approx(record['evidence']['benchmark']['return_pct'])
    assert chart==before and path.read_bytes()==raw


@pytest.mark.parametrize('fault',['hash','time','return','missing'])
def test_bad_benchmark_does_not_replace_or_break_strategy_history(tmp_path,fault):
    record,chart,path=minute_chart(tmp_path)
    if fault=='hash':record['spec']['research_data_sha256']='wrong'
    elif fault=='time':chart['curve'][0]['date']='2026-10-16T13:30:00+00:00'
    elif fault=='return':record['evidence']['benchmark']['return_pct']=42
    else:path.unlink()
    result=with_minute_benchmark(record,chart)
    assert result['curve']==chart['curve'] and not result['benchmark_curve']
    assert result['benchmark_unavailable']


def test_daily_history_and_metadata_survive_new_minute_focus(tmp_path):
    store=Store(tmp_path,initialize(tmp_path,quote_push_enabled=False))
    daily=store.propose({'policy':{'name':'residual_momentum20'},'research_data_sha256':'old-daily'})
    minute=store.propose({'policy':{'name':'opening_range_5m'},'execution_timeframe':'5m','research_data_sha256':'new-minute'})
    saved={'curve':[{'date':'2026-10-12','equity':12321.19}],
           'benchmark_curve':[{'date':'2026-10-12','equity':10995.96}],'window':['2026-05-04','2026-10-12']}
    path=tmp_path/'artifacts'/('chart-'+daily+'.json');path.parent.mkdir(exist_ok=True);path.write_text(json.dumps(saved))
    before=path.read_bytes()
    assert chart_data(store,daily,'history')==saved
    status=public_status(store)
    rows={r['full_id']:r for r in status['strategies']}
    assert rows[daily]['execution_timeframe']=='1d'
    assert rows[daily]['history_window']==saved['window']
    assert rows[daily]['chart_available'] and rows[minute]['execution_timeframe']=='5m'
    assert path.read_bytes()==before and not store.orders()
