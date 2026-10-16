"""Isolated signed-loopback fault matrix; no live runtime or external orders."""
from contextlib import ExitStack
from pathlib import Path
import json
import sys
import tempfile
import traceback
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from tests.test_quant_supervisor_mock import rehearsal_store
from tests.test_quant_shared_options import shared
from scripts.mock_quant_opening import CASES,run_case
from quant_agent import capital,store,champion,paper_activation,option_paper,promotion

def main():
    results=[]
    for mandate in ('parallel-fixture','shared-900k-100k'):
        for case in CASES:
            with tempfile.TemporaryDirectory(prefix='quant-resilience-') as directory:
                try:
                    with ExitStack() as patches:
                        for module in (capital,store,champion,paper_activation,option_paper,promotion):
                            patches.enter_context(patch.object(module,'utcnow',lambda:'2026-10-09T20:00:00+00:00'))
                        if mandate=='parallel-fixture':s=rehearsal_store(Path(directory))
                        else:
                            s,b=shared(Path(directory));champion.rotation_tick(s,b,{},'2026-10-13')
                    result=run_case(s.home,s.settings,case,http_mock=True)
                    result['mandate']=mandate
                except Exception as exc:
                    result={'mandate':mandate,'case':case,'result':'FAIL','error_type':type(exc).__name__,'traceback':traceback.format_exc()}
                results.append(result)
                print(mandate,case,result['result'],flush=True)
    report={'mode':'MOCK signed local HTTP','external_orders_sent':0,'passed':all(r['result']=='PASS' for r in results),'results':results}
    target=Path(__file__).with_name('opening-fault-matrix.json')
    target.write_text(json.dumps(report,ensure_ascii=False,indent=2));target.chmod(0o600)
    if not report['passed']:raise SystemExit(1)

if __name__=='__main__':main()
