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


def price_revision_hash(old,new):
    before,after=old.set_index('date'),new.set_index('date')
    overlap=before.index.intersection(after.index);keys=['open','high','low','close']
    return digest({'dates':[int(d) for d in overlap],
        'observed':before.loc[overlap,keys].to_numpy().tolist(),
        'revised':after.loc[overlap,keys].to_numpy().tolist()})


def inherited_review(source,research_dir,previous,latest):
    """Reuse only the identical already-reviewed region; new tail stays strict."""
    audit_path=Path(source)/'revision_audit.json'
    if not audit_path.exists():return None
    original=json.loads(audit_path.read_text()).get('price_review') or {}
    regions=original.get('regions',{})
    if not regions:return None
    approved={}
    for symbol,region in regions.items():
        old=pd.read_pickle(Path(source)/(symbol[3:]+'.pkl'))
        new=pd.read_pickle(Path(research_dir)/(symbol[3:]+'.pkl'))
        cutoff=region['through_date']
        before,after=old.loc[old.date<=cutoff],new.loc[new.date<=cutoff]
        if (len(before)!=len(after) or len(before)!=region['rows'] or
                price_revision_hash(before,after)!=region['price_hash']):
            return None
        try:append_observed(old.loc[old.date>cutoff],new.loc[new.date>cutoff])
        except ValueError:return None
        approved[symbol]=price_revision_hash(old,new)
    inherited={**original,'source_sha256':previous.metadata['input_sha256'],
        'research_sha256':latest.metadata['input_sha256'],'symbols':approved,
        'inherited_from':str(audit_path)}
    inherited.pop('append_ohlc',None)  # An old reviewed bar is now frozen overlap.
    return inherited


def append_observed(old,new,*,reviewed_price_hash=None):
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
    price_revision=changed[keys[:3]].any().any() or (close_drift>.005).any()
    # A review binds the exact observed/revised OHLC arrays. It authorizes
    # retaining those observations, never rewriting or rescaling frozen bars.
    price_hash=price_revision_hash(old,new)
    if price_revision and reviewed_price_hash!=price_hash:
        raise ValueError('Execution OHLC basis revised; corporate-action review required')
    if price_revision and ((delta[keys[:4]]/before.loc[overlap,keys[:4]].abs())>.01).any().any():
        # A small cash adjustment can exceed 1% of an old multi-year low.
        # Permit only an exact uniform affine adjustment with a <=1% current
        # reference effect and near-unit scale; share splits remain excluded.
        x=before.loc[overlap,keys[:4]].to_numpy().ravel()
        y=after.loc[overlap,keys[:4]].to_numpy().ravel()
        scale,offset=np.linalg.lstsq(np.column_stack([x,np.ones(len(x))]),y,rcond=None)[0]
        reference=float(before.loc[overlap,'close'].iloc[-1])
        if not (.99<=scale<=1.01 and abs(offset)/reference<=.01 and
                float(close_drift.iloc[-1])<=.01 and np.allclose(y,scale*x+offset,rtol=1e-8,atol=1e-6)):
            raise ValueError('Reviewed append cannot waive a price change over 1%; split/basis migration required')
    corrections=[]
    for day in overlap:
        fields=[k for k in (keys if price_revision else keys[3:]) if changed.loc[day,k]]
        if fields:
            corrections.append({'date':str(int(day)),'fields':fields,
                'observed':{k:float(before.loc[day,k]) for k in fields},
                'vendor_revised':{k:float(after.loc[day,k]) for k in fields}})
    appended=new.loc[new.date>old.date.max()]
    return pd.concat([old,appended],ignore_index=True),corrections


