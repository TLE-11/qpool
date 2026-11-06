"""Opt-in real engine probe of the isolated desktop home, without -c overrides.

This does not operate the GUI or attest browser permissions. It only uses the
authorized temporary history COPY prepared by isolated_desktop_qa.py.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import time

from isolated_desktop_qa import TEMP_CHAT, check, runtime_root
from isolated_qa_probe import Client, api


def probe(root):
    root = runtime_root(root)
    home = root / 'desktop-home'
    state = json.loads((root / 'desktop-qa.json').read_text())
    history = home / Path(state['source_history']).relative_to(Path(state['main_config']).parent)
    workspace = root / 'engine-workspace'
    workspace.mkdir(exist_ok=True)
    results = []
    client = Client(home=home)

    def run(tid, stage, prompt, plan, model):
        started = time.time()
        result = client.turn(tid, prompt)
        sid = hashlib.sha256(tid.encode()).hexdigest()[:16]
        with sqlite3.connect(root / 'native/sessions.db') as db:
            rows = db.execute('SELECT account,model,status FROM requests WHERE session=? AND started>=? ORDER BY started',
                              (sid, started)).fetchall()
        row = {'stage': stage, 'engine_status': result['status'], 'receipts': rows}
        results.append(row)
        print(json.dumps(row), flush=True)
        assert rows and all(r[2] == 'completed' for r in rows), 'No completed gateway receipt'
        assert plan in rows[-1][0] and rows[-1][1] == model, 'Unexpected execution account/model'
        return result

    try:
        tid = client.call('thread/start', {'cwd': str(workspace), 'approvalPolicy': 'never',
                                          'sandbox': 'read-only'})['thread']['id']
        run(tid, 'new-chat-default-config', '测试执行验证：本次只回复 CONFIG_PLUS_OK，不调用工具、不修改代码。',
            'plus', 'gpt-6.1-sol')
        client.call('thread/resume', {'threadId': TEMP_CHAT, 'path': str(history), 'cwd': str(workspace),
                    'model': 'gpt-6.1-sol', 'approvalPolicy': 'never', 'sandbox': 'read-only', 'excludeTurns': True})
        run(TEMP_CHAT, 'old-chat-native-config', '隔离配置验证，请记住标记 PROFILE_HISTORY_1106，只回复该标记，不用工具。',
            'pro', 'gpt-6.1-sol')
        active = api(root, '/api/state')['native']['active']
        raw = json.loads((root / 'pools.json').read_text())
        channels = {c['id']: c for c in raw['channels']}
        target = channels[active['primary'][0]]['account_id']
        api(root, '/api/sessions/migrate', {'session': state['session'], 'target_account_id': target, 'confirm': True})
        result = run(TEMP_CHAT, 'old-chat-migrated-plus', '只回复上一轮记住的 PROFILE_HISTORY 标记，不用工具。',
                     'plus', 'gpt-6.1-sol')
        assert 'PROFILE_HISTORY_1106' in result['text'], 'History marker not recalled'
        client.call('thread/settings/update', {'threadId': TEMP_CHAT, 'model': 'qpool-auto'})
        run(TEMP_CHAT, 'old-chat-review-astra',
            '仅Review这段代码的并发风险，用一句话说明：两个线程同时执行 if balance>=80: balance-=80。不运行工具，不改代码。',
            'plus', 'gpt-6-astra')
    finally:
        client.close()
        report = {'entry': 'bundled app-server, separate user home, no CLI -c overrides',
                  'stages': results, 'isolation': check(root),
                  'full_gui_regression': 'not_run_app_control_prohibited'}
        (root / 'desktop-config-probe.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
        assert report['isolation']['originals_unchanged'], 'Source config/history changed'
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--live', action='store_true', required=True)
    args = parser.parse_args()
    probe(args.root)
    print('CONFIG_PROFILE_PROBE_PASSED', flush=True)
