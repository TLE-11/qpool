"""Choose a turn before Codex builds its model-specific instructions and tools.

Plans are opaque, authenticated, durable capabilities, carried in the supported
Responses turn metadata. The HTTP bridge consumes the exact preselected binding.
"""
import copy
import json
import secrets
import sys
import time
from pathlib import Path

from . import desktop, desktop_switch
from .collectors.base import CollectorError
from .native import atomic, digest

METADATA_KEY = 'qpool_control_plan'


def validate_plan_route(native, grant, route, *, pinned=False):
    # In-place publication keeps the grant and route identity. A turn already
    # bound to its plan must finish tools/compaction on that immutable binding.
    # Unstarted plans still require the exact revision; revocation always wins.
    if (grant != digest(native.grant or '') or not native.grant or not native.active or
            (route != native.active and not (pinned and isinstance(route, dict) and
                                             route.get('id') == native.active.get('id')))):
        raise CollectorError('准备后路由已变化，请重新提问')


def previous_execution(native, sid, subject, model):
    """Snapshot before selection can replace the persisted execution binding."""
    with native.receipts() as db:
        pin = db.execute('SELECT * FROM desktop_turns WHERE session=?', (sid,)).fetchone()
        if pin and pin['subject'] != subject:
            raise CollectorError('此聊天的原生账号已变化')
        if pin:
            model = pin['model']
            saved = db.execute('SELECT * FROM control_previous WHERE session=? AND turn=? AND subject=?',
                               (sid, pin['turn'], subject)).fetchone()
            if saved:
                return json.loads(saved['detail'])
            for row in db.execute('SELECT detail FROM control_plans WHERE session=? AND subject=? ORDER BY created DESC', (sid, subject)):
                plan = json.loads(row['detail'])
                if plan.get('turn') == pin['turn'] and plan['model'] == model:
                    return {k: v for k, v in plan.items() if k != 'previous'}
        mode = pin['mode'] if pin else 'native'
        if not pin:
            saved = db.execute('SELECT * FROM control_previous WHERE session=? AND turn IS NULL AND subject=?',
                               (sid, subject)).fetchone()
            if saved:
                return json.loads(saved['detail'])
            last = db.execute('SELECT entry_mode FROM requests WHERE session=? ORDER BY started DESC LIMIT 1', (sid,)).fetchone()
            if last and last['entry_mode'] != 'native':
                mode = 'qpool'
        binding = None
        if mode == 'qpool':
            row = db.execute('SELECT binding FROM session_bindings WHERE session=?', (sid,)).fetchone()
            row = row or db.execute('SELECT binding FROM legacy_bindings WHERE session=?', (sid,)).fetchone()
            binding = json.loads(row['binding']) if row and row['binding'] else None
            if not binding:
                raise CollectorError('缺少上轮执行绑定，不能安全压缩旧历史')
            # Old HTTP-only routing could execute a compatible model different
            # from the model loaded by Codex. Preserve both; gateway validation
            # still checks that old contract before forwarding compaction.
        result = {'mode': mode, 'model': model, 'binding': binding, 'turn': pin['turn'] if pin else None,
                  'route': native.active, 'grant': digest(native.grant or '')}
        db.execute('INSERT OR REPLACE INTO control_previous VALUES (?,?,?,?)',
                   (sid, result['turn'], subject, json.dumps(result)))
        return result


