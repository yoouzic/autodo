"""Public model discovery through the installed CLI's standard ACP surface."""
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import tempfile
import threading
import time

from .workbuddy import EFFORTS


def models_from_acp(result):
    model_state=result.get('models') if isinstance(result,dict) else None
    rows=model_state.get('availableModels') if isinstance(model_state,dict) else None
    if not isinstance(rows,list) or not 0<len(rows)<=1000:raise ValueError('Missing CodeBuddy model catalogue')
    models=[];seen=set()
    for row in rows:
        if not isinstance(row,dict):raise ValueError('Invalid CodeBuddy model metadata')
        name=row.get('modelId');label=row.get('name',name);meta=row.get('_meta') or {}
        levels=meta.get('effortOptions',[]) if isinstance(meta,dict) else None
        if (not isinstance(name,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}',name)
            or name in seen or not isinstance(label,str) or not label or len(label)>256
            or not isinstance(levels,list) or any(not isinstance(v,dict) or not isinstance(v.get('value'),str) or v['value'] not in EFFORTS for v in levels)):
            raise ValueError('Invalid CodeBuddy model metadata')
        seen.add(name);models.append({'id':name,'label':label,'efforts':list(dict.fromkeys(v['value'] for v in levels))})
    return models


def query_models(command,timeout=25):
    """Initialize and create an empty volatile session; never call session/prompt."""
    argv=list(command)+['--acp','--tools','','--strict-mcp-config','--mcp-config','{"mcpServers":{}}',
        '--setting-sources','none','--settings','{"disableAllHooks":true,"autoUpdates":false}',
        '--permission-mode','dontAsk','--no-session-persistence']
    env=dict(os.environ,CODEBUDDY_DISABLE_TRACE_COLLECTOR='1',CODEBUDDY_DISABLE_FILE_LOGS='1',
        CODEBUDDY_DISABLE_COMPILE_CACHE='1',CODEBUDDY_DISABLE_CRON='1',CODEBUDDY_CODE_DISABLE_BACKGROUND_TASKS='1',
        CODEBUDDY_CODE_DISABLE_AUTO_MEMORY='1')
    messages=queue.Queue(maxsize=100)
    with tempfile.TemporaryDirectory(prefix='dispatcher-models-') as directory:
        process=subprocess.Popen(argv,cwd=directory,env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
            text=True,encoding='utf-8',errors='replace',creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        def read():
            while True:
                line=process.stdout.readline(4*1024*1024+1)
                if not line or len(line)>4*1024*1024:return
                try:value=json.loads(line)
                except ValueError:continue
                try:messages.put(value,timeout=.1)
                except queue.Full:return
        reader=threading.Thread(target=read,daemon=True);reader.start()
        def send(value):process.stdin.write(json.dumps(value)+'\n');process.stdin.flush()
        def request(number,method,params):
            send({'jsonrpc':'2.0','id':number,'method':method,'params':params})
            deadline=time.monotonic()+timeout
            while time.monotonic()<deadline:
                try:value=messages.get(timeout=max(.1,deadline-time.monotonic()))
                except queue.Empty:break
                if not isinstance(value,dict):continue
                if value.get('id')==number and 'method' not in value:
                    if 'error' in value or not isinstance(value.get('result'),dict):
                        raise ValueError('CodeBuddy rejected model discovery; check the official CLI login')
                    return value['result']
                if 'method' in value and 'id' in value:
                    send({'jsonrpc':'2.0','id':value['id'],'error':{'code':-32601,'message':'Client tools are disabled for model discovery'}})
            raise TimeoutError('CodeBuddy model discovery timed out; existing catalogue is unchanged')
        try:
            request(1,'initialize',{'protocolVersion':1,'clientCapabilities':{'fs':{'readTextFile':False,'writeTextFile':False},'terminal':False},
                'clientInfo':{'name':'todo-dispatcher-catalog','version':'1.0'}})
            return models_from_acp(request(2,'session/new',{'cwd':str(Path(directory).resolve()),'mcpServers':[]}))
        finally:
            process.stdin.close()
            try:process.wait(timeout=3)
            except subprocess.TimeoutExpired:process.terminate();process.wait(timeout=5)
            reader.join(timeout=1);process.stdout.close()
