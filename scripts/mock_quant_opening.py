"""Isolated opening rehearsal: real supervisor + REST adapter, fake wire/time.

Copies the runtime SQLite read-only, never sends network requests or touches the
running ledger. Synthetic quotes/fills are labelled MOCK in the saved report.
"""
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime,timezone
import argparse
import base64
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import socket
import threading
import urllib.parse
import urllib.request
from types import SimpleNamespace
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler,HTTPServer
from http.client import RemoteDisconnected

import pandas as pd
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from quant_agent import broker,runner,schedule,exchange_clock,option_paper,store as store_module
from quant_agent import promotion,capital,champion
from quant_agent.broker import FutuSim
from quant_agent.config import DEFAULT_HOME,Settings,canonical,digest
from quant_agent.store import Store
from quant_agent.web import public_status


CASES=['normal','opening_wide_spread','missing_quote_recovery','stale_quote_recovery',
       'read_rate_limit','malformed_snapshot','partial_fill_restart','partial_then_cancel',
       'accepted_ack_lost','cancel_ack_lost','held_quote_recovery','option_eligible',
       'option_over_budget','all_quotes_unavailable','risk_drawdown','held_option_stale',
       'persistent_wide_spread','invalid_json_recovery','read_disconnect']
CASES.append('preopen_to_open')


