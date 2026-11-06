"""Real same-chat official continuation after the builtin product restore path.

Requires the isolated runtime stopped and a completed desktop config probe.
Does not operate the GUI or touch the source chat/config.
"""
import argparse
import json
import os
from pathlib import Path
import socket
from types import SimpleNamespace
from unittest.mock import patch

from isolated_desktop_qa import TEMP_CHAT, check, runtime_root
from isolated_qa_probe import Client
from qp import desktop
from qp.native import atomic


def probe(root):
    root = runtime_root(root)
    runtime = json.loads((root / 'runtime.json').read_text())
    try:
        connection = socket.create_connection(('127.0.0.1', runtime['port']), timeout=1)
    except OSError:
        pass
    else:
        connection.close()
        raise RuntimeError('Stop this isolated gateway before running restore QA')
    previous = json.loads((root / 'desktop-config-probe.json').read_text())
    if len(previous['stages']) != 4 or not previous['isolation']['originals_unchanged']:
        raise RuntimeError('Complete the config/history probe first')
    state_path = root / 'native/desktop.json'
    state = json.loads(state_path.read_text())
    if state.get('transport') != 'builtin-openai':
        raise RuntimeError('Expected the isolated builtin transport')
    saved_route = json.loads((root / 'native/desktop-route.json').read_text())
    home = root / 'desktop-home'
    # Establish a synthetic official baseline, then exercise the actual product
    # connect+restore lifecycle. No LaunchAgent is installed for this fixture.
    atomic(home / 'config.toml', 'model="gpt-6.1-sol"\nmodel_provider="openai"\n')
    state_path.unlink()
    native = SimpleNamespace(root=root / 'native', active=saved_route['route'], grant=saved_route['token'],
                             port=runtime['port'], db_path=root / 'ledger.db', desktop_error=None)
    with patch.dict(os.environ, CODEX_HOME=str(home), QPOOL_CONFIG=str(root / 'pools.json')):
        desktop.connect(native, install_background=False)
        desktop.restore(native)
    client = Client(home=home)
    try:
        resumed = client.call('thread/resume', {'threadId': TEMP_CHAT, 'excludeTurns': True})
        if resumed['model'] == 'qpool-auto' or resumed['modelProvider'] != 'openai':
            raise RuntimeError('Chat alias/provider was not restored')
        result = client.turn(TEMP_CHAT, '只回复你记住的 PROFILE_HISTORY 标记，不用工具。')
        if 'PROFILE_HISTORY_1106' not in result['text']:
            raise RuntimeError('History recall failed after official restore')
        report = {'same_thread': resumed['thread']['id'] == TEMP_CHAT,
                  'provider': resumed['modelProvider'], 'model': resumed['model'],
                  'official_turn': result['status'], 'history_recalled': True,
                  'gateway_stopped_before_restore': True,
                  'isolation': check(root), 'full_gui_regression': 'not_run_app_control_prohibited'}
    finally:
        client.close()
    (root / 'builtin-restore-probe.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    assert report['isolation']['originals_unchanged']
    print(json.dumps({k: v for k, v in report.items() if k != 'isolation'}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--live', action='store_true', required=True)
    args = parser.parse_args()
    probe(args.root)
