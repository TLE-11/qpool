"""Small explicit live-QA helpers; prints only selected status fields."""
import argparse,http.client,json,os,queue,subprocess,threading,time,re
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
BINARY='/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex'


def api(root,path,data=None,timeout=90):
    port=json.loads((Path(root)/'runtime.json').read_text())['port']
    c=http.client.HTTPConnection('127.0.0.1',port,timeout=timeout)
    c.request('GET' if data is None else 'POST',path,None if data is None else json.dumps(data),
              {'Content-Type':'application/json','X-Qpool-Request':'1'})
    r=c.getresponse();value=json.loads(r.read());c.close()
    if r.status!=200:raise RuntimeError(f'{path}: HTTP {r.status}: {value.get("error")}')
    return value


class Client:
    def __init__(self,home=None,proxy=False,overrides=None,binary=None):
        command=[str(binary or BINARY)]+([] if proxy else ['--no-daemon'])+['app-server']
        command+=['proxy'] if proxy else ['--listen','stdio://']
        def toml(value):
            if isinstance(value,dict):return '{'+','.join(json.dumps(k)+'='+toml(v) for k,v in value.items())+'}'
            if isinstance(value,list):return '['+','.join(toml(v) for v in value)+']'
            return json.dumps(value)
        for k,v in (overrides or {}).items():command+=['-c',k+'='+toml(v)]
        env=dict(os.environ)
        if home:env['CODEX_HOME']=str(home)
        self.p=subprocess.Popen(command,env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        self.events=queue.Queue();self.responses=queue.Queue();self.ident=0
        def read():
            for line in self.p.stdout:
                try:
                    value=json.loads(line)
                    (self.responses if 'id' in value and 'method' not in value else self.events).put(value)
                except ValueError:pass
        threading.Thread(target=read,daemon=True).start()
        try:
            self.call('initialize',{'clientInfo':{'name':'qpool_isolated_qa','title':'qpool isolated QA','version':'1'},
                                    'capabilities':{'experimentalApi':True}})
        except BaseException as exc:
            detail=self.p.stderr.read()[-2000:] if self.p.poll() is not None else ''
            detail=re.sub(r'[A-Za-z0-9_./+\-=]{80,}', '[redacted]', detail)
            self.close()
            if detail:raise RuntimeError('Isolated engine startup: '+detail) from exc
            raise
        self.send({'method':'initialized','params':{}})
    def send(self,value):self.p.stdin.write(json.dumps(value)+'\n');self.p.stdin.flush()
    def call(self,method,params):
        self.ident+=1;ident=self.ident;self.send({'id':ident,'method':method,'params':params})
        while True:
            try:d=self.responses.get(timeout=30)
            except queue.Empty:raise RuntimeError('app-server did not respond; process exit '+str(self.p.poll()))
            if d.get('id')==ident:
                if 'error' in d:raise RuntimeError(str(d['error']))
                return d['result']
    def turn(self,thread,prompt,queued=False):
        params={'threadId':thread,'input':[{'type':'text','text':prompt}]}
        if queued:
            import uuid
            item=self.call('thread/queue/add',dict(params,clientUserMessageId=str(uuid.uuid4())))['queuedSubmission']
            # The production queue automatically dispatches when the thread is idle.
        else:
            self.call('turn/start',params)
        return self.wait_turn(thread)

    def wait_turn(self,thread):
        text='';items=[];settings=[]
        until=time.monotonic()+180
        while time.monotonic()<until:
            d=self.events.get(timeout=max(.1,until-time.monotonic()))
            if d.get('params',{}).get('threadId') not in (None,thread):continue
            if d.get('method')=='warning' and d['params'].get('message','').startswith('qpool 排队已暂停'):
                raise RuntimeError(d['params']['message'])
            if d.get('method')=='item/agentMessage/delta':text+=d['params']['delta']
            if d.get('method')=='item/completed':items.append(d['params']['item'].get('type'))
            if d.get('method')=='thread/settings/updated':
                s=d['params']['threadSettings'];settings.append({'model':s['model'],'effort':s.get('effort')})
            if d.get('method')=='error':
                error=d['params'].get('error',{})
                message=d['params'].get('message') or error.get('message') or 'turn error'
                # QA stops at the first failure, including automatic reconnects.
                # Do not consume more live quota while diagnosing a broken path.
                raise RuntimeError(re.sub(r'[A-Za-z0-9_./+\-=]{80,}', '[redacted]',str(message)))
            if d.get('method')=='turn/completed':
                status=d['params']['turn']['status']
                if status!='completed':
                    error=d['params']['turn'].get('error') or {}
                    raise RuntimeError('turn '+status+': '+str(error.get('message','')))
                return {'status':status,'text':text,'item_types':items,'settings_notifications':settings,
                        'turn_id':d['params']['turn']['id']}
        raise RuntimeError('turn deadline exceeded')
    def close(self):
        self.p.terminate()
        try:self.p.wait(timeout=8)
        except subprocess.TimeoutExpired:self.p.kill();self.p.wait()
        for h in (self.p.stdin,self.p.stdout,self.p.stderr):h.close()


def main():
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);p.add_argument('action',choices=['sync','judge','proxy-inspect'])
    args=p.parse_args()
    if args.action=='sync':
        result=api(args.root,'/api/sync',{});print(json.dumps(result,ensure_ascii=False),flush=True)
        state=api(args.root,'/api/state')
        print('subscription_windows',json.dumps([{'account':p['account_id'],'windows':p['windows']} for p in state['pools'] if p['kind']=='subscription'],ensure_ascii=False),flush=True)
    elif args.action=='judge':
        decision=api(args.root,'/api/state')['native']['active']['decision']
        cases=[('test-run','运行已有测试，整理通过和失败结果，不改业务代码。','regular'),
               ('test-framework','设计并发故障注入测试框架及覆盖策略，本次仅做测试框架设计。','regular'),
               ('failure-analysis','分析复杂分布式测试失败，定位并发一致性根因，仅分析不实现修复。','regular'),
               ('code-review','Review本次代码变更，检查安全、竞态与兼容性，只做代码审查。','primary'),
               ('implementation','实现幂等扣减额度接口，并修复并发重复扣减代码。','primary'),
               ('proposal','为多租户网关出架构技术方案，比较一致性与故障恢复设计。','primary')]
        results=[]
        for label,task,expected in cases:
            r=api(args.root,'/api/decision/model-test',{'decision':decision,'task':task,'allow_api':False})
            row=dict(case=label,expected=expected,**{k:r.get(k) for k in ['group','fallback','source','model','actual_model','reason','input_tokens','output_tokens']})
            row['passed']=r['group']==expected and not r.get('fallback')
            results.append(row);print(json.dumps(row,ensure_ascii=False),flush=True)
        (args.root/'judge-results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
        if not all(r['passed'] for r in results):raise SystemExit('real classification acceptance failed')
    else:
        c=Client(proxy=True)
        try:
            result=c.call('thread/loaded/list',{})
            print('desktop_proxy',result)
        finally:c.close()


if __name__=='__main__':main()
