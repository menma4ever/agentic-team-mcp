import jsonschema

S = {'type': 'string'}
I = {'type': 'integer', 'minimum': 0}
B = {'type': 'boolean'}
A = {'type': 'array', 'items': S}
SPAWN = {'name': S, 'model': S, 'harness': S, 'task_description': S, 'role_title': S,
         'thinking_budget': I, 'reasoning_effort': S}
# Tool names are shared by the provider loop, MCP proxy and engine authorization.
ACTIONS = {
 'get_team_tree': ('Read project, agent IDs, execution state and results.', {}, [], 'all'),
 'list_capabilities': ('Read configured providers/models and available harnesses.', {}, [], 'all'),
 'create_manager': ('CEO creates and starts the manager after planning.', SPAWN, ['name','model','task_description'], 'CEO'),
 'spawn_worker': ('Start a specialist worker; returns immediately with its ID.', SPAWN, ['name','model','task_description'], 'MANAGER,CEO'),
 'send_team_message': ('Send as your authenticated identity. Interrupt cancels the current turn first.', {'target_agent_id':S,'message':S,'is_interrupt':B}, ['target_agent_id','message'], 'all'),
 'reconfigure_agent': ('Change model/harness on the same agent with a durable handoff. after_turn waits safely; interrupt stops and resumes on the new runtime.', {'target_agent_id':S,'model':S,'harness':S,'mode':{'enum':['after_turn','interrupt']},'reasoning_effort':S,'provider':S,'name':S}, ['target_agent_id','model','harness'], 'all'),
 'resume_agent': ('Resume a paused assignment, inspecting existing work first.', {'target_agent_id':S,'message':S}, ['target_agent_id'], 'MANAGER,CEO'),
 'read_agent_status': ('Read status.md for a team member.', {'agent_id':S}, ['agent_id'], 'all'),
 'read_file': ('Read a project-relative UTF-8 file, or a file under owner-granted read roots.', {'path':S}, ['path'], 'all'),
 'list_files': ('List one directory within project/read roots.', {'path':S}, ['path'], 'all'),
 'write_file': ('Write a UTF-8 file in your own folder, shared/ or artifacts/.', {'path':S,'content':S}, ['path','content'], 'all'),
 'run_command': ('Execute an argument array in your workspace, only if owner enabled commands.', {'argv':A,'timeout_seconds':{'type':'integer','minimum':1,'maximum':600}}, ['argv'], 'WORKER,MANAGER'),
 'update_status': ('Record a real milestone and any blockers; do not invent percentages.', {'stage':S,'details':S}, ['stage'], 'all'),
 'report_result': ('Finish this worker turn and notify manager with evidence.', {'outcome':{'enum':['completed','blocked','needs_input']},'summary':S,'artifacts':A}, ['outcome','summary','artifacts'], 'WORKER'),
 'wait_for_workers': ('End manager turn while outstanding workers execute; results wake you.', {}, [], 'MANAGER,CEO'),
 'escalate': ('Ask your parent for guidance and stop this turn.', {'issue_summary':S}, ['issue_summary'], 'all'),
 'finish_project': ('Manager/CEO verifies acceptance and completes the project.', {'summary':S,'artifacts':A}, ['summary','artifacts'], 'MANAGER,CEO'),
 'terminate_worker': ('Stop execution, archive files, then optionally delete its working folder.', {'worker_id':S,'cleanup_folder':B}, ['worker_id'], 'MANAGER,CEO'),
 'connect_agents': ('Add an optional visual collaboration link. Does not enqueue work, change the supervisor, or grant permissions. Use send_team_message to communicate.', {'source_id':S,'target_id':S}, ['source_id','target_id'], 'all'),
 'disconnect_agents': ('Remove visual collaboration links between two agents. Does not stop execution or block messages.', {'source_id':S,'target_id':S}, ['source_id','target_id'], 'all'),
 'list_google_accounts': ('Read registered Google auth accounts and their health/availability.', {}, [], 'all'),
 'force_agent_auth': ('Set your own or a subordinate agent Google account preference for its next credential acquisition. Does not interrupt or switch a running lease. Use auto to clear.', {'target_agent_id':S,'account_id':S}, ['target_agent_id'], 'MANAGER,CEO,HUMAN'),
}


def definitions(role):
    result = []
    for name, (description, fields, required, roles) in ACTIONS.items():
        if role == 'HUMAN' or roles == 'all' or role in roles.split(','):
            result.append({'type':'function','function':{'name':name,'description':description,
                'parameters':{'type':'object','properties':fields,'required':required,'additionalProperties':False}}})
    return result


def validate(role, name, args):
    if name not in ACTIONS:
        raise ValueError('Unknown action')
    _, fields, required, roles = ACTIONS[name]
    if role != 'HUMAN' and roles != 'all' and role not in roles.split(','):
        raise PermissionError(f'{role} cannot call {name}')
    try:
        jsonschema.validate(args, {'type':'object','properties':fields,'required':required,'additionalProperties':False})
    except jsonschema.ValidationError as exc:
        raise ValueError(exc.message) from exc