class Wire:
    def __init__(self,settings,panel,clock,case,quote_capture=None,quote_mode='opening-fresh'):
        self.settings,self.panel,self.clock,self.case=settings,panel,clock,case
        self.rows=[];self.calls=[];self.cycle=0;self.positions={};self.quote_failures=0
        self.restart=False;self.accepted_loss=False;self.cancel_loss=False
        self.prices={s:float(panel.fields['close'][-1,j]) for j,s in enumerate(panel.symbols)}
        self.capture=quote_capture;self.quote_mode=quote_mode;self.initial_clock=clock.now
        self.observed={q['code']:q for q in quote_capture['snapshots']} if quote_capture else {}
        if quote_capture:
            assert len(self.observed)==len(quote_capture['snapshots']), 'Duplicate captured symbol'
            assert quote_mode in ('original','premarket-age','opening-fresh')
            # At the next regular session prev_close rolls to the observed
            # completed-session last. Never substitute a mismatched basis.
            if quote_mode!='original':
                for symbol,q in self.observed.items():
                    assert symbol in self.prices and math.isclose(float(q['last_price']),self.prices[symbol],rel_tol=.005), 'Captured regular last does not match frozen daily basis: '+symbol
        self.contracts={}
        self.target='US.QMCO' if 'US.QMCO' in panel.symbols else next(s for s in panel.symbols if s!='US.QQQ')

    def quote(self,symbol):
        price=self.contracts.get(symbol,{}).get('premium',self.prices.get(symbol,100.))
        if self.case=='risk_drawdown' and self.cycle>=3 and symbol in self.positions:price*=.5
        q={'code':symbol,'last_price':price,'prev_close_price':self.prices.get(symbol,price),
           'bid_price':price*.9995,'ask_price':price*1.0005,
           'data_date':self.clock.now.date().isoformat(),'update_time':int(self.clock.now.timestamp()*1000)}
        if self.capture and symbol not in self.contracts:
            if symbol not in self.observed:return None
            q=dict(self.observed[symbol])
            if self.quote_mode!='original':
                q['last_price']=q.get('pre_price') or q['last_price']
                q['prev_close_price']=self.observed[symbol]['last_price']
                q['data_date']=self.clock.now.tz_convert('America/New_York').date().isoformat()
                if self.quote_mode=='opening-fresh':
                    q['update_time']=int(self.clock.now.timestamp()*1000)
                else:
                    # Rebase the observation once, retaining captured age.
                    # No manufactured continuous updates in this strict replay.
                    age=pd.Timestamp(self.capture['captured_at'])-pd.Timestamp(q['update_time'],unit='ms',tz='UTC')
                    q['update_time']=int((self.initial_clock-age).timestamp()*1000)
            price=float(q['last_price'])
        if self.case in ('opening_wide_spread','persistent_wide_spread') and symbol==self.target and (self.case=='persistent_wide_spread' or self.cycle<2):
            q.update(bid_price=price*.98,ask_price=price*1.02)
        if self.case=='stale_quote_recovery' and symbol==self.target and self.cycle<2:
            q['update_time']-=180000
        if symbol in self.contracts:
            q.update(option_valid=True,option_contract_multiplier=100,option_contract_size=100,
                     delta=.45,option_open_interest=3000)
            if self.case=='held_option_stale' and self.cycle==3:
                q.update(last_price=999.,update_time=q['update_time']-180000)
        return q

    def __call__(self,method,path,query,body):
        self.calls.append({'cycle':self.cycle,'method':method,'route':path.rsplit('/',1)[-1]})
        if self.case=='read_disconnect' and self.cycle<2 and path.endswith('cash-info'):
            raise broker.ReadUnavailable('MOCK read socket disconnected')
        if self.case=='read_rate_limit' and self.cycle<2 and path.endswith('cash-info'):
            return {'ret_code':-11,'ret_msg':'retry after 1s'}
        if path.endswith('/accounts'):
            data={'accounts':[{'account_id':self.settings.stock_account,'market_id':100,'account_title':'美股模拟 MOCK'}]}
        elif path.endswith('/cash-info'):
            cash=self.settings.stock_budget+self.settings.option_budget
            for row in self.rows:
                value=row.get('cum_qty',0)*float(row.get('avg_fill_price') or 0)*(100 if 'US.'+row['symbol'] in self.contracts else 1)
                cash-=(1 if row['side']==1 else -1)*value+value*self.settings.fee_reserve_bps/10000
            mv=sum(qty*self.quote(symbol)['last_price']*(100 if symbol in self.contracts else 1) for symbol,qty in self.positions.items() if qty)
            data={'balance':cash,'total_asset':cash+mv}
        elif path.endswith('/positions'):
            data={'positions':[{'symbol':s[3:],'qty':qty,'pstn_type':0} for s,qty in self.positions.items() if qty]}
        elif method=='GET' and (path.endswith('/orders') or path.endswith('/history-orders')):
            data={'orders':[r.copy() for r in self.rows]}
        elif path.endswith('/snapshot'):
            if self.case=='all_quotes_unavailable':return {'ret_code':0,'data':{'snapshot_list':None}}
            if self.case=='malformed_snapshot' and self.cycle<2:return {'ret_code':0,'data':{'snapshot_list':None}}
            if self.case=='invalid_json_recovery' and self.cycle<2:return {'mock_invalid_json':True}
            symbols=body['code_list']
            if self.case=='missing_quote_recovery' and self.cycle<2:symbols=[s for s in symbols if s!=self.target]
            if self.case=='held_quote_recovery' and self.cycle==3:symbols=[s for s in symbols if s!=self.target]
            data={'snapshot_list':[q for s in symbols if (q:=self.quote(s)) is not None]}
        elif path.endswith('/option-chain'):
            # Captured stocks do not establish an observed option chain or
            # contract quote. Do not invent option execution in this replay.
            if self.capture and self.case not in ('option_eligible','option_over_budget','held_option_stale'):
                return {'ret_code':0,'data':{'option_chain':[]}}
            owner=path.split('/')[-2];expiry=(self.clock.now+pd.Timedelta(days=21)).date().isoformat()
            code=owner+self.clock.now.__class__(expiry).strftime('%y%m%d')+'C'+str(round(self.prices.get(owner,100)*1000))
            premium=max(12.,self.settings.option_budget/100*1.2) if self.case=='option_over_budget' else 6.
            self.contracts[code]={'premium':premium}
            data={'option_chain':[{'code':code,'strike_time':expiry,'stock_owner':owner,'option_type':'CALL',
                'lot_size':100,'option_standard_type':'STANDARD','strike_price':self.prices.get(owner,100)}]}
        elif path.endswith('/max-buy-sell'):
            data={'max_cash_buy_qty_round_lot':'1','max_sell_qty_round_lot':'1'}
        elif method=='POST' and path.endswith('/orders'):
            assert '/sim-trade/mock-opening/' in path and body['market']==100 and body['order_type']==1
            identity='mock-'+str(len(self.rows)+1)
            row={'order_id':identity,'symbol':body['symbol'],'qty':int(body['qty']),'side':body['order_side'],
                 'status':2,'cum_qty':0,'avg_fill_price':0,'text':body['text'],
                 'mock_limit':float(body['price']),'mock_created_cycle':self.cycle}
            self.rows.append(row);data={'order_id':identity}
            if self.case=='accepted_ack_lost' and not self.accepted_loss:
                self.accepted_loss=True
                from http.client import RemoteDisconnected
                raise RemoteDisconnected('MOCK accepted order, acknowledgement lost')
        elif path.endswith('/cancel'):
            identity=path.split('/')[-2];next(r for r in self.rows if r['order_id']==identity)['status']=5
            data={}
            if self.case=='cancel_ack_lost' and not self.cancel_loss:
                self.cancel_loss=True
                from http.client import RemoteDisconnected
                raise RemoteDisconnected('MOCK accepted cancellation, acknowledgement lost')
        else:raise AssertionError('Unhandled MOCK route: '+path)
        return {'ret_code':0,'data':data}

    def advance(self):
        self.cycle+=1;self.clock.now+=pd.Timedelta(seconds=60)
        for row in self.rows:
            if row['status'] not in (2,3):continue
            age=self.cycle-row['mock_created_cycle']
            cumulative=row['qty'] if age>=2 else row['qty']//2
            if self.case in ('partial_then_cancel','cancel_ack_lost'):cumulative=row['qty']//2
            delta=cumulative-row['cum_qty']
            symbol='US.'+row['symbol']
            self.positions[symbol]=self.positions.get(symbol,0)+delta*(1 if row['side']==1 else -1)
            row.update(cum_qty=cumulative,avg_fill_price=row['mock_limit'] if cumulative else 0,
                       status=4 if cumulative==row['qty'] else 3 if cumulative else 2)


