"""Synthetic exhaustion in an ISOLATED quota snapshot; real Pro inference."""
import hashlib,http.client,json,sqlite3,time
from datetime import datetime,timezone
from pathlib import Path
root=Path(__file__).resolve().parents[1]/'.qpool/isolated-qa-20261106'
entry=json.loads((root/'native/entry.json').read_text())
session='qpool-isolated-pro-fallback-20261106-'+str(time.time_ns())
sid=hashlib.sha256(session.encode()).hexdigest()[:16]
with sqlite3.connect(root/'ledger.db') as db:
    rows=db.execute("SELECT id,used_percent,last_synced_at FROM quota_entries WHERE pool_id=?",('codex-4a67781596ca-subscription',)).fetchall()
    assert rows
    batch=db.execute('SELECT last_batch_at FROM quota_pools WHERE id=?',('codex-4a67781596ca-subscription',)).fetchone()[0]
    now=datetime.now(timezone.utc).isoformat()
    db.execute('UPDATE quota_entries SET used_percent=100,last_synced_at=? WHERE pool_id=?',(now,'codex-4a67781596ca-subscription'))
    db.execute('UPDATE quota_pools SET last_batch_at=? WHERE id=?',(now,'codex-4a67781596ca-subscription'))
try:
    c=http.client.HTTPConnection('127.0.0.1',entry['port'],timeout=120)
    body={'model':'qpool-auto','store':False,'stream':True,
          'input':[{'role':'user','content':'仅做回退链路验证：回复 PRO_FALLBACK_OK，不用任何工具。'}]}
    c.request('POST','/v1/responses',json.dumps(body),{'Authorization':'Bearer '+entry['token'],
              'Content-Type':'application/json','session_id':session})
    r=c.getresponse();assert r.status==200,r.status
    completed=False
    for line in r:
        if line.startswith(b'data: '):
            try:event=json.loads(line[6:])
            except ValueError:continue
            if event.get('type')=='response.completed':completed=True
    c.close();assert completed
    with sqlite3.connect(root/'native/sessions.db') as db:
        receipt=db.execute('SELECT account,model,status FROM requests WHERE session=? ORDER BY started DESC LIMIT 1',(sid,)).fetchone()
    assert receipt and receipt[0].endswith(' · pro') and receipt[2]=='completed',receipt
    result={'fixture':'Plus exhausted in isolated snapshot only','actual_inference':receipt,
            'api_used':False,'note':'Plus decision quota is also blocked; conservative primary fallback is expected'}
    (root/'fallback-result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps(result,ensure_ascii=False),flush=True)
finally:
    with sqlite3.connect(root/'ledger.db') as db:
        db.executemany('UPDATE quota_entries SET used_percent=?,last_synced_at=? WHERE id=?',[(used,synced,ident) for ident,used,synced in rows])
        db.execute('UPDATE quota_pools SET last_batch_at=? WHERE id=?',(batch,'codex-4a67781596ca-subscription'))
    print('Original isolated Plus snapshots restored',flush=True)
