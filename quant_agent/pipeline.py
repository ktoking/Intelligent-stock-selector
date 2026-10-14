"""Frozen retrospective protocol; independent accounting; real future gates."""
from __future__ import annotations
from .exchange_clock import us_calendar

from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
from contextlib import contextmanager
import fcntl

from research.daily_factor_lab.core import Policy, benchmark, catalog, load_panel
from research.daily_factor_lab.paper import data_prefix_hash
from scripts.verify_daily_factor_run import audit_ledger

from . import engine
from .ai import ask
from .config import canonical, digest
from .store import utcnow
from .evolution import compile_strategy as compile_spec
from .dataset import frozen_view


def engine_hash(spec=None):
    files = [Path(engine.__file__),Path(__file__).with_name("features.py"),Path(__file__).parents[1]/"research/daily_factor_lab/core.py"]
    if spec and spec.get('factor_expression'):files.append(Path(__file__).with_name('evolution.py'))
    return sha256(b"".join(p.read_bytes() for p in files)).hexdigest()


def artifact(store, name, value):
    directory = store.home / "artifacts"
    directory.mkdir(exist_ok=True,mode=0o700)
    path = directory / (name+".json")
    with tempfile.NamedTemporaryFile(mode="w",dir=directory,delete=False) as f:
        temporary=Path(f.name)
        f.write(canonical(value));f.flush();os.fsync(f.fileno())
    try:
        os.link(temporary,path)  # Atomic create, never replace a frozen artifact.
    finally:
        temporary.unlink()
    return str(path)


def propose(store, names=None, ai=False):
    panel = load_panel(store.meta("current_data_dir",store.settings.data_dir))
    proposals = [{"policy_name":n,"rationale":"Fixed catalog baseline; not an AI finding"} for n in (names or ["trend_buffer15","momentum_blend20","residual_momentum20"])]
    if ai:
        packet = {"purpose":"alpha_discovery","data":panel.metadata,"budgets":{"stock":10000,"option":1000},
                  "strategy_references":store.meta("strategy_references",[]),"universe_scan":store.meta("universe_scan"),
                  "previous_trials":[{"id":r["id"][:12],"stage":r["stage"],"policy":r["spec"]["policy"],
                      "factor_weights":r["spec"].get("factor_weights",[]),"gates":r["evidence"].get("gates"),
                      "stats":r["evidence"].get("stats"),"reason":r["evidence"].get("reason")} for r in store.strategies()],
                  "news":__import__('quant_agent.news',fromlist=['evidence_at']).evidence_at(store),"news_status":"Timestamped headlines only; no historical replay or causal proof"}
        response = ask(store.settings.ai_provider,packet,model_name=store.settings.ai_model,reasoning_effort=store.settings.ai_reasoning_effort)
        store.set_meta('ai_runtime',{'model':response['model'],'reasoning_effort':response.get('reasoning_effort'),'observed_at':utcnow()})
        name="discovery-"+digest(response)[:16]
        if not (store.home/"artifacts"/(name+".json")).exists():
            artifact(store,name,response)
        store.event("ai_discovery",response)
        proposals = response["result"]["candidates"]
    ids = []
    for candidate in proposals:
        policy = catalog()[candidate["policy_name"]]
        spec = {"schema_version":1,"instrument":"stock","policy":asdict(policy),"engine_sha256":engine_hash(),
                "hypothesis":candidate["rationale"],"origin":"ai" if ai else "fixed_catalog",
                "factor_weights":candidate.get("factor_weights",[]),
                "research_data_sha256":panel.metadata["input_sha256"]}
        ids.append(store.propose(spec))
    return ids


def evaluate(store):
    with (store.home/"research.lock").open("a") as lock:
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Research evaluation already running") from None
        try:
            return _evaluate(store)
        finally:
            fcntl.flock(lock,fcntl.LOCK_UN)


