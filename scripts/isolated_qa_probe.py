"""Small explicit live-QA helpers; prints only selected status fields."""
import http.client,json,os,queue,subprocess,threading,time,re
from pathlib import Path

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
    def __init__(self,home=None,proxy=False,overrides=None):
        command=[BINARY]+([] if proxy else ['--no-daemon'])+['app-server']
        command+=['proxy'] if proxy else ['--listen','stdio://']
        def toml(value):
            if isinstance(value,dict):return '{'+','.join(json.dumps(k)+'='+toml(v) for k,v in value.items())+'}'
            if isinstance(value,list):return '['+','.join(toml(v) for v in value)+']'
            return json.dumps(value)
        for k,v in (overrides or {}).items():command+=['-c',k+'='+toml(v)]
        env=dict(os.environ)
        if home:env['CODEX_HOME']=str(home)
        self.p=subprocess.Popen(command,env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        self.events=queue.Queue();self.ident=0
        def read():
            for line in self.p.stdout:
                try:self.events.put(json.loads(line))
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
            try:d=self.events.get(timeout=30)
            except queue.Empty:raise RuntimeError('app-server did not respond; process exit '+str(self.p.poll()))
            if d.get('id')==ident:
                if 'error' in d:raise RuntimeError(str(d['error']))
                return d['result']
    def turn(self,thread,prompt):
        self.call('turn/start',{'threadId':thread,'input':[{'type':'text','text':prompt}]})
        text='';items=[]
        until=time.monotonic()+180
        while time.monotonic()<until:
            d=self.events.get(timeout=max(.1,until-time.monotonic()))
            if d.get('method')=='item/agentMessage/delta':text+=d['params']['delta']
            if d.get('method')=='item/completed':items.append(d['params']['item'].get('type'))
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
                return {'status':status,'text':text,'item_types':items}
        raise RuntimeError('turn deadline exceeded')
    def close(self):
        self.p.terminate()
        try:self.p.wait(timeout=8)
        except subprocess.TimeoutExpired:self.p.kill();self.p.wait()
        for h in (self.p.stdin,self.p.stdout,self.p.stderr):h.close()
