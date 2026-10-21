"""Local structured diagnostics. Never log request headers, bodies or query strings."""
from datetime import datetime,timezone
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import re
import threading
import traceback

_LOG=logging.getLogger('quant_agent')
_LOCK=threading.RLock()
_ERRORS=0


def error_evidence(exc):
    value={'error_type':type(exc).__name__,'frames':[
        {'file':Path(f.filename).name,'line':f.lineno,'function':f.name}
        for f in traceback.extract_tb(exc.__traceback__)[-12:]]}
    # Only application-defined safe diagnostics, never arbitrary HTTP/CLI text.
    if type(exc).__module__ in ('quant_agent.broker','quant_agent.checkpoints','quant_agent.ai'):
        value['reason']=str(exc)[:240]
    elif str(exc).startswith(('Invalid OHLCV:','New cache','AI evidence packet too large')):
        value['reason']=str(exc)[:240]
    for key in ('request_id','http_status','request_method','request_route','read_failure_kind','ret_code','retry_after','exit_code','diagnostic_code','prompt_chars','stderr_sha256'):
        if hasattr(exc,key):value[key]=getattr(exc,key)
    return value


def safe_route(path):
    route=re.sub(r'/sim-trade/(?!accounts(?:$|/))[^/]+(?=/|$)', '/sim-trade/{account}',path.split('?',1)[0])
    return re.sub(r'/orders/[^/]+/cancel$', '/orders/{order}/cancel',route)


class LocalHandler(RotatingFileHandler):
    def _open(self):
        stream=super()._open();Path(self.baseFilename).chmod(0o600);return stream
    def handleError(self,record):
        global _ERRORS
        _ERRORS+=1  # SQLite remains the primary durable execution journal.


def configure(home):
    directory=Path(home)/'logs';directory.mkdir(parents=True,exist_ok=True,mode=0o700);directory.chmod(0o700)
    path=str(directory/'runtime.jsonl')
    with _LOCK:
        if any(getattr(h,'baseFilename',None)==path for h in _LOG.handlers):return
        for handler in list(_LOG.handlers):_LOG.removeHandler(handler);handler.close()
        handler=LocalHandler(path,maxBytes=5*1024*1024,backupCount=5,encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(message)s'));_LOG.addHandler(handler)
        _LOG.setLevel(logging.INFO);_LOG.propagate=False


def emit(event,**fields):
    try:
        _LOG.info(json.dumps({'time':datetime.now(timezone.utc).isoformat(),'thread':threading.current_thread().name,'event':event,**fields},ensure_ascii=False,allow_nan=False))
    except Exception:
        global _ERRORS
        _ERRORS+=1


def status():
    return {'file_enabled':bool(_LOG.handlers),'write_failures':_ERRORS,'rotation_bytes':5*1024*1024,'retained_files':6,
            'scope':'本机 JSONL；SQLite 保存订单意图、成交、任务检查点和故障事件；不记录签名、私钥、请求正文或查询参数。'}
