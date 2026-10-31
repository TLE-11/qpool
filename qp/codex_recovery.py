"""Restore managed chats via Codex's protocol before removing their provider."""
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time

from .collectors.base import CollectorError


def recover(home, provider, model, backup_root, original_provider='openai'):
    home = Path(home)
    if not list(home.glob('state_*.sqlite')):
        return []
    binary = '/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex'
    if not Path(binary).is_file():
        raise CollectorError('未找到 Codex 会话恢复引擎，保留接入配置')
    process = subprocess.Popen([binary, '--no-daemon', 'app-server', '--listen', 'stdio://'],
        env=dict(os.environ, CODEX_HOME=str(home)), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True)
    events = queue.Queue()
    def read():
        for line in process.stdout:
            try:
                events.put(json.loads(line))
            except ValueError:
                pass
    threading.Thread(target=read, daemon=True).start()
    def call(ident, method, params):
        process.stdin.write(json.dumps({'id': ident, 'method': method, 'params': params}) + '\n')
        process.stdin.flush()
        until = time.monotonic() + 45
        while True:
            item = events.get(timeout=max(.01, until - time.monotonic()))
            if item.get('id') == ident:
                if 'error' in item:
                    raise CollectorError('Codex 未能恢复聊天；保留提供方配置和历史备份')
                return item['result']
    try:
        call(1, 'initialize', {'clientInfo': {'name': 'qpool_recovery', 'version': '1.0'},
                              'capabilities': {'experimentalApi': True}})
        process.stdin.write('{"method":"initialized","params":{}}\n');process.stdin.flush()
        sequence = iter(range(2, 100000))
        def list_managed():
            records = []
            for archived in (False, True):
                cursor = None
                while True:
                    page = call(next(sequence), 'thread/list', {'modelProviders': [provider],
                        'sourceKinds': ['cli', 'vscode', 'exec', 'appServer', 'subAgent', 'subAgentReview',
                                        'subAgentCompact', 'subAgentThreadSpawn', 'subAgentOther', 'unknown'],
                        'archived': archived, 'cursor': cursor, 'limit': 100, 'useStateDbOnly': True})
                    records.extend(page['data'])
                    cursor = page.get('nextCursor')
                    if not cursor:
                        break
            return records
        records = list_managed()
        if not records:
            return []
        if not model or model.startswith('qpool'):
            raise CollectorError('原配置未提供可恢复的官方模型，保留接入配置')
        if original_provider != 'openai':
            raise CollectorError('原提供方不是官方 OpenAI，保留配置以便人工恢复聊天')
        from .native import atomic
        backup_root = Path(backup_root) / ('chat-recovery-' + str(time.time_ns()))
        backup_root.mkdir(parents=True, mode=0o700)
        atomic(backup_root / 'before.json', records)
        for row in records:
            source = Path(row['path'])
            if not source.resolve().is_relative_to(home.resolve()):
                raise CollectorError('聊天历史路径不在 Codex 目录内，未继续恢复')
            destination = backup_root / (row['id'] + '.jsonl')
            shutil.copyfile(source, destination)
            destination.chmod(0o600)
        for row in records:
            result = call(next(sequence), 'thread/resume', {'threadId': row['id'], 'modelProvider': 'openai',
                                                 'model': model, 'excludeTurns': True})
            if result['thread']['id'] != row['id'] or result['modelProvider'] != 'openai':
                raise CollectorError('聊天恢复结果不匹配，保留提供方配置')
        if list_managed():
            raise CollectorError('仍有聊天依赖 qpool，保留提供方配置，请重试恢复')
    except (OSError, queue.Empty, KeyError) as exc:
        raise CollectorError('聊天恢复失败；保留提供方配置和历史备份') from exc
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill();process.wait()
        process.stdin.close()
        process.stdout.close()
    return [row['id'] for row in records]
