"""The temporary Codex analysis process must disable reporting hooks explicitly."""
import json
from pathlib import Path
from types import SimpleNamespace

from quant_agent import ai


def test_codex_analysis_has_no_hooks_or_broker_credentials(monkeypatch):
    monkeypatch.setattr(ai.shutil,'which',lambda _: '/usr/local/bin/codex')
    monkeypatch.setenv('FUTUNN_APP_KEY','test-only-key')
    monkeypatch.setenv('FUTUNN_PRIVATE_KEY_FILE','test-only-private-path')
    calls=[]
    def local_result(command,**kwargs):
        calls.append((command,kwargs))
        output=Path(command[command.index('--output-last-message')+1])
        output.write_text(json.dumps({'candidates':[],'review':'离线测试','factor_ideas':[]}))
        return SimpleNamespace(returncode=0,stderr='model: gpt-6.1-sol\nreasoning effort: high\n')
    monkeypatch.setattr(ai.subprocess,'run',local_result)
    result=ai.ask('codex',{'purpose':'offline-test'})
    command,kwargs=calls[0]
    disabled={command[i+1] for i,flag in enumerate(command[:-1]) if flag=='--disable'}
    assert {'hooks','codex_hooks','plugins','apps','shell_tool'}<=disabled
    assert '--ignore-user-config' in command and '--ephemeral' in command
    assert 'FUTUNN_APP_KEY' not in kwargs['env'] and 'FUTUNN_PRIVATE_KEY_FILE' not in kwargs['env']
    assert command[command.index('--cd')+1].split('/')[-1].startswith('quant-analysis-')
    assert result['model']=='gpt-6.1-sol' and result['result']['review']=='离线测试'
