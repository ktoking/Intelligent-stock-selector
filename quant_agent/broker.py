"""Ed25519 REST adapter: quote reads and verified US simulated orders only."""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import time
import urllib.parse
import urllib.request
import urllib.error
from http.client import RemoteDisconnected,HTTPException

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import exchange_calendars as xc
import pandas as pd
from .exchange_clock import us_calendar


class BrokerError(RuntimeError):
    def __init__(self,message,ret_code=None,retry_after=None):
        super().__init__(message)
        self.ret_code,self.retry_after=ret_code,retry_after


class QuoteUnavailable(BrokerError):
    """A quote gate blocks this observation, without invalidating the ledger."""


class AccountIntegrityError(BrokerError):
    """Verified account/order/position facts conflict with the durable ledger."""


class ReadUnavailable(BrokerError):
    """Read transport failed; the next tick must reconcile before executing."""


class SignalUnavailable(BrokerError):
    """Completed daily data are stale; block orders until the cache catches up."""


class OrderRejected(SignalUnavailable):
    """A definite rejection releases its intent; it is not a lost acknowledgement."""


class FutuSim:
    def __init__(self, settings, transport=None):
        self.settings, self.transport = settings, transport
        self.verified = False
        self.key = None
        self.option_codes = set()
        self.on_definite_rejection = None

    def request(self, method, path, query=None, body=None):
        from .observability import emit,error_evidence,safe_route
        request_id=secrets.token_hex(8);started=time.monotonic()
        fields={'request_id':request_id,'method':method,'route':safe_route(path),
                'mutation':method=='POST' and path.startswith('/api/v1.0/sim-trade/')}
        if fields['mutation'] and isinstance(body,dict) and re.fullmatch(r'qa:[a-f0-9]{32}',str(body.get('text',''))):
            fields['intent_id']=body['text'][3:]
        emit('http_started',**fields)
        self._response_status=None
        try:
            value=self._request(method,path,query,body)
        except Exception as exc:
            exc.request_id=request_id
            if not hasattr(exc,'http_status') and self._response_status is not None:exc.http_status=self._response_status
            emit('http_failed',**{**fields,**error_evidence(exc),'elapsed_ms':round((time.monotonic()-started)*1000)})
            raise
        emit('http_completed',**fields,http_status=self._response_status,ret_code=value.get('ret_code'),elapsed_ms=round((time.monotonic()-started)*1000))
        return value

    def _request(self, method, path, query=None, body=None):
        # Exact route allowlist also blocks path traversal and all real-account routes.
        account = re.escape(self.settings.stock_account)
        read_paths = [r"/api/v1\.0/sim-trade/accounts", r"/api/v1\.0/quote/user-security", rf"/api/v1\.0/sim-trade/{account}/(?:cash-info|positions|orders|history-orders)",
                      r"/api/v1\.0/quote/US\.[A-Za-z0-9.-]+/(?:history-kline|option-chain|option-expiration)",
                      rf"/api/v1\.0/sim-trade/{account}/max-buy-sell",
                      r"/api/v1\.0/quote/(?:plate-list|plate-stock|find-news|valuation/index-stocks)",
                      r"/api/v1\.0/quote/economic-calendar/hot"]
        write_paths = [r"/api/v1\.0/quote/snapshot"]
        if self.settings.stock_account:
            write_paths += [rf"/api/v1\.0/sim-trade/{account}/orders", rf"/api/v1\.0/sim-trade/{account}/orders/[A-Za-z0-9_-]+/cancel"]
        allowed = read_paths if method == "GET" else write_paths if method == "POST" else []
        if not any(re.fullmatch(p, path) for p in allowed):
            raise BrokerError("Endpoint outside quote/simulated-trade allowlist")
        is_trade = method == "POST" and path.startswith("/api/v1.0/sim-trade/")
        if is_trade and (not self.verified or not self.settings.paper_enabled):
            raise BrokerError("Verified simulated account and paper enable flag required")
        if self.transport:
            result = self.transport(method, path, query, body)
        else:
            if self.key is None:
                key_path = Path(os.environ.get("FUTUNN_PRIVATE_KEY_FILE", str(Path.home()/".config/stock-agent/futunn-ed25519.key")))
                self.key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
                if not isinstance(self.key, Ed25519PrivateKey):
                    raise BrokerError("Ed25519 key required")
            query_string = urllib.parse.urlencode(query or {})
            payload = None if body is None else json.dumps(body,separators=(",", ":")).encode()
            stamp = str(int(time.time()*1000))
            signature = base64.b64encode(self.key.sign("\n".join([stamp,method,path,query_string,
                    hashlib.sha256(payload).hexdigest() if payload is not None else ""]).encode())).decode()
            req = urllib.request.Request("https://webapi.futunn.com"+path+("?"+query_string if query_string else ""), data=payload, method=method,
                headers={"Content-Type":"application/json","X-Api-Key":os.environ.get("FUTUNN_APP_KEY","1cf56769835a3edefc52a41347131bb6"),
                         "X-Timestamp":stamp,"X-Nonce":secrets.token_urlsafe(24),"Authorization":signature})
            # No automatic retries for mutations. Caller persists ambiguous outcomes.
            try:
                with urllib.request.urlopen(req, timeout=20) as response:
                    self._response_status=getattr(response,'status',None)
                    result = json.load(response)
            except (urllib.error.URLError, HTTPException, TimeoutError, ConnectionError,json.JSONDecodeError) as exc:
                status=getattr(exc,'code',None)
                from .observability import safe_route
                route=safe_route(path)
                details={'http_status':status,'request_method':method,'request_route':route,
                    'read_failure_kind':('authentication' if status in (401,403) else
                        'transient_http' if status in (408,429) or status is not None and status>=500 else
                        'http_request' if status is not None else 'transport')}
                if not is_trade:
                    # Every failed read blocks this tick before execution. A 4xx
                    # remains explicitly diagnosed, rather than permanently
                    # pausing an otherwise reconciled ledger or claiming it transient.
                    error=ReadUnavailable('Futunn read unavailable: '+type(exc).__name__)
                    for key,value in details.items():setattr(error,key,value)
                    raise error from None
                if status in (400,401,403,404,405,422,429):
                    error=OrderRejected('Simulated order HTTP rejection: '+str(status))
                    error.submission_rejected=True
                    for key,value in details.items():setattr(error,key,value)
                    raise error from None
                # Preserve mutation exception semantics: an acknowledgement may
                # be lost, so the caller must reconcile, never blindly retry.
                for key,value in details.items():setattr(exc,key,value)
                raise
        if not isinstance(result,dict) or 'ret_code' not in result:
            error=BrokerError if is_trade else ReadUnavailable
            raise error('Futunn response envelope invalid')
        if result.get("ret_code")==0 and not is_trade and "data" not in result:
            raise ReadUnavailable("Futunn read payload missing")
        if result.get("ret_code") != 0:
            retry=re.search(r"retry after (\d+)s",str(result.get("ret_msg","")),re.I)
            error=ReadUnavailable if not is_trade else OrderRejected
            exc=error(f"Futunn ret_code={result.get('ret_code')}; request unsuccessful",result.get('ret_code'),int(retry.group(1)) if retry else None)
            if is_trade:exc.submission_rejected=True
            if not is_trade:
                from .observability import safe_route
                exc.request_method=method;exc.request_route=safe_route(path)
                exc.read_failure_kind='rate_limit' if exc.ret_code==-11 else 'business_rejection'
            raise exc
        return result

    def accounts(self):
        result=self.request("GET","/api/v1.0/sim-trade/accounts")
        try:
            rows=result['data']['accounts']
            if not isinstance(rows,list) or any(not isinstance(r,dict) or not all(k in r for k in ('account_id','market_id','account_title')) for r in rows):raise ValueError()
        except (KeyError,TypeError,ValueError):raise ReadUnavailable('Simulated account-list response schema invalid') from None
        return rows

    def verify(self):
        matches = [a for a in self.accounts() if str(a["account_id"]) == self.settings.stock_account]
        if (len(matches) != 1 or matches[0]["market_id"] != self.settings.stock_market
                or "美股" not in matches[0].get("account_title", "")):
            raise BrokerError("Account ID, observed market and US simulated title must match")
        self.verified = True
        return self.state()

    def state(self):
        root = "/api/v1.0/sim-trade/"+self.settings.stock_account
        try:
            cash = self.request("GET", root+"/cash-info")["data"]
            positions = self.request("GET",root+"/positions",{"market":self.settings.stock_market})["data"]["positions"]
            orders = self.request("GET",root+"/orders",{"market":self.settings.stock_market})["data"]["orders"]
            import math
            if not isinstance(cash,dict) or not all(math.isfinite(float(cash[k])) for k in ('balance','total_asset')):raise ValueError()
            if not isinstance(positions,list) or not isinstance(orders,list):raise ValueError()
            for row in positions:
                if not isinstance(row,dict) or not all(k in row for k in ('symbol','qty')):raise ValueError()
                if not isinstance(row['symbol'],str) or not math.isfinite(float(row['qty'])):raise ValueError()
            for row in orders:
                if not isinstance(row,dict) or not all(k in row for k in ('order_id','symbol','qty','side','status')):raise ValueError()
                if not isinstance(row['symbol'],str) or not all(math.isfinite(float(row[k])) for k in ('qty','side','status')):raise ValueError()
        except (KeyError,TypeError,ValueError):raise ReadUnavailable('Simulated account-state response schema invalid') from None
        return {"cash":cash,"positions":positions,"orders":orders}

    def history(self, begin_us):
        rows, next_key = [], None
        for _ in range(20):
            q = {"market":self.settings.stock_market,"time_begin":begin_us,
                 "time_end":int(time.time()*1e6),"page_size":100}
            if next_key is not None:
                q["next_key"] = next_key
            result = self.request("GET","/api/v1.0/sim-trade/"+self.settings.stock_account+"/history-orders",q)
            try:
                batch=result['data']['orders']
                if not isinstance(batch,list) or any(not isinstance(r,dict) or not all(k in r for k in ('order_id','symbol','qty','side','status')) for r in batch):raise ValueError()
            except (KeyError,TypeError,ValueError):raise ReadUnavailable('Simulated history response schema invalid') from None
            rows.extend(batch)
            pagination = result.get("pagination",{})
            if not pagination.get("has_more"):
                return rows
            if pagination.get("next_key") in (None,next_key):
                raise ReadUnavailable("History pagination incomplete")
            next_key = pagination["next_key"]
        raise ReadUnavailable("History pagination exceeded bounded limit")

    def snapshots(self, symbols):
        result=self.request("POST","/api/v1.0/quote/snapshot",body={"code_list":list(symbols)})
        rows=result.get('data',{}).get('snapshot_list') if isinstance(result.get('data'),dict) else None
        if not isinstance(rows,list):raise QuoteUnavailable('Invalid quote snapshot response')
        return rows

    def submit(self, order):
        self._order_post_started=False
        try:return self._submit(order)
        except Exception as exc:
            if not self._order_post_started:exc.submission_not_sent=True
            if (getattr(exc,'submission_not_sent',False) or getattr(exc,'submission_rejected',False)) and self.on_definite_rejection:
                self.on_definite_rejection(order['id'],exc)
            raise

    def _submit(self, order):
        is_option=order['bucket']=='option'
        kind=order.get('order_type',1)
        if kind not in (1,3) or kind==3 and (is_option or not self.settings.stock_market_fallback or not session_now()):
            raise BrokerError('Market orders require authorized simulated stocks in regular session')
        if is_option and (not self.settings.option_paper_enabled or order['symbol'] not in self.option_codes or order['multiplier']!=100):
            raise BrokerError('Option contract has not passed simulated capacity verification')
        if order["bucket"] not in ("stock","option") or not re.fullmatch(r"US\.[A-Za-z0-9][A-Za-z0-9.-]*",order["symbol"]) or order["side"] not in ("BUY","SELL"):
            raise BrokerError("Only verified US stock simulated limit orders enabled; options need separate verification")
        if kind==3 and order['side']=='BUY':
            capacity=self.request('GET','/api/v1.0/sim-trade/'+self.settings.stock_account+'/max-buy-sell',
                {'market':self.settings.stock_market,'symbol':order['symbol'][3:],'order_type':3})['data']
            try:cash_qty=float(capacity['max_cash_buy_qty_round_lot'])
            except (KeyError,TypeError,ValueError):raise ReadUnavailable('Market cash capacity unavailable') from None
            if not __import__('math').isfinite(cash_qty) or cash_qty<order['qty']:raise OrderRejected('Market order exceeds cash-only broker capacity')
        body = {"market":self.settings.stock_market,"symbol":order["symbol"][3:],"order_type":kind,
                "order_side":1 if order["side"] == "BUY" else 2,"qty":str(order["qty"]),
                "text":"qa:"+order["id"]}
        if kind==1:body['price']=format(order['price'],'.2f')
        self._order_post_started=True
        data = self.request("POST","/api/v1.0/sim-trade/"+self.settings.stock_account+"/orders",body=body)["data"]
        if not data.get("order_id"):
            raise BrokerError("Missing order acknowledgement")
        return str(data["order_id"])

    def option_capacity(self, code, price):
        if not self.verified or not self.settings.option_paper_enabled or not re.fullmatch(r'US\.[A-Z.]+\d{6}[CP]\d+',code):
            raise BrokerError('Verified US simulated account and standard option code required')
        data=self.request('GET','/api/v1.0/sim-trade/'+self.settings.stock_account+'/max-buy-sell',
            {'market':100,'symbol':code[3:],'order_type':1,'price':format(price,'.2f')})['data']
        for k in ('max_cash_buy_qty_round_lot','max_sell_qty_round_lot'):
            if data.get(k) is None or float(data[k])<0:raise BrokerError('Invalid simulated option capacity response')
        self.option_codes.add(code)
        return data

    def cancel(self, broker_id):
        return self.request("POST","/api/v1.0/sim-trade/"+self.settings.stock_account+"/orders/"+str(broker_id)+"/cancel",body={})


