"""Persist actual retrospective paths; never invent a forward equity curve."""
import json
import math

from research.daily_factor_lab.core import benchmark, load_panel
from . import engine
from .evolution import compile_strategy as compile_spec
from .pipeline import artifact, engine_hash


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
    records=[r for r in store.strategies() if r["evidence"].get("stats")]
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
