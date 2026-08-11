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
from scripts.okx_candidate_ws import (build_book_tca_snapshot,ensure_shadow_schema,
                                      microstructure_confirmation,record_shadow_signal,
                                      unavailable_book_tca_snapshot)  # noqa:E402
from scripts.okx_intraday_agent import OKX,atr,settings  # noqa:E402
from scripts.okx_trade_replay import closed_chronological  # noqa:E402
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
V5_REPORT_PATH=ROOT/'data'/'okx_gap_strategy_v5_backtest.json'
V5_EXPERIMENT_ID='gap_relative_fade_adaptive_horizon_v5_frozen_20260808_r2'
V5_STAGE='GAP_FADE_V5_FORWARD'
V5_FROZEN_AT='2026-08-08T04:08:03+00:00'
V5_SYMBOL_EDGE_SIZING_ID='gap_v5_same_gross_symbol_edge_sizing_shadow_20260808_r2'
V5_STRICT_CONFIRM_EXPERIMENT_ID='gap_v5_strict_first5_no_backfill_shadow_20260808'
V5_STRICT_CONFIRM_STAGE='GAP_FADE_V5_STRICT_CONFIRM_FORWARD'
V5_STRICT_CONFIRM_FROZEN_AT='2026-08-08T04:15:28+00:00'


def research_symbols() -> tuple[str,...]:
    discovered=load_symbols('forward_observation')
    return tuple(symbol for symbol in discovered if symbol not in BENCHMARKS) or tuple(
        x for x in DEFAULT_SYMBOLS if not x.startswith(('SPY-','QQQ-'))
    )


def top_ranked(rows: list[tuple[float,str,float,float,float]],
               allowed: set[str] | None = None, limit: int = 5) -> list[tuple[float,str,float,float,float]]:
    """Select a ranked lane without letting research-only names consume Demo slots."""
    return [row for row in rows if allowed is None or row[1] in allowed][:limit]


def shadow_market_data_symbols(research_pool: tuple[str,...], execution_pool: tuple[str,...],
                               v5_historical_pool: tuple[str,...]) -> tuple[str,...]:
    """Fetch every lane's symbols without changing any lane's ranking universe."""
    return tuple(dict.fromkeys((*research_pool,*execution_pool,*v5_historical_pool)))


def v5_strict_first5_subset(rows: list[dict[str,Any]]) -> list[dict[str,Any]]:
    """Filter the already-ranked V5 list; deliberately never backfill vacancies."""
    return [row for row in rows
            if float(row['relative_gap_bps'])*float(row['relative_first5_bps'])<0]


def strategy_lanes() -> dict[str,dict[str,Any]]:
    """Make the optional legacy Demo and both V5 shadow lanes unambiguous."""
    return {
        'legacy_demo': {
            'experiment_id':EXPERIMENT_ID,'candidate_field':'demo_candidates',
            'strategy':'legacy 150-minute relative-gap diagnostic',
            'mode':'optional_demo_only','v5':False,
        },
        'v5_shadow': {
            'experiment_id':V5_EXPERIMENT_ID,'stage':V5_STAGE,
            'strategy':'frozen V5 adaptive-horizon relative-gap fade',
            'mode':'shadow_only','execution_enabled':False,
        },
        'v5_strict_first5_shadow': {
            'experiment_id':V5_STRICT_CONFIRM_EXPERIMENT_ID,'stage':V5_STRICT_CONFIRM_STAGE,
            'strategy':'V5 ranked candidates with strict first-5-minute reversal',
            'mode':'shadow_only','execution_enabled':False,'selection':'no_backfill_subset',
        },
    }


def v5_side_choices() -> dict[str,dict[str,Any]]:
    """Load the frozen next-session decision emitted by the V5 research run."""
    try:
        report=json.loads(V5_REPORT_PATH.read_text())
    except (OSError,ValueError):
        return {}
    return {str(side):dict(value) for side,value in report.get('next_session_side_choices',{}).items()
            if side in {'LONG','SHORT'} and int(value.get('horizon_minutes') or 0) in {30,60,90}}


