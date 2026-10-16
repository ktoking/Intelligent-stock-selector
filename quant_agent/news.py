"""Timestamped Futu headlines. Availability is first observation, never backdated."""
from datetime import datetime,timezone
import html
import re
from urllib.parse import urlsplit

import pandas as pd
import requests

from .broker import FutuSim
from .config import canonical,digest
from .pipeline import artifact
from .store import utcnow


def normalize(item,symbol,observed_at,source):
    try:
        raw=float(item['publish_time']); stamp=pd.Timestamp(raw,unit='ms' if raw>1e12 else 's',tz='UTC')
        if stamp>pd.Timestamp(observed_at) or stamp<pd.Timestamp(observed_at)-pd.Timedelta(days=7):return None
        url=item['url'];parsed=urlsplit(url)
        if parsed.scheme!='https' or parsed.hostname not in ('news.futunn.com','www.futunn.com'):return None
        title=html.unescape(re.sub('<[^>]+>','',str(item['title']))).strip()[:500]
        if not title:return None
        return {'id':str(item['news_id']),'symbol':symbol,'title':title,'published_at':stamp.isoformat(),
                'available_at':observed_at,'url':url,'source':source,'content_level':'headline_only'}
    except (ValueError,KeyError,TypeError):return None


def refresh_news(store,symbols=None):
    observed=utcnow();broker=FutuSim(store.settings)
    if symbols is None:
        from .briefing import screen_us
        scan=screen_us(store)
        dynamic=[s for group in scan['groups'].values() for s in group[:2]]
        pool=list(store.portfolio()['positions'])+dynamic+[r['symbol'] for r in store.meta('universe_scan',{}).get('hits',[])[:4]]+['US.QQQ','US.SPY']
        symbols=list(dict.fromkeys(pool))[:12]
    rows=[];errors=[];successful_reads=0
    for symbol in symbols[:12]:
        keyword=symbol.removeprefix('US.')
        source='Futunn REST find-news'
        try:
            try:
                data=broker.request('GET','/api/v1.0/quote/find-news',{'symbol':keyword,'size':3,'news_type':1,'sort_type':2,'lang':'zh-CN'})['data']
            except Exception:
                source='Futu news-search Skill API'
                r=requests.get('https://ai-news-search.futunn.com/news_search',params={'keyword':keyword,'size':3,'news_type':1,'sort_type':2,'lang':'zh-CN'},headers={'User-Agent':'futunn-news-search/0.0.2 (Skill)'},timeout=20)
                r.raise_for_status();body=r.json()
                if body.get('code')!=0:raise ValueError('News API unsuccessful')
                data=body['data']
            successful_reads+=1
            for item in data:
                value=normalize(item,symbol,observed,source)
                if value:rows.append(value)
        except Exception as exc:
            from .observability import error_evidence
            errors.append({'symbol':symbol,**error_evidence(exc)})
    with store.transaction() as db:
        for row in rows:
            db.execute('INSERT OR IGNORE INTO news VALUES (?,?,?)',(row['id'],row['available_at'],canonical(row)))
        recorded=[__import__('json').loads(r['payload']) for r in db.execute('SELECT payload FROM news ORDER BY available_at DESC LIMIT 80')]
    value={'observed_at':observed if successful_reads else store.meta('news_packet',{}).get('observed_at'),
           'attempted_at':observed,'successful_reads':successful_reads,'items':sorted(recorded,key=lambda r:r['published_at'],reverse=True)[:40],
           'targets':symbols,'errors':errors,'content_level':'headline_only',
           'limitation':'Headlines, not full article verification or causal attribution. Usable only at or after first observation.'}
    store.set_meta('news_packet',value)
    artifact(store,'news-'+digest(value)[:16],value)
    store.event('news_refreshed',{'items':len(rows),'errors':len(errors)})
    if symbols and not successful_reads:
        from .broker import ReadUnavailable
        raise ReadUnavailable('News reads unavailable; previous evidence retained without advancing freshness')
    from .briefing import refresh_context
    try:
        context=refresh_context(store,value)
        value['global_items']=[r for r in context['headlines'] if r['symbol'].startswith('主题:')]
        value['scope']='动态技术候选 + 异动 + 持仓 + 跨市场主题；标题证据'
        store.set_meta('news_packet',value)
    except Exception as exc:
        store.event('brief_context_failed',{'error_type':type(exc).__name__})
    return value


def evidence_at(store,cutoff=None):
    cutoff=cutoff or utcnow()
    items=store.meta('news_packet',{}).get('items',[])
    stamp=pd.Timestamp(cutoff)
    return [r for r in items if pd.Timestamp(r['available_at'])<=stamp and pd.Timestamp(r['published_at'])<=stamp][:24]