def http_adapter(wire,stack):
    """Exercise the unchanged signing/HTTP/JSON path against loopback only."""
    private=Ed25519PrivateKey.generate();real_open=urllib.request.urlopen
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):self.handle_api()
        def do_POST(self):self.handle_api()
        def handle_api(self):
            route=urllib.parse.urlsplit(self.path)
            payload=self.rfile.read(int(self.headers.get('Content-Length',0))) if self.command=='POST' else None
            signed='\n'.join([self.headers['X-Timestamp'],self.command,route.path,route.query,
                hashlib.sha256(payload).hexdigest() if payload is not None else ''])
            private.public_key().verify(base64.b64decode(self.headers['Authorization']),signed.encode())
            assert self.headers['X-Api-Key']=='mock-app-key'
            try:
                if wire.case=='read_disconnect' and wire.cycle<2 and route.path.endswith('cash-info'):
                    raise RemoteDisconnected('MOCK read disconnected')
                result=wire(self.command,route.path,dict(urllib.parse.parse_qsl(route.query)),json.loads(payload) if payload else None)
            except (RemoteDisconnected,broker.ReadUnavailable):
                self.connection.shutdown(socket.SHUT_RDWR);self.close_connection=True;return
            content=b'{"unfinished":' if result.get('mock_invalid_json') else canonical(result).encode()
            self.send_response(200);self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(content)));self.end_headers();self.wfile.write(content)
    server=HTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=lambda:server.serve_forever(poll_interval=.01),daemon=True);thread.start()
    def close():server.shutdown();server.server_close();thread.join(timeout=2)
    stack.callback(close)
    def local_only(request,timeout=None):
        assert isinstance(request,urllib.request.Request)
        url=urllib.parse.urlsplit(request.full_url)
        assert url.scheme=='https' and url.netloc=='webapi.futunn.com', 'MOCK denied other network target'
        target='http://127.0.0.1:'+str(server.server_port)+url.path+('?' + url.query if url.query else '')
        local=urllib.request.Request(target,data=request.data,method=request.method,headers=dict(request.headers))
        return real_open(local,timeout=timeout)
    stack.enter_context(patch('urllib.request.urlopen',side_effect=local_only))
    import os
    stack.enter_context(patch.dict(os.environ,{'FUTUNN_APP_KEY':'mock-app-key'}))
    def factory(settings):
        instance=FutuSim(settings);instance.key=private;return instance
    return factory


