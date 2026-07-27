#!/usr/bin/env python3
"""Forward-only shadow runner for the preregistered relative-gap fade."""
from __future__ import annotations

import json, logging, signal, sys, time
from datetime import datetime, time as clock, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT=Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from scripts.okx_candidate_ws import ensure_shadow_schema,microstructure_confirmation,record_shadow_signal  # noqa:E402
from scripts.okx_intraday_agent import OKX,settings  # noqa:E402
from scripts.okx_multitimeframe_backtest import DEFAULT_SYMBOLS  # noqa:E402
from scripts.okx_research_universe import load_symbols  # noqa:E402

UTC=timezone.utc; NY=ZoneInfo('America/New_York')
STATE_PATH=ROOT/'data'/'okx_gap_shadow_state.json'
EXPERIMENT_ID='gap_relative_fade_confirm_e0936_quote_h150_t100_cost14_v3'
CHALLENGER_EXPERIMENT_ID='gap_relative_fade_confirm_e0936_quote_h60_t100_cost14_v1'
EXECUTION_EQUIVALENT=False
# Forward research is intentionally broader than the executable scanner.  The
# runner remains shadow-only, and it reloads the public-liquidity universe each
# opening evaluation so newly listed/illiquid names can be added or removed
# without modifying an order path.
BENCHMARKS={'SPY-USDT-SWAP','QQQ-USDT-SWAP','SMH-USDT-SWAP'}
LOG=logging.getLogger('okx-gap-shadow')
EVENT_PATH=ROOT/'data'/'okx_event_calendar.json'


def research_symbols() -> tuple[str,...]:
    discovered=load_symbols('forward_observation')
    return tuple(symbol for symbol in discovered if symbol not in BENCHMARKS) or tuple(
        x for x in DEFAULT_SYMBOLS if not x.startswith(('SPY-','QQQ-'))
    )


def event_labels(inst_id: str, local: datetime) -> list[str]:
    """Attach known event risk to a shadow observation; never predict direction."""
    try:
        data=json.loads(EVENT_PATH.read_text())
    except (OSError, ValueError):
        return []
    date=local.date().isoformat(); symbol=inst_id.split('-',1)[0]
    labels=[str(event.get('event_type')) for event in data.get('macro_events', [])
            if str(event.get('scheduled_at','')).startswith(date)]
    labels.extend('EARNINGS' for event in data.get('earnings', [])
                  if event.get('symbol')==symbol and str(event.get('scheduled_at','')).startswith(date))
    return labels


def next_evaluation_at(local: datetime, completed_today: bool = False) -> str:
    candidate=datetime.combine(local.date(),clock(9,36),NY)
    if completed_today or local.weekday()>=5 or local>=candidate:
        candidate+=timedelta(days=1)
    while candidate.weekday()>=5:
        candidate+=timedelta(days=1)
    return candidate.astimezone(UTC).isoformat()


def row_at(rows: list[list[str]], day, value: clock) -> list[str] | None:
    for row in rows:
        if len(row)<9 or row[8]!='1': continue
        local=datetime.fromtimestamp(int(row[0])/1000,UTC).astimezone(NY)
        if local.date()==day and (local.hour,local.minute)==(value.hour,value.minute): return row
    return None


def gap_context(current: list[list[str]], previous: list[list[str]], day) -> dict[str,float] | None:
    today_open=row_at(current,day,clock(9,30))
    previous_days=[]
    for row in previous:
        if len(row)>=9 and row[8]=='1':
            local=datetime.fromtimestamp(int(row[0])/1000,UTC).astimezone(NY)
            if local.date()<day and (local.hour,local.minute)==(15,55): previous_days.append((local.date(),row))
    if not today_open or not previous_days: return None
    previous_date,previous_close_row=max(previous_days,key=lambda x:x[0])
    previous_open=row_at(previous,previous_date,clock(9,30))
    if not previous_open: return None
    previous_close=float(previous_close_row[4]); today_open_px=float(today_open[1])
    if not previous_close or not today_open_px or not float(previous_open[1]): return None
    return {
        'gap_bps':(today_open_px/previous_close-1)*10000,
        'first5_bps':(float(today_open[4])/today_open_px-1)*10000,
        'previous_day_bps':(previous_close/float(previous_open[1])-1)*10000,
    }


