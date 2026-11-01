import json
from contextlib import contextmanager
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import commons
import hosted
from memory import Memory, MemoryDeadline, MemoryFault
import worker


AGENT = {'id': 'aye', 'name': 'Aye', 'runtime': 'ollama', 'model': 'local:latest',
         'endpoint': 'http://127.0.0.1:11435', 'workbench': 'http://127.0.0.1:8799'}


class TurnSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir='/tmp/opencode')
        self.root = Path(self.tmp.name)
        self.bridge = hosted.Bridge({'agents': [AGENT]}, self.root)
        self.job = {'agent': 'aye', 'runtime': AGENT['runtime'], 'model': AGENT['model'],
                    'id': 1, 'lease': 'first', 'room': 'model-lab', 'digest': 'a'*64,
                    'system': '', 'prompt': 'Run a command to write counter.txt.',
                    'query': 'Run a command to write counter.txt.'}

    def tearDown(self):
        self.bridge.lock.close()
        self.tmp.cleanup()

    def call(self, path, job=None):
        return self.bridge.handle(path, {**AGENT, 'agent': AGENT['id'], 'job': job or self.job})

    def start(self):
        with patch.object(hosted, 'request', return_value={}):
            return self.call('/workspace-start')

    def action(self, command='printf x >> counter.txt'):
        self.job.update(sequence=0, action={'tool': 'run', 'command': command},
                        work_deadline_ns=time.monotonic_ns()+60_000_000_000)
        return self.call('/workspace-tool')

    def test_missing_expired_queued_or_excessive_deadlines_cannot_start_tools(self):
        self.start()
        clock = time.monotonic_ns()
        self.job.update(sequence=0, action={'tool': 'run', 'command': 'must-not-run'})
        for deadline in (None, True, 'later', clock-1, clock+24_000_000_000, clock+101_000_000_000):
            with self.subTest(deadline_type=type(deadline).__name__):
                self.job['work_deadline_ns'] = deadline
                with patch.object(hosted, 'tool') as tool:
                    with self.assertRaisesRegex(ValueError, 'deadline'):
                        self.call('/workspace-tool')
                    tool.assert_not_called()
                state = json.loads((self.bridge.state/(self.bridge.key(AGENT, self.job)+'-work.json')).read_text())
                self.assertEqual(state['actions'], [])

    def test_new_lease_recovers_observed_effects_without_model_or_more_actions(self):
        self.start()
        original = self.action()
        original_key = self.bridge.key(AGENT, self.job)
        self.job['lease'] = 'retry'
        self.assertEqual(self.call('/workspace-tool'), original)
        self.job.update(sequence=1, action={'tool': 'run', 'command': 'printf y >> counter.txt'})
        with self.assertRaisesRegex(hosted.RecoveryRequired, 'retry lease'):
            self.call('/workspace-tool')
        def dispatch(agent, path, job=None, **kwargs):
            return self.call(path, job)
        with patch.object(worker, 'inventory', return_value='a'*64), \
             patch.object(worker, 'bridge', side_effect=dispatch), patch.object(worker, 'request') as model:
            generation = worker.infer(AGENT, self.job)
            model.assert_not_called()
        self.assertEqual((self.root/'aye'/'counter.txt').read_text(), 'x')
        self.assertIn('exited with code 0', generation['text'])
        self.assertEqual(self.bridge.key(AGENT, self.job), original_key)
        self.job['lease'] = 'third'
        self.assertEqual(self.call('/workspace-start')['generation'], generation)
        self.call('/accept')
        self.assertEqual(len(list((self.root/'shared'/'aye').glob('*.json'))), 1)
        self.assertEqual(self.bridge.read_anchor(AGENT, self.job)['state'], 'accepted')

    def test_uncertain_effect_blocks_every_retry_without_reexecution(self):
        self.start()
        with patch.object(hosted, 'tool', side_effect=OSError('lost response')):
            with self.assertRaises(OSError):
                self.action()
        for lease in ('first', 'second', 'third'):
            self.job['lease'] = lease
            with patch.object(hosted, 'tool') as tool:
                with self.assertRaisesRegex(hosted.RecoveryRequired, 'Uncertain'):
                    self.call('/workspace-start')
                with self.assertRaises(hosted.RecoveryRequired):
                    self.call('/workspace-tool')
                tool.assert_not_called()

    def test_unmigrated_legacy_receipts_block_new_jobs(self):
        self.bridge.lock.close()
        (self.bridge.state/'anchors-version.json').unlink()
        worker.save(self.bridge.state/('b'*64+'-work.json'), {'actions': [], 'before': {}, 'context': ''})
        self.bridge = hosted.Bridge({'agents': [AGENT]}, self.root)
        with self.assertRaisesRegex(hosted.RecoveryRequired, 'migration'):
            self.start()
        self.assertEqual(list(self.bridge.anchors.iterdir()), [])

    def test_changed_job_route_cannot_reuse_an_anchor(self):
        self.start()
        self.job.update(lease='retry', room='hot-tub')
        with self.assertRaisesRegex(hosted.RecoveryRequired, 'identity conflict'):
            self.start()

    def test_command_timeout_preserves_partial_effects_and_kills_tree(self):
        result = commons.run(self.root/'aye', 'printf before > partial.txt; sleep 10; printf after >> partial.txt',
                             time.monotonic_ns()+2_000_000_000)
        self.assertTrue(result['timed_out'])
        self.assertNotEqual(result['exit_code'], 0)
        self.assertEqual((self.root/'aye'/'partial.txt').read_text(), 'before')
        self.assertIn('partial effects may remain', worker.work_report([({'tool': 'run', 'command': 'bounded test'}, result)]))

    def test_stale_delivery_retains_spool_without_network_call(self):
        pending = self.root/'pending-result.json'
        worker.save(pending, {'id': 1, 'lease': 'old'})
        with patch.object(worker, 'request') as request:
            self.assertFalse(worker.deliver('https://8c.is', 'k'*32, pending, deadline=time.monotonic_ns()-1))
            request.assert_not_called()
        self.assertTrue(pending.exists())

    def test_hosted_response_deadline_reserves_cleanup(self):
        agent = {'id': 'astra', 'name': 'Astra', 'runtime': 'opencode', 'provider': 'openai', 'model': 'hosted-model'}
        with tempfile.TemporaryDirectory(dir='/tmp/opencode') as directory:
            bridge = hosted.Bridge({'agents': [agent]}, directory)
            now = time.monotonic_ns()
            job = {**self.job, 'inference_deadline_ns': now+40_000_000_000}
            def infer(a, prompt):
                self.assertLessEqual(a['turn_deadline_ns'], job['inference_deadline_ns']-12_000_000_000)
                self.assertEqual(a['response_deadline_ns'], job['inference_deadline_ns'])
                return ('A bounded reply.', 'ses_test', 10, [], None)
            try:
                with patch.object(hosted, 'request', return_value={}), patch.object(hosted, 'opencode', side_effect=infer) as model:
                    first = bridge.handle('/infer', {**agent, 'agent': 'astra', 'job': job})
                    job['lease'] = 'retry'
                    self.assertEqual(bridge.handle('/infer', {**agent, 'agent': 'astra', 'job': job}), first)
                    self.assertEqual(model.call_count, 1)
            finally:
                bridge.lock.close()


