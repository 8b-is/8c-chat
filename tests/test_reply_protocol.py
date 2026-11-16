"""Offline regressions: fake model/bridge; only synthetic spool files are written."""
import hashlib
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

import reply_protocol as protocol
import worker

TRUNCATED = '{"tool":"run","command":"echo hello | awk '
ARRAY = '[{"tool":"write","path":"artifacts/response.txt","text":"Thank you for being here. I created the file."}]'
CAP = {"presets": list(protocol.PRESETS)}


class ProtocolTests(unittest.TestCase):
    def test_exact_reported_payloads_fail(self):
        for text in (TRUNCATED, ARRAY):
            with self.subTest(text=text), self.assertRaisesRegex(protocol.ReplyProtocolError, "saved; please retry"):
                public, action = protocol.classify(text)
                if action is not None:
                    raise protocol.ReplyProtocolError()

    def test_malformed_arrays_and_reordered_keys_fail(self):
        for text in ('[ {"path":"artifacts/x", "tool":"write","text":"unfinished',
                     '{"command":"unfinished", "tool":"run"',
                     '[1, {"tool":"run","command":"echo hello"}]',
                     '[[{"tool":"list"}]]', '{"tool', '{"tool":"run"} trailing',
                     '{"tool":"say","tool":"say","text":"hi"}',
                     '{"to\\u006fl":"run","command":"unfinished',
                     "{'tool':'run','command':'not JSON'}", '{tool:"run",command:"not JSON"}'):
            with self.subTest(text=text), self.assertRaises(protocol.ReplyProtocolError):
                protocol.classify(text)

    def test_whole_fenced_protocol_is_never_dispatchable(self):
        for text in ('```json\n{"tool":"list"}\n```', '```\n' + ARRAY + '\n```',
                     '```JSON\n' + TRUNCATED, '```json\n{"tool":"say","text":"hi"}\n```'):
            with self.subTest(text=text), self.assertRaises(protocol.ReplyProtocolError):
                protocol.classify(text)

    def test_any_language_fenced_protocol_is_rejected(self):
        for language in ('javascript', 'python', 'JSON', 'text', 'arbitrary-label extra'):
            for payload in (TRUNCATED, ARRAY, '{"tool":"run","command":"echo ```"}'):
                text = '```' + language + '\n' + payload + '\n```'
                with self.subTest(language=language, payload=payload), self.assertRaises(protocol.ReplyProtocolError):
                    protocol.classify(text)
        explanation = '```javascript\n{"tool":"run","command":"example"}\n```\nThis explains the protocol; do not execute it.'
        self.assertEqual(protocol.classify(explanation), (explanation, None))
        code = '```javascript\nconsole.log("Hello");\n```'
        self.assertEqual(protocol.classify(code), (code, None))

    def test_inline_fence_opener_cannot_swallow_a_tool_payload(self):
        for text in ('```json {"tool":"run",\n"command":"never executed"}\n```',
                     '```javascript [{"tool":"write",\n"path":"artifacts/x","text":"never written"}]\n```',
                     '``` {"tool":"run","command":"unfinished\n```'):
            with self.subTest(text=text), self.assertRaises(protocol.ReplyProtocolError):
                protocol.classify(text)

    def test_ordinary_json_and_explanatory_prose_are_unchanged(self):
        for text in ('Hello!', '[]', '[1, {"greeting":"hello"}]', '{"count":2}',
                     '{"example":{"tool":"run","command":"echo hi"}}',
                     '{"text":"\\\"tool\\\": quoted data"}',
                     'Example only: ' + ARRAY,
                     'Use this example:\n```json\n{"tool":"list"}\n```',
                     '```json\n{"tool":"list"}\n```\nThis is an example, not a request.',
                     '```python\nprint("tool")\n```'):
            with self.subTest(text=text):
                self.assertEqual(protocol.classify(text), (text, None))

    def test_say_contents_are_literal_not_recursive_actions(self):
        for text in (ARRAY, TRUNCATED, '{"tool":"set_room_theme","preset":"violet-night-v1"}'):
            self.assertEqual(protocol.classify(json.dumps({"tool":"say", "text":text})), (text.strip(), None))

    def test_say_fields_are_strict(self):
        for action in ({"tool":"say"}, {"tool":"say", "text":7},
                       {"tool":"say", "text":""}, {"tool":"say", "text":"hi", "command":"echo hi"}):
            with self.subTest(action=action), self.assertRaises(protocol.ReplyProtocolError):
                protocol.classify(json.dumps(action))

    def test_valid_single_commands_remain_actions(self):
        for action in ({"tool":"list"}, {"tool":"read", "path":"src/a"},
                       {"tool":"write", "path":"artifacts/a", "text":"contents"},
                       {"tool":"run", "command":"echo hello"}):
            self.assertEqual(protocol.classify(json.dumps(action)), (None, action))

    def test_action_fields_are_strict(self):
        for text in ('{"tool":"unknown"}', '{"tool":"read"}', '{"tool":"run","command":[]}',
                     '{"tool":"write","path":"a","text":"hello","extra":true}'):
            with self.subTest(text=text), self.assertRaises(protocol.ReplyProtocolError):
                protocol.classify(text)

    def test_length_finishes_fail_even_for_plain_text(self):
        for reason in ('length', 'max_tokens', 'max_output_tokens'):
            with self.subTest(reason=reason), self.assertRaises(protocol.ReplyProtocolError):
                protocol.classify('Some unfinished prose', reason)

    def test_invalid_text_and_limits(self):
        for text in (None, 7, [], '', '<think>unfinished', 'x' * 4001):
            with self.subTest(kind=type(text)), self.assertRaises(protocol.ReplyProtocolError):
                protocol.classify(text)

    def test_marker_binds_exact_public_text(self):
        answer = protocol.mark_public({"text": ARRAY})
        self.assertTrue(protocol.checked_public(answer))
        answer['text'] = TRUNCATED
        self.assertFalse(protocol.checked_public(answer))