def invariants(store,wire):
    all_orders=store.orders();keys=[o['id'] for o in all_orders]
    assert len(keys)==len(set(keys))
    expected={s:p['qty'] for bucket in ('stock','option') for s,p in store.portfolio(bucket)['positions'].items()}
    actual={s:q for s,q in wire.positions.items() if q}
    # The last wire advance can precede the next reconciliation; callers run
    # this immediately after a tick, before advancing the simulated clock.
    assert actual==expected,('positions',actual,expected)
    for bucket,budget in [('stock',store.settings.stock_budget),('option',store.settings.option_budget)]:
        book=store.portfolio(bucket);assert book['cash']>=-1e-7
        with store.transaction() as db:
            rows=db.execute('SELECT f.*,o.side,o.multiplier FROM fills f JOIN orders o ON o.id=f.order_id WHERE o.bucket=?',(bucket,)).fetchall()
        expected_cash=budget-sum((1 if r['side']=='BUY' else -1)*r['qty']*r['price']*r['multiplier']+r['fee'] for r in rows)
        assert math.isclose(book['cash'],expected_cash,abs_tol=1e-7)
        owners=store.meta('paper_allocations',{}).get(bucket,{})
        assert math.isclose(sum(store.strategy_portfolio(i,bucket)['cash'] for i in owners),book['cash'],abs_tol=1e-7)
        for identity in owners:
            own=store.strategy_portfolio(identity,bucket);assert own['cash']>=-1e-7
            bought=sum(r['qty']*r['price']*r['multiplier']+r['fee'] for o in all_orders if o['strategy']==identity and o['side']=='BUY'
                for r in rows if r['order_id']==o['id'])
            assert bought<=own['budget']+1e-7


