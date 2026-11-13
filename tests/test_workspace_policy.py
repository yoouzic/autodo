import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class WorkspacePolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.source=self.root/'project';self.source.mkdir()
    def tearDown(self):self.temp.cleanup()
    def policy(self,items,runs=()):
        from dispatcher.workspace_policy import WorkspacePolicy
        class Store:
            def list_runs(self):return list(runs)
        policy=WorkspacePolicy(self.root,Store(),{})
        policy.catalog.list=lambda **kwargs:{'items':items,'warnings':[]}
        return policy
    def item(self,path):
        return {'id':'ws_'+hashlib.sha256(str(path).replace('\\','/').casefold().encode()).hexdigest()[:24],
                'path':str(path),'available':True,'label':path.name,'agents':['codex'],'origin':'external','reason':''}

    def test_resolve_revalidates_known_id_and_rejects_arbitrary_path(self):
        item=self.item(self.source);policy=self.policy([item])
        self.assertEqual(policy.resolve({'mode':'existing','workspace_id':item['id']})['path'],str(self.source))
        with self.assertRaises(ValueError):policy.resolve({'mode':'existing','workspace_id':'ws_'+'f'*24})
        with self.assertRaises(ValueError):policy.resolve({'mode':'existing','path':str(self.source)})

    def test_busy_parent_and_child_are_unavailable(self):
        item=self.item(self.source)
        with patch('dispatcher.workspace_policy.busy_paths',return_value=[str(self.source/'sub')]):
            policy=self.policy([item]);self.assertFalse(policy.list()['items'][0]['available'])
            with self.assertRaises(ValueError):policy.resolve({'mode':'existing','workspace_id':item['id']})

    def test_metadata_ancestors_and_frozen_snapshots_are_not_execution_roots(self):
        run_dir=self.root/'workspaces/codex/task/runs/one';snapshot=run_dir/'workspace'
        snapshot.mkdir(parents=True)
        run={'id':'one','run_dir':str(run_dir),'workspace_binding':{'strategy':'in_place',
             'workspace_path':str(self.source),'snapshot_path':str(snapshot)}}
        items=[self.item(self.root),self.item(run_dir),self.item(snapshot),self.item(self.source)]
        found=self.policy(items,[run]).list()['items']
        self.assertEqual([item['available'] for item in found],[False,False,False,True])

    def test_legacy_live_root_allowed_but_its_subfolder_requires_full_root(self):
        run_dir=self.root/'workspaces/codex/task/runs/one';live=run_dir/'workspace';(live/'sub').mkdir(parents=True)
        items=[self.item(live),self.item(live/'sub')]
        found=self.policy(items,[{'id':'one','run_dir':str(run_dir)}]).list()['items']
        self.assertEqual([item['available'] for item in found],[True,False])

    def test_external_git_metadata_is_not_an_execution_directory(self):
        metadata=self.source/'.git';metadata.mkdir()
        found=self.policy([self.item(self.source),self.item(metadata)]).list()['items']
        self.assertEqual([item['available'] for item in found],[True,False])

    def test_empty_managed_containers_cannot_be_selected_before_default_run_exists(self):
        containers=[self.root/'workspaces',self.root/'workspaces/dsh',
                    self.root/'workspaces/dsh/task',self.root/'workspaces/dsh/task/runs']
        containers[-1].mkdir(parents=True)
        policy=self.policy([self.item(path) for path in containers])
        self.assertTrue(all(not item['available'] for item in policy.list()['items']))
        for item in policy.list()['items']:
            with self.assertRaises(ValueError):policy.resolve({'mode':'existing','workspace_id':item['id']})

    def test_registered_persistent_project_and_subdirectory_remain_selectable(self):
        project=self.root/'workspaces/dsh/named/project';(project/'notes').mkdir(parents=True)
        run_dir=project.parent/'runs/one';(run_dir/'workspace').mkdir(parents=True)
        run={'id':'one','run_dir':str(run_dir),'workspace_binding':{
            'workspace_path':str(project),'snapshot_path':str(run_dir/'workspace')}}
        paths=[project.parent,project,project/'notes',run_dir.parent,run_dir/'workspace']
        found=self.policy([self.item(path) for path in paths],[run]).list()['items']
        self.assertEqual([item['available'] for item in found],[False,True,True,False,False])

    def test_guard_updates_are_not_hidden_by_discovery_cache(self):
        from dispatcher.workspace_guard import start_guard, release_guard, quarantine_guard
        item=self.item(self.source);policy=self.policy([item])
        self.assertTrue(policy.list()['items'][0]['available'])
        frozen=start_guard(self.root,'guarded',self.source)
        self.assertFalse(policy.list()['items'][0]['available'])
        release_guard(self.root,frozen)
        self.assertTrue(policy.list()['items'][0]['available'])
        frozen=start_guard(self.root,'quarantined',self.source)
        quarantine_guard(self.root,frozen,'timeout')
        self.assertIn('隔离',policy.list()['items'][0]['reason'])
        with self.assertRaisesRegex(ValueError,'隔离'):
            policy.resolve({'mode':'existing','workspace_id':item['id']})

    def test_guard_parent_and_child_are_blocked_without_leaking_identity(self):
        from dispatcher.workspace_guard import start_guard
        child=self.source/'child';child.mkdir()
        sibling=self.root/'sibling';sibling.mkdir()
        frozen=start_guard(self.root,'guarded',child)
        value=self.policy([self.item(self.source),self.item(child),self.item(sibling)]).list()
        self.assertEqual([item['available'] for item in value['items']],[False,False,True])
        self.assertNotIn(frozen['guard_id'],str(value))

    def test_corrupt_guard_blocks_even_cached_existing_selection(self):
        from dispatcher.workspace_guard import start_guard
        item=self.item(self.source);policy=self.policy([item])
        policy.list()
        start_guard(self.root,'guarded',self.source)
        record=next((self.root/'data/workspace-guards').glob('*.json'))
        record.write_text('{',encoding='utf-8')
        value=policy.list()
        self.assertFalse(value['items'][0]['available'])
        self.assertTrue(value['warnings'])
        with self.assertRaisesRegex(ValueError,'保护记录'):
            policy.resolve({'mode':'existing','workspace_id':item['id']})


if __name__=='__main__':unittest.main()