def gap_bps(current: list[list[str]], previous: list[list[str]], day) -> float | None:
    context=gap_context(current,previous,day)
    return context['gap_bps'] if context else None


class Runner:
    def __init__(self):
        ensure_shadow_schema()
        self.client=OKX(settings()); self.running=True; self.last_day=''
        old={}
        try: old=json.loads(STATE_PATH.read_text())
        except Exception: pass
        self.last_state=old if old.get('experiment_id')==EXPERIMENT_ID else {}
        self.started_at=old.get('experiment_started_at') if old.get('experiment_id')==EXPERIMENT_ID else datetime.now(UTC).isoformat()

    def stop(self): self.running=False

    def evaluate_once(self, now: datetime | None=None) -> dict[str,Any]:
        local=(now or datetime.now(NY)).astimezone(NY); day=local.date(); day_text=day.isoformat()
        # The signal is known after the opening print.  A narrow but retryable
        # window makes every forward entry an observed, executable quote rather
        # than pretending that a later ticker was the historical 09:35 open.
        active=local.weekday()<5 and clock(9,36)<=local.time().replace(tzinfo=None)<clock(9,39)
        if not active or self.last_day==day_text:
            state={**self.last_state,'updated_at':datetime.now(UTC).isoformat(),'mode':'shadow_only','experiment_id':EXPERIMENT_ID,
                   'experiment_started_at':self.started_at,'active_window':active,
                   'execution_equivalent':EXECUTION_EQUIVALENT,
                   'execution_block_reason':'fixed-horizon diagnostic does not match protected-order exits',
                   'next_evaluation_at':next_evaluation_at(local,self.last_day==day_text)}
            state.setdefault('signals',0);state.setdefault('candidates',[])
            STATE_PATH.write_text(json.dumps(state,ensure_ascii=False,indent=2)); return state
        previous_day=day-timedelta(days=1)
        while previous_day.weekday()>=5: previous_day-=timedelta(days=1)
        previous_end=int(datetime.combine(previous_day,clock(16,0),NY).astimezone(UTC).timestamp()*1000)
        contexts={}
        trade_symbols=research_symbols()
        for symbol in ('SPY-USDT-SWAP',*trade_symbols):
            current=self.client.candles(symbol,limit=120,bar='5m')
            previous=self.client.candles_ending_at(symbol,previous_end,limit=100,bar='5m')
            value=gap_context(current,previous,day)
            if value is not None: contexts[symbol]=value
        spy=contexts.get('SPY-USDT-SWAP')
        if spy is None: raise RuntimeError('SPY opening gap unavailable')
        ranked=[]
        for symbol,value in contexts.items():
            if symbol not in trade_symbols: continue
            relative=value['gap_bps']-spy['gap_bps']
            relative_first5=value['first5_bps']-spy['first5_bps']
            relative_previous=value['previous_day_bps']-spy['previous_day_bps']
            confirmed=(relative*relative_first5<0 or relative*relative_previous<=0)
            if abs(relative)>=100 and confirmed:
                ranked.append((abs(relative),symbol,relative,relative_first5,relative_previous))
        ranked=sorted(ranked,reverse=True)[:5]
        entry_ts=int(local.astimezone(UTC).timestamp()*1000)
        candidates=[]
        for magnitude,symbol,relative,relative_first5,relative_previous in ranked:
            side='SHORT' if relative>0 else 'LONG'; ticker=self.client.ticker(symbol)
            last=float(ticker.get('last') or 0); bid=float(ticker.get('bidPx') or 0); ask=float(ticker.get('askPx') or 0)
            spread=(ask-bid)/((ask+bid)/2)*10000 if bid>0 and ask>bid else float('inf')
            # Shadow the price that the intended market order could actually
            # cross at, not the more flattering last-trade price.
            price=ask if side=='LONG' and ask>0 else bid if side=='SHORT' and bid>0 else last
            micro=microstructure_confirmation(symbol,side); side_depth=float(micro.get('ask_depth' if side=='LONG' else 'bid_depth') or 0)
            instrument=self.client.instrument(symbol); ct_val=float(instrument.get('ctVal') or 0)
            depth_notional=side_depth*price*ct_val
            row={'instId':symbol,'side':side,'atr14':price/120,'score':magnitude,'volume_ratio':0,
                 'spread_bps':spread if spread!=float('inf') else 999,'estimated_slippage_bps':max(0,6-spread) if spread!=float('inf') else 999,
                 'microstructure':micro,'experiment_id':EXPERIMENT_ID,'horizon_minutes':150}
            record_shadow_signal(row,entry_ts,price,'GAP_FADE_DIAGNOSTIC_PASS')
            # Pre-registered early-exit challenger.  It observes the exact
            # same executable quote and liquidity state as the 150m baseline;
            # only the independent 60-minute label horizon differs.  Neither
            # diagnostic stage has an order path.
            challenger={**row,'experiment_id':CHALLENGER_EXPERIMENT_ID,'horizon_minutes':60}
            record_shadow_signal(challenger,entry_ts,price,'GAP_FADE_60M_CHALLENGER')
            liquidity_qualified=bool(micro.get('aligned') and spread<=5 and depth_notional>=5000)
            executable=bool(EXECUTION_EQUIVALENT and liquidity_qualified)
            if executable: record_shadow_signal(row,entry_ts,price,'GAP_FADE_EXECUTABLE_PASS')
            candidates.append({'inst_id':symbol,'side':side,'relative_gap_bps':round(relative,2),'price':price,
                               'relative_first5_bps':round(relative_first5,2),'relative_previous_day_bps':round(relative_previous,2),
                               'spread_bps':round(spread,2),'depth_notional_usdt':round(depth_notional,2),
                               'event_labels':event_labels(symbol,local),
                               'challenger_experiment_id':CHALLENGER_EXPERIMENT_ID,
                               'challenger_horizon_minutes':60,
                               'liquidity_qualified':liquidity_qualified,'executable':executable})
        self.last_day=day_text
        state={'updated_at':datetime.now(UTC).isoformat(),'mode':'shadow_only','experiment_id':EXPERIMENT_ID,
               'experiment_started_at':self.started_at,'active_window':True,'entry_ts':entry_ts,
               'signals':len(candidates),'executable_signals':sum(x['executable'] for x in candidates),
               'execution_equivalent':EXECUTION_EQUIVALENT,
               'execution_block_reason':'fixed-horizon diagnostic does not match protected-order exits',
               'next_evaluation_at':next_evaluation_at(local,True),'candidates':candidates}
        self.last_state=state
        STATE_PATH.write_text(json.dumps(state,ensure_ascii=False,indent=2)); return state

    def run(self):
        while self.running:
            try: self.evaluate_once()
            except Exception as exc:
                LOG.exception('gap shadow failed'); STATE_PATH.write_text(json.dumps({'updated_at':datetime.now(UTC).isoformat(),'mode':'shadow_only','experiment_id':EXPERIMENT_ID,'experiment_started_at':self.started_at,'error':str(exc)},ensure_ascii=False,indent=2))
            for _ in range(60):
                if not self.running: break
                time.sleep(1)


def main():
    runner=Runner();signal.signal(signal.SIGTERM,lambda *_:runner.stop());signal.signal(signal.SIGINT,lambda *_:runner.stop());runner.run()

if __name__=='__main__':
    logging.basicConfig(level='INFO',format='%(asctime)s %(levelname)s %(message)s');main()
