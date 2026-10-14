"""Immutable US session lookup; avoid shared pandas index engines across threads."""
from bisect import bisect_left,bisect_right
from datetime import datetime,timezone
from types import MappingProxyType
import threading

import exchange_calendars as xc
import pandas as pd

_lock=threading.Lock()
_calendar=None


class USCalendar:
    def __init__(self):
        # Warm the library default before frozen option code can access it.
        default=xc.get_calendar('XNYS')
        default.schedule.index.get_indexer(default.schedule.index)
        default.session_open(default.sessions[0])
        source=type(default)(start='2000-01-01',end='2040-12-31')
        self.days=tuple(str(d.date()) for d in source.schedule.index)
        self.bounds=MappingProxyType({day:(pd.Timestamp(o),pd.Timestamp(c)) for day,o,c in
            zip(self.days,source.schedule['open'],source.schedule['close'])})

    @staticmethod
    def day(value):return str(pd.Timestamp(value).date())

    def is_session(self,day):return self.day(day) in self.bounds
    def session_open(self,day):return self.bounds[self.day(day)][0]
    def session_close(self,day):return self.bounds[self.day(day)][1]

    def sessions_in_range(self,start,end):
        return pd.DatetimeIndex(self.days[bisect_left(self.days,self.day(start)):bisect_right(self.days,self.day(end))])

    def date_to_session(self,day,direction='none'):
        day=self.day(day)
        if day in self.bounds:return pd.Timestamp(day)
        if direction=='previous':index=bisect_right(self.days,day)-1
        elif direction=='next':index=bisect_left(self.days,day)
        else:raise ValueError('Not a US exchange session')
        if not 0<=index<len(self.days):raise ValueError('Outside supported exchange calendar')
        return pd.Timestamp(self.days[index])

    def next_session(self,day):
        index=bisect_right(self.days,self.day(day))
        if index>=len(self.days):raise ValueError('Outside supported exchange calendar')
        return pd.Timestamp(self.days[index])


def us_calendar():
    global _calendar
    with _lock:
        if _calendar is None:_calendar=USCalendar()
        return _calendar


def latest_completed_session(now=None):
    now=pd.Timestamp(now or datetime.now(timezone.utc))
    if now.tzinfo is None:raise ValueError('Current time must include timezone')
    cal=us_calendar()
    days=cal.sessions_in_range(now.date()-pd.Timedelta(days=14),now.date())
    return next(str(day.date()) for day in reversed(days) if cal.session_close(day)<=now)
