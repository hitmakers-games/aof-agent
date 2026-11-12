import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from operations import archive, jobs, terraform


class OperationsTest(unittest.TestCase):
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
