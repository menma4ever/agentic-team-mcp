"""Pure stdio MCP proxy. Every host connects to the SAME background engine."""
import asyncio
import os
import httpx
from mcp.server.fastmcp import FastMCP
from core.service import ensure_engine, validate_endpoint

mcp_server=FastMCP('Agentic Team MCP',instructions=
    'Persistent CEO/manager/worker team. Use list_projects and get_team_tree first. '
    'Messages use authenticated sender identities. Tools enqueue work; inspect events/results for completion.')
_info=None


async def request(method,path,body=None,params=None):
    global _info
    if os.environ.get('TEAM_TOKEN'):
        info={'url':os.environ.get('TEAM_ENDPOINT',''),'token':os.environ['TEAM_TOKEN']}
    else:
        if _info is None: _info=await asyncio.to_thread(ensure_engine)
        info=_info
    validate_endpoint(info['url'])
    async with httpx.AsyncClient(timeout=30,trust_env=False) as client:
        try:
            response=await client.request(method,info['url']+path,json=body,params=params,
                                          headers={'Authorization':'Bearer '+info['token']})
        except httpx.TransportError:
            # Reconnect on the next request. Never replay a mutation with an
            # uncertain outcome (the server may already have queued the work).
            if not os.environ.get('TEAM_TOKEN'): _info = None
            raise ValueError('Engine connection failed; outcome may be unknown. Inspect team state before retrying a write.') from None
        if response.status_code in (401, 403) and not os.environ.get('TEAM_TOKEN'):
            _info = await asyncio.to_thread(ensure_engine)
            info = _info
            validate_endpoint(info['url'])
            response=await client.request(method,info['url']+path,json=body,params=params,
                                          headers={'Authorization':'Bearer '+info['token']})
    if response.status_code>=400:
        raise ValueError(str(response.json().get('detail','Engine request failed')))
    return response.json()


async def act(project,action,arguments):
    return await request('POST','/api/action',{'project_name':project,'action':action,'arguments':arguments})


@mcp_server.tool()
async def list_projects():
    return await request('GET','/api/projects')


@mcp_server.tool()
async def create_project(name:str,description:str='',ceo_model:str='',ceo_name:str='CEO',
                         harness:str='direct_api',reasoning_effort:str|None=None,
                         allow_commands:bool=False):
    return await request('POST','/api/projects',{'name':name,'description':description,
        'ceo_model':ceo_model or None,'ceo_name':ceo_name,'harness':harness,
        'reasoning_effort':reasoning_effort,'allow_commands':allow_commands})


@mcp_server.tool()
async def get_team_tree(project_name:str):
    return await act(project_name,'get_team_tree',{})


@mcp_server.tool()
async def get_agent_activity(agent_id:str,after:int=0):
    """Owner inspection: real execution events, up to 500 per page.

    Pass the last event ID as after to read the next page. Agent-scoped clients
    cannot read this owner transcript endpoint. An empty page is not completion.
    """
    if after < 0: raise ValueError('after must be nonnegative')
    return await request('GET',f'/api/agents/{agent_id}/events',params={'after':after})


@mcp_server.tool()
async def get_agent_conversation(agent_id:str):
    """Owner inspection of persisted messages with sender identities.

    This includes team messages, not every private provider thinking token.
    """
    return await request('GET',f'/api/agents/{agent_id}/messages')


@mcp_server.tool()
async def list_capabilities(project_name:str):
    return await act(project_name,'list_capabilities',{})


@mcp_server.tool()
async def create_manager(project_name:str,name:str,model:str,task_description:str,
                         harness:str='direct_api',reasoning_effort:str|None=None):
    args={'name':name,'model':model,'task_description':task_description,'harness':harness}
    if reasoning_effort: args['reasoning_effort']=reasoning_effort
    return await act(project_name,'create_manager',args)


@mcp_server.tool()
async def spawn_worker(project_name:str,name:str,model:str,task_description:str,
                       role_title:str='Specialist',harness:str='direct_api',
                       thinking_budget:int=0,reasoning_effort:str|None=None):
    args={'name':name,'model':model,'task_description':task_description,'role_title':role_title,
          'harness':harness,'thinking_budget':thinking_budget}
    if reasoning_effort: args['reasoning_effort']=reasoning_effort
    return await act(project_name,'spawn_worker',args)


@mcp_server.tool()
async def send_team_message(project_name:str,target_agent_id:str,message:str,is_interrupt:bool=False):
    return await act(project_name,'send_team_message',{'target_agent_id':target_agent_id,
                      'message':message,'is_interrupt':is_interrupt})


@mcp_server.tool()
async def reconfigure_agent(project_name:str,target_agent_id:str,model:str,harness:str,mode:str='after_turn'):
    """Switch the same logical agent with a saved handoff. after_turn preserves ongoing execution."""
    return await act(project_name,'reconfigure_agent',{'target_agent_id':target_agent_id,
        'model':model,'harness':harness,'mode':mode})


@mcp_server.tool()
async def read_worker_status(project_name:str,worker_name:str):
    tree=await get_team_tree(project_name)
    agent=next((a for a in tree['workers'] if a['name']==worker_name),None)
    if not agent: raise ValueError('Worker not found')
    return await act(project_name,'read_agent_status',{'agent_id':agent['id']})


@mcp_server.tool()
async def terminate_worker(project_name:str,worker_id:str,cleanup_folder:bool=True):
    return await act(project_name,'terminate_worker',{'worker_id':worker_id,'cleanup_folder':cleanup_folder})


@mcp_server.tool()
async def escalate_to_ceo(project_name:str,issue_summary:str):
    # Engine routes to authenticated sender's parent, preserving identity.
    return await act(project_name,'escalate',{'issue_summary':issue_summary})


@mcp_server.tool()
async def team_action(project_name:str,action:str,arguments:dict):
    """Other actions: read_file(path), write_file(path,content), list_files(path),
    update_status(stage,details), report_result(outcome,summary,artifacts),
    wait_for_workers(), resume_agent(target_agent_id), escalate(issue_summary),
    finish_project(summary,artifacts), run_command(argv,timeout_seconds).
    Paths are project-relative. Workers can access own folder, shared/ and artifacts/.
    report_result outcome is completed, blocked, or needs_input.
    """
    return await act(project_name,action,arguments)


def run_mcp_server():
    mcp_server.run()