def session_now(now=None):
    now = pd.Timestamp(now or datetime.now(timezone.utc))
    if now.tzinfo is None:
        raise ValueError("Timezone required")
    calendar = us_calendar()
    local_date = now.tz_convert("America/New_York").date()
    day = pd.Timestamp(local_date)
    if not calendar.is_session(day):
        return None
    opening, closing = calendar.session_open(day), calendar.session_close(day)
    return str(day.date()) if opening <= now < closing else None


def executable_quote(quote, settings, now=None, *, check_spread=True):
    """Require current regular session and an observed, fresh, two-sided market."""
    import math
    now = pd.Timestamp(now or datetime.now(timezone.utc))
    session = session_now(now)
    if not session:
        raise QuoteUnavailable("Outside US regular trading session")
    if 'data_date' in quote and quote['data_date']!=session:
        raise QuoteUnavailable('Quote data_date is not the current session')
    # Futunn timestamps are formatted in the market timezone, not host timezone.
    try:
        raw_stamp = quote['update_time']
        if raw_stamp is None:raise ValueError('Missing quote time')
        stamp = pd.Timestamp(raw_stamp, unit="ms", tz="UTC") if isinstance(raw_stamp,(int,float)) else pd.Timestamp(raw_stamp)
        if pd.isna(stamp):raise ValueError('Invalid quote time')
        if stamp.tzinfo is None:stamp = stamp.tz_localize("America/New_York")
    except (KeyError,ValueError,TypeError,OverflowError):
        raise QuoteUnavailable('Quote timestamp unavailable or invalid') from None
    age = (now-stamp).total_seconds()
    if stamp.tz_convert("America/New_York").date().isoformat() != session or not 0 <= age <= settings.max_quote_age_seconds:
        raise QuoteUnavailable("Stale, future-dated or wrong-session quote")
    try:bid, ask = float(quote.get("bid_price") or 0), float(quote.get("ask_price") or 0)
    except (ValueError,TypeError):raise QuoteUnavailable('Executable bid/ask invalid') from None
    if not all(math.isfinite(p) and p > 0 for p in (bid,ask)) or ask < bid:
        raise QuoteUnavailable("Executable bid/ask unavailable")
    spread=(ask-bid)/((ask+bid)/2)*10000
    if check_spread and spread > settings.max_spread_bps:
        raise QuoteUnavailable(f"Spread gate exceeded: {spread:.1f}bps > {settings.max_spread_bps}bps (bid={bid}, ask={ask})")
    return bid, ask