def v5_symbol_edge_scores() -> dict[str,dict[str,dict[str,Any]]]:
    """Load prior-only symbol scores emitted by the same daily V5 refresh."""
    try:
        report=json.loads(V5_REPORT_PATH.read_text())
    except (OSError,ValueError):
        return {}
    source=report.get('next_session_symbol_edge_scores') or {}
    return {
        str(side):{str(symbol):dict(value) for symbol,value in rows.items()}
        for side,rows in source.items() if side in {'LONG','SHORT'} and isinstance(rows,dict)
    }


def v5_ranked_candidates(contexts: dict[str,dict[str,float]], spy: dict[str,float], symbols: tuple[str,...],
                         choices: dict[str,dict[str,Any]], limit: int=5) -> list[dict[str,Any]]:
    """Apply the frozen V5 entry rule to features known at 09:35."""
    values=[]
    if abs(float(spy.get('gap_bps') or 0))>75:
        return []
    for symbol in symbols:
        value=contexts.get(symbol)
        if not value: continue
        relative=float(value['gap_bps'])-float(spy['gap_bps'])
        values.append((symbol,value,relative,abs(relative)))
    count=len(values)
    if not count:
        return []
    result=[]
    for symbol,value,relative,magnitude in values:
        equal=sum(abs(other-magnitude)<1e-9 for *_prefix,other in values)
        less=sum(other<magnitude-1e-9 for *_prefix,other in values)
        rank_pct=(less+(equal+1)/2)/count
        relative_first5=float(value['first5_bps'])-float(spy['first5_bps'])
        relative_previous=float(value['previous_day_bps'])-float(spy['previous_day_bps'])
        side='SHORT' if relative>0 else 'LONG'
        confirmed=(relative*relative_first5<0 or relative*relative_previous<=0)
        if not (100<=magnitude<=600 and rank_pct>=.75 and confirmed and side in choices):
            continue
        choice=choices[side]
        result.append({'magnitude':magnitude,'inst_id':symbol,'side':side,'relative_gap_bps':relative,
                       'relative_first5_bps':relative_first5,'relative_previous_day_bps':relative_previous,
                       'relative_gap_rank':rank_pct,'horizon_minutes':int(choice['horizon_minutes']),
                       'prior_side_samples':int(choice.get('prior_side_samples') or 0),
                       'prior_best_horizon_expectancy_pct':float(choice.get('prior_best_horizon_expectancy_pct') or 0)})
    return sorted(result,key=lambda item:item['magnitude'],reverse=True)[:limit]


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


def gap_decision_book_snapshot(client: OKX, inst_id: str, side: str, ct_val: float) -> dict[str,Any]:
    """Capture exactly one books5 frame for a candidate's shadow decision."""
    try:
        rows=client.request('GET','/api/v5/market/books',{'instId':inst_id,'sz':'5'}).get('data') or []
        if not rows:
            return unavailable_book_tca_snapshot('OKX books5 response contained no data')
        return build_book_tca_snapshot(rows[0],side,ct_val)
    except Exception as exc:
        return unavailable_book_tca_snapshot(f'OKX books5 request failed: {exc}')


def gap_decision_mark_prices(client: OKX) -> tuple[dict[str,dict[str,Any]],str | None]:
    """Fetch one public mark-price batch and map it to all candidate symbols."""
    try:
        rows=client.request('GET','/api/v5/public/mark-price',{'instType':'SWAP'}).get('data') or []
        result={}
        for row in rows:
            inst_id=str(row.get('instId') or '')
            try: mark_px=float(row.get('markPx') or 0)
            except (TypeError,ValueError): mark_px=0.0
            if not inst_id or mark_px<=0: continue
            stamp=str(row.get('ts') or '')
            result[inst_id]={'decision_mark_px':mark_px,
                             'mark_exchange_ts':int(stamp) if stamp.isdigit() else None,
                             'mark_snapshot_status':'available','mark_snapshot_error':None}
        return result,None
    except Exception as exc:
        return {},f'OKX mark-price request failed: {exc}'


