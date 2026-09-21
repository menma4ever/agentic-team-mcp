"""An owner console over the SAME managed session; never a competing CLI process."""
import json
import threading
import time
import urllib.request
from core.service import read_descriptor

def run_console(agent_id):
    info=read_descriptor()
    if not info: raise RuntimeError('Shared engine is not running')
    def call(path, body=None):
        req=urllib.request.Request(info['url']+path, data=json.dumps(body).encode() if body is not None else None,
            headers={'Authorization':'Bearer '+info['token'],'Content-Type':'application/json'})
        with urllib.request.urlopen(req,timeout=20) as response: return json.load(response)
    agent=None
    from urllib.parse import quote
    for project in call('/api/projects')['projects']:
        tree=call('/api/tree?project='+quote(project['name']))
        for a in [tree.get('ceo'),tree.get('manager'),*tree.get('workers',[])]:
            if a and a['id']==agent_id: agent=a
    if not agent: raise ValueError('Agent not found')
    print(f"{agent['role']} / {agent['name']} / {agent['model']}\nManaged harness session: {agent.get('session_id') or 'not started'}")
    print('Messages below are sent as HUMAN OWNER to this same agent. /exit closes this console only.\n')
    stop=threading.Event()
    def tail():
        cursor=-1
        try:
            while not stop.is_set():
                events=call(f'/api/agents/{agent_id}/events?after={cursor}')['events']
                for e in events:
                    cursor=max(cursor,e['id'])
                    data=e.get('data',{})
                    if e['type']=='output': print(data.get('text',''),end='',flush=True)
                    elif e['type'] in ('error','configuration_changed','auth_failover'): print(e['type'],json.dumps(data))
                stop.wait(1)
        except Exception as exc: print('Stream unavailable:',exc)
    worker=threading.Thread(target=tail,daemon=True);worker.start()
    try:
        while True:
            text=input('\nHuman Owner > ').strip()
            if text=='/exit': break
            if text:
                call('/api/chat',{'project_name':agent['project_name'],'target_agent_id':agent_id,'content':text,'is_interrupt':False})
                print('Queued as Human Owner.')
    except (EOFError,KeyboardInterrupt): pass
    finally: stop.set()
