"""Daily current index constituents from Futunn, archived with observation time."""
import re
import pandas as pd

from .broker import FutuSim
from .config import digest
from .pipeline import artifact
from .store import utcnow
from .schedule import calendar_state

INDEXES={'nasdaq100':('US..NDX',80,130),'sp500':('US..SPX',450,550)}


def constituents(broker,symbol,minimum,maximum):
    members=set();cursor=None;total=None
    for _ in range(20):
        query={'symbol':symbol,'limit':50,'valuation_type':1}
        if cursor is not None:query['next_key']=cursor
        result=broker.request('GET','/api/v1.0/quote/valuation/index-stocks',query)
        for r in result['data']['stock_list']:
            s=r['symbol']
            if not re.fullmatch(r'US\.[A-Za-z0-9][A-Za-z0-9.-]*',s):
                raise ValueError('Unexpected index constituent symbol')
            members.add(s)
        pagination=result.get('pagination',{});total=pagination.get('total',total)
        if not pagination.get('has_more'):
            if not minimum<=len(members)<=maximum or (total is not None and len(members)!=int(total)):
                raise ValueError('Index membership incomplete or implausible')
            return sorted(members)
        next_key=pagination.get('next_key')
        if next_key in (None,'-1',cursor):raise ValueError('Index pagination did not advance')
        cursor=next_key
    raise ValueError('Index pagination exceeded limit')


def refresh_indices(store,force=False):
    asof=calendar_state()['completed_session'];cached=store.meta('index_universes')
    if cached and cached.get('asof')==asof and not force:return cached
    broker=FutuSim(store.settings);indexes={}
    for name,(symbol,minimum,maximum) in INDEXES.items():
        indexes[name]={'index_symbol':symbol,'members':constituents(broker,symbol,minimum,maximum)}
    value={'observed_at':utcnow(),'asof':asof,'source':'Futunn REST valuation/index-stocks, all pages verified',
           'indexes':indexes,'limitation':'Current observed constituents, not historical point-in-time membership'}
    artifact(store,'index-members-'+digest(value)[:16],value)
    store.set_meta('index_universes',value)
    store.event('index_members_refreshed',{name:len(r['members']) for name,r in indexes.items()})
    return value
