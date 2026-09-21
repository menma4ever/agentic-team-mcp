import argparse
import json
import os
import secrets
import socket
import sys
import uuid
import webbrowser
from pathlib import Path


def serve(port):
    import uvicorn
    from core.config import DATA_DIR
    from core.service import DESCRIPTOR
    from engine.orchestrator import Orchestrator
    from web.app import create_app
    # Claim the port before publishing any credentials/state.
    sock=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
    if hasattr(socket,'SO_EXCLUSIVEADDRUSE'): sock.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
    sock.bind(('127.0.0.1',port))
    sock.listen(128)
    instance=uuid.uuid4().hex
    # A controlled idle upgrade can retain existing browser/MCP owner sessions.
    # Consume this environment variable here so model subprocesses never inherit it.
    token=os.environ.pop('TEAM_OWNER_TOKEN', None) or secrets.token_urlsafe(40)
    endpoint=f'http://127.0.0.1:{port}'
    DATA_DIR.mkdir(parents=True,exist_ok=True)
    engine=Orchestrator()
    engine.endpoint=endpoint
    app=create_app(engine,token,instance)
    temp=DESCRIPTOR.with_suffix('.tmp')
    temp.write_text(json.dumps({'url':endpoint,'token':token,'instance_id':instance,'pid':os.getpid()}),encoding='utf-8')
    temp.replace(DESCRIPTOR)
    try:
        uvicorn.Server(uvicorn.Config(app,log_level='warning',loop='asyncio')).run(sockets=[sock])
    finally:
        sock.close()
        try:
            if json.loads(DESCRIPTOR.read_text()).get('instance_id')==instance:
                DESCRIPTOR.unlink()
        except (OSError,ValueError): pass


def main():
    parser=argparse.ArgumentParser(description='Agentic Team: shared local engine, dashboard and MCP')
    parser.add_argument('--console', help='Open the managed agent session as Human Owner')
    parser.add_argument('--mcp',action='store_true')
    parser.add_argument('--agent-mcp',action='store_true',help=argparse.SUPPRESS)
    parser.add_argument('--serve',action='store_true',help=argparse.SUPPRESS)
    parser.add_argument('--no-browser',action='store_true')
    parser.add_argument('--port',type=int,default=8765)
    args=parser.parse_args()
    if args.console:
        from core.console import run_console
        run_console(args.console)
    elif args.serve:
        serve(args.port)
    elif args.mcp or args.agent_mcp:
        if args.agent_mcp and not (os.environ.get('TEAM_TOKEN') and os.environ.get('TEAM_ENDPOINT')):
            parser.error('Agent MCP requires scoped TEAM_TOKEN and TEAM_ENDPOINT; owner fallback is disabled')
        from mcp_server.server import run_mcp_server
        run_mcp_server()
    else:
        from core.service import ensure_engine
        info=ensure_engine(args.port)
        print('Agentic Team is available at '+info['url'])
        if not args.no_browser:
            webbrowser.open(info['url']+'/#token='+info['token'])


if __name__=='__main__':
    main()

