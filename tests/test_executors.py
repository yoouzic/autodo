import json
import unittest
import tempfile
from pathlib import Path
from dispatcher.executors import parse_result_text, normalize_result, sandbox_command

REPORT={'status':'complete','summary':'done','artifacts':['result.md'],'checks':['read file'],'needs_input':''}


class ExecutorTests(unittest.TestCase):
    def test_strict_business_result(self):
        self.assertEqual(parse_result_text('```json\n'+json.dumps(REPORT)+'\n```'),REPORT)
        for raw in ['all done', '{}', json.dumps(dict(REPORT,artifacts='result.md'))]:
            with self.assertRaises(ValueError): parse_result_text(raw)

    def test_single_json_fence_can_follow_vendor_narration(self):
        raw='已核对文件。返回结果：\n```json\n'+json.dumps(REPORT)+'\n```'
        self.assertEqual(parse_result_text(raw),REPORT)
        with self.assertRaises(ValueError): parse_result_text(raw+'\n```json\n{}\n```')

    def test_dsh_final_is_not_enough_when_turn_failed(self):
        events=[{'type':'session','sessionId':'d1'}, {'type':'status','phase':'turn_end','reason':{'kind':'completed'}}, {'type':'final','text':json.dumps(REPORT)}]
        r=normalize_result('dsh','\n'.join(map(json.dumps,events)))
        self.assertEqual(r['session_id'],'d1')
        self.assertEqual(r['executor'],'dsh')
        events.insert(1,{'type':'status','phase':'turn_end','reason':{'kind':'error'}})
        with self.assertRaises(ValueError): normalize_result('dsh','\n'.join(map(json.dumps,events)))

    def test_dsh_requires_terminal_completion(self):
        with self.assertRaises(ValueError):
            normalize_result('dsh',json.dumps({'type':'final','text':json.dumps(REPORT)}))

    def test_antigravity_vendor_status_is_separate_from_task_status(self):
        envelope={'conversation_id':'g1','status':'SUCCESS','response':json.dumps(REPORT)}
        r=normalize_result('antigravity',json.dumps(envelope))
        self.assertEqual(r['session_id'],'g1')
        envelope['status']='ERROR'
        with self.assertRaises(ValueError): normalize_result('antigravity',json.dumps(envelope))

    def test_antigravity_denial_is_needs_input_not_false_completion(self):
        envelope={'conversation_id':'g1','status':'SUCCESS','response':'','denied_actions':[{'action':'command'}]}
        self.assertEqual(normalize_result('antigravity',json.dumps(envelope))['status'],'needs_input')

    def test_antigravity_prefers_canonical_output_over_narrative_response(self):
        envelope={'conversation_id':'g1','status':'SUCCESS',
                  'response':'File confirmed - now returning the report. '+json.dumps(REPORT),
                  'structured_output':REPORT}
        events=[{'event':'init','conversation_id':'g1'},{'event':'result','result':envelope}]
        result=normalize_result('antigravity','\n'.join(map(json.dumps,events)))
        self.assertEqual(result,dict(REPORT,executor='antigravity',session_id='g1'))
        # A parseable response also cannot override the authoritative object.
        envelope['response']=json.dumps(dict(REPORT,status='failed',summary='legacy text'))
        self.assertEqual(normalize_result('antigravity',json.dumps(envelope))['status'],'complete')

    def test_antigravity_missing_or_null_canonical_output_uses_legacy_response(self):
        envelope={'conversation_id':'g1','status':'SUCCESS','response':json.dumps(REPORT)}
        for extra in ({},{'structured_output':None}):
            with self.subTest(extra=extra):
                self.assertEqual(normalize_result('antigravity',json.dumps(dict(envelope,**extra)))['status'],'complete')

    def test_antigravity_invalid_canonical_output_cannot_hide_behind_valid_response(self):
        for canonical in ({},[],False,123,'',dict(REPORT,status='unknown'),dict(REPORT,artifacts='result.md')):
            with self.subTest(canonical=canonical):
                envelope={'conversation_id':'g1','status':'SUCCESS','response':json.dumps(REPORT),
                          'structured_output':canonical}
                with self.assertRaises(ValueError):
                    normalize_result('antigravity',json.dumps(envelope))

    def test_antigravity_vendor_error_with_valid_canonical_output_still_fails(self):
        envelope={'conversation_id':'g1','status':'ERROR','response':json.dumps(REPORT),
                  'structured_output':REPORT}
        with self.assertRaisesRegex(ValueError,'did not report SUCCESS'):
            normalize_result('antigravity',json.dumps(envelope))

    def test_antigravity_canonical_output_does_not_bypass_existing_denial_guard(self):
        envelope={'conversation_id':'g1','status':'SUCCESS','response':'',
                  'denied_actions':[{'action':'command'}]}
        for canonical in (None,REPORT):
            with self.subTest(canonical=canonical):
                result=normalize_result('antigravity',json.dumps(dict(envelope,structured_output=canonical)))
                self.assertEqual(result['status'],'needs_input')
                self.assertEqual(result['artifacts'],[])

    def test_zcode_summary(self):
        r=normalize_result('zcode',json.dumps({'sessionId':'z1','response':json.dumps(REPORT)}))
        self.assertEqual(r['session_id'],'z1')
        self.assertEqual(r['artifacts'],['result.md'])

    def test_outer_sandbox_has_workspace_profile(self):
        argv=sandbox_command(['codex.exe'],['other.exe','literal;$(evil)'],'C:/work','unelevated')
        self.assertEqual(argv[-3:],['--','other.exe','literal;$(evil)'])
        self.assertIn(':workspace',argv)
        self.assertNotIn('danger-full-access',' '.join(argv))

    def test_runtime_inside_literal_bracket_workspace_needs_no_extra_path_grant(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace=Path(directory)/'[Task] 工作区';runtime=workspace/'.runtime'
            runtime.mkdir(parents=True)
            external=Path(directory)/'vendor-cache';external.mkdir()
            argv=sandbox_command(['codex.exe'],['worker'],workspace,state_roots=[runtime,external])
            profile=next(value for value in argv if value.startswith('permissions.dispatcher='))
            self.assertNotIn('.runtime',profile)
            self.assertIn(str(external).replace('\\','/'),profile)
            self.assertIn('network={enabled=true}',profile)
            self.assertEqual(argv[argv.index('-C')+1],str(workspace))


if __name__=='__main__': unittest.main()
