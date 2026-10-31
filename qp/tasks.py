"""Local task history and workspace exclusion; separate from the quota ledger."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import tempfile
import time
import uuid

from . import executors, ledger, profiles, routing
from .db import Database, default_db_path


@contextmanager
def workspace_lock(cwd, db_path=None):
    directory = Path(cwd or Path.cwd()).expanduser().resolve()
    if not directory.is_dir():
        raise ledger.LedgerError("工作目录不存在")
    # Shared across qpool instances even when they use different ledger paths.
    root = Path(tempfile.gettempdir()) / ("qpool-workspaces-" + str(os.getuid()))
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink() or root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077:
        raise ledger.LedgerError("工作目录锁目录权限不安全")
    name = hashlib.sha256(str(directory).encode()).hexdigest() + ".lock"
    with (root / name).open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ledger.LedgerError("此目录已有任务运行，请等待完成后再启动")
        try:
            yield str(directory)
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def config_revision():
    path = profiles.config_path()
    return hashlib.sha256(path.read_bytes() if path.exists() else b"").hexdigest()


class TaskManager:
    def __init__(self, db_path=None):
        self.db_path = db_path or default_db_path()
        self.path = self.db_path.with_name(self.db_path.name + ".tasks.db")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._service_lock = self.path.with_suffix(".lock").open("a")
        try:
            fcntl.flock(self._service_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._service_lock.close()
            raise ledger.LedgerError("此数据库已有 qpool 控制台运行")
        self.lock = threading.RLock()
        self.previews = {}
        self.threads = []
        self.active = 0
        with self.connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, task TEXT NOT NULL, cwd TEXT NOT NULL,
                status TEXT NOT NULL, created_at TEXT NOT NULL, finished_at TEXT,
                plan TEXT NOT NULL, outcome TEXT)""")
            conn.execute("UPDATE tasks SET status='interrupted', finished_at=?, outcome=? WHERE status='running'",
                         (ledger.iso(ledger.now_utc()), json.dumps({"error": "服务中断；请检查工作区及残留进程后再提交任务"})))
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(str(self.path), timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def preview(self, data):
        if data.get("cwd") is not None and not isinstance(data["cwd"], str):
            raise ledger.LedgerError("工作目录必须是路径文本")
        cwd = str(Path(data.get("cwd") or Path.cwd()).expanduser().resolve())
        if not Path(cwd).is_dir():
            raise ledger.LedgerError("工作目录不存在")
        timeout = data.get("timeout", 900)
        if type(timeout) is not int or not 1 <= timeout <= 3600:
            raise ledger.LedgerError("执行超时必须是 1 至 3600 秒的整数")
        revision = config_revision()
        db = Database(self.db_path)
        try:
            plan = routing.plan_task(db, data.get("task"), mode=data.get("mode", "auto"),
                                     amount=data.get("amount", 50000), min_tier=data.get("min_tier", 1),
                                     max_cost=data.get("max_cost"), strategy="cost")
        finally:
            db.close()
        if revision != config_revision():
            raise ledger.LedgerError("配置在预览期间发生变化，请重新预览")
        pid = uuid.uuid4().hex
        with self.lock:
            now = time.monotonic()
            self.previews = {k: v for k, v in self.previews.items() if now - v["at"] < 600}
            if len(self.previews) >= 100:
                self.previews.pop(next(iter(self.previews)))
            self.previews[pid] = dict(plan=plan, task=data["task"], cwd=cwd, timeout=timeout,
                                      revision=revision, at=now, task_id=None)
        return dict(id=pid, cwd=cwd, plan=routing.public_plan(plan), expires_in_seconds=600)

    def start(self, pid):
        with self.lock:
            preview = self.previews.get(pid)
            if not preview:
                raise ledger.LedgerError("预览不存在或已过期，请重新预览")
            if preview["task_id"]:
                return {"id": preview["task_id"]}  # repeated clicks return the same run
            if time.monotonic() - preview["at"] >= 600:
                raise ledger.LedgerError("预览已过期，请重新预览")
            if preview["revision"] != config_revision():
                raise ledger.LedgerError("配置已变化，请重新预览")
            if not preview["plan"]["candidates"]:
                raise ledger.LedgerError("没有符合要求的执行渠道")
            if self.active >= 4:
                raise ledger.LedgerError("已有 4 个任务运行，请等待完成")
            guard = workspace_lock(preview["cwd"], self.db_path)
            guard.__enter__()
            tid = uuid.uuid4().hex
            try:
                with self.connect() as conn:
                    conn.execute("INSERT INTO tasks VALUES (?, ?, ?, 'running', ?, NULL, ?, NULL)",
                                 (tid, preview["task"], preview["cwd"], ledger.iso(ledger.now_utc()),
                                  json.dumps(routing.public_plan(preview["plan"]), ensure_ascii=False)))
                preview["task_id"] = tid
                self.active += 1
                thread = threading.Thread(target=self._run, args=(tid, preview, guard))
                self.threads = [t for t in self.threads if t.is_alive()]
                self.threads.append(thread)
                thread.start()
            except Exception:
                guard.__exit__(None, None, None)
                raise
        return {"id": tid}

    def _run(self, tid, preview, guard):
        db = None
        outcome, status = {"attempts": [], "error": "没有可执行候选"}, "failed"
        try:
            db = Database(self.db_path)
            if preview["revision"] != config_revision():
                raise ledger.LedgerError("配置已变化，请重新预览")
            for candidate in preview["plan"]["candidates"]:
                result = executors.execute(candidate, preview["task"], db=db,
                                           cwd=preview["cwd"], timeout=preview["timeout"])
                outcome["attempts"].append({"channel_id": candidate["channel_id"], "model": candidate["model"],
                                            "account_id": candidate["account_id"], "pool_id": candidate["pool_id"],
                                            "success": result.success, "error": result.error,
                                            "duration_s": result.duration_s})
                outcome.update(output=result.output[-262144:], error=result.error)
                if result.success:
                    status = "succeeded"
                    break
                if not result.retry_safe:
                    break
        except Exception as exc:
            outcome["error"] = str(exc) if isinstance(exc, ledger.LedgerError) else "执行失败，请检查配置和工作目录"
        finally:
            if db:
                db.close()
            try:
                with self.connect() as conn:
                    conn.execute("UPDATE tasks SET status=?, finished_at=?, outcome=? WHERE id=?",
                                 (status, ledger.iso(ledger.now_utc()), json.dumps(outcome, ensure_ascii=False), tid))
            finally:
                guard.__exit__(None, None, None)
                with self.lock:
                    self.active -= 1

    def list(self):
        with self.connect() as conn:
            rows = conn.execute("SELECT id, substr(task,1,160) AS task, cwd, status, created_at, finished_at FROM tasks ORDER BY created_at DESC LIMIT 100").fetchall()
        return [dict(r) for r in rows]

    def get(self, tid):
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        if not row:
            raise ledger.LedgerError("任务不存在")
        result = dict(row)
        result["plan"] = json.loads(result["plan"])
        result["outcome"] = json.loads(result["outcome"]) if result["outcome"] else None
        return result

    def close(self):
        for thread in self.threads:
            thread.join()
        self._service_lock.close()
