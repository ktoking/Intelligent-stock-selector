"""AppKey WebSocket telemetry for a bounded focus list. Orders still use REST checks."""
import base64
import json
import os
from pathlib import Path
import secrets
import time

from cryptography.hazmat.primitives import serialization
import websocket

from .broker import session_now
from .store import utcnow

HOST='wss://webapi-quote.futunn.com/ws'


def unpack(value):
    # The deployed server wraps control acknowledgements in the REST envelope.
    if 'ret_code' in value:
        if value['ret_code']!=0:raise RuntimeError('WebSocket control request rejected')
        return value.get('data',{})
    return value


def auth_frame(action='auth'):
    stamp=int(time.time()*1000);nonce=secrets.token_urlsafe(24)
    path=Path(os.environ.get('FUTUNN_PRIVATE_KEY_FILE',str(Path.home()/'.config/stock-agent/futunn-ed25519.key')))
    key=serialization.load_pem_private_key(path.read_bytes(),password=None)
    signature=base64.b64encode(key.sign(f'{stamp}\n{nonce}\nWEBSOCKET\nws/auth'.encode())).decode()
    data={'credential_id':os.environ.get('FUTUNN_APP_KEY','1cf56769835a3edefc52a41347131bb6'),
          'authorization':signature,'timestamp_ms':stamp,'nonce':nonce}
    if action=='auth':data['auth_type']='appkey'
    return {'id':action+'-'+secrets.token_hex(4),'action':action,'data':data}


def handshake(sock,symbols):
    frame=auth_frame();sock.send(json.dumps(frame));deadline=time.monotonic()+10
    while time.monotonic()<deadline:
        r=unpack(json.loads(sock.recv()))
        if r.get('session_id'):break
        if r.get('code',0)!=0 or r.get('error'):raise RuntimeError('WebSocket authentication rejected')
    else:raise TimeoutError('WebSocket auth timeout')
    sub={'id':'focus-sub','action':'subscribe','quote':list(symbols)}
    sock.send(json.dumps(sub));deadline=time.monotonic()+10
    while time.monotonic()<deadline:
        r=unpack(json.loads(sock.recv()))
        if r.get('id')=='focus-sub':
            if r.get('code')!=0:raise RuntimeError('WebSocket subscription rejected')
            return
    raise TimeoutError('WebSocket subscription timeout')


def probe(store,symbols=None):
    symbols=symbols or ['US.NVDA'];messages=[]
    sock=websocket.create_connection(HOST,timeout=10)
    try:
        handshake(sock,symbols);sock.settimeout(2);deadline=time.monotonic()+6
        while time.monotonic()<deadline:
            try:
                value=json.loads(sock.recv())
                if value.get('type'):messages.append(value['type'])
            except websocket.WebSocketTimeoutException:pass
    finally:sock.close()
    result={'observed_at':utcnow(),'auth_ok':True,'subscribe_ok':True,'symbols':symbols,
            'message_types':sorted(set(messages)),'market_open':bool(session_now()),
            'limitation':'Handshake and subscription acknowledgement verified; continuous fresh streaming requires open-session observation'}
    store.set_meta('push_probe',result);return result


def focus_symbols(store):
    holdings=list(store.portfolio()['positions'])
    pending=[o['symbol'] for o in store.orders(active=True)]
    ranked=list((store.meta('last_tick') or {}).get('quotes',{}))
    movers=[r['symbol'] for r in store.meta('universe_scan',{}).get('hits',[])[:15]]
    return list(dict.fromkeys(holdings+pending+ranked+movers+['US.NVDA','US.QQQ','US.SPY']))[:40]


def run(store,stop):
    while not stop.is_set():
        try:session=session_now()
        except Exception as exc:
            store.set_meta('push_status',{'status':'fallback_rest','observed_at':utcnow(),'error_type':type(exc).__name__})
            stop.wait(30);continue
        if not session:
            store.set_meta('push_status',{'status':'closed','observed_at':utcnow(),'reason':'Outside regular session'})
            stop.wait(30);continue
        sock=None
        try:
            symbols=focus_symbols(store);sock=websocket.create_connection(HOST,timeout=10);handshake(sock,symbols);sock.settimeout(1)
            store.set_meta('push_status',{'status':'subscribed','observed_at':utcnow(),'subscribed_symbols':len(symbols)})
            refreshed=flushed=time.monotonic();quotes={};messages=0
            while not stop.is_set() and session_now():
                if time.monotonic()-refreshed>=300:
                    sock.send(json.dumps(auth_frame('refresh')))
                    wanted=focus_symbols(store)
                    removed=sorted(set(symbols)-set(wanted));added=sorted(set(wanted)-set(symbols))
                    if removed:sock.send(json.dumps({'id':'remove-focus','action':'unsubscribe','quote':removed}))
                    if added:sock.send(json.dumps({'id':'add-focus','action':'subscribe','quote':added}))
                    symbols=wanted;refreshed=time.monotonic()
                try:r=unpack(json.loads(sock.recv()))
                except websocket.WebSocketTimeoutException:continue
                if r.get('code',0)!=0 or r.get('error'):raise RuntimeError('WebSocket control request unsuccessful')
                if r.get('type')=='QUOTE':
                    quotes[r['symbol']]={'symbol':r['symbol'],**r.get('data',{}),'received_at':utcnow()};messages+=1
                if time.monotonic()-flushed>=5 and quotes:
                    store.set_meta('push_quotes',quotes)
                    store.set_meta('push_status',{'status':'receiving','observed_at':utcnow(),'subscribed_symbols':len(symbols),'quote_messages':messages})
                    flushed=time.monotonic()
        except Exception as exc:
            store.set_meta('push_status',{'status':'fallback_rest','observed_at':utcnow(),'error_type':type(exc).__name__})
            stop.wait(60)
        finally:
            if sock:sock.close()
