"""Record actual Git/tool results so a summary cannot manufacture completion."""
import json
from pathlib import Path

from coding import git
from telegram.storage import put, setting


def checkout(db, request, repository, path):
    key=f'checkout:{request}:{path.parent.name}:{repository}'
    with db:
        db.execute('INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO NOTHING',
            (key,json.dumps({'repository':repository,'path':str(path),'head':git.run(['rev-parse','HEAD'],path)})))


def published(db, request, repository, path):
    with db:
        put(db,f'published:{request}:{path.parent.name}:{repository}',git.run(['rev-parse','HEAD'],path))


def inspect(db, request):
    result=[]
    for key,value in db.execute('SELECT key,value FROM settings WHERE key LIKE ?',('checkout:'+str(request)+':%',)):
        before=json.loads(value)
        path=Path(before['path'])
        from developer.git_tools import validate_checkout
        validate_checkout(path,'https://github.com/hitmakers-games/'+before['repository']+'.git')
        head=git.run(['rev-parse','HEAD'],path)
        result.append({'repository':before['repository'],'path':str(path),'commit':head,
            'committed':head!=before['head'],'changed_files':git.changes(path),
            'published':setting(db,key.replace('checkout:','published:',1))==head})
    return result
