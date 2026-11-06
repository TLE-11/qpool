"""Live desktop-engine replay of a COPY of the authorized temporary old chat."""
import hashlib,json,os,shutil,sqlite3,sys,time
from pathlib import Path
from isolated_qa_probe import Client,api

ROOT=Path(__file__).resolve().parents[1]
runtime=ROOT/'.qpool/isolated-qa-20261106'
home=runtime/'codex-home'
tid='01a19033-8638-7902-8ab1-4001e14a8c75'
sid=hashlib.sha256(tid.encode()).hexdigest()[:16]
port=json.loads((runtime/'runtime.json').read_text())['port']
original_config=Path('/Users/liwenqiang/.codex/config.toml').read_bytes()
with sqlite3.connect('file:/Users/liwenqiang/.codex/state_5.sqlite?mode=ro',uri=True) as db:
    path=Path(db.execute('SELECT rollout_path FROM threads WHERE id=?',(tid,)).fetchone()[0])
history=path.read_bytes()
copy=home/'sessions'/'2026'/'10'/'31'/path.name
copy.parent.mkdir(parents=True,exist_ok=True)
copy.write_bytes(history);copy.chmod(0o600)
raw=json.loads((runtime/'pools.json').read_text())
pro=next(a for a in raw['accounts'] if a['id']=='codex-dc804ba89e83')
source_login=json.loads(Path(pro['source']['credentials_file']).read_text())
login=source_login['tokens']
auth={'auth_mode':'chatgpt','tokens':{k:login[k] for k in ['access_token','account_id','id_token'] if k in login}}
auth['tokens']['refresh_token']=''  # Required schema field; no shared refresh ownership.
if source_login.get('last_refresh'):auth['last_refresh']=source_login['last_refresh']
(home/'auth.json').write_text(json.dumps(auth));(home/'auth.json').chmod(0o600)
del login,auth,source_login
provider={'name':'isolated native old-chat QA','base_url':f'http://127.0.0.1:{port}/legacy/v1',
          'wire_api':'responses','requires_openai_auth':True,'supports_websockets':False,
          'request_max_retries':0,'stream_max_retries':0}
overrides={'model_provider':'openai','openai_base_url':f'http://127.0.0.1:{port}/legacy/v1'}
results=[]

def run(c,label,prompt,model=None):
    if model:c.call('thread/settings/update',{'threadId':tid,'model':model})
    r=c.turn(tid,prompt)
    with sqlite3.connect(runtime/'native/sessions.db') as db:
        receipts=db.execute('SELECT account,model,status,actual_model FROM requests WHERE session=? ORDER BY started',(sid,)).fetchall()
    assert receipts,'The request bypassed the isolated gateway'
    row={'stage':label,'engine_status':r['status'],'item_types':r['item_types'],'latest_receipt':receipts[-1]}
    results.append(row);print(json.dumps(row,ensure_ascii=False),flush=True)
    assert receipts[-1][2]=='completed'
    return r

c=Client(home=home,overrides=overrides)
try:
    r=c.call('thread/resume',{'threadId':tid,'path':str(copy),'cwd':str(runtime/'engine-workspace'),
             'modelProvider':'openai','model':'gpt-6.1-sol','approvalPolicy':'never','sandbox':'read-only','excludeTurns':True})
    assert r['thread']['id']==tid
    first=run(c,'old-pro-history','这是隔离历史续接验证。此前本聊天检查过什么浏览器？简短回答并记住标记 QA_HISTORY_6217。不要用工具。')
    assert 'QA_HISTORY_6217' in first['text']
    with sqlite3.connect(runtime/'native/sessions.db') as db:
        account=json.loads(db.execute('SELECT binding FROM legacy_bindings WHERE session=?',(sid,)).fetchone()[0])['channel']['account_id']
    assert account=='codex-dc804ba89e83'
    api(runtime,'/api/sessions/migrate',{'session':sid,'target_account_id':'codex-4a67781596ca','confirm':True})
    second=run(c,'migrated-plus-history','请原样回复上一轮记住的 QA_HISTORY 标记，不用工具。')
    assert 'QA_HISTORY_6217' in second['text']
    with sqlite3.connect(runtime/'native/sessions.db') as db:
        moved=json.loads(db.execute('SELECT binding FROM legacy_bindings WHERE session=?',(sid,)).fetchone()[0])
    assert moved['channel']['account_id']=='codex-4a67781596ca'
    run(c,'test-framework-sol','仅设计测试框架：为并发配额预留设计故障注入测试矩阵，列3项即可，不实现业务代码、不用工具。',model='qpool-auto')
    with sqlite3.connect(runtime/'native/sessions.db') as db:
        assert db.execute('SELECT model FROM requests WHERE session=? ORDER BY started DESC LIMIT 1',(sid,)).fetchone()[0]=='gpt-6.1-sol'
    run(c,'code-review-astra','仅Review这段代码的竞态风险并给一句结论：余额为100时两个线程分别执行 if balance>=80: balance-=80。不要运行工具或改代码。')
    with sqlite3.connect(runtime/'native/sessions.db') as db:
        assert db.execute('SELECT model FROM requests WHERE session=? ORDER BY started DESC LIMIT 1',(sid,)).fetchone()[0]=='gpt-6-astra'
    old=api(runtime,'/api/state')['native']['active']
    api(runtime,'/api/routes/save',old);api(runtime,'/api/agent/enable',{'route_id':old['id']})
    run(c,'same-plan-reenabled','只回复你记住的 QA_HISTORY 标记，不用工具。')
    print('PAUSED_FOR_ISOLATED_RESTART',flush=True);sys.stdin.readline()
    run(c,'after-gateway-restart','只回复你记住的 QA_HISTORY 标记，不用工具。')
finally:
    c.close()
    (runtime/'history-engine-results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
    assert path.read_bytes()==history,'Original temporary chat history changed during copy-only test'
    assert Path('/Users/liwenqiang/.codex/config.toml').read_bytes()==original_config
    print('ORIGINAL_HISTORY_AND_MAIN_CONFIG_UNCHANGED',flush=True)
