"""Loopback-only console. API keys are write-only; snapshots contain no secrets."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from . import ledger, pools, profiles, routing, desktop
from .native import Native
from .collectors.base import CollectorError
from .db import Database, default_db_path
from .tasks import TaskManager, config_revision

STATIC = Path(__file__).parent / "static"


class Console:
    def __init__(self, db_path=None):
        self.db_path = db_path or default_db_path()
        self.manager = TaskManager(self.db_path)
        self.config_lock = threading.Lock()
        self.sync_lock = threading.Lock()
        try:
            self.native = Native(self.db_path, self.config_lock)
        except Exception:
            self.manager.close()
            raise

    def snapshot(self):
        with self.config_lock:
            config = profiles.load()
            revision = config_revision()
        db = Database(self.db_path)
        try:
            entries = [dict(r) for r in ledger.list_entries(db)]
            accounts = [{"id": a["id"], "label": a.get("label", a["id"]), "agent": a["agent"],
                         "model_inventory": a.get('model_inventory'), "paid_enabled": a.get('paid_enabled', False)}
                        for a in config["accounts"].values()]
            result = []
            for pool in config["pools"].values():
                channels = []
                for channel in config["channels"]:
                    if channel["pool_id"] != pool["id"]:
                        continue
                    from .executors import channel_status
                    ready, why = channel_status(channel, pool, config)
                    if channel.get("type") == "native":
                        ready, why = True, "供原 Codex 入口使用"
                    channels.append({"id": channel["id"], "model": channel["model"], "available": channel.get('available', True),
                                     "group": channel.get("group"), "capability_tier": channel.get("capability_tier"),
                                     "executable": ready, "execution_reason": why,
                                     **pools.evaluate(db, pool, channel["model"])})
                windows = [{k: r[k] for k in ("window_key", "scope", "role", "used_percent", "remaining",
                            "total", "unit", "last_synced_at", "resets_at", "source_kind")}
                           for r in entries if r["pool_id"] == pool["id"]]
                result.append({"id": pool["id"], "account_id": pool["account_id"], "kind": pool["kind"],
                               "channels": channels, "windows": windows})
            native_models = self.native.model_metadata()
            settings = {"routing": config["routing"], "revision": revision,
                        "channels": [{"id": c["id"], "model": c["model"], "account_id": c["account_id"], "type": c.get("type"), "native_supported": c["model"] in native_models and c.get('available', True),
                                      "group": c.get("group", ""), "capability_tier": c.get("capability_tier", config["pools"][c["pool_id"]]["capability_tier"])}
                                     for c in config["channels"]]}
            return {"accounts": accounts, "pools": result, "settings": settings,
                    "cwd": str(Path.cwd()), "config_path": str(profiles.config_path()),
                    "tasks": self.manager.list(), "native": self.native.status()}
        finally:
            db.close()

    def save_settings(self, data):
        with self.config_lock:
            if data.get("revision") != config_revision():
                raise ledger.LedgerError("配置已被其他操作修改，请刷新后重新保存")
            path = profiles.config_path()
            if path.is_symlink():
                raise ledger.LedgerError("配置是符号链接，请在本地编辑目标文件")
            raw = json.loads(path.read_text()) if path.exists() else {"accounts": [], "channels": []}
            raw["routing"] = routing.validate_settings(data.get("routing", {}))
            updates = data.get("channels")
            if not isinstance(updates, list):
                raise ledger.LedgerError("模型配置必须为列表")
            by_id = {c["id"]: c for c in raw.get("channels", [])}
            seen = set()
            for update in updates:
                if not isinstance(update, dict) or set(update) - {"id", "group", "capability_tier"}:
                    raise ledger.LedgerError("无效模型配置")
                cid = update.get("id")
                if cid not in by_id or cid in seen:
                    raise ledger.LedgerError("未知或重复的执行渠道")
                seen.add(cid)
                channel = by_id[cid]
                if update.get("group"):
                    channel["group"] = update["group"]
                else:
                    channel.pop("group", None)
                channel["capability_tier"] = update.get("capability_tier")
            profiles.load(raw)  # validate the entire merged config before replacement
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd, name = tempfile.mkstemp(prefix=".qpool-config-", dir=path.parent)
            try:
                with os.fdopen(fd, "w") as handle:
                    json.dump(raw, handle, ensure_ascii=False, indent=2)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                # Do not overwrite edits that occurred while preparing the new file.
                if data["revision"] != config_revision():
                    raise ledger.LedgerError("配置已变化，请刷新后重新保存")
                os.replace(name, path)
            finally:
                if os.path.exists(name):
                    os.unlink(name)
        return {"saved": True}

    def sync(self):
        if not self.sync_lock.acquire(blocking=False):
            raise ledger.LedgerError("额度同步正在进行，请稍候")
        db = None
        try:
            db = Database(self.db_path)
            result = pools.sync_accounts(db, apply=True)
            return {"synced": result["synced"], "errors": result["errors"]}
        finally:
            if db:
                db.close()
            self.sync_lock.release()


class Server(ThreadingHTTPServer):
    daemon_threads = False
    block_on_close = True

    def __init__(self, address, console):
        if address[0] != "127.0.0.1":
            raise ValueError("qpool console only binds to 127.0.0.1")
        self.console = console
        super().__init__(address, Handler)
        console.native.port = self.server_address[1]
        desktop.resume(console.native)


class Handler(BaseHTTPRequestHandler):
    server_version = "qpool"

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, *args):
        pass  # never log request bodies, prompts or endpoint authentication

    def respond(self, status, value, content_type="application/json; charset=utf-8"):
        body = json.dumps(value, ensure_ascii=False, allow_nan=False).encode() if isinstance(value, (dict, list)) else value
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(body)

    def allowed(self, api=False):
        port = self.server.server_address[1]
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        host = self.headers.get("Host")
        origin = self.headers.get("Origin")
        return (host in hosts and (origin is None or origin == "http://" + host)
                and self.headers.get("Sec-Fetch-Site") != "cross-site"
                and (not api or self.headers.get("X-Qpool-Request") == "1"))

    def do_GET(self):
        path = urlsplit(self.path).path
        if path.startswith('/legacy/v1/'):
            # Tell the built-in client to fall back to its supported HTTP/SSE transport.
            self.protocol_version = 'HTTP/1.1'
            self.respond(426, {'error': 'Use HTTP Responses streaming'})
            self.close_connection = True
            return
        if not self.allowed(path.startswith("/api/")):
            self.respond(403, {"error": "仅允许本机同源控制台请求"})
            return
        try:
            if path == "/api/state":
                self.respond(200, self.server.console.snapshot())
            elif path == "/api/desktop/health":
                native = self.server.console.native
                self.respond(200, {'runtime': desktop.runtime_id(native.root), 'active': bool(native.active and native.grant)})
            elif path == "/api/tasks":
                self.respond(200, self.server.console.manager.list())
            elif path.startswith("/api/tasks/"):
                self.respond(200, self.server.console.manager.get(path.rsplit("/", 1)[1]))
            elif path in ("/", "/app.js", "/style.css"):
                file = "index.html" if path == "/" else path[1:]
                mime = {"index.html": "text/html", "app.js": "application/javascript", "style.css": "text/css"}[file]
                self.respond(200, (STATIC / file).read_bytes(), mime + "; charset=utf-8")
            else:
                self.respond(404, {"error": "页面不存在"})
        except (ledger.LedgerError, CollectorError, ValueError) as exc:
            self.respond(400, {"error": str(exc)})
        except Exception:
            self.respond(500, {"error": "无法读取本地数据，请检查配置和文件权限"})

    def do_POST(self):
        if self.path.startswith(("/v1/", "/legacy/v1/")):
            from .gateway import handle
            handle(self, self.path)
            return
        if not self.allowed(True):
            self.respond(403, {"error": "仅允许本机同源控制台请求"})
            return
        try:
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                raise ledger.LedgerError("需要 application/json 请求")
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 131072 or self.headers.get("Transfer-Encoding"):
                raise ledger.LedgerError("请求过大或格式不受支持")
            data = routing.strict_json(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ledger.LedgerError("请求必须为 JSON 对象")
            app = self.server.console
            path = self.path
            if path == "/api/accounts/discover":
                result = app.native.discover()
            elif path == "/api/accounts/connect":
                result = app.native.connect(data)
            elif path == "/api/accounts/login":
                result = app.native.start_login()
            elif path == "/api/accounts/api-connect":
                result = app.native.connect_api(data)
            elif path == "/api/models/sync":
                result = app.native.sync_models(data.get('account_id'))
            elif path == "/api/routes/save":
                result = app.native.save_route(data)
            elif path == "/api/routes/apply":
                result = app.native.save_route(dict(data, apply_to_active=True))
            elif path == "/api/agent/enable":
                result = app.native.enable(data.get("route_id"))
            elif path == "/api/sessions/migrate":
                result = app.native.schedule_migration(data)
            elif path == "/api/agent/launch":
                result = app.native.launch(data)
            elif path == "/api/agent/disable":
                result = app.native.disable()
            elif path == "/api/desktop/connect":
                with app.config_lock:
                    result = desktop.connect(app.native)
            elif path == "/api/desktop/restore":
                with app.config_lock:
                    result = desktop.restore(app.native)
            elif path == "/api/preview":
                result = app.manager.preview(data)
            elif path == "/api/run":
                if not isinstance(data.get("preview_id"), str):
                    raise ledger.LedgerError("缺少预览 ID")
                result = app.manager.start(data["preview_id"])
            elif path == "/api/settings":
                result = app.save_settings(data)
            elif path == "/api/decision/model-test":
                result = app.native.test_decision(data)
            elif path == "/api/decision/test":
                result = routing.decide("将这段说明整理为三条要点。", routing.validate_settings(data))
            elif path == "/api/sync":
                result = app.sync()
            else:
                self.respond(404, {"error": "接口不存在"})
                return
            self.respond(200, result)
        except (ledger.LedgerError, CollectorError, ValueError, TypeError, KeyError):
            # Validation messages are useful; config/JSON parsing can embed raw data.
            import sys
            exc = sys.exc_info()[1]
            message = str(exc) if isinstance(exc, (ledger.LedgerError, CollectorError)) else "参数格式不正确，请检查输入"
            self.respond(400, {"error": message})
        except Exception:
            self.respond(500, {"error": "操作未完成，请检查本地配置和文件权限"})


def serve(port=8765):
    console = Console()
    try:
        server = Server(("127.0.0.1", port), console)
    except Exception:
        console.native.close()
        console.manager.close()
        raise
    print(f"qpool 控制台：http://127.0.0.1:{server.server_address[1]}", flush=True)
    console.native.start_model_sync()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在关闭控制台；已启动的任务会等待完成。", flush=True)
    finally:
        server.server_close()
        console.native.close()
        console.manager.close()