def run_case(home,settings,case,cycles=11,http_mock=False,quote_capture=None,quote_mode='opening-fresh'):
    home=Path(home);settings=replace(settings,stock_account='mock-opening',quote_push_enabled=False,
        daily_research=False,auto_refresh=False,evolution_enabled=False)
    store=Store(home,settings)
    assert not store.orders() and not store.portfolio()['positions'] and not store.portfolio('option')['positions'], 'MOCK rehearsal expects an empty copied ledger'
    store.set_meta('bound_account',digest([settings.stock_account,settings.stock_market]))
    from research.daily_factor_lab.core import load_panel
    directory=store.meta('execution_data_dir',store.meta('current_data_dir',settings.data_dir));panel=load_panel(directory)
    cal=exchange_clock.us_calendar();session=cal.next_session(panel.metadata['end']).date().isoformat()
    offset=-620 if case=='promotion_then_open' else -230 if case=='preopen_to_open' else 10
    clock=SimpleNamespace(now=cal.session_open(session)+pd.Timedelta(seconds=offset))
    if case=='preopen_to_open':cycles+=4
    if case=='promotion_then_open':cycles+=11
    wire=Wire(settings,panel,clock,case,quote_capture,quote_mode);timeline=[];seed={r['id']:r['evidence']['seed_hash'] for r in store.strategies(['paper'])}
    store.set_meta('universe_scan',{'created_at':clock.now.isoformat(),'hits':[]})
    class Timestamp(pd.Timestamp):
        @classmethod
        def now(cls,tz=None):return clock.now.tz_convert(tz) if tz else clock.now.tz_localize(None)
    class DateTime(datetime):
        @classmethod
        def now(cls,tz=None):return clock.now.to_pydatetime().astimezone(tz) if tz else clock.now.to_pydatetime().replace(tzinfo=None)
    fake_pd=SimpleNamespace(**{k:getattr(pd,k) for k in dir(pd) if not k.startswith('__')});fake_pd.Timestamp=Timestamp
    class NoBackground:
        def __init__(self,*a,**kw):pass
        def start(self):pass
        def is_alive(self):return False
    class Stop:
        def __init__(self,end):self.end=end
        def is_set(self):return wire.cycle>=self.end
        def wait(self,_):
            # Observe invariants after every real supervisor loop.
            if not store.meta('paused') and (store.meta('health') or {}).get('status')=='ok':invariants(store,wire)
            with store.transaction() as db:fill_count=db.execute('SELECT COUNT(*) FROM fills').fetchone()[0]
            timeline.append({'second':int((clock.now-cal.session_open(session)).total_seconds()),'health':store.meta('health'),
                'orders':len(store.orders()),'fill_increments':fill_count,
                'stock_positions':len(store.portfolio()['positions']),'option_positions':len(store.portfolio('option')['positions']),
                'option_marks':{s:p['mark'] for s,p in store.portfolio('option')['positions'].items()},
                'option_status':store.meta('option_paper_status',{}).get('status'),'paused':store.meta('paused')})
            wire.advance()
    load=runner.load_panel;cache={directory:panel}
    def cached(path):
        if path not in cache:cache[path]=load(path)
        return cache[path]
    with ExitStack() as stack:
        # A hard local network ban also catches accidental background requests.
        if http_mock:factory=http_adapter(wire,stack)
        else:
            stack.enter_context(patch('urllib.request.urlopen',side_effect=AssertionError('MOCK network access forbidden')))
            factory=lambda config:FutuSim(config,transport=wire)
        for module in (runner,broker,schedule,exchange_clock,option_paper,capital,champion):stack.enter_context(patch.object(module,'pd',fake_pd))
        for module in (broker,schedule,exchange_clock):stack.enter_context(patch.object(module,'datetime',DateTime))
        for module in (runner,store_module,option_paper,promotion,capital,champion):stack.enter_context(patch.object(module,'utcnow',lambda:clock.now.isoformat()))
        stack.enter_context(patch.object(broker.time,'time',lambda:clock.now.timestamp()))
        stack.enter_context(patch.object(runner,'FutuSim',factory))
        stack.enter_context(patch.object(runner,'load_panel',cached))
        stack.enter_context(patch.object(runner.threading,'Thread',NoBackground))
        stack.enter_context(patch.object(runner,'daily_due',lambda _:False))
        if case in ('option_eligible','option_over_budget','held_option_stale'):
            stack.enter_context(patch.object(option_paper,'direction',lambda p,s:{'symbol':s,'type':'CALL','strength':.1,'signal_date':panel.metadata['end']}))
        if case=='partial_fill_restart':
            runner.serve(store,Stop(2));store=Store(home,settings);wire.restart=True
        runner.serve(store,Stop(cycles))
        # Reconcile the final broker observation without generating new plans.
        runner.reconcile(store,factory(settings))
        invariants(store,wire)
        status=public_status(store)
    all_records={r['id']:r for r in store.strategies()}
    assert all(all_records[i]['evidence']['seed_hash']==value for i,value in seed.items())
    orders=store.orders();stock=[o for o in orders if o['bucket']=='stock'];option=[o for o in orders if o['bucket']=='option']
    submissions=[c for c in wire.calls if c['method']=='POST' and c['route']=='orders']
    assert len(submissions)==len(orders),('duplicate submit',len(submissions),len(orders))
    safe_pause=case=='risk_drawdown' or any(o['status'] in ('unknown','submitting') for o in orders)
    if case in ('accepted_ack_lost','cancel_ack_lost') and not safe_pause:
        with store.read() as db:
            assert db.execute("SELECT 1 FROM events WHERE kind='acknowledgement_recovered'").fetchone()
    assert bool(store.meta('paused'))==safe_pause,(case,store.meta('pause_reason'),timeline)
    captured_block=quote_capture is not None and quote_mode in ('original','premarket-age')
    if not safe_pause and case!='all_quotes_unavailable' and not captured_block:
        current_owners={i for i,cap in store.meta('paper_allocations')['stock'].items() if cap>0}
        assert {o['strategy'] for o in stock}==current_owners,(case,'stock strategies did not submit',timeline)
        assert all(r['health']['status']=='ok' for r in timeline[-2:]),(case,'did not recover',timeline)
    if case=='all_quotes_unavailable':assert orders==[] and timeline[-1]['health']['execution_status']=='blocked'
    if captured_block:
        assert not orders, 'Captured stale reference data unexpectedly authorized an order'
        assert any(e['status']=='blocked' for r in timeline for e in (r.get('health') or {}).get('strategy_checks',[])) or timeline[-1]['health']['execution_status']=='blocked'
    if case=='option_eligible':assert option and option[0]['filled']==1 and len(option)==1
    if case=='held_option_stale':
        assert option and option[0]['filled']==1
        assert all(v!=999. for v in timeline[3]['option_marks'].values()),'Stale held-option quote polluted the NAV mark'
    if case=='option_over_budget':assert option==[]
    if case=='preopen_to_open':
        assert all(r['orders']==0 for r in timeline if r['second']<0),'MOCK submitted before regular open'
        assert store.meta('execution_readiness')['ready'] and store.meta('preflight_session')==session
    if case in ('partial_then_cancel','cancel_ack_lost'):assert any(o['status']=='cancelled' for o in orders)
    with store.transaction() as db:
        events=[{'time':r['created'],'kind':r['kind'],'payload':json.loads(r['payload'])} for r in db.execute("SELECT * FROM events WHERE created>=? ORDER BY seq",(cal.session_open(session).isoformat(),))]
    return {'case':case,'result':'PASS','mode':'MOCK, no external orders',
        'session':session,'cycles':len(timeline),'stock_orders':len(stock),'option_orders':len(option),
        'signed_loopback_http':http_mock,
        'filled_orders':sum(o['status']=='filled' for o in orders),'cancelled_orders':sum(o['status']=='cancelled' for o in orders),
        'expected_safe_pause':safe_pause,'paused':store.meta('paused'),'pause_reason':store.meta('pause_reason'),
        'seed_unchanged':True,'budgets_checked':True,'ledger_checked_each_healthy_cycle':True,
        'restart_tested':wire.restart,'timeline':timeline,'events':events,
        'quote_input':{'source':quote_capture['source'],'captured_at':quote_capture['captured_at'],
            'capture_sha256':digest(quote_capture),'symbols':len(wire.observed),'mode':quote_mode,
            'prices_and_spreads':'Observed pre_price and bid/ask retained' if quote_mode!='original' else 'Original snapshot fields unchanged',
            'modeled_fields':[] if quote_mode=='original' else ['last_price <- captured pre_price','prev_close_price <- independently checked regular last','data_date <- virtual regular session','update_time <- virtual fresh updates' if quote_mode=='opening-fresh' else 'update_time <- initial virtual clock minus original age; no subsequent updates'],
            'broker_fills':'Synthetic local loopback acknowledgements and fills'} if quote_capture else None,
        'dashboard_stock_positions':status['holdings']['stock']['position_count']}


