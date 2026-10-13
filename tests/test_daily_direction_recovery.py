"""Low launchd file limit and independent-news delivery regression cases."""
import json
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest
import requests

from scripts import daily_direction_push as push


def test_download_survives_launchd_fd_limit_with_temporary_curl_cycles():
    # The leak is modelled with real FDs in a separate process. Auto-GC is
    # disabled so the test fails if download cleanup is removed.
    script = '''
import os, gc, resource
import pandas as pd
from data import us_movers_scan as scan
resource.setrlimit(resource.RLIMIT_NOFILE, (256, resource.getrlimit(resource.RLIMIT_NOFILE)[1]))
gc.disable()
class Handle:
    def __init__(self):
        self.fd = os.open('/dev/null', os.O_RDONLY)
        self.cycle = self
    def __del__(self):
        os.close(self.fd)
seen = []
def download(batch, **kwargs):
    for ticker in batch:
        Handle()
        Handle()
    seen.append(len(batch))
    frame = pd.DataFrame({'Close': [1], 'High': [2], 'Low': [0.5], 'Volume': [3]})
    return pd.concat({ticker: frame for ticker in batch}, axis=1)
scan.yf.download = download
result = scan._download_ohlcv_by_ticker([f'T{i}' for i in range(240)])
assert len(result) == 240, len(result)
with open('/dev/null', 'w') as f:
    f.write('audit and delivery still have FD capacity')
assert len(os.listdir('/dev/fd')) < 32
assert max(seen) <= 20
'''
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=30,
                            cwd=Path(__file__).resolve().parents[1])
    assert result.returncode == 0, result.stderr


def test_failed_download_still_cleans_up(monkeypatch):
    from data import us_movers_scan as scan
    collected = []
    monkeypatch.setattr(scan.gc, "collect", lambda: collected.append(True))
    def fail(*a, **kw):
        raise requests.Timeout()
    monkeypatch.setattr(scan.yf, "download", fail)
    assert scan._download_ohlcv_by_ticker([f'T{i}' for i in range(21)]) == {}
    assert len(collected) == 2


def test_csi300_pool_uses_bounded_official_request(monkeypatch):
    from data import universe
    class Response:
        content = b'xls'
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def raise_for_status(self):
            pass
    def get(url, *, timeout):
        assert url.endswith('/000300cons.xls') and timeout == (4, 10)
        return Response()
    monkeypatch.setattr(universe.requests, 'get', get)
    monkeypatch.setattr(universe.pd, 'read_excel', lambda _: pd.DataFrame({'成分券代码': [str(600000+i) for i in range(50)]}))
    assert len(universe.get_csi300_tickers_akshare()) == 50
    monkeypatch.setattr(universe.requests, 'get', lambda *a, **kw: (_ for _ in ()).throw(requests.Timeout()))
    assert universe.get_csi300_tickers_akshare() is None


def setup_fallback(monkeypatch, tmp_path, news):
    monkeypatch.setattr(push, 'load_env', lambda: None)
    monkeypatch.setattr(push, 'DELIVERY_STATE_PATH', tmp_path / 'direction-delivery.json')
    monkeypatch.setattr(push, 'NEWS_DELIVERY_STATE_PATH', tmp_path / 'news-delivery.json')
    monkeypatch.setattr(push, 'scan_markets', lambda *a, **kw: {'us': {'label': '美股', 'top_gainers': []}})
    monkeypatch.setattr(push, 'quality_errors', lambda _: ['美股行情日期滞后'])
    monkeypatch.setattr(push, 'collect_direction_news', lambda _: news)
    monkeypatch.setattr(push, 'generate_direction_report', lambda *a, **kw: pytest.fail('Stale prices must not produce direction report'))
    monkeypatch.setattr('sys.argv', ['daily_direction_push.py', '--markets', 'us', '--send', '--news-fallback',
                                   '--delivery', 'seatalk-bot', '--group-id', 'G1', '--output-dir', str(tmp_path / 'run')])


def test_stale_price_still_delivers_news_once_without_suppressing_later_direction(monkeypatch, tmp_path):
    news = {'market_news': [{'title': '已公布政策', 'publisher': '东方财富', 'published': '2026-10-13T01:00:00Z',
                             'url': 'https://example.com/policy', 'market': 'us'}]}
    setup_fallback(monkeypatch, tmp_path, news)
    sent = []
    def send(text, **kw):
        sent.append(text)
        return {'delivery': 'seatalk-bot', 'group_id': 'G1', 'thread_id': ''}
    monkeypatch.setattr('daily_direction.seatalk_bot.send_group_text_via_bot', send)
    assert push.main() == 0
    assert len(sent) == 1 and '已公布政策' in sent[0]
    assert '行情简报暂缓' in sent[0] and '【进攻】' not in sent[0]
    assert not (tmp_path / 'direction-delivery.json').exists()
    assert (tmp_path / 'news-delivery.json').exists()
    assert json.loads((tmp_path / 'run/delivery.json').read_text())['news_only'] is True
    assert push.main() == 0 and len(sent) == 1


def test_no_news_no_prices_does_not_claim_delivery_success(monkeypatch, tmp_path):
    setup_fallback(monkeypatch, tmp_path, {})
    monkeypatch.setattr('daily_direction.seatalk_bot.send_group_text_via_bot', lambda *a, **kw: pytest.fail('Must not send empty fallback'))
    assert push.main() == 1
    assert not (tmp_path / 'news-delivery.json').exists()