def prepare(native, data, headers):
    subject = digest(desktop_switch.identity(native, headers))
    thread = data.get('threadId')
    if not isinstance(thread, str) or not 0 < len(thread) <= 512:
        raise CollectorError('缺少控制代理聊天标识')
    sid = digest(thread)[:16]
    with native.turn_lock(('control-prepare', sid)):
        state = desktop.read_state(native.root)
        if native.audit_error or not desktop.status(native)['connected']:
            raise CollectorError('桌面入口或请求记录不可用')
        with native.receipts() as db:
            pref = db.execute('SELECT * FROM control_preferences WHERE session=?', (sid,)).fetchone()
        if pref and pref['subject'] != subject:
            raise CollectorError('此聊天的原生账号已变化')
        selected = data.get('model')
        model = pref['model'] if pref else selected or data.get('currentModel')
        effort = pref['effort'] if pref else data.get('effort')
        # Preparation can be deferred before settings are applied. The engine
        # snapshot, when supplied, is authoritative over the last planned model;
        # otherwise a retry can mistake the unchanged engine for a new choice.
        current_model = data.get('currentModel') or (pref['actual_model'] if pref else None)
        current_effort = data.get('currentEffort') or (pref['actual_effort'] if pref else None)
        if selected and (not pref or selected != current_model or data.get('explicit')):
            model, effort = selected, data.get('effort')
        if data.get('effortExplicit') or (pref and data.get('effort') and data['effort'] != current_effort):
            effort = data.get('effort')
        native_effort = effort
        known = native.model_metadata()
        if model not in known:
            raise CollectorError('原生模型不在本机能力目录中')
        if data.get('preferenceOnly'):
            with native.receipts() as db:
                db.execute('INSERT OR REPLACE INTO control_preferences VALUES (?,?,?,?,?,?)',
                           (sid, subject, model, effort, pref['actual_model'] if pref else model,
                            pref['actual_effort'] if pref else effort))
            return {}
        items = data.get('items')
        if not isinstance(items, list) or not items:
            raise CollectorError('控制代理缺少本轮输入')
        token = secrets.token_urlsafe(32)
        mode = state['switch_mode']
        route, grant = copy.deepcopy(native.active), native.grant
        previous = previous_execution(native, sid, subject, data.get('currentModel') or selected)
        binding = None
        if mode == 'qpool':
            if not native.grant or not native.active:
                raise CollectorError('qpool 路由已停用')
            login = desktop_switch.identity(native, headers)
            old = desktop_switch.legacy_account(native, sid, login)
            binding, _ = native.select({'model': 'qpool-auto', 'input': items},
                                       {'session_id': thread}, legacy_account=old, protocol_turn=token)
            target = binding['channel']['model']
        else:
            target = model
        if target not in known:
            raise CollectorError('路由模型缺少本机能力信息')
        levels = {r['effort'] for r in known[target].get('supported_reasoning_levels', [])}
        if effort not in levels:
            effort = known[target].get('default_reasoning_level', 'medium')
        if effort not in levels:
            raise CollectorError('目标模型没有可用推理档位')
        if mode == 'qpool' and (native.grant != grant or native.active != route):
            raise CollectorError('准备期间路由已变化，请重新提问')
        plan = {'mode': mode, 'model': target, 'effort': effort, 'binding': binding,
                'grant': digest(grant or ''), 'route': route,
                'turn': None, 'native_model': model, 'previous': previous}
        with native.receipts() as db:
            db.execute('INSERT INTO control_plans VALUES (?,?,?,?,?)',
                       (token, sid, subject, json.dumps(plan), time.time()))
            db.execute('INSERT OR REPLACE INTO control_preferences VALUES (?,?,?,?,?,?)',
                       (sid, subject, model, native_effort, target, effort))
        return {'token': token, 'model': target, 'effort': effort, 'mode': mode}


def resolve(native, body, headers, session, subject, compact=False):
    metadata = body.get('client_metadata') or {}
    if not isinstance(metadata, dict):
        raise CollectorError('Codex 轮次元数据格式无效')
    encoded = metadata.get('x-codex-turn-metadata') or headers.get('x-codex-turn-metadata')
    detail = json.loads(encoded) if encoded else {}
    if not isinstance(detail, dict):
        raise CollectorError('Codex 轮次元数据格式无效')
    token = detail.get(METADATA_KEY)
    if not token:
        return None
    if not isinstance(token, str):
        raise CollectorError('控制代理轮次标识无效')
    compact = compact or 'compaction' in detail
    turn = metadata.get('turn_id') or detail.get('turn_id')
    with native.receipts() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM control_plans WHERE token=?', (token,)).fetchone()
        if not row or row['session'] != session or row['subject'] != digest(subject):
            raise CollectorError('控制代理计划与聊天或登录不匹配')
        plan = json.loads(row['detail'])
        execution = plan
        if compact and body.get('model', plan['model']) != plan['model']:
            previous = plan.get('previous')
            if previous and body.get('model') == previous['model']:
                execution = previous
        pinned = bool(execution.get('turn'))
        if execution['mode'] == 'qpool':
            validate_plan_route(native, execution['grant'], execution['route'], pinned=pinned)
        if body.get('model', execution['model']) != execution['model']:
            raise CollectorError('Codex 实际模型与控制代理计划不一致')
        if not compact:
            if not turn or (plan['turn'] and plan['turn'] != turn):
                raise CollectorError('控制代理计划不能跨用户轮次复用')
            plan['turn'] = turn
            db.execute('UPDATE control_plans SET detail=? WHERE token=?', (json.dumps(plan), token))
            db.execute('INSERT OR REPLACE INTO desktop_turns VALUES (?,?,?,?,?)',
                       (session, plan['turn'], plan['mode'], plan['model'], digest(subject)))
    # The capability belongs to the local control bridge, not the model provider.
    if encoded and metadata.get('x-codex-turn-metadata'):
        detail.pop(METADATA_KEY, None)
        body['client_metadata'] = dict(metadata, **{'x-codex-turn-metadata': json.dumps(detail)})
    return dict(execution, route_pinned=pinned)