def clone_runtime(source,destination):
    destination=Path(destination);destination.mkdir(parents=True,mode=0o700)
    with sqlite3.connect('file:'+str(Path(source)/'state.sqlite')+'?mode=ro',uri=True) as src,sqlite3.connect(destination/'state.sqlite') as dst:src.backup(dst)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--source',type=Path,default=DEFAULT_HOME)
    parser.add_argument('--output',type=Path,required=True);parser.add_argument('--cases',nargs='+',choices=CASES,default=CASES)
    parser.add_argument('--http-mock',action='store_true')
    parser.add_argument('--quote-capture',type=Path)
    parser.add_argument('--quote-mode',choices=['original','premarket-age','opening-fresh'],default='opening-fresh')
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True,mode=0o700)
    settings=Settings.load(args.source);results=[]
    quote_capture=json.loads(args.quote_capture.read_text()) if args.quote_capture else None
    for case in args.cases:
        destination=args.output/case;clone_runtime(args.source,destination)
        try:result=run_case(destination,settings,case,http_mock=args.http_mock,quote_capture=quote_capture,quote_mode=args.quote_mode)
        except Exception as exc:
            import traceback
            result={'case':case,'result':'FAIL','error':str(exc),'traceback':traceback.format_exc()}
        results.append(result);(destination/'result.json').write_text(canonical(result))
        print(case,result['result'],result.get('stock_orders',''),result.get('option_orders',''),flush=True)
    report={'mode':'MOCK','production_orders_sent':0,'results':results,'passed':all(r['result']=='PASS' for r in results)}
    (args.output/'report.json').write_text(canonical(report))
    if not report['passed']:raise SystemExit(1)


if __name__=='__main__':main()