class InferenceTests(unittest.TestCase):
    def setUp(self):
        self.agent = {'id':'aye', 'runtime':'ollama', 'model':'local:latest',
                      'endpoint':'http://127.0.0.1:11435', 'workbench':'http://127.0.0.1:8799'}
        self.job = {'id':1, 'lease':'synthetic', 'room':'model-lab', 'agent':'aye',
                    'runtime':'ollama', 'model':'local:latest', 'digest':'a' * 64,
                    'system':'Be helpful.', 'prompt':'Quoted earlier conversation.', 'query':'Hello there.'}
        self.start = patch.object(worker, 'inventory', return_value='a' * 64)
        self.start.start()
        self.addCleanup(self.start.stop)

    def infer(self, replies, job=None, agent=None, finish=None, start_fails=False):
        agent = self.agent if agent is None else agent
        def bridge(a, path, j=None, **kwargs):
            if path == '/workspace-start':
                if start_fails:
                    raise OSError('offline')
                return {'context':'Quoted workspace history.'}
            if path == '/workspace-tool':
                return {'result':{'path':'artifacts/a', 'sha256':'b' * 64}}
            if path == '/workspace-result':
                return {'work':{'tools':[], 'artifacts':[]}}
            self.fail('Unexpected bridge operation: ' + path)
        results = [{'done':True, 'model':agent['model'], 'eval_count':3,
                    'response':text, 'done_reason':finish} for text in replies]
        with patch.object(worker, 'bridge', side_effect=bridge) as calls, \
             patch.object(worker, 'request', side_effect=results) as model:
            self.calls, self.model = calls, model
            return worker.infer(agent, self.job if job is None else job)

    def test_reported_leaks_never_dispatch_or_publish(self):
        for text in (TRUNCATED, ARRAY):
            with self.subTest(text=text), self.assertRaises(protocol.ReplyProtocolError):
                self.infer([text])
            self.assertEqual([c.args[1] for c in self.calls.call_args_list], ['/workspace-start'])

    def test_no_workbench_and_failed_start_still_guard(self):
        for text in (TRUNCATED, ARRAY, '{"tool":"run","command":"echo nope"}'):
            for failed in (False, True):
                agent = self.agent if failed else {k:v for k,v in self.agent.items() if k != 'workbench'}
                with self.subTest(text=text, failed=failed), self.assertRaises(protocol.ReplyProtocolError):
                    self.infer([text], agent=agent, start_fails=failed)
                self.assertNotIn('/workspace-tool', [c.args[1] for c in self.calls.call_args_list])

    def test_say_literal_example_survives_inference(self):
        result = self.infer([json.dumps({'tool':'say', 'text':ARRAY})])
        self.assertEqual(result['text'], ARRAY)
        self.assertTrue(protocol.checked_public(result))
        self.assertNotIn('/workspace-tool', [c.args[1] for c in self.calls.call_args_list])
        receipt_call = next(c for c in self.calls.call_args_list if c.args[1] == '/workspace-result')
        self.assertNotIn('reply_protocol', receipt_call.args[2]['generation'])

    def test_single_action_then_public_answer(self):
        result = self.infer(['{"tool":"write","path":"artifacts/a","text":"file contents"}', 'Observed write.'])
        actions = [c.args[2] for c in self.calls.call_args_list if c.args[1] == '/workspace-tool']
        self.assertEqual(actions[0]['sequence'], 0)
        self.assertEqual(actions[0]['action']['text'], 'file contents')
        self.assertEqual(result['text'], 'Observed write.')
        self.assertEqual(result['output_tokens'], 6)

    def test_third_action_fails_without_dispatch(self):
        with self.assertRaises(protocol.ReplyProtocolError):
            self.infer(['{"tool":"list"}'] * 3)
        actions = [c.args[2] for c in self.calls.call_args_list if c.args[1] == '/workspace-tool']
        self.assertEqual([a['sequence'] for a in actions], [0, 1])
        self.assertNotIn('/workspace-result', [c.args[1] for c in self.calls.call_args_list])

    def test_fenced_paste_not_dispatched(self):
        for language in ('json', 'javascript', 'python'):
            with self.subTest(language=language), self.assertRaises(protocol.ReplyProtocolError):
                self.infer(['```' + language + '\n{"tool":"run","command":"echo nope"}\n```'])
            self.assertEqual([c.args[1] for c in self.calls.call_args_list], ['/workspace-start'])

    def test_current_query_is_anchored_after_workspace_context(self):
        self.infer(['Hi.'])
        body = self.model.call_args.args[2]
        self.assertTrue(body['prompt'].endswith('CURRENT REQUEST: "Hello there."\n'))
        self.assertIn('not quoted history', body['system'])
        self.assertNotIn('set_room_theme', body['system'])

    def test_length_finish_fails_with_or_without_workbench(self):
        for workbench in (True, False):
            agent = self.agent if workbench else {k:v for k,v in self.agent.items() if k != 'workbench'}
            with self.subTest(workbench=workbench), self.assertRaises(protocol.ReplyProtocolError):
                self.infer(['Half an answer'], agent=agent, finish='length')

    def test_theme_allowed_is_server_effect_not_workspace_tool(self):
        for preset in protocol.PRESETS:
            job = {**self.job, 'query':'Change this room background please.', 'room_theme_capability':CAP}
            result = self.infer([json.dumps({'tool':'set_room_theme', 'preset':preset})], job=job)
            self.assertEqual(result['room_theme'], {'preset':preset})
            self.assertNotIn('"tool"', result['text'])
            self.assertNotIn('/workspace-tool', [c.args[1] for c in self.calls.call_args_list])
            self.assertIn('set_room_theme', self.model.call_args.args[2]['system'])

    def test_theme_without_capability_or_autonomous_fails(self):
        for changes in ({}, {'room_theme_capability':CAP, 'autonomous':True},
                        {'room_theme_capability':{'presets':['arbitrary-css']}},
                        {'room_theme_capability':{**CAP, 'extra':True}}):
            with self.subTest(changes=changes), self.assertRaises(protocol.ReplyProtocolError):
                self.infer(['{"tool":"set_room_theme","preset":"violet-night-v1"}'], job={**self.job, **changes})
            self.assertNotIn('/workspace-tool', [c.args[1] for c in self.calls.call_args_list])
            self.assertNotIn('set_room_theme', self.model.call_args.args[2]['system'])

    def test_theme_unknown_extra_fields_and_arrays_fail(self):
        job = {**self.job, 'room_theme_capability':CAP}
        for text in ('{"tool":"set_room_theme","preset":"https://evil.example"}',
                     '{"tool":"set_room_theme","preset":"none","css":"body{}"}',
                     '[{"tool":"set_room_theme","preset":"none"}]'):
            with self.subTest(text=text), self.assertRaises(protocol.ReplyProtocolError):
                self.infer([text], job=job)

    def test_theme_works_without_optional_workbench(self):
        agent = {k:v for k,v in self.agent.items() if k != 'workbench'}
        answer = self.infer(['{"tool":"set_room_theme","preset":"none"}'],
                            job={**self.job, 'room_theme_capability':CAP}, agent=agent)
        self.assertEqual(answer['room_theme'], {'preset':'none'})
        self.calls.assert_not_called()

    def test_theme_after_failed_workbench_start(self):
        answer = self.infer(['{"tool":"set_room_theme","preset":"none"}'],
                            job={**self.job, 'room_theme_capability':CAP}, start_fails=True)
        self.assertEqual(answer['room_theme'], {'preset':'none'})
        self.assertEqual([c.args[1] for c in self.calls.call_args_list], ['/workspace-start'])

    def hosted(self, text, changes=None, extra=None):
        agent = {**self.agent, 'runtime':'opencode'}
        job = {**self.job, 'runtime':'opencode', 'digest':'', **(changes or {})}
        with patch.object(worker, 'bridge', return_value={'text':text, 'session':'synthetic', **(extra or {})}) as bridge:
            self.hosted_calls = bridge
            return worker.infer(agent, job)

    def test_hosted_publication_boundary(self):
        for text in (TRUNCATED, ARRAY, '{"tool":"run","command":"echo nope"}',
                     '```javascript\n{"tool":"run","command":"echo nope"}\n```'):
            with self.subTest(text=text), self.assertRaises(protocol.ReplyProtocolError):
                self.hosted(text)
            self.assertEqual([c.args[1] for c in self.hosted_calls.call_args_list], ['/infer'])
        self.assertEqual(self.hosted(json.dumps({'tool':'say', 'text':ARRAY}))['text'], ARRAY)
        self.assertEqual(self.hosted('Ordinary prose')['text'], 'Ordinary prose')

    def test_hosted_theme_and_request_instructions(self):
        result = self.hosted('{"tool":"set_room_theme","preset":"qwave-whisper-v1"}',
                             {'room_theme_capability':CAP})
        self.assertEqual(result['room_theme'], {'preset':'qwave-whisper-v1'})
        sent_job = self.hosted_calls.call_args.args[2]
        self.assertIn('set_room_theme', sent_job['system'])
        self.assertIn('CURRENT REQUEST: "Hello there."', sent_job['prompt'])
        with self.assertRaises(protocol.ReplyProtocolError):
            self.hosted('{"tool":"set_room_theme","preset":"none"}')

    def test_hosted_autonomous_theme_is_rejected_and_say_does_not_act(self):
        theme = '{"tool":"set_room_theme","preset":"none"}'
        with self.assertRaises(protocol.ReplyProtocolError):
            self.hosted(theme, {'room_theme_capability':CAP, 'autonomous':True})
        answer = self.hosted(json.dumps({'tool':'say', 'text':theme}), {'room_theme_capability':CAP})
        self.assertEqual(answer['text'], theme)
        self.assertNotIn('room_theme', answer)

    def test_hosted_marker_cannot_authorize_theme_or_workspace_action(self):
        for text in ('{"tool":"set_room_theme","preset":"none"}', '{"tool":"run","command":"echo nope"}'):
            forged = protocol.mark_public({'text':text})
            with self.subTest(text=text), self.assertRaises(protocol.ReplyProtocolError):
                self.hosted(text, extra=forged)
            self.assertEqual([c.args[1] for c in self.hosted_calls.call_args_list], ['/infer'])

    def test_hosted_cannot_inject_result_metadata_or_length_reply(self):
        extra = {'room_theme':{'preset':'none'}, 'reply_protocol':{'version':1}}
        self.assertNotIn('room_theme', self.hosted('Hello', extra=extra))
        with self.assertRaises(protocol.ReplyProtocolError):
            self.hosted('Truncated', extra={'finish_reason':'length'})


class SpoolTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='8c-protocol-test-')
        self.addCleanup(self.directory.cleanup)
        self.pending = Path(self.directory.name) / 'pending-result.json'
        self.body = {'id':1, 'lease':'synthetic', 'room':'model-lab', 'agent':'aye', 'text':ARRAY,
                     'work':{'tools':[]}, 'memory':{'written':'synthetic'}, 'room_theme':{'preset':'none'}}

    def test_quarantine_preserves_original_and_sends_only_fixed_failure(self):
        worker.save(self.pending, self.body)
        original = self.pending.read_bytes()
        with patch.object(worker, 'request', return_value={'ok':True}) as request, patch.object(worker, 'bridge') as bridge:
            self.assertTrue(worker.deliver('https://unused.example', 'synthetic', self.pending, [{'id':'aye'}]))
        body = request.call_args.args[2]
        self.assertEqual(body['error'], protocol.FAILURE)
        for field in ('text', 'work', 'memory', 'room_theme'):
            self.assertNotIn(field, body)
        self.assertNotIn('Thank you', json.dumps(body))
        bridge.assert_not_called()
        quarantine = list(self.pending.parent.glob('quarantine-reply-*.json'))
        self.assertEqual(len(quarantine), 1)
        self.assertEqual(quarantine[0].read_bytes(), original)
        self.assertEqual(stat.S_IMODE(quarantine[0].stat().st_mode), 0o600)
        self.assertEqual(quarantine[0].name, 'quarantine-reply-' + hashlib.sha256(original).hexdigest() + '.json')
        self.assertFalse(self.pending.exists())

    def test_failed_transport_retries_safe_error_idempotently(self):
        worker.save(self.pending, self.body)
        with patch.object(worker, 'request', side_effect=OSError('offline')):
            with self.assertRaises(OSError):
                worker.deliver('https://unused.example', 'synthetic', self.pending)
        safe = json.loads(self.pending.read_text())
        self.assertEqual(safe['error'], protocol.FAILURE)
        with patch.object(worker, 'request', return_value={'ok':False}):
            self.assertFalse(worker.deliver('https://unused.example', 'synthetic', self.pending))
        with patch.object(worker, 'request', return_value={'ok':True, 'duplicate':True}):
            self.assertTrue(worker.deliver('https://unused.example', 'synthetic', self.pending))
        self.assertEqual(len(list(self.pending.parent.glob('quarantine-reply-*.json'))), 1)

    def test_restart_after_quarantine_before_safe_save(self):
        worker.save(self.pending, self.body)
        original = self.pending.read_bytes()
        worker.quarantine_reply(self.pending, original)
        with patch.object(worker, 'request', return_value={'ok':True}):
            self.assertTrue(worker.deliver('https://unused.example', 'synthetic', self.pending))
        self.assertEqual(len(list(self.pending.parent.glob('quarantine-reply-*.json'))), 1)

    def test_quarantine_failure_prevents_delivery(self):
        worker.save(self.pending, self.body)
        with patch.object(worker, 'quarantine_reply', side_effect=OSError('disk full')), patch.object(worker, 'request') as request:
            with self.assertRaises(OSError):
                worker.deliver('https://unused.example', 'synthetic', self.pending)
        request.assert_not_called()
        self.assertEqual(json.loads(self.pending.read_text()), self.body)

    def test_validated_say_example_and_ordinary_legacy_reply_survive(self):
        for body in (protocol.mark_public({'id':1, 'lease':'synthetic', 'text':ARRAY}),
                     {'id':1, 'lease':'synthetic', 'text':'Hello!'}):
            worker.save(self.pending, body)
            with patch.object(worker, 'request', return_value={'ok':True}) as request:
                self.assertTrue(worker.deliver('https://unused.example', 'synthetic', self.pending))
            self.assertEqual(request.call_args.args[2]['text'], body['text'])
            self.assertNotIn('reply_protocol', request.call_args.args[2])
        self.assertFalse(list(self.pending.parent.glob('quarantine-reply-*.json')))

    def test_missing_mismatched_and_legacy_markers_quarantine_original(self):
        digest = hashlib.sha256(ARRAY.encode()).hexdigest()
        for marker in (None, {'version':0, 'text_sha256':digest},
                       {'version':True, 'text_sha256':digest},
                       {'version':1, 'text_sha256':'0' * 64}):
            body = dict(self.body)
            if marker is not None:
                body['reply_protocol'] = marker
            worker.save(self.pending, body)
            raw = self.pending.read_bytes()
            with self.subTest(marker=marker), patch.object(worker, 'request', return_value={'ok':True}) as request:
                self.assertTrue(worker.deliver('https://unused.example', 'synthetic', self.pending))
            sent = request.call_args.args[2]
            self.assertEqual(sent['error'], protocol.FAILURE)
            self.assertNotIn('reply_protocol', sent)
            self.assertNotIn('room_theme', sent)
            self.assertNotIn('text', sent)
            quarantine = self.pending.with_name('quarantine-reply-' + hashlib.sha256(raw).hexdigest() + '.json')
            self.assertEqual(quarantine.read_bytes(), raw)

    def test_mismatched_marker_on_plain_text_is_not_legacy_fallback(self):
        body = protocol.mark_public({**self.body, 'text':'Original public answer.'})
        body['text'] = 'Changed text after validation.'
        worker.save(self.pending, body)
        original = self.pending.read_bytes()
        with patch.object(worker, 'request', return_value={'ok':True}) as request, patch.object(worker, 'bridge') as bridge:
            self.assertTrue(worker.deliver('https://unused.example', 'synthetic', self.pending, [{'id':'aye'}]))
        sent = request.call_args.args[2]
        self.assertEqual(sent['error'], protocol.FAILURE)
        self.assertNotIn('text', sent)
        self.assertNotIn('room_theme', sent)
        self.assertNotIn('reply_protocol', sent)
        bridge.assert_not_called()
        self.assertEqual(next(self.pending.parent.glob('quarantine-reply-*.json')).read_bytes(), original)

    def test_ack_error_prevents_accept_even_without_published_false(self):
        body = protocol.mark_public({**self.body, 'text':'Room theme: None.'})
        for fields in ({}, {'published':True}):
            worker.save(self.pending, body)
            original = self.pending.read_bytes()
            ack = {'ok':True, 'error':'Room theme rejected; please retry.', **fields}
            with self.subTest(fields=fields), patch.object(worker, 'request', return_value=ack), patch.object(worker, 'bridge') as bridge:
                self.assertTrue(worker.deliver('https://unused.example', 'synthetic', self.pending, [{'id':'aye'}]))
            bridge.assert_not_called()
            self.assertFalse(self.pending.exists())
            self.assertEqual(next(self.pending.parent.glob('quarantine-reply-*.json')).read_bytes(), original)

    def test_rejected_theme_ack_preserves_original_without_peer_publication(self):
        body = protocol.mark_public({**self.body, 'text':'Room theme: None.'})
        worker.save(self.pending, body)
        original = self.pending.read_bytes()
        ack = {'ok':True, 'published':False, 'error':'Room theme changed; please retry.'}
        with patch.object(worker, 'request', return_value=ack), patch.object(worker, 'bridge') as bridge, patch('builtins.print') as log:
            self.assertTrue(worker.deliver('https://unused.example', 'synthetic', self.pending, [{'id':'aye'}]))
        bridge.assert_not_called()
        log.assert_called_once_with('Reply not published; server exposes a retry', flush=True)
        self.assertFalse(self.pending.exists())
        quarantine = list(self.pending.parent.glob('quarantine-reply-*.json'))
        self.assertEqual(len(quarantine), 1)
        self.assertEqual(quarantine[0].read_bytes(), original)
        self.assertEqual(stat.S_IMODE(quarantine[0].stat().st_mode), 0o600)

    def test_rejected_ack_quarantine_failure_retries_duplicate_rejection(self):
        body = protocol.mark_public({**self.body, 'text':'Room theme: None.'})
        worker.save(self.pending, body)
        ack = {'ok':True, 'published':False, 'error':'Room theme changed; please retry.'}
        with patch.object(worker, 'request', return_value=ack), patch.object(worker, 'bridge') as bridge, \
             patch.object(worker, 'quarantine_reply', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                worker.deliver('https://unused.example', 'synthetic', self.pending, [{'id':'aye'}])
        bridge.assert_not_called()
        self.assertEqual(json.loads(self.pending.read_text()), body)
        with patch.object(worker, 'request', return_value={**ack, 'duplicate':True}), patch.object(worker, 'bridge') as bridge:
            self.assertTrue(worker.deliver('https://unused.example', 'synthetic', self.pending, [{'id':'aye'}]))
        bridge.assert_not_called()
        self.assertFalse(self.pending.exists())

    def test_accept_uses_server_canonical_text_not_provider_reply(self):
        body = protocol.mark_public({**self.body, 'text':'Worker summary.'})
        worker.save(self.pending, body)
        accepted = 'Server-accepted summary.'
        with patch.object(worker, 'request', return_value={'ok':True, 'published':True, 'message':{'text':accepted}}), \
             patch.object(worker, 'bridge') as bridge:
            self.assertTrue(worker.deliver('https://unused.example', 'synthetic', self.pending, [{'id':'aye'}]))
        self.assertEqual(bridge.call_args.args[1], '/accept')
        self.assertEqual(bridge.call_args.args[2], {'id':1, 'lease':'synthetic', 'room':'model-lab', 'public_text':accepted})
        self.assertFalse(self.pending.exists())

    def test_duplicate_accept_falls_back_to_validated_spool_text(self):
        body = protocol.mark_public(dict(self.body))  # Literal say example, never an action.
        worker.save(self.pending, body)
        with patch.object(worker, 'request', return_value={'ok':True, 'duplicate':True}), patch.object(worker, 'bridge') as bridge:
            self.assertTrue(worker.deliver('https://unused.example', 'synthetic', self.pending, [{'id':'aye'}]))
        self.assertEqual(bridge.call_args.args[2]['public_text'], ARRAY)
        self.assertNotIn('reply_protocol', bridge.call_args.args[2])
        self.assertFalse(list(self.pending.parent.glob('quarantine-reply-*.json')))

    def test_invalid_server_public_text_retains_spool_without_peer_publication(self):
        body = protocol.mark_public({**self.body, 'text':'Worker summary.'})
        worker.save(self.pending, body)
        with patch.object(worker, 'request', return_value={'ok':True, 'message':{'text':None}}), patch.object(worker, 'bridge') as bridge:
            with self.assertRaises(protocol.ReplyProtocolError):
                worker.deliver('https://unused.example', 'synthetic', self.pending, [{'id':'aye'}])
        bridge.assert_not_called()
        self.assertEqual(json.loads(self.pending.read_text()), body)

    def test_unmarked_say_and_theme_spool_are_not_reinterpreted(self):
        for text in ('{"tool":"say","text":"I did it."}', '{"tool":"set_room_theme","preset":"none"}'):
            worker.save(self.pending, {**self.body, 'text':text})
            with patch.object(worker, 'request', return_value={'ok':True}) as request:
                self.assertTrue(worker.deliver('https://unused.example', 'synthetic', self.pending))
            self.assertEqual(request.call_args.args[2]['error'], protocol.FAILURE)


if __name__ == '__main__':
    unittest.main()