def queue_claim(native, data, headers):
    """Persist dispatch intent before starting a queued turn; never replay unknown work."""
    subject = digest(desktop_switch.identity(native, headers))
    thread, queued = data.get('threadId'), data.get('queuedSubmissionId')
    if not all(isinstance(v, str) and 0 < len(v) <= 512 for v in (thread, queued)):
        raise CollectorError('排队消息标识无效')
    sid = digest(thread)[:16]
    action = data.get('action', 'lookup')
    with native.receipts() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM control_queue WHERE session=? AND queued_id=?', (sid, queued)).fetchone()
        if row and row['subject'] != subject:
            raise CollectorError('排队消息的原生登录已变化')
        if action == 'claim':
            if row:
                raise CollectorError('排队消息已经提交或结果未确认，未自动重发')
            db.execute('INSERT INTO control_queue VALUES (?,?,?,NULL)', (sid, queued, subject))
        elif action == 'accepted':
            if not row or not isinstance(data.get('result'), dict):
                raise CollectorError('缺少排队提交记录')
            db.execute('UPDATE control_queue SET result=? WHERE session=? AND queued_id=?',
                       (json.dumps(data['result']), sid, queued))
        elif action == 'rejected':
            # Only an explicit protocol rejection, never a timeout/disconnect.
            db.execute('DELETE FROM control_queue WHERE session=? AND queued_id=? AND result IS NULL', (sid, queued))
        elif action != 'lookup':
            raise CollectorError('排队控制操作无效')
    return {'claimed': bool(row), 'result': json.loads(row['result']) if row and row['result'] else None}


def install(native, binary=None):
    """Create an opt-in CLI wrapper; never restart or reconfigure a live desktop."""
    state = desktop.read_state(native.root)
    if not state or not state.get('hot_switch') or not desktop.status(native)['connected']:
        raise CollectorError('请先接入常驻桌面入口')
    binary = Path(binary or '/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex').resolve()
    if not binary.is_file():
        raise CollectorError('未找到原生 Codex 程序')
    wrapper = native.root / 'codex-control'
    source = Path(__file__).resolve().parent.parent
    script = ('#!' + sys.executable + '\nimport sys\nsys.path.insert(0, ' + repr(str(source)) + ')\n'
              'from qp.control_proxy import main\nmain(' + repr(str(native.root)) + ', ' + repr(str(binary)) + ')\n')
    atomic(wrapper, script, 0o700)
    launcher = native.root / 'launch-controlled-codex'
    script = ('#!' + sys.executable + '\nimport os, subprocess, sys\n'
              'if subprocess.run(["pgrep", "-x", "ChatGPT"], stdout=subprocess.DEVNULL).returncode == 0:\n'
              '    sys.exit("请先退出 ChatGPT/Codex 桌面，再运行此入口；不会中断当前聊天。")\n'
              'os.environ["CODEX_CLI_PATH"] = ' + repr(str(wrapper)) + '\n'
              'os.environ["CODEX_APP_SERVER_FORCE_CLI"] = "1"\n'
              'os.environ.pop("CODEX_APP_SERVER_WS_URL", None)\n'
              'os.execv("/Applications/ChatGPT.app/Contents/MacOS/ChatGPT", ["ChatGPT"])\n')
    atomic(launcher, script, 0o700)
    return {'wrapper': str(wrapper), 'launcher': str(launcher), 'initial_reload_required': True}