def _evaluate(store):
    panel = load_panel(store.meta("current_data_dir",store.settings.data_dir))
    source_panel=panel
    dates = panel.dates[220:]
    if len(dates) < 260:
        raise ValueError("Research needs warmup plus >=260 sessions")
    n = len(dates); a,b = int(n*.6),int(n*.8)
    windows = {"train":(str(dates[0].date()),str(dates[a-1].date())),
               "validation":(str(dates[a].date()),str(dates[b-1].date())),
               "retrospective_test":(str(dates[b].date()),str(dates[-1].date()))}
    results = []
    for record in store.strategies(["proposed","backtested"]):
        key, spec = record["id"],record["spec"]
        panel=frozen_view(source_panel,{'evidence':{'symbols':spec.get('research_symbols',source_panel.symbols)}})
        if spec["engine_sha256"] != engine_hash(spec):
            store.transition(key,record["stage"],"rejected",{"reason":"Engine changed; create a new strategy version"})
            continue
        if spec.get("research_data_sha256") != panel.metadata["input_sha256"]:
            store.transition(key,record["stage"],"rejected",{"reason":"Dataset differs from proposed strategy version"})
            continue
        strategy_panel, policy = compile_spec(panel,spec)
        protocol = {"strategy":key,"data":panel.metadata,"windows":windows,"cost_bps_each_side":25,
                    "stress_bps":50,"signal_delay_stress":2,"engine_sha256":engine_hash(spec),"frozen_at":utcnow(),
                    "trial_number":len(store.strategies()),"thresholds":{"min_test_sessions":52,"min_test_orders":10,
                    "positive_validation_and_test":True,"max_drawdown_pct":store.settings.max_drawdown_pct,"test_excess_over_qqq_pct":0},
                    "options_status":"awaiting historical option bid/ask, greeks, expiry/exercise and verified simulated market"}
        protocol_name = "protocol-"+key[:16]
        protocol_path = store.home/"artifacts"/(protocol_name+".json")
        if protocol_path.exists():
            old = json.loads(protocol_path.read_text())
            if old["data"]["input_sha256"] != panel.metadata["input_sha256"] or old["engine_sha256"] != engine_hash(spec):
                raise ValueError("Frozen protocol differs from data or engine; new strategy version required")
            protocol = old
        else:
            artifact(store,protocol_name,protocol)
        stats, audits, stress, delayed, ending = {}, {}, {}, {}, {}
        for label,(start,end) in windows.items():
            run = engine.simulate(strategy_panel,policy,start,end,25)
            stats[label] = run["metrics"]
            ending[label] = run["state"]
            audits[label] = audit_ledger(panel,run["curve"],run["orders"],25)
            stress[label] = engine.simulate(strategy_panel,policy,start,end,50)["metrics"]
            delayed[label] = engine.simulate(strategy_panel,policy,start,end,25,signal_lag=2)["metrics"]
        start,end = windows["retrospective_test"]
        base = benchmark(panel,start,end,25)["metrics"]
        from .charts import save_run
        save_run(store,record,run,benchmark(panel,start,end,25),[start,end],panel.metadata["input_sha256"])
        gates = {"test_sample":stats["retrospective_test"]["trading_days"]>=52,
                 "test_activity":stats["retrospective_test"]["orders"]>=10,
                 "drawdown":all(v["max_drawdown_pct"]<=store.settings.max_drawdown_pct for v in stats.values()),
                 "validation_and_test_profit":all(stats[k]["return_pct"]>0 for k in ("validation","retrospective_test")),
                 "high_cost_profit":all(stress[k]["return_pct"]>0 for k in ("validation","retrospective_test")),
                 "delayed_profit":all(delayed[k]["return_pct"]>0 for k in ("validation","retrospective_test")),
                 "qqq_excess":stats["retrospective_test"]["return_pct"]>base["return_pct"]}
        evidence = {"protocol":str(protocol_path),"protocol_sha256":digest(protocol),"data":panel.metadata,
                    "stats":stats,"independent_ledger":audits,"ending_portfolios":ending,"stress":stress,"delayed":delayed,
                    "benchmark":base,"gates":gates,"passed":all(gates.values()),
                    "limitation":"Accounting independently checked. Historical splits are retrospective, not an untouched market holdout."}
        if record["stage"] == "proposed":
            store.transition(key,"proposed","backtested",evidence)
        store.transition(key,"backtested","validated" if evidence["passed"] else "rejected",evidence)
        if evidence["passed"]:
            frozen = {**evidence,"shadow_started_at":utcnow(),"seed_asof":panel.metadata["end"],
                      "symbols":panel.symbols,
                    "seed_hash":data_prefix_hash(panel,panel.metadata["end"]),"engine_sha256":engine_hash(spec),"state":engine.empty()}
            store.transition(key,"validated","shadow",frozen)
        results.append({"id":key,"passed":evidence["passed"],"gates":gates,"stats":stats})
    return results


def shadow_record(store, record, session, state, data_hash, observed=None):
    """No historical replay can add a forward session or bypass 20 future days."""
    import exchange_calendars as xc
    import pandas as pd
    now = pd.Timestamp(observed or datetime.now(timezone.utc))
    day = pd.Timestamp(session)
    calendar = us_calendar()
    start = pd.Timestamp(record["evidence"]["shadow_started_at"])
    if (not calendar.is_session(day) or calendar.session_open(day)<=start or
            now < calendar.session_close(day) or now > calendar.session_close(day)+pd.Timedelta(days=1)):
        raise ValueError("Forward observation must seal an actually observed future completed session")
    with store.transaction() as db:
        db.execute("INSERT INTO shadow VALUES (?,?,?,?,?)", (record["id"],session,now.isoformat(),canonical(state),data_hash))
        rows = [json.loads(r[0]) for r in db.execute("SELECT state FROM shadow WHERE strategy=? ORDER BY session", (record["id"],))]
    navs = [10000]+[s["nav"] for s in rows]
    peak,dd = navs[0],0
    for nav in navs:
        peak=max(peak,nav); dd=max(dd,(1-nav/peak)*100)
    orders = sum(s.get("order_count",0) for s in rows)
    if len(rows)>=store.settings.min_shadow_sessions:
        gates = {"forward_positive":navs[-1]>10000,"forward_drawdown":dd<=10,"forward_activity":orders>=5}
        evidence={**record["evidence"],"shadow_sessions":len(rows),"shadow_nav":navs[-1],"shadow_dd_pct":dd,"shadow_gates":gates}
        if all(gates.values()) and not store.settings.parallel_paper and not store.strategies(["paper","live_review"]):
            store.transition(record["id"],"shadow","paper",evidence)
        elif not all(gates.values()):
            store.transition(record["id"],"shadow","rejected",evidence)
        else:
            with store.transaction() as db:
                db.execute("UPDATE strategies SET evidence=? WHERE id=? AND stage='shadow'",(canonical(evidence),record['id']))


def live_review(store, strategy):
    """Create a review request only. Nothing can turn on real endpoints."""
    record = next(r for r in store.strategies() if r["id"] == strategy)
    if record["stage"] != "paper":
        raise ValueError("Paper evidence required")
    request = {"created":utcnow(),"strategy":strategy,"strategy_sha256":digest(record["spec"]),
               "configuration_sha256":store.settings.sha256,"account_tail":store.settings.stock_account[-4:],
               "budgets":{"stock":10000,"option":1000},"evidence":record["evidence"],
               "required":"Human review of paper fills, costs, drawdown, strategy/version, real account and separate authorization",
               "live_adapter_implemented":False,"live_enabled":False}
    return artifact(store,"live-review-"+strategy[:16],request)
