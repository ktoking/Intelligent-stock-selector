import json
import threading
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import pytest

from quant_agent.config import initialize
from quant_agent.store import Store
from quant_agent import self_check
from quant_agent.web import start_web


def test_diagnostics_do_not_leak_raw_exception_details():
    failure = self_check.safe_failure(RuntimeError('SECRET_TOKEN private-account-id https://secret.example'))
    assert 'SECRET_TOKEN' not in json.dumps(failure)
    assert failure['status'] == 'failed' and failure['evidence']['error_type'] == 'RuntimeError'


def test_mcp_inventory_does_not_probe_company_or_arbitrary_servers(tmp_path, monkeypatch):
    monkeypatch.setattr(self_check.Path, 'home', lambda: tmp_path)
    (tmp_path/'.codex').mkdir()
    (tmp_path/'.codex/config.toml').write_text('''[mcp_servers.skynet-base]
command="should-never-launch"
[mcp_servers.futu-mcp]
url="https://private.example/token-secret"
[mcp_servers.disabled]
enabled=false
command="should-never-launch"
''')
    inventory = self_check.mcp_inventory()
    assert inventory[0]['status'] == 'skipped'
    assert inventory[2]['status'] == 'skipped'
    assert 'token-secret' not in json.dumps(inventory)
    monkeypatch.setattr(self_check.requests, 'post', lambda *a,**kw: pytest.fail('Unapproved host contacted'))
    assert self_check.mcp_probe()['status'] == 'unverified'


def test_mcp_auth_required_is_not_treated_as_verified_tool_access(tmp_path, monkeypatch):
    monkeypatch.setattr(self_check.Path, 'home', lambda: tmp_path)
    (tmp_path/'.codex').mkdir()
    (tmp_path/'.codex/config.toml').write_text('[mcp_servers.futu-mcp]\nurl="https://mcp.futunn.com/mcp"\n')
    class Response:
        status_code=401
        def __enter__(self): return self
        def __exit__(self,*args): pass
    monkeypatch.setattr(self_check.requests,'post',lambda *a,**kw:Response())
    value=self_check.mcp_probe()
    assert value['status']=='unverified' and value['evidence']['authenticated'] is False


def test_single_flight_and_get_no_effect_with_cross_origin_gate(tmp_path, monkeypatch):
    store=Store(tmp_path,initialize(tmp_path,quote_push_enabled=False))
    entered=threading.Event();release=threading.Event();calls=[]
    def probe():
        calls.append('read-only');entered.set();release.wait(3)
        return self_check.result('ok','probe passed')
    monkeypatch.setattr(self_check,'check_jobs',lambda _: [('probe','Test','test',True,probe)])
    monkeypatch.setattr(self_check,'mcp_inventory',lambda: [])
    server=start_web(store,port=0);base=f'http://127.0.0.1:{server.server_port}'
    def post(origin=None,body=b'{}'):
        headers={'Content-Type':'application/json'}
        if origin:headers['Origin']=origin
        return urlopen(Request(base+'/api/self-check',data=body,headers=headers),timeout=3)
    try:
        with urlopen(base+'/api/self-check') as r:assert json.load(r)['status']=='idle'
        assert not calls
        for origin,body,code in [('https://evil.example',b'{}',403),(base,b'{"url":"http://private"}',400)]:
            with pytest.raises(HTTPError) as exc:post(origin,body)
            assert exc.value.code==code
        with post(base) as r:assert json.load(r)['status']=='running'
        assert entered.wait(2)
        with post(base) as r:assert json.load(r)['status']=='running'
        assert len(calls)==1
        release.set();report={}
        for _ in range(100):
            with urlopen(base+'/api/self-check') as r: report=json.load(r)
            if report['status']=='completed':break
            time.sleep(.01)
        assert report['overall']=='ok'
        with post(base) as r:assert json.load(r)['status']=='completed'
        assert len(calls)==1 and store.orders()==[]
    finally:
        release.set();server.shutdown();server.server_close()
