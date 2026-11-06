"""Prepare/check a separate desktop profile; never launch or control the GUI.

Only the previously authorized temporary chat can be copied. Browser profiles,
plugins, refresh tokens, and production settings are never copied. A request
receipt proves gateway traffic, not a successful GUI/browser regression.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shlex
import sqlite3
import sys
import time

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
TEMP_CHAT = '01a19033-8638-7902-8ab1-4001e14a8c75'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def runtime_root(path):
    root = Path(path).resolve()
    if not root.is_relative_to(REPO / '.qpool'):
        raise ValueError('Use an isolated runtime under .qpool')
    if not (root / 'runtime.json').is_file():
        raise ValueError('Start the isolated QA runtime first')
    return root


def prepare(root, source_home=None):
    from qp.native import atomic
    import tomlkit
    root = runtime_root(root)
    source = Path(source_home or Path.home() / '.codex').resolve()
    home = root / 'desktop-home'
    user_data = root / 'desktop-user-data'
    if home.exists() or user_data.exists():
        raise ValueError('Separate desktop profile already exists; keep its evidence, use a new runtime')
    config = tomlkit.parse((root / 'codex-home/config.toml').read_text())
    port = json.loads((root / 'runtime.json').read_text())['port']
    if (config.get('openai_base_url') != f'http://127.0.0.1:{port}/legacy/v1'
            or config.get('model_provider') != 'openai'
            or config.get('model_providers')):
        raise ValueError('Restart the QA runtime with the corrected desktop transport first')
    with sqlite3.connect((source / 'state_5.sqlite').as_uri() + '?mode=ro', uri=True) as db:
        row = db.execute('SELECT rollout_path FROM threads WHERE id=?', (TEMP_CHAT,)).fetchone()
    if row is None:
        raise ValueError('Authorized temporary chat was not found')
    history = Path(row[0])
    if history.is_symlink() or not history.resolve().is_relative_to(source / 'sessions'):
        raise ValueError('Temporary chat history must be in the source sessions directory')
    history = history.resolve()
    auth = json.loads((source / 'auth.json').read_text())
    tokens = auth.get('tokens', {})
    if not tokens.get('access_token') or not tokens.get('account_id'):
        raise ValueError('Source must be signed in with ChatGPT')
    scoped = {'auth_mode': 'chatgpt', 'tokens': {
        k: tokens[k] for k in ('access_token', 'account_id', 'id_token') if k in tokens}}
    scoped['tokens']['refresh_token'] = ''
    if auth.get('last_refresh'):
        scoped['last_refresh'] = auth['last_refresh']
    copied = home / history.relative_to(source)
    state = {'created': time.time(), 'thread': TEMP_CHAT,
             'session': hashlib.sha256(TEMP_CHAT.encode()).hexdigest()[:16],
             'main_config': str(source / 'config.toml'), 'main_config_sha256': sha(source / 'config.toml'),
             'source_history': str(history), 'source_history_sha256': sha(history),
             'home': str(home), 'user_data': str(user_data)}
    # No copied login or history is written until all inputs have been checked.
    home.mkdir(mode=0o700)
    user_data.mkdir(mode=0o700)
    try:
        atomic(home / 'config.toml', tomlkit.dumps(config))
        atomic(copied, history.read_text())
        atomic(home / 'auth.json', scoped)
        atomic(root / 'desktop-qa.json', state)
    except BaseException:
        (home / 'auth.json').unlink(missing_ok=True)
        raise
    # Installed desktop 26.1029.61003 uses these environment variables for
    # separate user data/home; not an assertion of a stable public GUI API.
    command = ['/usr/bin/open', '-n', '--env', 'CODEX_HOME=' + str(home),
               '--env', 'CODEX_ELECTRON_USER_DATA_PATH=' + str(user_data),
               '/Applications/ChatGPT.app', '--args', '--user-data-dir=' + str(user_data)]
    return {'home': str(home), 'launch_command': shlex.join(command),
            'full_gui_regression': 'not_run',
            'notice': 'Fresh desktop profile; no browser authorization or plugins copied'}


def check(root):
    root = runtime_root(root)
    state = json.loads((root / 'desktop-qa.json').read_text())
    unchanged = (sha(Path(state['main_config'])) == state['main_config_sha256']
                 and sha(Path(state['source_history'])) == state['source_history_sha256'])
    db_path = root / 'native/sessions.db'
    with sqlite3.connect(db_path.as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        rows = [dict(row) for row in db.execute(
            'SELECT id,account,model,status,started FROM requests WHERE session=? AND started>=? ORDER BY started',
            (state['session'], state['created']))]
    return {'originals_unchanged': unchanged, 'gateway_receipts': rows,
            'gateway_observed': bool(rows),
            # Engine receipts cannot attest GUI control or browser success.
            'full_gui_regression': 'requires_desktop_browser_observation'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('action', choices=('prepare', 'check', 'cleanup-credentials'))
    args = parser.parse_args()
    root = runtime_root(args.root)
    if args.action == 'prepare':
        result = prepare(root)
    elif args.action == 'check':
        result = check(root)
    else:
        (root / 'desktop-home/auth.json').unlink(missing_ok=True)
        result = {'temporary_login_removed': True}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.action == 'check' and (not result['originals_unchanged'] or not result['gateway_observed']):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
