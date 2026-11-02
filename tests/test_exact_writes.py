import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import commons
import hosted
import worker


class ExactWriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir='/tmp/opencode')
        self.root = Path(self.tmp.name)
        self.agent = {'id':'cheet','name':'Cheet','runtime':'ollama','model':'local:latest',
                      'endpoint':'http://127.0.0.1:11435','workbench':'http://127.0.0.1:8799'}
        self.bridge = hosted.Bridge({'agents':[self.agent]}, self.root)
        self.target = 'artifacts/exact.txt'
        self.literal = 'Orchid / 7'
        self.query = 'Use your write tool to create '+self.target+' containing exactly '+self.literal+'. Briefly tell us what you did.'
        self.job = {'agent':'cheet','runtime':'ollama','model':self.agent['model'],'digest':'a'*64,
                    'id':1,'lease':'first','room':'model-lab','system':'Answer the current visitor.',
                    'query':self.query,'prompt':self.query,'tools_ready':True}
        self.paths, self.payloads = [], []

    def tearDown(self):
        self.bridge.lock.close()
        self.tmp.cleanup()

    def call(self, path, job=None):
        self.paths.append(path)
        return self.bridge.handle(path, {**self.agent,'agent':'cheet','job':job or self.job})

    def infer(self, responses):
        responses = iter(responses)
        def generate(origin, path, body, **kwargs):
            self.payloads.append(body)
            return {'done':True,'model':self.agent['model'],'eval_count':10,'response':json.dumps(next(responses))}
        def dispatch(agent, path, job=None, **kwargs):
            return self.call(path, job)
        with patch.object(hosted,'request',return_value={}), patch.object(worker,'inventory',return_value='a'*64), \
             patch.object(worker,'bridge',side_effect=dispatch), patch.object(worker,'request',side_effect=generate):
            return worker.infer(self.agent,self.job)

    def good(self):
        return {'tool':'write','path':self.target,'text':self.literal}

    def test_wrong_narrative_is_corrected_before_any_write(self):
        bad = {**self.good(),'text':'Greetings, visitor! I made your file.'}
        calls = []
        actual = hosted.tool
        def observe(root, action, deadline):
            calls.append(action)
            self.assertFalse((root/self.target).exists())
            return actual(root,action,deadline)
        with patch.object(hosted,'tool',side_effect=observe):
            result = self.infer([bad,self.good()])
        self.assertEqual(calls,[self.good()])
        self.assertEqual(self.paths.count('/workspace-tool'),1)
        self.assertEqual((self.root/'cheet'/self.target).read_bytes(),self.literal.encode())
        self.assertEqual(result['work']['artifacts'][0]['sha256'],commons.sha(self.literal.encode()))
        self.assertNotIn('Greetings',result['text'])
        self.assertEqual(result['output_tokens'],20)
        schema = self.payloads[0]['format']['oneOf']
        write = next(v for v in schema if v['properties']['tool']['const']=='write')
        self.assertEqual(write['properties']['path'],{'const':self.target})
        self.assertEqual(write['properties']['text'],{'const':self.literal})
        self.assertEqual({v['properties']['tool']['const'] for v in schema},{'write','decline'})

    def test_wrong_path_is_corrected_without_creating_wrong_file(self):
        self.infer([{**self.good(),'path':'artifacts/other.txt'},self.good()])
        self.assertFalse((self.root/'cheet'/'artifacts'/'other.txt').exists())
        self.assertEqual((self.root/'cheet'/self.target).read_text(),self.literal)

    def test_shell_cannot_bypass_an_explicit_write_contract(self):
        self.infer([{'tool':'run','command':'printf wrong > artifacts/exact.txt','text':''},self.good()])
        self.assertEqual(self.paths.count('/workspace-tool'),1)
        self.assertEqual((self.root/'cheet'/self.target).read_text(),self.literal)

    def test_persistent_mismatch_never_reaches_the_workbench(self):
        with patch.object(hosted,'tool') as tool:
            with self.assertRaisesRegex(ValueError,'mismatched'):
                self.infer([{**self.good(),'text':'wrong'}]*4)
            tool.assert_not_called()
        self.assertNotIn('/workspace-tool',self.paths)
        self.assertFalse((self.root/'cheet'/self.target).exists())

    def test_decline_leaves_existing_bytes_untouched(self):
        path = self.root/'cheet'/self.target
        path.write_text('previous accepted artifact')
        result = self.infer([{'tool':'decline','text':'I am leaving this file unchanged.'}])
        self.assertEqual(path.read_text(),'previous accepted artifact')
        self.assertEqual(result['work']['tools'],[])
        self.assertEqual(result['work']['artifacts'],[])

    def test_bridge_independently_checks_bound_contract_before_recording_action(self):
        with patch.object(hosted,'request',return_value={}):
            self.call('/workspace-start')
        self.job.update(sequence=0,action={**self.good(),'text':'wrong'},work_deadline_ns=time.monotonic_ns()+60_000_000_000)
        with patch.object(hosted,'tool') as tool:
            with self.assertRaisesRegex(ValueError,'mismatch'):
                self.call('/workspace-tool')
            tool.assert_not_called()
        state = json.loads((self.bridge.state/(self.bridge.key(self.agent,self.job)+'-work.json')).read_text())
        self.assertEqual(state['actions'],[])
        self.job['action'] = self.good()
        self.call('/workspace-tool')
        self.job['sequence'] = 1
        with self.assertRaisesRegex(ValueError,'mismatch'):
            self.call('/workspace-tool')
        self.assertEqual((self.root/'cheet'/self.target).read_text(),self.literal)

    def test_changing_or_removing_the_bound_query_cannot_weaken_validation(self):
        with patch.object(hosted,'request',return_value={}):
            self.call('/workspace-start')
        self.job.update(sequence=0,action=self.good(),work_deadline_ns=time.monotonic_ns()+60_000_000_000)
        for query in ('Use the write tool.', self.query.replace('Orchid / 7','another value')):
            self.job['query'] = query
            with self.assertRaisesRegex(hosted.RecoveryRequired,'binding changed'):
                self.call('/workspace-tool')
        self.assertFalse((self.root/'cheet'/self.target).exists())

    def test_json_string_preserves_arbitrary_case_spaces_unicode_and_newlines(self):
        literal = '  Azure\nλ, v1.2!  '
        query = 'Create artifacts/literal.txt containing exactly '+json.dumps(literal)+'. Report the result.'
        self.assertEqual(commons.exact_write_contract(query),{'path':'artifacts/literal.txt','text':literal})
        self.literal, self.target = literal, 'artifacts/literal.txt'
        self.job.update(query=query,prompt=query)
        self.infer([self.good()])
        self.assertEqual((self.root/'cheet'/self.target).read_bytes(),literal.encode())
        self.assertEqual(len(self.payloads),1)  # No extra model turn/action after success.

    def test_json_empty_string_is_a_valid_exact_write(self):
        self.assertEqual(commons.exact_write_contract('Write artifacts/empty.txt containing exactly "".'),
                         {'path':'artifacts/empty.txt','text':''})

    def test_ambiguous_literals_and_multiple_contracts_fail_before_inference(self):
        queries = [
            'Create artifacts/a.txt containing exactly one. Two.',
            'Create artifacts/a.txt containing exactly one. Then run another command.',
            'Create artifacts/a.txt containing exactly one and create artifacts/b.txt containing exactly two.',
            'Create artifacts/a.txt containing exactly "unfinished',
            'Create artifacts/../a.txt containing exactly one.',
        ]
        for query in queries:
            with self.subTest(query=query):
                with self.assertRaises(ValueError):
                    commons.exact_write_contract(query)
                self.job.update(query=query,prompt=query)
                with patch.object(worker,'inventory',return_value='a'*64), patch.object(worker,'request') as model, patch.object(worker,'bridge',return_value={'context':''}) as bridge:
                    with self.assertRaises(worker.ParticipantResting):
                        worker.infer(self.agent,self.job)
                    model.assert_not_called()
                    bridge.assert_called_once()  # Seek an immutable receipt before new grammar validation.

    def test_completed_partial_work_recovers_before_new_grammar_validation(self):
        with patch.object(hosted,'request',return_value={}):
            self.call('/workspace-start')
        self.job.update(sequence=0,action=self.good(),work_deadline_ns=time.monotonic_ns()+60_000_000_000)
        self.call('/workspace-tool')
        artifact = self.root/'cheet'/self.target
        before = artifact.stat().st_mtime_ns
        self.job.update(lease='new',query='Create artifacts/exact.txt containing exactly old. Compound legacy syntax.')
        with patch.object(hosted,'tool') as tool:
            result = self.infer([])
            tool.assert_not_called()
        self.assertIn('Observed workbench actions',result['text'])
        self.assertEqual(artifact.read_text(),self.literal)
        self.assertEqual(artifact.stat().st_mtime_ns,before)
        self.assertEqual(self.payloads,[])

    def test_old_finalized_result_remains_immutable_not_silently_repaired(self):
        with patch.object(hosted,'request',return_value={}):
            self.call('/workspace-start')
        old = {'text':'Observed old result','digest':'a'*64,'elapsed_ms':1,'output_tokens':1,
               'work':{'workspace':'8c-commons/cheet','tools':[],'artifacts':[]}}
        path = self.bridge.state/(self.bridge.key(self.agent,self.job)+'.json')
        worker.save(path,old)
        before = path.read_bytes()
        self.job['lease'] = 'new'
        self.job['query'] = 'Create artifacts/exact.txt containing exactly old. Compound syntax no longer supported.'
        self.assertEqual(self.infer([]),old)
        self.assertEqual(path.read_bytes(),before)
        self.assertEqual(self.payloads,[])


if __name__ == '__main__':
    unittest.main()
