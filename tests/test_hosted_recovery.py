import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import hosted
import worker

SERVER = {'pid':836, 'url':'http://127.0.0.1:49375'}
INFO = {'pid':836, 'urls':[SERVER['url']]}
OWNED = {'session':'ses_owned', 'server':SERVER}
AGENT = {'id':'astra', 'name':'Astra', 'runtime':'opencode', 'provider':'openai',
         'model':'gpt-6-astra', 'binary':'/usr/bin/opencode'}


class HostedRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir='/tmp/opencode')
        self.root = Path(self.tmp.name)
        self.active = self.root/'active.json'
        self.agent = {**AGENT, 'workspace':str(self.root), 'active_file':str(self.active)}
        worker.save(self.active, OWNED)

    def tearDown(self):
        self.tmp.cleanup()

    def api(self, interrupt=False, active=None, info=INFO):
        def dispatch(agent, method, path, body=None, timeout=20):
            self.assertEqual(agent['opencode_server'], SERVER['url'])
            self.assertLessEqual(timeout, 5)
            if path == '/api/info':
                return info
            if path.endswith('/interrupt'):
                self.assertEqual(path, '/api/session/ses_owned/interrupt')
                if isinstance(interrupt, Exception):
                    raise interrupt
                return interrupt if isinstance(interrupt, dict) else {'interrupted':interrupt}
            if path == '/api/session/active':
                if isinstance(active, Exception):
                    raise active
                return active if active is not None else {'data':{}}
            self.fail('Recovery must not prompt or create sessions: ' + path)
        return dispatch

    def test_idle_noop_and_confirmed_interrupt_clear_only_owned_receipt(self):
        for interrupted in (False, True):
            with self.subTest(interrupted=interrupted):
                worker.save(self.active, OWNED)
                unrelated = {'data':{'ses_unrelated':{'type':'running'}}}
                with patch.object(hosted, 'api', side_effect=self.api(interrupted, unrelated)) as api:
                    hosted.recover_active(self.agent)
                self.assertFalse(self.active.exists())
                self.assertEqual([c.args[2] for c in api.call_args_list], [
                    '/api/info', '/api/session/ses_owned/interrupt', '/api/session/active', '/api/info'])

    def test_running_or_unverifiable_session_retains_exact_receipt(self):
        cases = [
            (False, {'data':{'ses_owned':{'type':'running'}}}),
            (True, {'data':{'ses_owned':{'type':'running'}}}),
            (False, OSError('status unavailable')),
            (False, {}), (False, {'data':[]}),
            (False, {'data':{'ses_unrelated':{'type':'unknown'}}}),
            ({'interrupted':'false'}, {'data':{}}),
            ({'data':{'interrupted':False}}, {'data':{}}),
            (subprocess.TimeoutExpired('interrupt', 5), OSError('status unavailable')),
        ]
        for interrupted, state in cases:
            with self.subTest(interrupted=interrupted, state=state):
                before = self.active.read_bytes()
                with patch.object(hosted, 'api', side_effect=self.api(interrupted, state)):
                    with self.assertRaises((ValueError, OSError, subprocess.TimeoutExpired)):
                        hosted.recover_active(self.agent)
                self.assertEqual(self.active.read_bytes(), before)

    def test_failed_interrupt_can_recover_only_with_fresh_confirmed_absence(self):
        with patch.object(hosted, 'api', side_effect=self.api(OSError('lost acknowledgement'))):
            hosted.recover_active(self.agent)
        self.assertFalse(self.active.exists())

    def test_changed_server_and_legacy_receipts_require_operator_recovery(self):
        with patch.object(hosted, 'api', side_effect=self.api(info={**INFO, 'pid':999})) as api:
            with self.assertRaisesRegex(ValueError, 'process changed'):
                hosted.recover_active(self.agent)
            self.assertEqual(api.call_count, 1)
        self.assertEqual(json.loads(self.active.read_text()), OWNED)
        worker.save(self.active, {'session':'ses_owned'})
        with patch.object(hosted, 'api') as api:
            with self.assertRaisesRegex(ValueError, 'operator recovery'):
                hosted.recover_active(self.agent)
            api.assert_not_called()
        self.assertTrue(self.active.exists())

    def test_server_restart_during_snapshot_does_not_release_ownership(self):
        responses = [INFO, {'interrupted':False}, {'data':{}}, {**INFO, 'pid':999}]
        with patch.object(hosted, 'api', side_effect=responses):
            with self.assertRaisesRegex(ValueError, 'changed during recovery'):
                hosted.recover_active(self.agent)
        self.assertEqual(json.loads(self.active.read_text()), OWNED)

    def test_opencode_adapter_does_not_overwrite_uncertain_receipt(self):
        with patch.object(hosted, 'api', side_effect=self.api(False, {'data':{'ses_owned':{'type':'running'}}})):
            with self.assertRaisesRegex(ValueError, 'quarantined'):
                hosted.opencode(self.agent, 'New work must not start')
        self.assertEqual(json.loads(self.active.read_text()), OWNED)

    def test_same_bridge_blocks_new_lease_then_resumes_after_confirmed_recovery(self):
        bridge = hosted.Bridge({'agents':[AGENT]}, self.root/'commons')
        try:
            owned = Path(bridge.agents['astra']['active_file'])
            worker.save(owned, OWNED)
            job = {'id':2, 'lease':'fresh', 'room':'model-lab', 'system':'', 'prompt':'hello'}
            body = {'agent':'astra', 'model':AGENT['model'], 'runtime':'opencode', 'job':job}
            with patch.object(hosted, 'api', side_effect=self.api(False, {'data':{'ses_owned':{'type':'running'}}})), \
                 patch.object(hosted, 'opencode') as generate, patch.object(bridge, 'prepare') as prepare:
                with self.assertRaisesRegex(ValueError, 'quarantined'):
                    bridge.handle('/infer', body)
                generate.assert_not_called()
                prepare.assert_not_called()
            self.assertEqual(json.loads(owned.read_text()), OWNED)
            self.assertFalse((bridge.state/(bridge.key(AGENT,job)+'-attempt.json')).exists())
            with patch.object(hosted, 'api', side_effect=self.api(False)), \
                 patch.object(hosted, 'request', return_value={}), \
                 patch.object(hosted, 'opencode', return_value=('Hello', 'ses_new', 3, [], None)) as generate:
                self.assertEqual(bridge.handle('/infer', body)['text'], 'Hello')
                generate.assert_called_once()
            self.assertFalse(owned.exists())
        finally:
            bridge.lock.close()

    def test_restart_does_not_rewrite_quarantined_workspace_or_block_peers(self):
        root = self.root/'commons'
        (root/'astra').mkdir(parents=True)
        (root/'astra'/'AGENTS.md').write_text('Preserve while prior owner is unresolved')
        (root/'receipts').mkdir()
        active = root/'receipts'/'active-astra.json'
        worker.save(active, OWNED)
        local = {'id':'aye', 'name':'Aye', 'runtime':'ollama', 'model':'local:latest'}
        with patch.object(hosted, 'api') as api:
            bridge = hosted.Bridge({'agents':[AGENT, local]}, root)
            api.assert_not_called()
        try:
            self.assertEqual((root/'astra'/'AGENTS.md').read_text(), 'Preserve while prior owner is unresolved')
            body = {'agent':'aye', 'runtime':'ollama', 'model':local['model'],
                    'job':{'id':3, 'lease':'local', 'room':'model-lab'}}
            with patch.object(hosted, 'request', return_value={}):
                self.assertIn('context', bridge.handle('/workspace-start', body))
            self.assertEqual(json.loads(active.read_text()), OWNED)
        finally:
            bridge.lock.close()

    def test_api_commands_use_pinned_server_and_private_connection_reference(self):
        service = self.root/'service.json'
        worker.save(service, {'url':SERVER['url'], 'pid':SERVER['pid'], 'password':'test-loopback-password'})
        agent = {**self.agent, 'opencode_server':SERVER['url'], 'opencode_pid':SERVER['pid'],
                 'opencode_service_file':str(service)}
        with patch.object(hosted, 'command', return_value='{"data":{}}') as command:
            hosted.api(agent, 'get', '/api/session/active')
        self.assertEqual(command.call_args.args[0], [
            '/usr/bin/opencode', 'api', 'get', '/api/session/active', '--server', SERVER['url']])
        self.assertNotIn('test-loopback-password', str(command.call_args.args))
        self.assertEqual(command.call_args.kwargs['connection_env'], {'OPENCODE_PASSWORD':'test-loopback-password'})
        for changed in ({'pid':999}, {'url':'http://127.0.0.1:12345'}, {'url':'https://example.com'}):
            worker.save(service, {'url':SERVER['url'], 'pid':SERVER['pid'], 'password':'test-loopback-password', **changed})
            with patch.object(hosted, 'command') as command:
                with self.assertRaises(ValueError):
                    hosted.api(agent, 'get', '/api/session/active')
                command.assert_not_called()

    def test_api_rejects_mismatched_live_process_without_discovery_fallback(self):
        service = self.root/'service.json'
        worker.save(service, {'url':SERVER['url'], 'pid':SERVER['pid'], 'password':'test-loopback-password'})
        with patch.object(hosted, 'command', return_value=json.dumps({**INFO, 'pid':999})):
            with self.assertRaisesRegex(ValueError, 'does not match'):
                hosted.api({**self.agent, 'opencode_service_file':str(service)}, 'get', '/api/info')
