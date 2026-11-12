"""Normal repository work with GitHub App credentials outside model commands."""
import json
from pathlib import Path
import re
from urllib.request import build_opener, ProxyHandler

from coding.config import BASES
from coding import git
from coding.publish import SECRET
from repositories.github_app import GitHubApp,NoRedirects
from worker.run import TaskError


def validate_checkout(path,remote):
    """Allow ordinary Git preferences without trusting executable/auth settings."""
    directory=path/'.git'
    if directory.is_symlink() or not directory.is_dir() or (directory/'config').is_symlink():
        raise ValueError('Invalid checkout metadata; existing files were preserved.')
    settings=git.run(['config','--local','--no-includes','--null','--list'],path)
    safe={'core.repositoryformatversion','core.filemode','core.bare','core.logallrefupdates',
          'core.ignorecase','core.precomposeunicode','core.symlinks','core.autocrlf',
          'core.eol','core.safecrlf','core.quotepath','commit.gpgsign','tag.gpgsign',
          'init.defaultbranch','pull.rebase','pull.ff','push.default','fetch.prune',
          'rebase.autostash','merge.ff','merge.conflictstyle','remote.origin.fetch'}
    origins=[]
    for entry in settings.split('\0'):
        if not entry:continue
        key,_,value=entry.partition('\n')
        key=key.lower()
        if key=='remote.origin.url':
            origins.append(value)
        elif key not in safe and not key.startswith(('user.','color.','advice.')) and not re.fullmatch(
                r'branch\..+\.(remote|merge|rebase|description|pushremote|vscode-merge-base)',key):
            raise ValueError('Git '+key.split('.')[0]+' settings need review before authenticated operations.')
    if origins!=[remote]:
        raise ValueError('The checkout remote must match its corporate repository.')


class Repositories:
    def __init__(self,root):
        self.root=root
        self.records=Path('/transport/developer-control')/(root.name+'.json')
        self.records.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.app=GitHubApp()
        self.app.opener=build_opener(ProxyHandler({}),NoRedirects())

    def execute(self,action,repository=None,message=None,deployment_reviewed=False,branch=None):
        if action=='list':
            return {'repositories':[{'name':name,'base':base,'path':str(self.root/name)} for name,base in BASES.items()]}
        if repository not in BASES:
            raise ValueError('Select a repository from the list.')
        path=self.root/repository
        record=json.loads(self.records.read_text()) if self.records.exists() else {}
        remote=f'https://github.com/hitmakers-games/{repository}.git'
        requested_branch=branch or ('master' if repository in {'aof-agent','aof-infra'} and action=='checkout' else None)
        branch=branch or ('master' if repository in {'aof-agent','aof-infra'} else 'agent/'+self.root.name)
        git.run(['check-ref-format','--branch',branch])
        if action=='checkout' and not path.exists():
            token=self.app.token(repository)
            git.run(['clone','--single-branch','--branch',BASES[repository],'--',remote,str(path)],token=token)
            if branch!=BASES[repository]:git.run(['checkout','-b',branch],path)
            git.run(['config','user.name','aof-agent[bot]'],path)
            git.run(['config','user.email','aof-agent[bot]@users.noreply.github.com'],path)
            record[repository]={'branch':branch}
            self.records.write_text(json.dumps(record))
        if path.is_symlink() or not path.is_dir() or repository not in record:
            raise ValueError('Checkout this repository first.')
        saved=record[repository]
        validate_checkout(path,remote)
        saved['branch']=git.run(['branch','--show-current'],path)
        if not saved['branch']:
            raise ValueError('Checkout a named working branch before committing or publishing.')
        if action=='checkout' and requested_branch and requested_branch!=saved['branch']:
            if git.changes(path):
                raise ValueError('Commit or finish the current changes before switching task branches; files were preserved.')
            git.run(['fetch','origin',BASES[repository]],path,self.app.token(repository))
            local=git.run(['branch','--list',requested_branch],path)
            if local:
                git.run(['checkout',requested_branch],path)
            else:
                remote_branch=git.run(['ls-remote','--heads',remote,'refs/heads/'+requested_branch],token=self.app.token(repository))
                if remote_branch:
                    git.run(['fetch','origin',requested_branch],path,self.app.token(repository))
                    git.run(['checkout','-b',requested_branch,'FETCH_HEAD'],path)
                else:git.run(['checkout','-b',requested_branch,'origin/'+BASES[repository]],path)
            saved['branch']=requested_branch
            self.records.write_text(json.dumps(record))
        branch=saved['branch']
        if action in {'checkout','status'}:
            if action=='checkout' and not git.changes(path):
                self.pull(path,remote,branch,repository)
            return {'path':str(path),'branch':git.run(['branch','--show-current'],path),'status':git.run(['status','--short'],path)}
        if action=='diff':return {'diff':git.run(['diff','HEAD'],path)[:48000]}
        if action=='log':return {'log':git.run(['log','-10','--oneline'],path)}
        if action=='fetch':
            git.run(['fetch','origin',BASES[repository]],path,self.app.token(repository))
            return {'fetched':BASES[repository]}
        if action=='commit':
            if not message or len(message)>200:raise ValueError('Supply a short commit message.')
            names=git.changes(path)
            if not names:return {'status':'No source changes to commit.'}
            for name in names:
                target=path/name
                if target.exists() and SECRET.search(target.read_bytes()):raise ValueError('Possible credential in changed file.')
            git.run(['add','--',*names],path)
            git.run(['diff','--cached','--check'],path)
            git.run(['commit','-m',message],path)
            return {'commit':git.run(['rev-parse','HEAD'],path)}
        if action=='push':
            if git.run(['branch','--show-current'],path)!=branch:raise ValueError('Push the dedicated working branch.')
            self.pull(path,remote,branch,repository)
            workflows=path/'.github/workflows'
            if workflows.exists() and any(workflows.glob('*.y*ml')) and not deployment_reviewed:
                raise ValueError('Inspect workflow triggers first, then set deployment_reviewed=true if publishing this working branch is authorized. Production deployments still need explicit authorization.')
            git.run(['push',remote,'HEAD:refs/heads/'+branch],path,self.app.token(repository,permissions={'contents':'write'}))
            return {'branch':branch,'commit_url':f'https://github.com/hitmakers-games/{repository}/commit/'+git.run(['rev-parse','HEAD'],path),'pull_request_created':False}
        raise ValueError('Unknown Git action')

    def pull(self,path,remote,branch,repository):
        token=self.app.token(repository)
        if not git.run(['ls-remote','--heads',remote,'refs/heads/'+branch],token=token):
            return
        try:
            git.run(['pull','--no-rebase','--no-edit','origin',branch],path,token)
        except TaskError:
            conflicts=git.run(['diff','--name-only','--diff-filter=U'],path)
            if conflicts:
                raise ValueError('Upstream changes conflict with this checkout. Resolve and commit the conflicts before pushing; no work was discarded.') from None
            raise
