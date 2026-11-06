"""Opt-in official continuation of a real failed-first-turn history copy."""
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
    failed = json.loads((root / 'failed-first-turn.json').read_text())
    assert failed['thread'] == TEMP_CHAT and failed['request_receipts'] == 0
    runtime = json.loads((root / 'runtime.json').read_text())
    try:
        connection = socket.create_connection(('127.0.0.1', runtime['port']), timeout=1)
    except OSError:
        pass
    else:
        connection.close()
        raise RuntimeError('Stop this isolated gateway first')
    home = root / 'desktop-home'
    state_path = root / 'native/desktop.json'
    state = json.loads(state_path.read_text())
    assert state['transport'] == 'builtin-openai'
    saved_route = json.loads((root / 'native/desktop-route.json').read_text())
    # The QA runtime has no user GUI baseline. Establish an official baseline
    # solely in this separate home, then use the product lifecycle without a
    # LaunchAgent. The already-failed chat's history/model are left untouched.
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
        assert resumed['modelProvider'] == 'openai'
        assert resumed['model'] == failed['selected_real_model']
        result = client.turn(TEMP_CHAT, '原样回复你在刚才失败的用户请求中看到的 P2_FAILED 开头的标记。只回复该标记，不用工具。')
        assert 'P2_FAILED_HISTORY_1106' in result['text'], 'Failed user turn was not recalled'
        report = {'same_thread': resumed['thread']['id'] == TEMP_CHAT,
                  'provider': resumed['modelProvider'], 'model': resumed['model'],
                  'official_turn': result['status'], 'failed_turn_marker_recalled': True,
                  'gateway_stopped': True, 'isolation': check(root),
                  'browser_gui': 'not_executed_in_this_failure_probe'}
    finally:
        client.close()
    assert report['isolation']['originals_unchanged']
    (root / 'p2-restored-official.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({k: v for k, v in report.items() if k != 'isolation'}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--live', action='store_true', required=True)
    args = parser.parse_args()
    probe(args.root)
