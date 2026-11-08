"""Private durable follow-ups; never handed to Codex's automatic queue worker."""
import json
import os
from pathlib import Path
import sqlite3
import uuid
from contextlib import contextmanager

from .collectors.base import CollectorError


class StoredQueue:
    def __init__(self, root, subject):
        self.path = Path(root) / 'control-inputs.db'
        self.subject = subject
        fd = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        with self.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS inputs
                (id TEXT PRIMARY KEY, thread TEXT NOT NULL, subject TEXT NOT NULL,
                 client_id TEXT NOT NULL, input TEXT NOT NULL, position INTEGER NOT NULL,
                 state TEXT NOT NULL, result TEXT,
                 UNIQUE(thread, subject, client_id))''')

    @contextmanager
    def connect(self):
        db = None
        try:
            db = sqlite3.connect(self.path, timeout=30)
            db.row_factory = sqlite3.Row
            with db:
                yield db
        except sqlite3.Error:
            raise CollectorError('排队持久记录不可用；未自动重发，请检查本地状态') from None
        finally:
            if db:
                db.close()

    @staticmethod
    def item(row):
        return {'id': row['id'], 'clientUserMessageId': row['client_id'], 'input': json.loads(row['input'])}

    def operate(self, action, data):
        thread = data['threadId']
        if not isinstance(thread, str) or not thread or len(thread) > 512:
            raise CollectorError('排队聊天标识无效')
        if action in ('update', 'delete') and (not isinstance(data.get('queuedSubmissionId'), str) or not data['queuedSubmissionId']):
            raise CollectorError('缺少排队消息标识，未修改任何消息')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            foreign = db.execute('SELECT 1 FROM inputs WHERE thread=? AND subject<>? LIMIT 1',
                                 (thread, self.subject)).fetchone()
            if foreign:
                raise CollectorError('排队消息的原生登录已变化；未读取或提交旧账号输入')
            rows = db.execute("SELECT * FROM inputs WHERE thread=? AND state NOT IN ('accepted','deleted') ORDER BY position,id",
                              (thread,)).fetchall()
            ident = data.get('queuedSubmissionId')
            row = db.execute('SELECT * FROM inputs WHERE thread=? AND id=?', (thread, ident)).fetchone() if ident else (rows[0] if rows else None)
            if action == 'list':
                cursor = data.get('cursor')
                if cursor:
                    indices = [i for i, r in enumerate(rows) if r['id'] == cursor]
                    if not indices:
                        raise CollectorError('排队分页已变化，请重新读取')
                    rows = rows[indices[0]+1:]
                limit = data.get('limit') or 100
                if not isinstance(limit, int) or not 0 < limit <= 1000:
                    raise CollectorError('排队分页数量无效')
                return {'data': [self.item(r) for r in rows[:limit]],
                        'nextCursor': rows[limit-1]['id'] if len(rows) > limit else None}
            if action in ('add', 'update'):
                inputs = data.get('input')
                if not isinstance(inputs, list) or not inputs or any(not isinstance(i, dict) or not isinstance(i.get('type'), str) for i in inputs):
                    raise CollectorError('排队输入不能为空且必须是有效输入列表')
            if action == 'add':
                client = data.get('clientUserMessageId')
                if not isinstance(client, str) or not 0 < len(client) <= 512:
                    raise CollectorError('排队消息标识无效')
                previous = db.execute('SELECT * FROM inputs WHERE thread=? AND client_id=?', (thread, client)).fetchone()
                if previous:
                    if json.loads(previous['input']) != inputs:
                        raise CollectorError('同一消息标识不能用于不同输入')
                    return {'queuedSubmission': self.item(previous)}
                ident = str(uuid.uuid4())
                position = db.execute('SELECT COALESCE(MAX(position),0)+1 FROM inputs WHERE thread=?', (thread,)).fetchone()[0]
                db.execute('INSERT INTO inputs VALUES (?,?,?,?,?,?,?,NULL)',
                           (ident, thread, self.subject, client, json.dumps(inputs), position, 'queued'))
                return {'queuedSubmission': {'id': ident, 'clientUserMessageId': client, 'input': inputs}}
            if action == 'reorder':
                ids = data.get('queuedSubmissionIds')
                if not isinstance(ids, list) or any(not isinstance(i, str) for i in ids) or len(ids) != len(rows) or set(ids) != {r['id'] for r in rows}:
                    raise CollectorError('排序必须包含全部待处理消息且不能重复')
                if any(r['state'] == 'dispatching' for r in rows):
                    raise CollectorError('存在提交结果未确认的消息，暂不能重排')
                for i, key in enumerate(ids):
                    db.execute('UPDATE inputs SET position=? WHERE id=?', (i, key))
                return {}
            if action == 'peek':
                return self.item(row) if row and row['state'] == 'queued' else None
            if action == 'lookup':
                return json.loads(row['result']) if row and row['state'] == 'accepted' else None
            if action == 'delete':
                exists = bool(row and row['state'] not in ('accepted', 'deleted'))
                if exists:
                    db.execute("UPDATE inputs SET state='deleted' WHERE id=?", (row['id'],))
                return {'deleted': exists}
            if not row:
                raise CollectorError('未找到排队消息')
            if action == 'claim':
                if row['state'] == 'accepted':
                    return {'result': json.loads(row['result'])}
                if any(r['state'] == 'dispatching' and r['id'] != row['id'] for r in rows):
                    raise CollectorError('前一条排队提交结果未确认，不能越序执行')
                if row['state'] not in ('queued', 'failed'):
                    raise CollectorError('排队提交结果未确认或已删除；请先检查聊天，未自动重发')
                db.execute("UPDATE inputs SET state='dispatching' WHERE id=?", (row['id'],))
                return {'item': self.item(row)}
            if action == 'accepted':
                if row['state'] != 'dispatching':
                    raise CollectorError('排队提交状态已变化')
                db.execute("UPDATE inputs SET state='accepted',result=? WHERE id=?",
                           (json.dumps(data['result']), row['id']))
                return {}
            if action == 'rejected':
                db.execute("UPDATE inputs SET state='failed' WHERE id=? AND state='dispatching'", (row['id'],))
                return {}
            if action == 'deferred':
                db.execute("UPDATE inputs SET state='queued' WHERE id=? AND state='dispatching'", (row['id'],))
                return {}
            if action == 'update':
                if row['state'] not in ('queued', 'failed'):
                    raise CollectorError('消息正在提交或已处理，不能修改')
                db.execute("UPDATE inputs SET input=?,state='queued' WHERE id=?", (json.dumps(inputs), row['id']))
                return {'queuedSubmission': dict(self.item(row), input=inputs)}
            raise CollectorError('排队操作无效')
