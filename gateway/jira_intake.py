"""Read assigned Jira work once a minute. Receiving work never executes it."""
import json
import time

from developer.jira import Jira
from gateway import work
from telegram.storage import connect, put, setting


def text(value):
    if isinstance(value,dict):
        return value.get('text','')+' '.join(text(child) for child in value.get('content',[]))
    return ''


def poll(db,jira):
    issues=[]
    query={'jql':'project = '+jira.config['projectKey']+' AND assignee = currentUser() AND statusCategory != Done ORDER BY key',
        'fields':['summary','description'],'maxResults':100}
    while True:
        page=jira.request('/search/jql',query)
        issues.extend(page.get('issues',[]))
        if not page.get('nextPageToken'):break
        query['nextPageToken']=page['nextPageToken']
    with db:
        assigned={issue['key'] for issue in issues}
        for (uid,key) in db.execute("SELECT id,issue_key FROM work_items WHERE source='jira' AND assigned=1").fetchall():
            if key not in assigned:work.withdraw(db,uid)
        for issue in issues:
            if not issue['key'].startswith(jira.config['projectKey']+'-'):continue
            fields=issue['fields']
            title=fields['summary'][:500]
            description=text(fields.get('description'))[:32000]
            source_revision=work.revision(title,description)
            previous=setting(db,'jira-source:'+issue['key'])
            existing=work.get(db,issue['key'],lock=True)
            if existing is None:
                work.create(db,None,title,description,source='jira',external_id=issue['id'],
                    issue_key=issue['key'],shared=True,notify=True)
            elif previous is not None and previous!=source_revision:
                revision=work.revision(title,description,repositories=json.loads(existing['repositories']))
                db.execute("""UPDATE work_items SET title=?,description=?,revision=?,approved_revision=NULL,
                    approved_by=NULL,cancel_requested=?,state=?,updated_at=? WHERE id=?""",
                    (title,description,revision,int(existing['state']=='running'),
                     'cancelling' if existing['state']=='running' else 'awaiting_approval',time.time(),existing['id']))
            put(db,'jira-source:'+issue['key'],source_revision)
            db.execute('UPDATE work_items SET assigned=1 WHERE issue_key=?',(issue['key'],))
        put(db,'jira_state','online; assignments await a Telegram go-ahead')


def follow():
    db=connect('/unused')
    jira=Jira()
    while True:
        try:
            poll(db,jira)
        except Exception:
            with db:put(db,'jira_state','assignment check failed; will retry')
        time.sleep(60)
