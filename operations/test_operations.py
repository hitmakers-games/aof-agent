import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from operations import archive, jobs, terraform
from operations.kube import Kube


class OperationsTest(unittest.TestCase):
    def test_readiness_must_remain_stable_after_a_brief_failure(self):
        now=[0]
        ready={'metadata':{'generation':1},'spec':{'replicas':1},
               'status':{'observedGeneration':1,'updatedReplicas':1,'readyReplicas':1}}
        unhealthy={**ready,'status':{**ready['status'],'readyReplicas':0}}
        client=object.__new__(Kube)
        with patch.object(client,'get',side_effect=[ready,unhealthy,ready,ready,ready]), \
             patch('operations.kube.time.monotonic',side_effect=lambda:now[0]), \
             patch('operations.kube.time.sleep',side_effect=lambda seconds:now.__setitem__(0,now[0]+seconds)):
            client.wait_deployment('bot',timeout=30,stable_seconds=6)
        self.assertEqual(now[0],12)

    def test_preflight_has_only_database_credentials_and_no_host_access(self):
        pod=jobs.preflight({'id':'aof-op-123456789abc'},'new-image')['spec']['template']['spec']
        self.assertFalse(pod['automountServiceAccountToken'])
        self.assertEqual([v['name'] for v in pod['volumes']],['database'])
        self.assertEqual(pod['containers'][0]['command'],['python3','-m','gateway.preflight'])
        self.assertTrue(pod['securityContext']['runAsNonRoot'])

    def test_source_cannot_extract_paths_or_links_outside_work_directory(self):
        for name,kind in [('../escape',tarfile.REGTYPE),('link',tarfile.SYMTYPE)]:
            with self.subTest(name=name),tempfile.TemporaryDirectory() as root:
                stream=io.BytesIO()
                with tarfile.open(fileobj=stream,mode='w:gz') as packed:
                    entry=tarfile.TarInfo(name);entry.type=kind;entry.linkname='/run/secrets'
                    packed.addfile(entry)
                with self.assertRaises(ValueError):archive.unpack(stream.getvalue(),Path(root)/'source')

    def test_controller_is_detached_and_colocated_with_persistent_workspace(self):
        request={'id':'aof-op-123456789abc'}
        pod=jobs.controller(request,'stable-image','source')['spec']['template']['spec']
        self.assertFalse(pod['automountServiceAccountToken'])
        self.assertEqual(pod['serviceAccountName'],'aof-agent-telegram-developer')
        self.assertIn('podAffinity',pod['affinity'])
        self.assertFalse(any('hostPath' in volume for volume in pod['volumes']))
        self.assertTrue(any(volume.get('persistentVolumeClaim',{}).get('claimName')=='aof-agent-workspace' for volume in pod['volumes']))

    def test_self_deploy_cannot_apply_a_shared_or_destructive_plan(self):
        changes=[('module.database.player_balances',['update']),
                 ('module.aof_agent.helm_release.agent["database"]',['delete','create']),
                 ('module.aof_agent.kubernetes_resource_quota_v1.agent',['update'])]
        for address,actions in changes:
            document={'resource_changes':[{'address':address,'change':{'actions':actions}}]}
            with self.subTest(address=address),patch.object(terraform,'run',side_effect=[b'',json.dumps(document).encode()]):
                with self.assertRaises(ValueError):terraform.plan(Path('/unused'),{},'sha256:example',deploy=True)

    def test_changed_saved_plan_is_never_applied(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root);(path/'operation.plan').write_bytes(b'changed')
            with patch.object(terraform,'run') as run:
                with self.assertRaises(ValueError):terraform.apply(path,{},hashlib.sha256(b'reviewed').hexdigest())
                run.assert_not_called()


if __name__=='__main__':unittest.main()
