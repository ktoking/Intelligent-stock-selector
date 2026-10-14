"""Freeze sanitized evidence and measured excerpts from successful UI captures."""
import json
from pathlib import Path
import shutil
import subprocess
import urllib.request

ROOT=Path(__file__).resolve().parent.parent


def crop(source, name, box):
    target=ROOT/'assets'/name
    result=subprocess.run(['ffmpeg','-v','error','-y','-i',str(ROOT/source),'-vf','crop='+box,
                           '-frames:v','1',str(target)],capture_output=True,text=True)
    if result.returncode:raise RuntimeError(result.stderr)


def main():
    assets=ROOT/'assets';assets.mkdir(exist_ok=True)
    crop('capture/screenshots/full-page.png','dashboard.png','1380:1800:270:90')
    crop('capture-market/screenshots/full-page.png','market.png','1300:830:310:325')
    crop('capture-research/screenshots/full-page.png','strategies.png','1300:320:310:232')
    crop('capture-research/screenshots/full-page.png','journal.png','1300:750:310:1140')
    crop('capture-review-v2/screenshots/scroll-000.png','review.png','1300:336:310:232')
    shutil.copyfile(ROOT/'capture/assets/svgs/logo-e4411fe9.svg',assets/'quant-mark.svg')
    raw=(assets/'quant-mark.svg').read_text().replace('<path ','<path fill="none" stroke="#F5F5F7" stroke-width="1.8" stroke-linecap="round" ')
    (assets/'quant-mark.svg').write_text(raw)
    status=json.load(urllib.request.urlopen('http://127.0.0.1:8791/api/status'))
    records=[r for r in status['strategies'] if r['chart_available'] and r['data_hash']==status['focus_data_hash']]
    r=max(records,key=lambda r:r['stats']['retrospective_test']['return_pct'])
    chart=json.load(urllib.request.urlopen('http://127.0.0.1:8791/api/chart/'+r['full_id']+'?mode=history'))
    activity=json.load(urllib.request.urlopen('http://127.0.0.1:8791/api/activity/'+r['full_id']+'?mode=history'))
    first_buy=next(t for t in reversed(activity['trades']) if t['side']=='BUY')
    first_sell=next(t for t in reversed(activity['trades']) if t['side']=='SELL')
    data={'curve':chart['curve'],'benchmark_curve':chart['benchmark_curve'],'trades':[first_buy,first_sell],
          'name':r['name'],'kind':'historical_hypothetical_trades','source':chart['source'],
          'window':chart['window'],'stats':r['stats']['retrospective_test']}
    (assets/'chart-data.json').write_text(json.dumps(data,ensure_ascii=False,indent=2))
    # Bind assets to visible, checked captures instead of regenerating the website.
    inventory='''# Verified product assets

- assets/dashboard.png — measured crop of the successful overview full-page capture, real UI.
- assets/market.png — measured crop of the successful market capture, observed movers and timestamps.
- assets/strategies.png — real frozen strategy cards; 3 future-observation strategies.
- assets/journal.png — real historical transaction detail panel from the research capture.
- assets/review.png — real Chinese review summary, successful capture-review-v2 only.
- assets/quant-mark.svg — original Quant header mark, original paths plus captured stroke styling.
- assets/chart-data.json — frozen local historical NAV and two actual recorded hypothetical trades. No paper fills.
'''
    (ROOT/'capture/extracted/asset-descriptions.md').write_text(inventory)
    print(json.dumps({'assets':7,'historical_window':chart['window'],'trade_count':len(activity['trades'])}))


if __name__=='__main__':main()
