import contextlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
from urllib.parse import urlsplit
from core.config import DATA_DIR, ROOT

DESCRIPTOR=DATA_DIR/'engine.json'


def validate_endpoint(url):
    """Accept only a bare local engine origin, never userinfo or a remote host."""
    try:
        parsed = urlsplit(url)
        valid = (parsed.scheme == 'http' and parsed.hostname == '127.0.0.1'
                 and parsed.port is not None and 1 <= parsed.port <= 65535
                 and parsed.username is None and parsed.password is None
                 and not parsed.path and not parsed.query and not parsed.fragment)
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise ValueError('MCP engine must use a bare http://127.0.0.1:PORT origin')
    return url


def read_descriptor():
    try:
        info=json.loads(DESCRIPTOR.read_text(encoding='utf-8'))
        # Runtime descriptors only refer to this local service.
        validate_endpoint(info['url'])
        req=urllib.request.Request(info['url']+'/health')
        with urllib.request.urlopen(req,timeout=1) as response:
            health=json.load(response)
        return info if health.get('instance_id')==info.get('instance_id') else None
    except (OSError,ValueError,KeyError):
        return None


@contextlib.contextmanager
def startup_lock():
    DATA_DIR.mkdir(parents=True,exist_ok=True)
    path=DATA_DIR/'engine.start.lock'
    f=path.open('a+b')
    if f.tell()==0: f.write(b'0'); f.flush()
    f.seek(0)
    deadline=time.monotonic()+30
    while True:
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(f.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            break
        except OSError:
            if time.monotonic()>deadline:
                f.close()
                raise RuntimeError('Timed out waiting for engine startup lock')
            time.sleep(.1)
    try: yield
    finally:
        f.seek(0)
        if os.name=='nt':
            import msvcrt
            msvcrt.locking(f.fileno(),msvcrt.LK_UNLCK,1)
        else:
            import fcntl
            fcntl.flock(f.fileno(),fcntl.LOCK_UN)
        f.close()


def ensure_engine(port=8765):
    with startup_lock():
        info=read_descriptor()
        if info: return info
        log=(DATA_DIR/'engine.log').open('ab')
        env=os.environ.copy()
        env.pop('TEAM_TOKEN',None)
        env.pop('TEAM_ENDPOINT',None)
        options={'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {'start_new_session':True}
        py_exe = str(ROOT/'.venv'/'Scripts'/'python.exe') if (ROOT/'.venv'/'Scripts'/'python.exe').exists() else sys.executable
        proc=subprocess.Popen([py_exe,str(ROOT/'main.py'),'--serve','--port',str(port)],
                              cwd=str(ROOT),env=env,stdout=log,stderr=log,**options)
        log.close()
        deadline=time.monotonic()+25
        while time.monotonic()<deadline:
            info=read_descriptor()
            if info: return info
            if proc.poll() is not None: break
            time.sleep(.1)
        raise RuntimeError('Engine did not start. See engine.log; the selected port may be occupied.')

