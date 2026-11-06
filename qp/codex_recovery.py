"""Restore managed chats via Codex's protocol before removing their provider."""
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time
import hashlib

from .collectors.base import CollectorError

BINARY = '/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex'


class RecoveryResult(list):
    """Restored thread IDs, including IDs that require a compatibility provider."""
    def __init__(self, ids=(), compatibility_ids=()):
        super().__init__(ids)
        self.compatibility_ids = list(ids) if compatibility_ids is True else list(compatibility_ids or ())
        self.compatibility_provider = bool(self.compatibility_ids)


class ProviderMismatch(CollectorError):
    pass


def recover(home, provider, model, backup_root, original_provider='openai', *, managed_sessions=None,
            require_owned_aliases=False):
    """Optionally restore only known receipt sessions still using a qpool alias."""
    if managed_sessions is not None and not managed_sessions and not require_owned_aliases:
        return []
    home = Path(home)
    if not list(home.glob('state_*.sqlite')):
        return []
    binary = BINARY
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
                    if item['error'].get('data', {}).get('type') == 'modelProviderMismatch':
                        raise ProviderMismatch('当前 Codex 要求保留聊天原提供方 ID')
                    if 'active writer' in item['error'].get('message', ''):
                        raise CollectorError('旧聊天仍被占用，请结束回复并关闭占用后重试；配置与备份已保留')
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
                    for row in page['data']:
                        if managed_sessions is not None:
                            sid = hashlib.sha256(row['id'].encode()).hexdigest()[:16]
                            if (require_owned_aliases and sid not in managed_sessions
                                    and (row.get('model') or '').startswith('qpool')):
                                raise CollectorError('存在无法确认恢复模型的 qpool 聊天，保留接入配置与备份；请先为该聊天选择真实模型')
                            if sid not in managed_sessions or not (row.get('model') or '').startswith('qpool'):
                                continue
                        records.append(dict(row, _qpool_archived=archived))
                    cursor = page.get('nextCursor')
                    if not cursor:
                        break
            return records
        records = list_managed()
        if not records:
            return []
        if managed_sessions is None and (not model or model.startswith('qpool')):
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
        compatibility_ids = []
        for row in records:
            previous_model = row.get('model')
            if managed_sessions is not None:
                sid = hashlib.sha256(row['id'].encode()).hexdigest()[:16]
                target_model = managed_sessions[sid]
                if not isinstance(target_model, str) or not target_model or target_model.startswith('qpool'):
                    raise CollectorError('已知聊天缺少可恢复的真实模型，保留接入配置')
            else:
                target_model = previous_model if previous_model and not previous_model.startswith('qpool') else model
            unarchived = False
            try:
                if row['_qpool_archived']:
                    call(next(sequence), 'thread/unarchive', {'threadId': row['id']})
                    unarchived = True
                target_provider = 'openai'
                try:
                    result = call(next(sequence), 'thread/resume', {'threadId': row['id'],
                        'modelProvider': target_provider, 'model': target_model, 'excludeTurns': True})
                except ProviderMismatch:
                    target_provider = provider
                    result = call(next(sequence), 'thread/resume', {'threadId': row['id'],
                        'modelProvider': target_provider, 'model': target_model, 'excludeTurns': True})
                    compatibility_ids.append(row['id'])
                if (result['thread']['id'] != row['id'] or result['modelProvider'] != target_provider
                        or result['model'] != target_model):
                    raise CollectorError('聊天恢复结果不匹配，保留提供方配置')
            finally:
                if unarchived:
                    call(next(sequence), 'thread/archive', {'threadId': row['id']})
        if {row['id'] for row in list_managed()} - set(compatibility_ids):
            raise CollectorError('仍有聊天依赖 qpool，保留提供方配置，请重试恢复')
    except (OSError, queue.Empty, KeyError, CollectorError) as exc:
        detail = str(exc) + '；' if isinstance(exc, CollectorError) else ''
        raise CollectorError(detail + '聊天恢复未完成，配置和历史备份已保留；部分聊天可能已恢复模型，'
                             '请重试恢复后再继续旧聊天') from exc
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill();process.wait()
        process.stdin.close()
        process.stdout.close()
    return RecoveryResult([row['id'] for row in records], compatibility_ids)
