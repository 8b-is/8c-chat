import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import commons
import hosted


class CommonsTests(unittest.TestCase):
    def test_paths_symlinks_and_artifact_limits(self):
        with tempfile.TemporaryDirectory(dir='/tmp/opencode') as directory:
            root = Path(directory)
            for path in ('../outside', '/etc/passwd', '.git/config', 'src/../secret'):
                with self.assertRaises(ValueError):
                    commons.file_at(root, path)
            (root/'src').symlink_to('/tmp/opencode', target_is_directory=True)
            with self.assertRaises(ValueError):
                commons.tool(root, {'tool': 'write', 'path': 'src/escape', 'text': 'no'})
            commons.tool(root, {'tool': 'write', 'path': 'artifacts/hello.txt', 'text': 'Hello 💙'})
            result = commons.artifacts(root)['artifacts/hello.txt']
            self.assertEqual(result['sha256'], commons.sha('Hello 💙'.encode()))
            with self.assertRaises(ValueError):
                commons.tool(root, {'tool': 'write', 'path': 'reference/override', 'text': 'no'})

    def test_actual_command_sandbox_hides_home_and_protects_shared_files(self):
        with tempfile.TemporaryDirectory(dir='/tmp/opencode') as directory:
            root = Path(directory)
            (root/'shared').mkdir()
            (root/'shared'/'peer.txt').write_text('keep')
            result = commons.run(root, 'test ! -e /home/hue/.ssh && test -z "$WORKER_KEY" && printf hello > result.txt')
            self.assertEqual(result['exit_code'], 0, result)
            self.assertEqual((root/'result.txt').read_text(), 'hello')
            result = commons.run(root, 'printf overwrite > shared/peer.txt')
            self.assertNotEqual(result['exit_code'], 0)
            self.assertEqual((root/'shared'/'peer.txt').read_text(), 'keep')

    def test_budget_pause_has_no_technical_error_text(self):
        self.assertTrue(hosted.budget_error({'error': 'insufficient_quota'}))
        self.assertTrue(hosted.budget_error({'error': 'RESOURCE_EXHAUSTED'}))
        self.assertFalse(hosted.budget_error({'error': 'file not found'}))
        self.assertIn('Pricey, but worth it', hosted.RESTING)

    def test_local_tool_replay_does_not_repeat_a_command(self):
        with tempfile.TemporaryDirectory(dir='/tmp/opencode') as directory:
            agent = {'id':'aye', 'name':'Aye', 'runtime':'ollama', 'model':'aye:latest'}
            bridge = hosted.Bridge({'agents':[agent]}, directory)
            job = {'id':1, 'lease':'local-test', 'room':'tokyo-lab'}
            body = {**agent, 'agent':'aye', 'job':job}
            try:
                with patch.object(hosted,'request',return_value={'total_capsules':5}):
                    bridge.handle('/workspace-start',body)
                job.update(sequence=0, action={'tool':'run','command':'printf x >> counter.txt'})
                first = bridge.handle('/workspace-tool',body)
                self.assertEqual(first,bridge.handle('/workspace-tool',body))
                self.assertEqual((Path(directory)/'aye'/'counter.txt').read_text(),'x')
                job['action']['command']='printf y >> counter.txt'
                with self.assertRaises(ValueError):bridge.handle('/workspace-tool',body)
            finally:bridge.lock.close()

    def test_hosted_receipt_replay_and_shared_artifacts_require_acceptance(self):
        with tempfile.TemporaryDirectory(dir='/tmp/opencode') as directory:
            agent = {'id': 'astra', 'name': 'Astra', 'runtime': 'opencode', 'provider': 'openai', 'model': 'gpt-6-astra', 'binary': '/usr/bin/opencode'}
            bridge = hosted.Bridge({'agents': [agent]}, directory)
            try:
                job = {'id': 2, 'lease': 'test', 'room': 'model-lab', 'system': '', 'prompt': 'hello'}
                body = {**agent, 'agent': 'astra', 'job': job}
                def generate(a, prompt):
                    commons.tool(Path(a['workspace']), {'tool': 'write', 'path': 'artifacts/test.txt', 'text': 'one artifact'})
                    return 'Hello', 'ses_test', 4, [{'name': 'patch', 'status': 'completed'}], None
                with patch.object(hosted, 'request', return_value={'total_capsules': 5}), patch.object(hosted, 'opencode', side_effect=generate) as model:
                    result = bridge.handle('/infer', body)
                    self.assertEqual(result, bridge.handle('/infer', body))
                    self.assertEqual(model.call_count, 1)
                self.assertFalse((Path(directory)/'shared').exists())
                bridge.handle('/accept', body)
                bridge.handle('/accept', body)
                self.assertEqual(len(list((Path(directory)/'shared'/'astra').glob('*.json'))), 1)
            finally:
                bridge.lock.close()
