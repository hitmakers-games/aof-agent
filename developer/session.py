"""Persistent chat workspaces and the trusted process identity for developer tools."""
import json
from pathlib import Path

def workspace(actor, chat_id, work_id=None):
    import re
    if work_id is not None and not re.fullmatch('[a-f0-9]{32}',work_id):
        raise ValueError('Invalid work identifier')
    scope='task-'+work_id if work_id else 'group-'+str(abs(int(chat_id)))+'-user-'+str(int(actor)) if int(chat_id)<0 else 'user-'+str(int(actor))
    root=Path('/workspace/conversations')/scope
    if root.is_symlink():
        raise ValueError('Invalid developer workspace')
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    return root


def config(actor, chat_id, request_id=None, work_id=None, model_options=None, role='operator'):
    root=workspace(actor,chat_id,work_id)
    if role=='viewer':
        root=root/'viewer'
        if root.is_symlink():raise ValueError('Invalid viewer workspace')
        root.mkdir(parents=True,exist_ok=True,mode=0o700)
    rules={str(root):'write','/workspace':'deny','/run/secrets':'deny',
           '/transport':'deny','/state/auth/auth.json':'deny','/workspace/.terraform-adoption':'deny'}
    if role=='admin':
        rules['/workspace']='write'
    elif role=='viewer':
        rules[str(root)]='read'
    table='{'+','.join(json.dumps(k)+'='+json.dumps(v) for k,v in rules.items())+'}'
    env={'AOF_MCP_ACTOR':str(actor),'AOF_MCP_CHAT':str(chat_id),'AOF_MCP_REQUEST':str(request_id or 0),
         'AOF_DATABASE_DSN_FILE':'/run/secrets/database/uri','AOF_DATABASE_CA_FILE':'/run/secrets/database/ca.crt',
         'AOF_MCP_WORK':work_id or ''}
    if model_options:
        env['AOF_MCP_MODEL']=model_options['model']
        env['AOF_MCP_REASONING']=model_options['effort']
    env_table='{'+','.join(k+'='+json.dumps(v) for k,v in env.items())+'}'
    parent=':read-only' if role=='viewer' else ':workspace'
    return root,['-c','default_permissions="developer"','-c','permissions.developer.extends='+json.dumps(parent),
        '-c','permissions.developer.filesystem='+table,'-c','permissions.developer.network.enabled=true',
        '-c','mcp_servers.developer.command="/usr/bin/python3"',
        '-c','mcp_servers.developer.args=["-I","/opt/aof-agent/developer/server.py"]',
        '-c','mcp_servers.developer.env='+env_table,'-c','mcp_servers.developer.required=true',
        '-c','mcp_servers.developer.tool_timeout_sec=180',
        '-c','mcp_servers.developer.default_tools_approval_mode="approve"']
