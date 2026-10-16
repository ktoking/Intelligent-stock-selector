from __future__ import annotations

import argparse
from dataclasses import asdict, replace
import json
from pathlib import Path
import signal
import threading

from .broker import FutuSim
from .config import DEFAULT_HOME, Settings, canonical, initialize
from .pipeline import evaluate, live_review, propose
from .runner import executor_lock, reconcile, serve, tick
from .store import Store
from .web import public_status, start_web


def main(argv=None):
    parser=argparse.ArgumentParser(description="AI 量化研究与富途模拟盘；实盘端点未实现")
    parser.add_argument("--home",type=Path,default=DEFAULT_HOME)
    commands=parser.add_subparsers(dest="command",required=True)
    p=commands.add_parser("init");p.add_argument("--discover-account",action="store_true")
    p=commands.add_parser("discover");p.add_argument("--ai",action="store_true");p.add_argument("--policies",nargs="+")
    commands.add_parser("research")
    commands.add_parser("charts")
    commands.add_parser("trades")
    commands.add_parser("forecasts")
    p=commands.add_parser("brief");p.add_argument('--refresh',action='store_true')
    commands.add_parser("refresh")
    for name in ('indices','news','push-probe','evolve'):
        commands.add_parser(name)
    p=commands.add_parser("scan");p.add_argument("--enrich",action="store_true")
    p=commands.add_parser("reference");p.add_argument("path",type=Path)
    commands.add_parser("status")
    p=commands.add_parser("account-capital");p.add_argument("--total-capital",type=float,required=True);p.add_argument("--authorization",required=True)
    commands.add_parser("probe")
    p=commands.add_parser("tick");p.add_argument("--daily",action="store_true")
    p=commands.add_parser("serve");p.add_argument("--port",type=int,default=8791)
    p=commands.add_parser("pause");p.add_argument("--reason",default="Operator pause")
    commands.add_parser("resume")
    p=commands.add_parser("live-review");p.add_argument("strategy")
    args=parser.parse_args(argv)
    from .observability import configure
    configure(args.home)
    if args.command=="init":
        settings=initialize(args.home)
        store=Store(args.home,settings)
        if args.discover_account:
            if store.orders() or store.portfolio()["positions"]:
                raise ValueError("Cannot rebind an account with ledger activity")
            accounts=FutuSim(settings).accounts()
            candidates=[a for a in accounts if a["market_id"]==100 and "美股" in a.get("account_title", "")]
            if len(candidates)!=1:
                raise ValueError("Expected one observed US simulated account")
            settings=replace(settings,stock_account=str(candidates[0]["account_id"]),paper_enabled=True)
            remote=FutuSim(settings).verify()
            if remote["positions"] or any(int(o["status"]) in (2,3) for o in remote["orders"]):
                raise ValueError("Simulated account not empty; isolation required")
            config=args.home/"config.json";config.write_text(canonical(asdict(settings)));config.chmod(0o600)
        print(canonical({"home":str(args.home),"stock_budget":settings.stock_budget,"option_budget":settings.option_budget,
                         "account_tail":settings.stock_account[-4:],"paper_enabled":settings.paper_enabled,"live_enabled":False}))
        return
    settings=Settings.load(args.home);store=Store(args.home,settings)
    if args.command=="status":
        result=public_status(store)
    elif args.command=="discover":
        result={"strategies":propose(store,args.policies,args.ai)}
    elif args.command=="research":
        result=evaluate(store)
    elif args.command=="charts":
        from .charts import build_existing
        result={"chart_artifacts":build_existing(store)}
    elif args.command=='trades':
        from .activity import backfill_activity
        result=backfill_activity(store)
    elif args.command=='forecasts':
        from .forecast_tracking import update_tracking
        result=update_tracking(store)
    elif args.command=='brief':
        from .briefing import generate_brief
        result=generate_brief(store,refresh=args.refresh)
    elif args.command=="refresh":
        from .market_data import refresh_cache
        result=refresh_cache(store)
    elif args.command=="scan":
        from .universe import scan,enrich
        result=scan(store)
        if args.enrich:result["enrichment"]=enrich(store,result)
    elif args.command=='indices':
        from .indices import refresh_indices
        result=refresh_indices(store)
    elif args.command=='news':
        from .news import refresh_news
        result=refresh_news(store)
    elif args.command=='push-probe':
        from .push import probe
        result=probe(store)
    elif args.command=='evolve':
        from .evolution import evolve
        result=evolve(store)
        result={**result,'evaluation':evaluate(store)}
    elif args.command=="reference":
        from .reference import import_reference
        result=import_reference(store,args.path)
    elif args.command=="account-capital":
        from .capital import migrate_account_capital
        from .champion import screen
        from .panel_cache import load_panel
        with executor_lock(store):
            migrate_account_capital(store,FutuSim(settings),args.total_capital,args.authorization)
            store.set_meta("promotion_policy",{"enabled":True,"mode":"account_champion",
                "authorization":args.authorization,"minimum_return_improvement_pct":.5})
            result=screen(store,load_panel(store.meta("current_data_dir",store.settings.data_dir)))

    elif args.command=="probe":
        remote=reconcile(store,FutuSim(settings))
        result={"account_tail":settings.stock_account[-4:],"positions":len(remote["positions"]),"orders":len(remote["orders"]),
                "broker_balance":remote["cash"]["balance"],"allocated_stock_budget":settings.stock_budget,"allocated_option_budget":settings.option_budget,"live_enabled":False}
    elif args.command=="tick":
        with executor_lock(store):
            tick(store,FutuSim(settings),args.daily)
        result=public_status(store)
    elif args.command=="pause":
        store.pause(args.reason);result={"paused":True}
    elif args.command=="resume":
        with executor_lock(store):
            reconcile(store,FutuSim(settings))
            if any(o["status"] in ("unknown","submitting") for o in store.orders()):
                raise ValueError("Unresolved submission prevents resume")
            store.set_meta("paused",False);store.set_meta("pause_reason",None)
            store.event("operator_resume",{})
        result={"paused":False}
    elif args.command=="live-review":
        result={"review_file":live_review(store,args.strategy),"live_enabled":False}
    elif args.command=="serve":
        from .exchange_clock import us_calendar
        us_calendar()  # Construct/warm session tables before starting any threads.
        stop=threading.Event()
        for sig in (signal.SIGTERM,signal.SIGINT):
            signal.signal(sig,lambda *_:stop.set())
        server=start_web(store,args.port)
        print(f"Status: http://127.0.0.1:{args.port}",flush=True)
        try:
            serve(store,stop)
        finally:
            server.shutdown()
        return
    print(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False))


if __name__=="__main__":
    main()