def prepare_execution_cache(store,research_dir,source=None,records=None,review=None):
    source=Path(source or store.meta('execution_data_dir',store.meta('current_data_dir',store.settings.data_dir)))
    research_dir=Path(research_dir)
    previous=load_panel(source);latest=load_panel(research_dir)
    if review:
        if (review.get('source_sha256')!=previous.metadata['input_sha256'] or
                review.get('research_sha256')!=latest.metadata['input_sha256'] or
                not review.get('evidence') or not review.get('reviewed_at')):
            raise ValueError('Price review is missing evidence or bound to a different data version')
    records=store.strategies(['paper','shadow']) if records is None else records
    for record in records:
        evidence=record['evidence']
        if data_prefix_hash(frozen_view(previous,record),evidence['seed_asof'])!=evidence['seed_hash']:
            raise ValueError('Execution source does not match the frozen seed')
    if set(previous.symbols)-set(latest.symbols):raise ValueError('Execution universe member missing')
    tag=digest([previous.metadata['input_sha256'],latest.metadata['input_sha256'],
        *([digest(review)] if review else [])])[:20]
    output=store.home/'execution-data'/tag
    if not (output/'completed.json').exists():
        output.parent.mkdir(exist_ok=True,mode=0o700)
        if output.exists():shutil.rmtree(output)  # Only this unpublished attempt.
        output.mkdir(mode=0o700)
        corrections=[]
        for symbol in latest.symbols:
            name=symbol[3:]+'.pkl';new=pd.read_pickle(research_dir/name)
            if symbol in previous.symbols:
                approval=(review or {}).get('symbols',{}).get(symbol)
                if approval and symbol in store.portfolio()['positions']:
                    raise ValueError('Reviewed historical price change on a held symbol requires cashflow/position review: '+symbol)
                try:
                    frame,changes=append_observed(pd.read_pickle(source/name),new,
                        reviewed_price_hash=approval)
                except ValueError as exc:
                    raise ValueError(symbol+': '+str(exc)) from None
                # Some forward adjustments include the newly appended day.
                # An exact reviewed raw bar may bridge it onto the retained
                # share basis. Only the verified uniform affine inverse is
                # allowed; never replace any already observed row.
                raw_bars=(review or {}).get('append_ohlc',{}).get(symbol,[])
                if raw_bars:
                    if not approval:raise ValueError('Raw append requires a bound price review')
                    old=pd.read_pickle(source/name).set_index('date');vendor=new.set_index('date')
                    overlap=old.index.intersection(vendor.index);ohlc=['open','high','low','close']
                    x=old.loc[overlap,ohlc].to_numpy().ravel();y=vendor.loc[overlap,ohlc].to_numpy().ravel()
                    scale,offset=np.linalg.lstsq(np.column_stack([x,np.ones(len(x))]),y,rcond=None)[0]
                    if not (.99<=scale<=1.01 and abs(offset)/float(old.close.iloc[-1])<=.01 and np.allclose(y,scale*x+offset,rtol=1e-8,atol=1e-6)):
                        raise ValueError('Raw append does not match a verified uniform share-basis bridge')
                    for raw in raw_bars:
                        day=raw['date']
                        if day<=old.index.max() or day not in vendor.index:raise ValueError('Raw append would rewrite observed history or invent a day')
                        values=np.array([raw[k] for k in ohlc],dtype=float)
                        if not np.allclose(values,(vendor.loc[day,ohlc].to_numpy(dtype=float)-offset)/scale,rtol=1e-8,atol=1e-6):
                            raise ValueError('Raw append differs from reviewed affine bridge')
                        frame.loc[frame.date==day,ohlc]=values
                        changes.append({'date':str(day),'fields':ohlc,'kind':'verified_raw_append',
                            'observed':dict(zip(ohlc,values.tolist())),'vendor_revised':vendor.loc[day,ohlc].astype(float).to_dict()})
                corrections.extend({'symbol':symbol,**r} for r in changes)
            else:frame=new
            frame.to_pickle(output/name)
        for name in ('universe.json','security_types.json'):
            shutil.copyfile(research_dir/name,output/name)
        panel=load_panel(output)
        if panel.metadata['end']!=latest.metadata['end'] or not np.isfinite(panel.fields['close'][-1]).all():
            raise ValueError('Execution append does not cover latest completed session')
        report={'source_cache':str(source),'research_cache':str(research_dir),
            'price_review':review,
            'flow_corrections':corrections,'policy':'Preserve observed bars; append sessions. OHL/larger close changes require exact reviewed hashes on unheld symbols. <=1% localized corrections or uniform near-unit affine adjustments with <=1% recent reference impact only; no split/position migration.',
            'data_asof':panel.metadata['end']}
        (output/'revision_audit.json').write_text(canonical(report))
        (output/'completed.json').write_text(canonical({'input_sha256':panel.metadata['input_sha256']}))
    panel=load_panel(output)
    for record in records:
        evidence=record['evidence']
        if data_prefix_hash(frozen_view(panel,record),evidence['seed_asof'])!=evidence['seed_hash']:
            raise ValueError('Execution append changed frozen seed')
    return output,panel
