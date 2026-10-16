"""Exchange calendar driven network activity; the local dashboard stays available."""
from datetime import datetime, timezone

import exchange_calendars as xc
import pandas as pd

from .broker import session_now
from .exchange_clock import us_calendar


def calendar_state(now=None):
    now=pd.Timestamp(now or datetime.now(timezone.utc))
    if now.tzinfo is None:
        raise ValueError("Timezone required")
    cal=us_calendar()
    day=now.tz_convert("America/New_York").date().isoformat()
    days=cal.sessions_in_range(pd.Timestamp(day)-pd.Timedelta(days=10),pd.Timestamp(day)+pd.Timedelta(days=14))
    next_day=next(d for d in days if cal.session_open(d)>now)
    completed=next(d for d in reversed(days) if cal.session_close(d)<=now)
    return {"session":session_now(now),"next_session":str(next_day.date()),
            "next_open":cal.session_open(next_day).isoformat(),"next_close":cal.session_close(next_day).isoformat(),
            "completed_session":str(completed.date()),"completed_close":cal.session_close(completed).isoformat(),
            "now":now.isoformat(),"timezone":"Asia/Shanghai"}


def daily_due(store,now=None):
    state=calendar_state(now)
    stamp=pd.Timestamp(state["now"])
    asof=state["completed_session"]
    retry=store.meta("daily_retry_after:"+asof)
    from .research_evidence import research_pending
    from .market_data import finalization_due
    return (not state["session"] and stamp>=pd.Timestamp(state["completed_close"])+pd.Timedelta(minutes=15)
            and (not store.meta("daily_completed:"+asof) or research_pending(store,asof) or finalization_due(store,stamp))
            and (not retry or stamp>=pd.Timestamp(retry)))


def safe_research_due(store,now):
    try:return daily_due(store,now)
    except Exception as exc:
        from .recovery import failed
        failed(store,'research_scheduler',exc)
        return False


def activity(store,now=None,boot=False):
    state=calendar_state(now)
    stamp=pd.Timestamp(state["now"])
    if state["session"]:
        mode="market_open"
    elif store.orders(active=True):
        mode="order_recovery"
    elif any(store.meta("shadow_pending:"+r["id"]) for r in store.strategies(["shadow"])):
        mode="close_settlement"
    elif boot:
        mode="startup_reconcile"
    elif store.meta('promotion_policy',{}).get('mode')!='account_champion' and any(r['status']=='ready' for r in store.meta('promotion_queue',[])):
        mode='paper_promotion'
    elif stamp>=pd.Timestamp(state["next_open"])-pd.Timedelta(minutes=5) and store.meta("preflight_session")!=state["next_session"]:
        mode="pre_open"
    elif safe_research_due(store,stamp):
        mode="daily_research"
    else:
        mode="idle"
    return {**state,"mode":mode,"poll_seconds":store.settings.poll_seconds,
            "daily_retry_after":store.meta("daily_retry_after:"+state["completed_session"])}
