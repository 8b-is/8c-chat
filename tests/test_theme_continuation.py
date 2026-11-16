"""Combined theme/continuation regressions; no network, model or workspace effects."""
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import reply_protocol as protocol
import worker


class ThemeContinuationTests(unittest.TestCase):
    def setUp(self):
        self.agent = {'id': 'aye', 'name': 'Aye', 'runtime': 'ollama', 'model': 'local:test',
                      'endpoint': 'http://127.0.0.1:1', 'workbench': 'http://127.0.0.1:2'}
        self.job = {'id': 1, 'lease': 'first', 'room': 'model-lab', 'agent': 'aye',
                    'runtime': 'ollama', 'model': 'local:test', 'digest': 'a' * 64,
                    'system': 'Answer honestly.', 'prompt': 'Quoted room context.', 'query': 'Hello.'}
        self.cap = {'presets': list(protocol.PRESETS)}

    def infer(self, responses, changes=None, start=None, finalize=None):
        responses = iter(responses)
        self.paths, self.payloads, self.generations, self.actions = [], [], [], []
        def bridge(agent, path, job, **kwargs):
            self.paths.append(path)
            self.assertGreater(kwargs['timeout'], 0)
            if path == '/workspace-start':
                return start if start is not None else {'context': 'Workspace quotation.'}
            if path == '/workspace-result':
                self.generations.append(job['generation'])
                saved = {**job['generation'], 'work': {'tools': [], 'artifacts': []}}
                return {'generation': finalize(saved) if finalize else saved}
            if path == '/workspace-tool':
                self.actions.append(job['action'])
                self.assertGreater(job['work_deadline_ns'], time.monotonic_ns())
                return {'result': {'files': [], 'path': job['action'].get('path'), 'sha256': 'b' * 64}}
            self.fail('Unexpected bridge path ' + path)
        def generate(origin, path, payload, **kwargs):
            self.payloads.append(payload)
            self.assertTrue(kwargs['inference'])
            self.assertGreater(kwargs['timeout'], 0)
            response = next(responses)
            return {'done': True, 'model': self.agent['model'], 'eval_count': 3, 'response': response}
        with patch.object(worker, 'inventory', return_value='a' * 64), \
             patch.object(worker, 'bridge', side_effect=bridge), \
             patch.object(worker, 'request', side_effect=generate):
            return worker.infer(self.agent, {**self.job, **(changes or {})})

    def test_theme_lane_has_no_workspace_schema_even_when_query_mentions_tools(self):
        result = self.infer(['{"tool":"set_room_theme","preset":"violet-night-v1"}'],
                            {'query': 'Set the room theme and inspect src/example with your tool.',
                             'room_theme_capability': self.cap})
        self.assertEqual(result['room_theme'], {'preset': 'violet-night-v1'})
        self.assertEqual(self.actions, [])
        names = [v['properties']['tool']['const'] for v in self.payloads[0]['format']['oneOf']]
        self.assertEqual(names, ['say', 'decline', 'set_room_theme'])
        self.assertNotIn('CONVERSATION ONLY', self.payloads[0]['system'])
        self.assertNotIn('cannot directly change this website', self.payloads[0]['system'])
        self.assertNotIn('Design suggestion only', result['text'])
        self.assertTrue(protocol.checked_public(result))

    def test_theme_decline_is_not_forced_to_perform_filesystem_work(self):
        result = self.infer(['{"tool":"decline","text":"I will leave the setting unchanged."}'],
                            {'query': 'Set room theme to violet.', 'room_theme_capability': self.cap})
        self.assertNotIn('room_theme', result)
        self.assertEqual(self.actions, [])

    def test_no_browse_query_negation_can_enable_workspace_actions(self):
        for query in ('Do not browse with cloak-browse; explain it.',
                      "Don't query g8t-mcp; describe the tool.",
                      'Never browse using the tool.', 'Without querying g8t-mcp, explain it.'):
            with self.subTest(query=query):
                # The fourth case does not contain an imperative action verb.
                result = self.infer(['Here is a description.'], {'query': query})
                self.assertNotIn('format', self.payloads[0])
                self.assertEqual(self.actions, [])
                self.assertEqual(result['text'], 'Here is a description.')

    def test_greeting_cannot_dispatch_a_well_formed_workspace_schema(self):
        response = '{"tool":"run","command":"must-not-execute","text":""}'
        with self.assertRaisesRegex(ValueError, 'disallowed'):
            self.infer([response] * 4)
        self.assertEqual(self.actions, [])
        self.assertNotIn('/workspace-result', self.paths)
        self.assertTrue(all('format' not in p for p in self.payloads))

    def test_literal_say_survives_finalization_and_new_lease_without_inference(self):
        literal = '[{"tool":"run","command":"example, not execution"}]'
        first = self.infer([json.dumps({'tool': 'say', 'text': literal})])
        self.assertEqual(first['text'], literal)
        self.assertTrue(protocol.checked_public(self.generations[0]))
        saved = json.loads(json.dumps(first))
        second = self.infer([], {'lease': 'retry'}, start={'generation': saved})
        self.assertEqual(second, first)
        self.assertEqual(self.paths, ['/workspace-start'])
        self.assertEqual(self.payloads, [])
        self.assertEqual(saved, first)

    def test_old_cached_protocol_is_never_blessed_repaired_or_reexecuted(self):
        for text in ('{"tool":"run","command":"unsafe"}', '{"tool":"say","text":"Hello"}',
                     '[{"tool":"write","path":"artifacts/a","text":"unsafe"}]'):
            with self.subTest(text=text):
                saved = {'text': text, 'digest': 'a' * 64, 'output_tokens': 1, 'elapsed_ms': 1}
                before = dict(saved)
                with self.assertRaises(worker.WorkRecovery):
                    self.infer([], start={'generation': saved})
                self.assertEqual(saved, before)
                self.assertEqual(self.paths, ['/workspace-start'])
                self.assertEqual(self.payloads, [])

    def test_old_plain_cached_reply_is_guarded_without_mutating_original(self):
        saved = {'text': 'Observed result.', 'digest': 'a' * 64, 'output_tokens': 1, 'elapsed_ms': 1}
        answer = self.infer([], start={'generation': saved})
        self.assertEqual(answer, saved)
        self.assertNotIn('reply_protocol', saved)
        self.assertEqual(self.payloads, [])

    def test_cached_theme_requires_current_capability_even_with_valid_marker(self):
        saved = protocol.mark_public({'text': 'Room theme: None.', 'digest': 'a' * 64,
                                      'room_theme': {'preset': 'none'}})
        with self.assertRaises(worker.WorkRecovery):
            self.infer([], start={'generation': saved})
        result = self.infer([], {'room_theme_capability': self.cap}, start={'generation': saved})
        self.assertEqual(result, saved)
        self.assertEqual(self.payloads, [])

    def test_finalizer_cannot_change_text_under_old_marker(self):
        with self.assertRaises(worker.WorkRecovery):
            self.infer(['Hello.'], finalize=lambda saved: {**saved, 'text': 'Different.'})
        self.assertEqual(self.actions, [])

    def test_partial_completed_work_resumes_without_model_or_theme_effect(self):
        resumed = {'digest': 'a' * 64, 'output_tokens': 9, 'elapsed_ms': 12,
                   'observations': [{'action': {'tool': 'list', 'text': ''}, 'result': {'files': []}}]}
        result = self.infer([], {'room_theme_capability': self.cap}, start={'resume': resumed})
        self.assertIn('Listed 0 workspace entries', result['text'])
        self.assertNotIn('room_theme', result)
        self.assertEqual(self.actions, [])
        self.assertEqual(self.payloads, [])
        self.assertEqual(result['output_tokens'], 9)
        self.assertTrue(protocol.checked_public(result))

    def test_strict_workspace_lane_does_not_weaken_public_classifier(self):
        valid = ({'tool': 'decline', 'text': 'No.'}, {'tool': 'list', 'text': ''},
                 {'tool': 'read', 'path': 'src/a', 'text': ''},
                 {'tool': 'run', 'command': 'test command', 'text': ''})
        for action in valid:
            with self.subTest(action=action):
                self.assertEqual(protocol.workspace_action(json.dumps(action)), action)
                with self.assertRaises(protocol.ReplyProtocolError):
                    protocol.classify(json.dumps(action))
        for text in ('{"tool":"list"}', '{"tool":"list","text":"narrated"}',
                     '{"tool":"list","text":"","text":""}',
                     '{"tool":"set_room_theme","preset":"none"}',
                     '{"tool":"decline","text":"No","extra":1}'):
            with self.subTest(text=text), self.assertRaises(protocol.ReplyProtocolError):
                protocol.workspace_action(text)

    def test_completed_effect_is_reported_after_malformed_final_response(self):
        result = self.infer(['{"tool":"list","text":""}', '{"tool":"say","text":"unfinished'],
                            {'query': 'List files with your tool.'})
        self.assertEqual(self.actions, [{'tool': 'list', 'text': ''}])
        self.assertIn('Listed 0 workspace entries', result['text'])
        self.assertNotIn('unfinished', result['text'])
        self.assertTrue(protocol.checked_public(result))

    def test_delivery_deadline_retains_exact_spool_and_no_transport(self):
        with tempfile.TemporaryDirectory(prefix='8c-theme-deadline-') as directory:
            pending = Path(directory) / 'pending-result.json'
            worker.save(pending, {'id': 1, 'text': 'Hello.'})
            raw = pending.read_bytes()
            with patch.object(worker, 'request') as request:
                self.assertFalse(worker.deliver('https://unused.invalid', 'key', pending,
                                                deadline=time.monotonic_ns()))
            request.assert_not_called()
            self.assertEqual(pending.read_bytes(), raw)

    def test_peer_accept_failure_retains_original_marker_and_canonical_text(self):
        with tempfile.TemporaryDirectory(prefix='8c-theme-accept-') as directory:
            pending = Path(directory) / 'pending-result.json'
            worker.save(pending, protocol.mark_public({'id': 1, 'lease': 'first', 'agent': 'aye',
                'room': 'model-lab', 'text': 'Provisional text.', 'work': {'tools': []}}))
            raw = pending.read_bytes()
            ack = {'ok': True, 'published': True, 'message': {'text': 'Canonical accepted text.'}}
            with patch.object(worker, 'request', return_value=ack) as request, \
                 patch.object(worker, 'bridge', side_effect=OSError('unavailable')) as bridge:
                with self.assertRaises(OSError):
                    worker.deliver('https://unused.invalid', 'key', pending, [self.agent])
            self.assertEqual(pending.read_bytes(), raw)
            self.assertNotIn('reply_protocol', request.call_args.args[2])
            self.assertEqual(bridge.call_args.args[2]['public_text'], ack['message']['text'])


if __name__ == '__main__':
    unittest.main()