class LeaseBudgetTests(unittest.TestCase):
    def test_recall_inference_persistence_share_the_claim_budget(self):
        clock = [12_000_000_000]  # Polling already consumed twelve seconds.
        scopes = []
        class FakeMemory:
            @contextmanager
            def until(self, deadline):
                scopes.append(deadline)
                yield
            def prepare(self, job):
                clock[0] += 10_000_000_000
                return dict(job), {'refs': []}
            def remember_generation(self, agent, job, generated, receipt):
                clock[0] = 120_000_000_000
                return ['native-record']
        def sync(*args):
            clock[0] += 8_000_000_000
            return True
        def child(*args, **kwargs):
            payload = json.loads(kwargs['input'])
            self.assertEqual(payload['job']['inference_deadline_ns'], 95_000_000_000)
            self.assertEqual(kwargs['timeout'], 65)
            clock[0] = 94_000_000_000
            return SimpleNamespace(stdout=json.dumps({'text': 'Hello', 'digest': 'a'*64}))
        with patch.object(worker.time, 'monotonic_ns', side_effect=lambda: clock[0]), \
             patch.object(worker, 'sync_memory', side_effect=sync), patch.object(worker.subprocess, 'run', side_effect=child):
            result = worker.generate_job(AGENT, {'id': 1}, FakeMemory(), 'https://8c.is', 'k'*32, 0)
        self.assertEqual(scopes, [95_000_000_000, 128_000_000_000])
        self.assertEqual(result['memory']['written'], ['native-record'])
        self.assertEqual(worker.LEASE_BUDGET_NS-clock[0], 25_000_000_000)

    def test_exhausted_recall_cannot_start_inference(self):
        class FakeMemory:
            @contextmanager
            def until(self, deadline):
                yield
            def prepare(self, job):
                return dict(job), {}
        with patch.object(worker.time, 'monotonic_ns', return_value=100_000_000_000), \
             patch.object(worker, 'sync_memory', return_value=True), patch.object(worker.subprocess, 'run') as child:
            with self.assertRaises(subprocess.TimeoutExpired):
                worker.generate_job(AGENT, {'id': 1}, FakeMemory(), 'https://8c.is', 'k'*32, 0)
            child.assert_not_called()

    def test_native_deadline_is_bounded_restored_and_not_corruption(self):
        memory = object.__new__(Memory)
        memory.deadline_ns = None
        memory.binary = Path(sys.executable).resolve()
        memory.binary_stat = memory.binary.stat()
        with patch('memory.time.monotonic_ns', return_value=1_000_000_000):
            with memory.until(3_000_000_000), patch('memory.subprocess.run', return_value=SimpleNamespace(stdout='{}')) as run:
                memory.native('info')
                self.assertEqual(run.call_args.kwargs['timeout'], 2)
                with memory.until(1_000_000_001), self.assertRaises(MemoryDeadline):
                    memory.native('info')
                self.assertEqual(memory.deadline_ns, 3_000_000_000)
            with memory.until(3_000_000_000), patch('memory.subprocess.run', side_effect=subprocess.TimeoutExpired('aurora', 2)):
                with self.assertRaises(MemoryDeadline) as error:
                    memory.native('info')
                self.assertNotIsInstance(error.exception, MemoryFault)
        self.assertIsNone(memory.deadline_ns)


if __name__ == '__main__':
    unittest.main()
