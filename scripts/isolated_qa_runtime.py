"""Opt-in live QA runtime. Never connects the user's desktop or installs startup."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import threading
import time

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def configure_home(native, home, port):
    """Use the desktop's OAuth-preserving transport in this separate user home.

    Project config cannot set provider/endpoint keys. No environment-only API
    key is required by the GUI, and the original openai provider remains usable
    by copied old chats without changing their provider identity.
    """
    from qp.native import atomic
    import tomlkit
    managed = {'model': 'qpool-auto', 'model_provider': 'openai',
               'openai_base_url': f'http://127.0.0.1:{port}/legacy/v1',
               'model_catalog_json': str(native.root / 'catalog.json')}
    doc = tomlkit.document()
    for name, value in managed.items():
        doc[name] = value
    atomic(home / 'config.toml', tomlkit.dumps(doc))
    atomic(native.root / 'desktop.json', {
        'legacy_enabled': True, 'config_path': str(home / 'config.toml'),
        'backup_path': str(home / 'config.before-qa.toml'), 'managed': managed,
        'transport': 'builtin-openai', 'provider_id': 'openai', 'provider': {}})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--live', action='store_true', required=True)
    parser.add_argument('--trace-shapes', action='store_true',
                        help='Print only synthetic request model/type/role metadata, never message text')
    parser.add_argument('--fault-before-receipt', action='store_true',
                        help='QA only: honor a private session-specific adapter failure marker')
    args = parser.parse_args()
    root = args.root.resolve()
    if not root.is_relative_to(REPO / '.qpool'):
        raise SystemExit('QA runtime must be under the ignored .qpool directory')
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    from qp.native import atomic
    if not (root/'pools.json').exists():
        original = json.loads((REPO/'.qpool/console/pools.json').read_text())
        # Keep immutable account and credential references, never rewrite logins.
        atomic(root/'pools.json', original)
    home = root/'codex-home'
    home.mkdir(exist_ok=True,mode=0o700)
    native_home = Path('/Users/liwenqiang/.codex')
    if (native_home/'models_cache.json').exists():
        atomic(home/'models_cache.json', (native_home/'models_cache.json').read_text())
    os.environ.update(QPOOL_CONFIG=str(root/'pools.json'),QPOOL_DB=str(root/'ledger.db'),CODEX_HOME=str(home))
    from qp import web, desktop
    console = web.Console(root/'ledger.db')
    native = console.native
    if args.trace_shapes:
        select = native.select
        def traced(body, headers, *positional, **kwargs):
            shape = {'model': body.get('model'), 'legacy': bool(kwargs.get('legacy_account')),
                     'items': [{'type': i.get('type'), 'role': i.get('role'),
                                'content_types': [c.get('type') for c in i.get('content', []) if isinstance(c, dict)]
                                if isinstance(i.get('content'), list) else type(i.get('content')).__name__}
                               for i in body.get('input', []) if isinstance(i, dict)]}
            print(json.dumps({'request_shape': shape}), flush=True)
            return select(body, headers, *positional, **kwargs)
        native.select = traced
    previous = json.loads((root/'runtime.json').read_text()) if (root/'runtime.json').exists() else {}
    handler = web.Handler
    if args.fault_before_receipt:
        from qp.collectors.base import CollectorError
        context = threading.local()
        adapter = native.adapter
        def faulting_adapter(account, **kwargs):
            sid = getattr(context, 'session', None)
            marker = root / 'fault-before-receipt.json'
            if sid and marker.exists() and json.loads(marker.read_text()).get('session') == sid:
                with native.receipts() as conn:
                    bound = conn.execute('SELECT 1 FROM legacy_bindings WHERE session=? AND binding IS NOT NULL', (sid,)).fetchone()
                    receipt = conn.execute('SELECT 1 FROM requests WHERE session=?', (sid,)).fetchone()
                if bound and not receipt:
                    events = root / 'fault-events.json'
                    rows = json.loads(events.read_text()) if events.exists() else []
                    rows.append({'session': sid, 'at': time.time(), 'stage': 'after-binding-before-receipt'})
                    atomic(events, rows)
                    raise CollectorError('QA injected adapter failure before first receipt')
            return adapter(account, **kwargs)
        native.adapter = faulting_adapter
        class FailureHandler(web.Handler):
            def do_POST(self):
                raw = self.headers.get('session_id') or self.headers.get('thread-id') or self.headers.get('x-codex-session-id')
                context.session = hashlib.sha256(raw.encode()).hexdigest()[:16] if raw else None
                try:
                    return super().do_POST()
                finally:
                    context.session = None
        handler = FailureHandler
    server = web.Server(('127.0.0.1', previous.get('port',0)),console)
    server.RequestHandlerClass = handler
    port = server.server_port
    if not native.active:
        routes = native.data()['routes']
        if not routes:
            plus,pro='codex-4a67781596ca','codex-dc804ba89e83'
            route=native.save_route({'name':'隔离回归 · 开发与测试分工','mode':'model','allow_api':False,
                'regular':[plus+'-gpt-6.1-sol',pro+'-gpt-6.1-sol'],
                'primary':[plus+'-gpt-6-astra',pro+'-gpt-6-astra'],
                'decision':{'provider':'account','channel_id':plus+'-gpt-5.6-luna',
                            'simple_threshold':.8,'timeout_seconds':20,'task_policy':'development_primary'}})
        else: route=routes[0]
        native.enable(route['id'])
    # This is the isolated test home only; no LaunchAgent or desktop.connect().
    configure_home(native, home, port)
    desktop.persist_route(native)
    atomic(root/'runtime.json',{'port':port,'pid':os.getpid(),'root':str(root),
                              'main_config_sha256':hashlib.sha256((native_home/'config.toml').read_bytes()).hexdigest()})
    print(json.dumps({'port':port,'root':str(root),'pid':os.getpid()}),flush=True)
    def stop(*_):raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM,stop)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:
        server.server_close();native.close();console.manager.close()


if __name__=='__main__':main()
