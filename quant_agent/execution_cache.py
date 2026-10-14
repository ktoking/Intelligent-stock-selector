"""Append observed bars while retaining the running strategies' frozen history.

Research sees vendor corrections. Execution keeps its as-observed OHLCV prefix.
Open/high/low or material close revisions block execution. Small close-only
corrections remain audited observations without rewriting the frozen prefix.
"""
import json
from pathlib import Path
import shutil

import numpy as np
import pandas as pd

from research.daily_factor_lab.core import load_panel
from research.daily_factor_lab.paper import data_prefix_hash
from .config import canonical,digest
from .dataset import frozen_view


def append_observed(old,new):
    keys=['open','high','low','close','volume','turnover']
    before=old.set_index('date');after=new.set_index('date')
    if before.index.has_duplicates or after.index.has_duplicates:
        raise ValueError('Duplicate execution history date')
    overlap=before.index.intersection(after.index)
    if len(overlap)!=len(before):raise ValueError('Vendor history lost previously observed bars')
    delta=(before.loc[overlap,keys]-after.loc[overlap,keys]).abs()
    changed=delta>(before.loc[overlap,keys].abs()*1e-9+1e-7)
    # A localized close correction does not rescale the share basis. Preserve
    # the observed bar (including its original close), within the same 0.5%
    # bound enforced between daily close and the live prev-close reference.
    close_drift=delta['close']/before.loc[overlap,'close'].abs()
    if changed[keys[:3]].any().any() or (close_drift>.005).any():
        raise ValueError('Execution OHLC basis revised; corporate-action review required')
    corrections=[]
    for day in overlap:
        fields=[k for k in keys[3:] if changed.loc[day,k]]
        if fields:
            corrections.append({'date':str(int(day)),'fields':fields,
                'observed':{k:float(before.loc[day,k]) for k in fields},
                'vendor_revised':{k:float(after.loc[day,k]) for k in fields}})
    appended=new.loc[new.date>old.date.max()]
    return pd.concat([old,appended],ignore_index=True),corrections


def prepare_execution_cache(store,research_dir,source=None,records=None):
    source=Path(source or store.meta('execution_data_dir',store.meta('current_data_dir',store.settings.data_dir)))
    research_dir=Path(research_dir)
    previous=load_panel(source);latest=load_panel(research_dir)
    records=store.strategies(['paper','shadow']) if records is None else records
    for record in records:
        evidence=record['evidence']
        if data_prefix_hash(frozen_view(previous,record),evidence['seed_asof'])!=evidence['seed_hash']:
            raise ValueError('Execution source does not match the frozen seed')
    if set(previous.symbols)-set(latest.symbols):raise ValueError('Execution universe member missing')
    tag=digest([previous.metadata['input_sha256'],latest.metadata['input_sha256']])[:20]
    output=store.home/'execution-data'/tag
    if not (output/'completed.json').exists():
        output.parent.mkdir(exist_ok=True,mode=0o700)
        if output.exists():shutil.rmtree(output)  # Only this unpublished attempt.
        output.mkdir(mode=0o700)
        corrections=[]
        for symbol in latest.symbols:
            name=symbol[3:]+'.pkl';new=pd.read_pickle(research_dir/name)
            if symbol in previous.symbols:
                frame,changes=append_observed(pd.read_pickle(source/name),new)
                corrections.extend({'symbol':symbol,**r} for r in changes)
            else:frame=new
            frame.to_pickle(output/name)
        for name in ('universe.json','security_types.json'):
            shutil.copyfile(research_dir/name,output/name)
        panel=load_panel(output)
        if panel.metadata['end']!=latest.metadata['end'] or not np.isfinite(panel.fields['close'][-1]).all():
            raise ValueError('Execution append does not cover latest completed session')
        report={'source_cache':str(source),'research_cache':str(research_dir),
            'flow_corrections':corrections,'policy':'Preserve observed bars; append sessions. Audit flow and <=0.5% close-only corrections; reject OHL or larger close changes.',
            'data_asof':panel.metadata['end']}
        (output/'revision_audit.json').write_text(canonical(report))
        (output/'completed.json').write_text(canonical({'input_sha256':panel.metadata['input_sha256']}))
    panel=load_panel(output)
    for record in records:
        evidence=record['evidence']
        if data_prefix_hash(frozen_view(panel,record),evidence['seed_asof'])!=evidence['seed_hash']:
            raise ValueError('Execution append changed frozen seed')
    return output,panel
