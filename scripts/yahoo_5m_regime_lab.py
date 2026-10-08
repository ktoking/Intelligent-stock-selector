#!/usr/bin/env python3
"""Fixed regime-aware hypotheses, evaluated with the unchanged five-minute account engine."""
from datetime import datetime, timezone
import argparse
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.yahoo_5m_factor_lab import load_data,features,simulate


def regime_features(data,pairs=None):
    pairs=tuple(pairs) if pairs is not None else (('SOXL','SMH'),('TQQQ','QQQ'))
    result=features(data,pairs=pairs)
    for symbol,reference in pairs:
        raw=data[symbol]; f=result[symbol]
        day=raw.index.tz_convert('America/New_York').strftime('%Y-%m-%d')
        slot=raw.index.tz_convert('America/New_York').strftime('%H:%M')
        typical=(raw.high+raw.low+raw.close)/3
        vwap=(typical*raw.volume).groupby(day).cumsum()/raw.volume.groupby(day).cumsum().replace(0,np.nan)
        true_range=pd.concat([raw.high-raw.low,(raw.high-raw.close.shift()).abs(),(raw.low-raw.close.shift()).abs()],axis=1).max(axis=1)
        atr=true_range.rolling(14).mean()
        distance=(raw.close-vwap)/atr
        efficiency=raw.close.diff(20).abs()/raw.close.diff().abs().rolling(20).sum().replace(0,np.nan)
        change=raw.close.diff()
        up=change.clip(lower=0).rolling(14).mean()
        down=-change.clip(upper=0).rolling(14).mean()
        rsi=100*up/(up+down).replace(0,np.nan)
        benchmark=data[reference].close
        benchmark_day_return=benchmark/benchmark.groupby(day).transform('first')-1
        reference_stable=benchmark_day_return>-.015
        normal=raw.volume.groupby(slot).transform(lambda x:x.shift().rolling(20,min_periods=3).median())
        rvol=raw.volume/normal.replace(0,np.nan)
        turning=(raw.close>raw.close.shift())&(raw.close>raw.open)
        range_low=raw.low.groupby(day).transform(lambda x:x.shift().rolling(12,min_periods=6).min())
        ema=raw.close.ewm(span=9,adjust=False).mean()
        masks={
            'vwap_reclaim':(distance.shift()<-1)&(distance>distance.shift())&(distance<0)&turning&reference_stable,
            'oversold_bounce':(distance<-1)&(rsi<45)&turning&reference_stable,
            'range_bounce':(efficiency<.35)&(raw.low<=range_low)&turning&(raw.close>range_low)&reference_stable,
            'trend_retest':(efficiency>=.35)&(f.trend15>0)&(f.reference_trend>0)&(raw.low<=ema)&(raw.close>ema)&turning,
        }
        # Score only ranks simultaneous candidates; each entry mask explicitly
        # requires its own price/regime/reference confirmations.
        f['score']=(-distance).clip(lower=0).fillna(0)+turning.astype(float)
        for name,mask in masks.items():
            f[name+'_plain']=mask.fillna(False)
            f[name+'_volume']=(mask & (rvol>=1.2)).fillna(False)
        f['distance_atr']=distance
        f['efficiency']=efficiency
        f['rvol']=rvol
    return result


def configs():
    return [dict(name=f'{family}_{gate}_risk{int(risk*10000)}',family=f'{family}_{gate}',threshold=0,risk=risk)
        for family in ('vwap_reclaim','oversold_bounce','range_bounce','trend_retest')
        for gate in ('plain','volume') for risk in (.005,.01)]


def run(source,previous,output):
    if output.exists():raise FileExistsError('use a fresh output directory')
    prior=json.loads(previous.read_text())
    if hashlib.sha256(source.read_bytes()).hexdigest()!=prior['manifest']['source_sha256']:
        raise ValueError('expected same frozen pilot data')
    output.mkdir(parents=True)
    manifest={'created_at':datetime.now(timezone.utc).isoformat(),'source':str(source),
        'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
        'upstream_report_sha256':hashlib.sha256(previous.read_bytes()).hexdigest(),
        'configs':configs(),'partitions':prior['manifest']['partitions'],
        'history_status':'All partitions previously observed; chronological selection is a historical diagnostic, not new holdout evidence.',
        'selection':'>=10 training trades,>=3 validation trades; maximize worse train/validation daily Sharpe',
        'engine':'unchanged yahoo_5m_factor_lab.simulate; 7bp fee+2bp slippage each side; stress15bp fee',
        'hypotheses':'price recovery below VWAP,oversold bounce,low-efficiency range bounce,high-efficiency trend retest; volume gate on/off',
        'code':{}}
    for file in (Path(__file__),ROOT/'scripts/yahoo_5m_factor_lab.py',ROOT/'scripts/equity_factor_lab.py'):
        payload=file.read_bytes();(output/file.name).write_bytes(payload);manifest['code'][file.name]=hashlib.sha256(payload).hexdigest()
    (output/'manifest.json').write_text(json.dumps(manifest,indent=2))
    data=load_data(source);fs=regime_features(data);board=[]
    for config in configs():
        row={'config':config}
        for part in ('train','validation'):
            result=simulate(data,fs,config,manifest['partitions'][part]);row[part]=result['metrics']
            for item in ('daily','trades'):
                pd.DataFrame(result[item]).to_csv(output/f"{config['name']}_{part}_{item}.csv",index=False)
        board.append(row)
        print(config['name'],f"train {row['train']['return_pct']:.2f}%/{row['train']['trades']}trades val {row['validation']['return_pct']:.2f}%/{row['validation']['trades']}trades",flush=True)
    eligible=[r for r in board if r['train']['trades']>=10 and r['validation']['trades']>=3]
    selected=max(eligible,key=lambda r:(min(r[x]['sharpe_zero_rf'] for x in ('train','validation')),r['config']['name'])) if eligible else None
    (output/'selection_before_diagnostic_tail.json').write_text(json.dumps({'selected':selected,'board':board},indent=2))
    tail={}
    if selected:
        for fee in (0,7,15):
            result=simulate(data,fs,selected['config'],manifest['partitions']['final'],cost_bps=fee,slippage_bps=0 if fee==0 else 2)
            tail[f'fee{fee}']=result['metrics']
            for item in ('daily','trades','bars'):
                pd.DataFrame(result[item]).to_csv(output/f'selected_tail_fee{fee}_{item}.csv',index=False)
    report={'manifest':manifest,'board':board,'selected':selected,'diagnostic_tail':tail,
        'promotion_passed':False,'execution_enabled':False}
    (output/'report.json').write_text(json.dumps(report,indent=2,allow_nan=False))
    return report


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--source',type=Path,required=True);p.add_argument('--previous',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();run(a.source,a.previous,a.output)
