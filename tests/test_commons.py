import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import subprocess
import time
import commons
import hosted
import worker


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
                job.update(work_deadline_ns=time.monotonic_ns()+60_000_000_000, sequence=0, action={'tool':'run','command':'printf x >> counter.txt'})
                first = bridge.handle('/workspace-tool',body)
                self.assertEqual(first,bridge.handle('/workspace-tool',body))
                self.assertEqual((Path(directory)/'aye'/'counter.txt').read_text(),'x')
                job['action']['command']='printf y >> counter.txt'
                with self.assertRaises(ValueError):bridge.handle('/workspace-tool',body)
            finally:bridge.lock.close()

    def test_finalized_local_turn_replays_entire_generation_and_rejects_more_tools(self):
        with tempfile.TemporaryDirectory(dir='/tmp/opencode') as directory:
            agent = {'id':'aye', 'name':'Aye', 'runtime':'ollama', 'model':'aye:latest',
                     'endpoint':'http://127.0.0.1:11435', 'workbench':'http://127.0.0.1:8799'}
            bridge = hosted.Bridge({'agents':[agent]}, directory)
            job = {'id':1, 'lease':'finalized', 'room':'tokyo-lab', 'agent':'aye',
                   'runtime':'ollama', 'model':agent['model'], 'digest':'a'*64,
                   'system':'', 'prompt':'Write artifacts/first.txt.'}
            body = {'agent':'aye', 'runtime':'ollama', 'model':agent['model'], 'job':job}
            try:
                with patch.object(hosted, 'request', return_value={}):
                    bridge.handle('/workspace-start', body)
                job.update(work_deadline_ns=time.monotonic_ns()+60_000_000_000, sequence=0, action={'tool':'write', 'path':'artifacts/first.txt', 'text':'first'})
                bridge.handle('/workspace-tool', body)
                job['generation'] = {'text':'Wrote the first file.', 'digest':'a'*64,
                                     'elapsed_ms':50, 'output_tokens':12}
                original = bridge.handle('/workspace-result', body)
                job['generation'] = {'text':'A different story.', 'digest':'a'*64,
                                     'elapsed_ms':99, 'output_tokens':30}
                self.assertEqual(bridge.handle('/workspace-result', body), original)
                with patch.object(bridge, 'prepare') as prepare:
                    self.assertEqual(bridge.handle('/workspace-start', body), original)
                    prepare.assert_not_called()
                job.update(sequence=1, action={'tool':'write', 'path':'artifacts/second.txt', 'text':'second'})
                with self.assertRaisesRegex(ValueError, 'Finalized'):
                    bridge.handle('/workspace-tool', body)
                self.assertFalse((Path(directory)/'aye'/'artifacts'/'second.txt').exists())
                def dispatch(a, path, j=None, **kwargs):
                    return bridge.handle(path, {**body, 'job':j})
                with patch.object(worker, 'inventory', return_value='a'*64), \
                     patch.object(worker, 'bridge', side_effect=dispatch), patch.object(worker, 'request') as model:
                    replayed = worker.infer(agent, job)
                    model.assert_not_called()
                self.assertEqual(replayed, original['generation'])
                bridge.handle('/accept', body)
                peer = json.loads(next((Path(directory)/'shared'/'aye').glob('*.json')).read_text())
                self.assertEqual(peer['text'], replayed['text'])
                self.assertEqual(peer['artifacts'], replayed['work']['artifacts'])
            finally:
                bridge.lock.close()

    def test_uncertain_local_action_cannot_be_finalized(self):
        with tempfile.TemporaryDirectory(dir='/tmp/opencode') as directory:
            agent = {'id':'aye', 'name':'Aye', 'runtime':'ollama', 'model':'aye:latest'}
            bridge = hosted.Bridge({'agents':[agent]}, directory)
            job = {'id':1, 'lease':'uncertain', 'room':'tokyo-lab'}
            body = {'agent':'aye', 'runtime':'ollama', 'model':agent['model'], 'job':job}
            try:
                with patch.object(hosted, 'request', return_value={}):
                    bridge.handle('/workspace-start', body)
                job.update(work_deadline_ns=time.monotonic_ns()+60_000_000_000, sequence=0, action={'tool':'run', 'command':'test command'})
                with patch.object(hosted, 'tool', side_effect=OSError('connection lost')):
                    with self.assertRaises(OSError):
                        bridge.handle('/workspace-tool', body)
                job['generation'] = {'text':'Done', 'digest':'a'*64, 'elapsed_ms':1, 'output_tokens':1}
                with self.assertRaisesRegex(ValueError, 'Uncertain'):
                    bridge.handle('/workspace-result', body)
                self.assertFalse((bridge.state/(bridge.key(agent, job)+'.json')).exists())
            finally:
                bridge.lock.close()

    def test_local_correction_keeps_action_sequence_and_publishes_only_say_text(self):
        with tempfile.TemporaryDirectory(dir='/tmp/opencode') as directory:
            agent = {'id':'cheet', 'name':'Cheet', 'runtime':'ollama', 'model':'local:latest',
                     'endpoint':'http://127.0.0.1:11435', 'workbench':'http://127.0.0.1:8799'}
            bridge = hosted.Bridge({'agents':[agent]}, directory)
            job = {'id':1, 'lease':'corrected', 'room':'model-lab', 'agent':'cheet', 'runtime':'ollama',
                   'model':agent['model'], 'digest':'a'*64, 'system':'Answer the current visitor.',
                   'prompt':'Use the write tool to create artifacts/marker.txt with copper lighthouse.',
                   'query':'Use the write tool to create artifacts/marker.txt with copper lighthouse.'}
            responses = iter([
                {'tool':'say','text':'I created the file.'},  # No observed action: must be corrected.
                {'tool':'write','path':'artifacts/marker.txt','text':'copper lighthouse'},
                {'tool':'read','path':'artifacts/marker.txt','text':''},
                {'tool':'say','text':'Created and read back the copper lighthouse file.'}])
            sequences = []
            def dispatch(a, path, j=None, **kwargs):
                if path == '/workspace-tool': sequences.append(j['sequence'])
                return bridge.handle(path, {'agent':a['id'], 'runtime':a['runtime'], 'model':a['model'], 'job':j})
            def generate(origin, path, body, **kwargs):
                return {'done':True, 'model':agent['model'], 'eval_count':10, 'response':json.dumps(next(responses))}
            try:
                with patch.object(hosted,'request',return_value={}), patch.object(worker,'inventory',return_value='a'*64), \
                     patch.object(worker,'bridge',side_effect=dispatch), patch.object(worker,'request',side_effect=generate) as model:
                    result = worker.infer(agent,job)
                self.assertEqual(sequences,[0,1])
                self.assertIn('Wrote "artifacts/marker.txt" (SHA-256 ', result['text'])
                self.assertIn('Read "artifacts/marker.txt".', result['text'])
                self.assertNotIn('Created and read back', result['text'])
                self.assertEqual(result['work']['artifacts'][0]['text'],'copper lighthouse')
                self.assertEqual(result['output_tokens'],40)
                self.assertEqual(model.call_args.args[2]['format'],worker.tool_format(['say','decline']))
                self.assertFalse((Path(directory)/'shared').exists())
            finally:bridge.lock.close()

    def test_opencode_whole_turn_deadline_interrupts_and_retains_uncertain_session(self):
        with tempfile.TemporaryDirectory(dir='/tmp/opencode') as directory:
            active = Path(directory)/'active.json'
            agent = {'id':'astra', 'name':'Astra', 'provider':'openai', 'model':'gpt-6-astra',
                     'workspace':directory, 'active_file':str(active)}
            for interrupt_fails in (False, True):
                clock = [0]
                timeouts = []
                def api(a, method, path, body=None, timeout=20):
                    if path == '/api/info':
                        return {'pid':836, 'urls':['http://127.0.0.1:49375']}
                    if path == '/api/session/active':
                        if interrupt_fails: raise OSError('status unavailable')
                        return {'data':{}}
                    timeouts.append(timeout)
                    if path == '/api/session':
                        clock[0] += 20_000_000_000
                        return {'data':{'id':'ses_deadline'}}
                    if path.endswith('/prompt'):
                        clock[0] += 20_000_000_000
                    elif path.endswith('/wait'):
                        clock[0] += 46_000_000_000
                    elif path.endswith('/interrupt'):
                        if interrupt_fails: raise subprocess.TimeoutExpired('interrupt',5)
                        return {'interrupted':False}
                    else: self.fail('Transcript retrieval started after the whole-turn deadline')
                with patch.object(hosted.time,'monotonic_ns',side_effect=lambda:clock[0]), patch.object(hosted,'api',side_effect=api):
                    with self.assertRaises(subprocess.TimeoutExpired) as failure: hosted.opencode(agent,'Hello')
                self.assertEqual(failure.exception.cmd, 'OpenCode turn')
                self.assertEqual(timeouts,[20,20,45,5])
                self.assertEqual(active.exists(),interrupt_fails)

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

    def test_safe_host_command_scopes_read_tools_and_autonomous_pr_creation(self):
        self.assertEqual(commons.safe_host_command('g8t-mcp health --json'), ['/usr/local/bin/g8t-mcp', 'health', '--json'])
        self.assertEqual(commons.safe_host_command('cloak-browse info'), ['/usr/local/bin/cloak-browse', 'info'])
        self.assertEqual(commons.safe_host_command('cloak-browse https://8c.is --max-chars 2000'),
                         ['/usr/local/bin/cloak-browse', 'https://8c.is', '--max-chars', '2000'])
        self.assertEqual(commons.safe_host_command('gh pr list -R 8b-is/g8t'), ['/usr/bin/gh', 'pr', 'list', '-R', '8b-is/g8t'])
        for rejected in ('g8t-mcp health; id', 'cloak-browse http://127.0.0.1:49375', 'gh repo delete 8b-is/g8t',
                         'gh pr merge 1', 'gh pr create --head agent/fix --title test'):
            self.assertIsNone(commons.safe_host_command(rejected, autonomous=False), rejected)
        self.assertEqual(commons.safe_host_command('gh pr create --head agent/fix --title test', autonomous=True),
                         ['/usr/bin/gh', 'pr', 'create', '--head', 'agent/fix', '--title', 'test'])
        self.assertIsNone(commons.safe_host_command('gh pr create --head main --title test', autonomous=True))
        self.assertEqual(commons.safe_host_command('agent-worktree list', autonomous=True),
                         ['/data/workspace-admin/bin/agent-worktree', 'list'])

    def test_setup_workspace_configures_mcp_servers_and_autonomous_profile(self):
        with tempfile.TemporaryDirectory(dir='/tmp/opencode') as directory:
            root = Path(directory)
            for name in ('sol', 'grok'):
                agent = {'id': name, 'name': name.title(), 'runtime': 'opencode',
                         'provider': 'openai' if name == 'sol' else 'xai',
                         'model': 'gpt-6.1-sol' if name == 'sol' else 'grok-4.7',
                         'fallback_provider': 'openrouter',
                         'fallback_model': 'openai/gpt-6.1-sol' if name == 'sol' else 'x-ai/grok-4.7'}
                hosted.setup_workspace(agent, root, None)
                cfg = json.loads((root / name / '.opencode' / 'opencode.json').read_text())
                self.assertIn('commons', cfg['agents'])
                self.assertIn('commons-autonomous', cfg['agents'])
                self.assertIn('commons-funded', cfg['agents'])
                if Path('/usr/local/bin/st').exists():
                    self.assertIn('smart-tree', cfg['mcp']['servers'])
                if Path('/usr/local/bin/g8t-mcp').exists():
                    self.assertIn('g8t', cfg['mcp']['servers'])
                if Path('/usr/local/bin/cloak-browse').exists():
                    self.assertIn('cloakbrowser', cfg['mcp']['servers'])
