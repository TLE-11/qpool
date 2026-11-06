"""Real Plus tool round-trip; the host executes the native browser action."""
import http.client,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]/'.qpool/isolated-qa-20261106'
entry=json.loads((ROOT/'native/entry.json').read_text())
sid='qpool-native-browser-roundtrip-20261106'
target=f'http://127.0.0.1:{entry["port"]}/#routes'
def response(history,force=False):
    body={'model':'qpool-auto','instructions':'You are testing a local routing console. Use the supplied native browser tool once, then report its result. Do not modify any settings.',
          'input':history,'stream':True,'store':False,'reasoning':{'effort':'medium'},
          'tools':[{'type':'function','name':'inspect_native_browser','description':'Use the host-managed desktop browser to read the allowed local console and its error logs.',
                    'parameters':{'type':'object','additionalProperties':False,'properties':{'url':{'type':'string','enum':[target]}},'required':['url']}}]}
    if force:body['tool_choice']={'type':'function','name':'inspect_native_browser'}
    c=http.client.HTTPConnection('127.0.0.1',entry['port'],timeout=120)
    c.request('POST','/v1/responses',json.dumps(body),{'Authorization':'Bearer '+entry['token'],
              'Content-Type':'application/json','session_id':sid})
    r=c.getresponse()
    if r.status!=200:raise RuntimeError('gateway status '+str(r.status))
    result=None
    for line in r:
        if line.startswith(b'data: '):
            try:item=json.loads(line[6:])
            except ValueError:continue
            if item.get('type')=='response.completed':result=item['response']
    c.close();assert result and result['status']=='completed'
    return result
phase=ROOT/'browser-roundtrip-phase.json'
if sys.argv[1]=='prepare':
    history=[{'role':'user','content':'执行一次桌面浏览器验证：读取隔离路由页面的实际方案、模型分组，获取截图和控制台error日志，然后报告。不修改代码或配置。'}]
    result=response(history,True)
    calls=[r for r in result['output'] if r.get('type')=='function_call']
    assert len(calls)==1 and calls[0]['name']=='inspect_native_browser'
    assert json.loads(calls[0]['arguments'])['url']==target
    phase.write_text(json.dumps({'history':history+result['output'],'call_id':calls[0]['call_id']}));phase.chmod(0o600)
    print('PLUS_NATIVE_BROWSER_CALL_REQUESTED',result.get('model'),target,flush=True)
else:
    saved=json.loads(phase.read_text());observed=json.loads((ROOT/'native-browser-result.json').read_text())
    saved['history'].append({'type':'function_call_output','call_id':saved['call_id'],'output':json.dumps(observed,ensure_ascii=False)})
    result=response(saved['history'])
    text=''.join(p.get('text','') for r in result['output'] if r.get('type')=='message' for p in r.get('content',[]))
    report={'model':result.get('model'),'status':result['status'],'text':text,'native_observation':observed}
    (ROOT/'browser-roundtrip-result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print('PLUS_NATIVE_BROWSER_CONTINUATION_COMPLETED',result.get('model'),text,flush=True)
