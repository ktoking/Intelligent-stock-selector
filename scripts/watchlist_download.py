"""Resumable watchlist5m downloader with per-symbol metadata, no price filling."""
from pathlib import Path
import json,hashlib,logging
from concurrent.futures import ThreadPoolExecutor,as_completed
import pandas as pd
import yfinance as yf
logging.getLogger('yfinance').setLevel(logging.CRITICAL)
P=Path('data/factor_lab/20260907_watchlist_v1')

def download(row):
    s=row['symbol'];dest=P/'raw'/f'{s}.pkl';meta=P/'raw'/f'{s}.json'
    if meta.exists():return json.loads(meta.read_text())
    r={'symbol':s,'requested_name':row['name']}
    try:
        ticker=yf.Ticker(s);f=ticker.history(start='2026-07-10',end='2026-09-05',interval='5m',auto_adjust=False,prepost=False,raise_errors=True,timeout=20)
        if f.empty:raise ValueError('empty data')
        f.columns=f.columns.str.lower();f.index=f.index.tz_convert('UTC');f.to_pickle(dest)
        md=ticker.get_history_metadata();r.update(status='downloaded',bars=len(f),first=str(f.index[0]),last=str(f.index[-1]),metadata={k:md.get(k) for k in ('symbol','shortName','longName','instrumentType','currency','exchangeName','fullExchangeName')},sha256=hashlib.sha256(dest.read_bytes()).hexdigest())
    except Exception as e:r.update(status='error',error=str(e))
    meta.write_text(json.dumps(r,indent=2));return r

if __name__=='__main__':
    (P/'raw').mkdir(exist_ok=True);rows=[r for r in json.loads((P/'universe.json').read_text()) if r['symbol']]
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures=[pool.submit(download,r) for r in rows]
        for n,f in enumerate(as_completed(futures),1):
            r=f.result()
            if n%20==0 or r['status']=='error':print(n,'/',len(rows),r['symbol'],r['status'],flush=True)
    records=[json.loads((P/'raw'/f'{r["symbol"]}.json').read_text()) for r in rows];(P/'download_report.json').write_text(json.dumps(records,indent=2));print('done',len(records),flush=True)
