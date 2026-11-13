import json
import tempfile
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from dispatcher.external import invocation
from dispatcher.runner import CodexRunner, MultiAgentRunner
from dispatcher.core import Store
from dispatcher.service import Service
from tests.test_service import FakeDida


class ModelSettingsRunnerTests(unittest.TestCase):
    def row(self, **changes):
        return dict({'payload': {'id':'t','projectId':'p','title':'Settings test'},
                     'difficulty':'hard','kind':'document','fingerprint':'abc'}, **changes)

    def test_codex_settings_frozen_before_execution_and_verifier_unchanged(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as leases:
            root=Path(directory)
            runner=CodexRunner(root, {'codex_command':['codex'], 'model':'gpt-6.1-sol','review_effort':'medium'})
            row=self.row(_agent_defaults={'codex':{'model':'gpt-6-astra','effort':'high'}},
                         execution_overrides={'effort':'low'})
            run=runner.prepare(row)
            leases.callback(runner.release_workspace,row)
            self.assertTrue((run/'execution-settings.json').exists(), 'Run settings must be frozen before invocation')
            row['_agent_defaults']['codex']['model']='gpt-6-sol'
            report={'status':'complete','summary':'done','artifacts':['r.md'],'checks':['read'],'needs_input':''}
            (run/'workspace/r.md').write_text('actual artifact',encoding='utf-8')
            with patch.object(runner,'_invoke',return_value=report.copy()) as invoke:
                result=runner.execute(row,run)
                self.assertEqual(invoke.call_args.kwargs['execution_settings']['model'],'gpt-6-astra')
                self.assertEqual(result['execution_settings']['effort'],'low')
                runner.verify(row,run,result)
                self.assertNotIn('execution_settings',invoke.call_args.kwargs)
                self.assertEqual(invoke.call_args.args[3],'medium')

    def test_modified_snapshot_file_is_rejected_before_model_invocation(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as leases:
            runner=CodexRunner(directory,{'codex_command':['codex'],'model':'gpt-6.1-sol'})
            row=self.row();run=runner.prepare(row)
            leases.callback(runner.release_workspace,row)
            path=run/'execution-settings.json'
            saved=json.loads(path.read_text(encoding='utf-8'));saved['model']='gpt-6-astra'
            path.write_text(json.dumps(saved),encoding='utf-8')
            with patch.object(runner,'_invoke') as invoke:
                with self.assertRaises(ValueError): runner.execute(row,run)
                invoke.assert_not_called()

    def test_service_records_selected_snapshot_even_if_archive_file_was_modified(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            config={'model':'gpt-6.1-sol','codex_command':['codex'],'require_human_review':True}
            class TamperedRunner(CodexRunner):
                def prepare(self,row):
                    run=super().prepare(row)
                    path=run/'execution-settings.json'
                    data=json.loads(path.read_text(encoding='utf-8'));data['model']='different-model'
                    path.write_text(json.dumps(data),encoding='utf-8')
                    return run
            runner=TamperedRunner(root,config)
            store=Store(root/'queue.db')
            task={'id':'t','projectId':'p','title':'Write a document','status':0,'tags':['AI']}
            service=Service(root,config,store,FakeDida(task),runner)
            service.sync();row=store.claim()
            with patch.object(runner,'_invoke') as invoke:
                service.execute(row)
                invoke.assert_not_called()
            run=store.get_run(row['current_run_id'])
            self.assertEqual(run['state'],'failed')
            self.assertEqual(run['execution_settings']['model'],'gpt-6.1-sol')
            store.close()

    def test_vendor_flags_replace_legacy_pinned_model_without_conflicts(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'workspace').mkdir()
            settings={'model':'gemini-3.8-flash','cli_model':'gemini-3.8-flash-high','effort':'high'}
            argv,_,_,_=invocation('antigravity',['agy','--model','gemini-3.8-flash-medium'],
                root/'workspace',root,'test',{},settings=settings)
            self.assertEqual(argv.count('--model'),1)
            self.assertEqual(argv[argv.index('--model')+1],'gemini-3.8-flash-high')
            self.assertEqual(argv[argv.index('--effort')+1],'high')

    def test_dsh_uses_per_run_patch_and_qoder_uses_argv(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'workspace').mkdir()
            invocation('dsh',['node','dsh.js'],root/'workspace',root,'test',{},
                settings={'model':'deepseek-flash','effort':'max','cli_model':'deepseek-flash','provider':'deepseek-official'})
            patch_data=json.loads((root/'dsh.patch.json').read_text(encoding='utf-8'))
            selected=next(p['config'] for p in patch_data if p['id']=='agent-default-model')
            self.assertEqual(selected,{'provider':'deepseek-official','model':'deepseek-flash','reasoningEffort':'max'})
            argv,_,_,_=invocation('qoder',['qoder'],root/'workspace',root,'test',{},
                settings={'model':'GLM-5.3','effort':'max','cli_model':'GLM-5.3'})
            self.assertEqual(argv[argv.index('--model')+1],'GLM-5.3')
            self.assertEqual(argv[argv.index('--reasoning-effort')+1],'max')

    def test_zcode_session_settings_use_bridge_without_copying_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'workspace').mkdir()
            argv,env,_,_=invocation('zcode',['node','/vendor/glm/zcode.cjs'],root/'workspace',root,'test',{},
                settings={'model':'glm-5.3-flash','effort':'low','cli_model':'glm-5.3-flash'})
            self.assertEqual(argv[0],sys.executable)
            self.assertTrue(argv[1].endswith('zcode_session.py'))
            request=json.loads((root/'zcode-request.json').read_text(encoding='utf-8'))
            self.assertEqual((request['model'],request['effort']),('glm-5.3-flash','low'))
            self.assertEqual(json.loads((root/'zcode-command.json').read_text(encoding='utf-8')),['node','/vendor/glm/zcode.cjs'])
            self.assertEqual(Path(env['ZCODE_PERSONAL_PROVIDER_CONFIG_FILE']),Path.home()/'.zcode/v2/provider_config.json')


if __name__ == '__main__': unittest.main()