def mark_snapshot_for_symbol(mark_prices: dict[str,dict[str,Any]], error: str | None,
                             inst_id: str) -> dict[str,Any]:
    return mark_prices.get(inst_id) or {
        'decision_mark_px':None,'mark_exchange_ts':None,
        'mark_snapshot_status':'snapshot_unavailable',
        'mark_snapshot_error':error or f'mark price missing for {inst_id}',
    }


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
        self.cfg=settings(); self.client=OKX(self.cfg); self.running=True; self.last_day=''
        old={}
        try: old=json.loads(STATE_PATH.read_text())
        except Exception: pass
        self.last_state=old if old.get('experiment_id')==EXPERIMENT_ID else {}
        if self.last_state.get('entry_ts'):
            self.last_day=datetime.fromtimestamp(int(self.last_state['entry_ts'])/1000,UTC).astimezone(NY).date().isoformat()
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
                   'v5_experiment_id':V5_EXPERIMENT_ID,'v5_stage':V5_STAGE,'v5_forward_only':True,
                   'v5_experiment_started_at':V5_FROZEN_AT,
                   'v5_strict_confirm_experiment_id':V5_STRICT_CONFIRM_EXPERIMENT_ID,
                   'v5_strict_confirm_stage':V5_STRICT_CONFIRM_STAGE,
                   'v5_strict_confirm_experiment_started_at':V5_STRICT_CONFIRM_FROZEN_AT,
                   'strategy_lanes':strategy_lanes(),
                   'v5_side_choices':v5_side_choices(),
                   'next_evaluation_at':next_evaluation_at(local,self.last_day==day_text)}
            state.setdefault('signals',0);state.setdefault('candidates',[])
            state.setdefault('v5_signals',0);state.setdefault('v5_candidates',[])
            state.setdefault('v5_strict_confirm_signals',0)
            state.setdefault('v5_strict_confirm_candidates',[])
            state.setdefault('v5_strict_confirm_selection','post_rank_no_backfill_subset')
            state.setdefault('v5_strict_confirm_forward_only',True)
            STATE_PATH.write_text(json.dumps(state,ensure_ascii=False,indent=2)); return state
        previous_day=day-timedelta(days=1)
        while previous_day.weekday()>=5: previous_day-=timedelta(days=1)
        previous_end=int(datetime.combine(previous_day,clock(16,0),NY).astimezone(UTC).timestamp()*1000)
        contexts={}; current_rows={}
        research_pool=research_symbols()
        execution_pool=tuple(symbol for symbol in self.cfg.symbols
                             if symbol not in BENCHMARKS and symbol != 'BTC-USDT-SWAP')
        v5_pool=tuple(symbol for symbol in load_symbols('historical_90d') if symbol not in BENCHMARKS)
        trade_symbols=shadow_market_data_symbols(research_pool,execution_pool,v5_pool)
        for symbol in ('SPY-USDT-SWAP',*trade_symbols):
            current=self.client.candles(symbol,limit=120,bar='5m')
            current_rows[symbol]=current
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
        ranked=sorted(ranked,reverse=True)
        research_ranked=top_ranked(ranked)
        demo_ranked=top_ranked(ranked,set(execution_pool))
        v5_choices=v5_side_choices()
        v5_symbol_scores=v5_symbol_edge_scores()
        v5_ranked=v5_ranked_candidates(contexts,spy,v5_pool,v5_choices)
        v5_strict_ranked=v5_strict_first5_subset(v5_ranked)
        v5_strict_symbols={item['inst_id'] for item in v5_strict_ranked}
        legacy_selected={row[1]:row for row in (*research_ranked,*demo_ranked)}
        selected=dict(legacy_selected)
        for item in v5_ranked:
            selected.setdefault(item['inst_id'],(item['magnitude'],item['inst_id'],item['relative_gap_bps'],
                                                  item['relative_first5_bps'],item['relative_previous_day_bps']))
        entry_ts=int(local.astimezone(UTC).timestamp()*1000)
        mark_prices,mark_price_error=gap_decision_mark_prices(self.client) if selected else ({},None)
        candidate_by_symbol={}
        for magnitude,symbol,relative,relative_first5,relative_previous in selected.values():
            side='SHORT' if relative>0 else 'LONG'; ticker=self.client.ticker(symbol)
            last=float(ticker.get('last') or 0); bid=float(ticker.get('bidPx') or 0); ask=float(ticker.get('askPx') or 0)
            spread=(ask-bid)/((ask+bid)/2)*10000 if bid>0 and ask>bid else float('inf')
            # Shadow the price that the intended market order could actually
            # cross at, not the more flattering last-trade price.
            price=ask if side=='LONG' and ask>0 else bid if side=='SHORT' and bid>0 else last
            micro=microstructure_confirmation(symbol,side); side_depth=float(micro.get('ask_depth' if side=='LONG' else 'bid_depth') or 0)
            instrument=self.client.instrument(symbol); ct_val=float(instrument.get('ctVal') or 0)
            mark_snapshot=mark_snapshot_for_symbol(mark_prices,mark_price_error,symbol)
            decision_book={**gap_decision_book_snapshot(self.client,symbol,side,ct_val),**mark_snapshot}
            depth_notional=side_depth*price*ct_val
            row={'instId':symbol,'side':side,'atr14':price/120,'score':magnitude,'volume_ratio':0,
                 'spread_bps':decision_book.get('spread_bps') if decision_book.get('spread_bps') is not None else spread,
                 'estimated_slippage_bps':decision_book.get('estimated_slippage_bps'),
                 'decision_book_snapshot':decision_book,
                 'microstructure':micro,'experiment_id':EXPERIMENT_ID,'horizon_minutes':150}
            atr_px=atr(closed_chronological(current_rows[symbol]))
            atr_bps=atr_px/price*10_000 if atr_px>0 and price>0 else 0.0
            row['atr14']=atr_px; row['atr_bps']=atr_bps
            row['stop_bps']=max(75.0,min(300.0,atr_bps*1.2)) if atr_bps>0 else 75.0
            if symbol in legacy_selected:
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
            v5_item=next((item for item in v5_ranked if item['inst_id']==symbol),None)
            if v5_item:
                symbol_score=(v5_symbol_scores.get(side) or {}).get(symbol)
                if symbol_score is None:
                    horizon=int(v5_item['horizon_minutes'])
                    global_edge=float(v5_choices[side].get(f'edge_{horizon}_pct') or 0)
                    symbol_score={'prior_symbol_samples':0,'prior_symbol_expectancy_pct':None,
                                  'shrunk_symbol_edge_pct':global_edge,
                                  'allocation_multiplier':max(.5,min(1.5,1+global_edge))}
                v5_row={**row,'experiment_id':V5_EXPERIMENT_ID,'horizon_minutes':v5_item['horizon_minutes'],
                        'symbol_edge_pct':float(symbol_score.get('shrunk_symbol_edge_pct') or 0),
                        'allocation_multiplier':float(symbol_score.get('allocation_multiplier') or 1)}
                record_shadow_signal(v5_row,entry_ts,price,V5_STAGE)
                if symbol in v5_strict_symbols:
                    strict_row={**v5_row,'experiment_id':V5_STRICT_CONFIRM_EXPERIMENT_ID}
                    record_shadow_signal(strict_row,entry_ts,price,V5_STRICT_CONFIRM_STAGE)
            candidate_by_symbol[symbol]={'inst_id':symbol,'side':side,'relative_gap_bps':round(relative,2),'price':price,
                                         'relative_first5_bps':round(relative_first5,2),'relative_previous_day_bps':round(relative_previous,2),
                                         'spread_bps':round(spread,2),'depth_notional_usdt':round(depth_notional,2),
                                         'book_snapshot_status':decision_book['book_snapshot_status'],
                                         'book_snapshot_error':decision_book.get('book_snapshot_error'),
                                         'decision_bid_px':decision_book.get('bid_px'),'decision_ask_px':decision_book.get('ask_px'),
                                         'decision_spread_bps':decision_book.get('spread_bps'),
                                         'decision_mark_px':decision_book.get('decision_mark_px'),
                                         'decision_mark_exchange_ts':decision_book.get('mark_exchange_ts'),
                                         'mark_snapshot_status':decision_book.get('mark_snapshot_status'),
                                         'mark_snapshot_error':decision_book.get('mark_snapshot_error'),
                                         'decision_bid_depth_usdt':decision_book.get('bid_depth_notional_usdt'),
                                         'decision_ask_depth_usdt':decision_book.get('ask_depth_notional_usdt'),
                                         'estimated_slippage_bps':decision_book.get('estimated_slippage_bps'),
                                         'slippage_status':decision_book.get('slippage_status'),
                                         'tca_model_version':decision_book.get('tca_model_version'),
                                         'slippage_reference_notional_usdt':decision_book.get('slippage_reference_notional_usdt'),
                                         'atr_bps':round(atr_bps,2),'stop_bps':round(row['stop_bps'],2),
                                         'event_labels':event_labels(symbol,local),
                                         'challenger_experiment_id':CHALLENGER_EXPERIMENT_ID,
                                         'challenger_horizon_minutes':60,
                                         'liquidity_qualified':liquidity_qualified,'executable':executable}
            if v5_item:
                candidate_by_symbol[symbol].update({
                    'v5_experiment_id':V5_EXPERIMENT_ID,'v5_forward_only':True,
                    'v5_horizon_minutes':v5_item['horizon_minutes'],
                    'v5_relative_gap_rank':round(v5_item['relative_gap_rank'],4),
                    'v5_prior_side_samples':v5_item['prior_side_samples'],
                    'v5_prior_best_horizon_expectancy_pct':round(v5_item['prior_best_horizon_expectancy_pct'],6),
                    'v5_symbol_edge_sizing_experiment_id':V5_SYMBOL_EDGE_SIZING_ID,
                    'v5_prior_symbol_samples':int(symbol_score.get('prior_symbol_samples') or 0),
                    'v5_prior_symbol_expectancy_pct':symbol_score.get('prior_symbol_expectancy_pct'),
                    'v5_shrunk_symbol_edge_pct':round(float(symbol_score.get('shrunk_symbol_edge_pct') or 0),6),
                    'v5_allocation_multiplier':round(float(symbol_score.get('allocation_multiplier') or 1),6),
                })
                if symbol in v5_strict_symbols:
                    candidate_by_symbol[symbol].update({
                        'v5_strict_confirm_experiment_id':V5_STRICT_CONFIRM_EXPERIMENT_ID,
                        'v5_strict_confirm_stage':V5_STRICT_CONFIRM_STAGE,
                        'v5_strict_confirm_no_backfill':True,
                    })
        candidates=[candidate_by_symbol[row[1]] for row in research_ranked]
        demo_candidates=[candidate_by_symbol[row[1]] for row in demo_ranked]
        v5_candidates=[candidate_by_symbol[item['inst_id']] for item in v5_ranked]
        v5_strict_candidates=[candidate_by_symbol[item['inst_id']] for item in v5_strict_ranked]
        self.last_day=day_text
        state={'updated_at':datetime.now(UTC).isoformat(),'mode':'shadow_only','experiment_id':EXPERIMENT_ID,
               'experiment_started_at':self.started_at,'active_window':True,'entry_ts':entry_ts,
               'signals':len(candidates),'executable_signals':sum(x['executable'] for x in candidates),
               'execution_equivalent':EXECUTION_EQUIVALENT,
               'execution_block_reason':'fixed-horizon diagnostic does not match protected-order exits',
               'strategy_lanes':strategy_lanes(),
               'next_evaluation_at':next_evaluation_at(local,True),'candidates':candidates}
        state.update({'demo_signals':len(demo_candidates),
                      'demo_liquidity_qualified':sum(x['liquidity_qualified'] for x in demo_candidates),
                      'demo_candidates':demo_candidates})
        state.update({'v5_experiment_id':V5_EXPERIMENT_ID,'v5_stage':V5_STAGE,
                      'v5_experiment_started_at':V5_FROZEN_AT,
                      'v5_forward_only':True,'v5_signals':len(v5_candidates),
                      'v5_side_choices':v5_choices,'v5_candidates':v5_candidates,
                      'v5_symbol_edge_sizing_experiment_id':V5_SYMBOL_EDGE_SIZING_ID,
                      'v5_symbol_edge_sizing_shadow_only':True})
        state.update({'v5_strict_confirm_experiment_id':V5_STRICT_CONFIRM_EXPERIMENT_ID,
                      'v5_strict_confirm_stage':V5_STRICT_CONFIRM_STAGE,
                      'v5_strict_confirm_experiment_started_at':V5_STRICT_CONFIRM_FROZEN_AT,
                      'v5_strict_confirm_forward_only':True,
                      'v5_strict_confirm_selection':'post_rank_no_backfill_subset',
                      'v5_strict_confirm_signals':len(v5_strict_candidates),
                      'v5_strict_confirm_candidates':v5_strict_candidates})
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
