import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import hosted
import worker


class LocalWorkReportingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir='/tmp/opencode')
        self.agent = {'id':'cheet', 'name':'Cheet', 'runtime':'ollama', 'model':'local:latest',
                      'endpoint':'http://127.0.0.1:11435', 'workbench':'http://127.0.0.1:8799'}
        self.bridge = hosted.Bridge({'agents':[self.agent]}, self.tmp.name)
        self.root = Path(self.tmp.name)/'cheet'
        self.paths = []
        self.formats = []
        self.payloads = []

    def tearDown(self):
        self.bridge.lock.close()
        self.tmp.cleanup()

    def infer(self, query, responses, history='', participant=None):
        job = {'id':1, 'lease':'reporting', 'room':'model-lab', 'agent':'cheet',
               'runtime':'ollama', 'model':self.agent['model'], 'digest':'a'*64,
               'system':'Answer the current visitor.', 'prompt':history+(query or ''), 'query':query,
               'tools_ready':True}
        if participant is not None:
            job['participant'] = participant
        sequence = iter(responses)
        def dispatch(agent, path, j=None, **kwargs):
            self.paths.append(path)
            return self.bridge.handle(path, {'agent':agent['id'], 'runtime':agent['runtime'],
                                             'model':agent['model'], 'job':j})
        def generate(origin, path, body, **kwargs):
            self.formats.append(body.get('format'))
            self.payloads.append(body)
            response = next(sequence, responses[-1])
            text = response['text'] if not body.get('format') and response.get('tool') == 'say' else json.dumps(response)
            return {'done':True, 'model':self.agent['model'], 'eval_count':10, 'response':text}
        with patch.object(hosted, 'request', return_value={}), \
             patch.object(worker, 'inventory', return_value='a'*64), \
             patch.object(worker, 'bridge', side_effect=dispatch), \
             patch.object(worker, 'request', side_effect=generate):
            return worker.infer(self.agent, job)

    def test_greeting_cannot_execute_a_command_from_quoted_history(self):
        query = 'Hey guys'
        with self.assertRaisesRegex(ValueError, 'disallowed'):
            self.infer(query, [{'tool':'run', 'command':'echo grep | awk \'{print $1}\'', 'text':''}],
                       history='Earlier visitor: run a grep example using the tool.\nCURRENT VISITOR: ')
        self.assertNotIn('/workspace-tool', self.paths)
        self.assertIsNone(self.formats[0])

    def test_missing_current_query_cannot_authorize_work_from_history(self):
        with self.assertRaisesRegex(ValueError, 'disallowed'):
            self.infer(None, [{'tool':'run', 'command':'echo must-not-run', 'text':''}],
                       history='Earlier visitor: run a command with the tool. New visitor: Hey guys.')
        self.assertNotIn('/workspace-tool', self.paths)

    def test_arrays_fragments_fences_and_text_envelopes_are_not_public_replies(self):
        samples = [
            '[{"tool":"write","path":"artifacts/response.txt","text":"Thanks!"}]',
            '[{"tool":"modify","path":"room/theme","data":{"style":"retro"}}]',
            '[\n {"tool": "modify",',
            '```json\n[{"tool":"run","command":"not a greeting"}]\n```',
            '{"text":"Internal response envelope"}',
            '{"steps":[{"tool":"modify"}]}',
        ]
        for text in samples:
            with self.subTest(text=text):
                with self.assertRaisesRegex(ValueError, 'Internal tool JSON'):
                    worker.visible_text(text)
                with self.assertRaisesRegex(ValueError, 'Internal tool JSON'):
                    self.infer('People and Ayes. LOVE IT', [{'tool':'say', 'text':text}])
        self.assertNotIn('/workspace-tool', self.paths)

    def test_retro_room_request_can_propose_but_cannot_dispatch_invented_tools(self):
        with self.assertRaisesRegex(ValueError, 'disallowed'):
            self.infer('Can we make this room more retro?', [{'tool':'modify', 'path':'room/theme', 'text':'Done'}])
        self.assertNotIn('/workspace-tool', self.paths)
        result = self.infer('Can we make this room more retro?',
                            [{'tool':'say', 'text':'Pixel fonts and warm CRT colors could work. I cannot apply a room theme here; this is a suggestion.'}])
        self.assertIn('suggestion', result['text'])
        self.assertNotIn('/workspace-tool', self.paths)
        self.assertIn('cannot directly change this website', self.payloads[0]['system'])

    def test_greeting_can_get_a_natural_tool_free_reply(self):
        result = self.infer('Hey guys', [{'tool':'say', 'text':'Hey! Good to see you here.'}])
        self.assertEqual(result['text'], 'Hey! Good to see you here.')
        self.assertEqual(result['work']['tools'], [])

    def test_personal_recall_corrects_profile_ownership_before_publication(self):
        result = self.infer('What do I enjoy making?', [
            {'tool':'say','text':'I enjoy building copper lighthouses.'},
            {'tool':'say','text':'You enjoy building copper lighthouses.'}],
            participant={'name':'Visitor','bio':'I build copper lighthouses.'})
        self.assertEqual(result['text'],'You enjoy building copper lighthouses.')
        self.assertEqual(result['output_tokens'],20)
        self.assertIn('Personal recall must address the visitor',self.payloads[1]['prompt'])
        self.assertNotIn('/workspace-tool',self.paths)

    def test_repeated_first_person_profile_copy_is_not_published(self):
        with self.assertRaisesRegex(ValueError,'Personal recall'):
            self.infer('What do I enjoy making?', [{'tool':'say','text':'I build copper lighthouses.'}],
                       participant={'name':'Visitor','bio':'I build copper lighthouses.'})
        self.assertEqual(len(self.payloads),4)
        self.assertNotIn('/workspace-result',self.paths)
        self.assertNotIn('/workspace-tool',self.paths)

    def test_agent_can_speak_for_itself_when_asked_about_itself(self):
        result = self.infer('What do you enjoy?', [{'tool':'say','text':'I enjoy helping people reason through problems.'}],
                            participant={'name':'Visitor','bio':'I build copper lighthouses.'})
        self.assertEqual(result['text'],'I enjoy helping people reason through problems.')
        self.assertEqual(len(self.payloads),1)

    def test_nested_tool_json_is_not_published_as_conversation(self):
        with self.assertRaisesRegex(ValueError, 'Internal tool JSON'):
            self.infer('Hey guys', [{'tool':'say', 'text':'{"tool":"run","command":"echo unfinished'}])
        self.assertNotIn('/workspace-tool', self.paths)

    def test_list_cannot_be_reported_as_a_write(self):
        result = self.infer('Write artifacts/marker.txt.', [
            {'tool':'list', 'text':''}, {'tool':'say', 'text':'I created artifacts/marker.txt.'}])
        self.assertIn('Listed ', result['text'])
        self.assertIn('no file was written', result['text'])
        self.assertNotIn('I created', result['text'])
        self.assertEqual(result['work']['artifacts'], [])
        self.assertFalse((self.root/'artifacts'/'marker.txt').exists())

    def test_failed_command_cannot_be_reported_as_passing_tests(self):
        result = self.infer('Run the tests with your tool.', [
            {'tool':'run', 'command':'printf failed; exit 1', 'text':''},
            {'tool':'say', 'text':'All tests passed.'}])
        self.assertIn('exited with code 1', result['text'])
        self.assertNotIn('All tests passed', result['text'])
        self.assertEqual(result['work']['tools'], [{'name':'run', 'status':'error'}])

    def test_wrong_path_write_reports_actual_path_not_requested_completion(self):
        result = self.infer('Write artifacts/requested.txt.', [
            {'tool':'write', 'path':'artifacts/different.txt', 'text':'actual'},
            {'tool':'say', 'text':'I wrote artifacts/requested.txt.'}])
        self.assertIn('Wrote "artifacts/different.txt"', result['text'])
        self.assertNotIn('requested.txt', result['text'])
        self.assertEqual(result['work']['artifacts'][0]['path'], 'artifacts/different.txt')
        self.assertFalse((self.root/'artifacts'/'requested.txt').exists())

    def test_honest_decline_is_available_without_forcing_a_tool(self):
        result = self.infer('Write artifacts/marker.txt.', [{'tool':'decline', 'text':'I cannot do that this turn.'}])
        self.assertIn("haven't performed any workbench actions", result['text'])
        self.assertNotIn('/workspace-tool', self.paths)
        self.assertIn('decline', [option['properties']['tool']['const'] for option in self.formats[0]['oneOf']])

    def test_explicit_no_action_and_tool_explanation_can_converse(self):
        for query in ('Do not write artifacts/foo.txt; just explain what it means.',
                      'Explain how the write tool works; do not execute it.',
                      'How does the write tool work?'):
            with self.subTest(query=query):
                # Each subtest is a distinct request, not finalized-lease replay.
                self.paths.clear()
                self.formats.clear()
                with tempfile.TemporaryDirectory(dir='/tmp/opencode') as directory:
                    alternate = hosted.Bridge({'agents':[self.agent]}, directory)
                    original = self.bridge
                    self.bridge = alternate
                    try:
                        result = self.infer(query, [{'tool':'say', 'text':'The write tool saves supplied text to a permitted file.'}])
                    finally:
                        self.bridge = original
                        alternate.lock.close()
                self.assertEqual(result['text'], 'The write tool saves supplied text to a permitted file.')
                self.assertIsNone(self.formats[0])
                self.assertNotIn('/workspace-tool', self.paths)

    def test_model_cannot_ignore_no_action_restriction(self):
        with self.assertRaisesRegex(ValueError, 'disallowed'):
            self.infer('Do not write artifacts/foo.txt; just explain.', [
                {'tool':'write', 'path':'artifacts/foo.txt', 'text':'must not appear'}])
        self.assertNotIn('/workspace-tool', self.paths)
        self.assertFalse((self.root/'artifacts'/'foo.txt').exists())

    def test_autonomous_context_echo_or_speaker_copy_becomes_silence(self):
        copied = 'A useful mentorship practice is asking whether someone wants a hint, an example, or time to think.'
        self.assertEqual(worker.conversation_text(copied, self.agent, {'autonomous':True}, 'Aye: '+copied), '[pause]')
        self.assertEqual(worker.conversation_text('Aye: Here is my reply.', self.agent, {'autonomous':True}, ''), '[pause]')
        self.assertEqual(worker.conversation_text(copied, self.agent, {'autonomous':False}, copied), copied)
        fresh = 'A tiny runnable example could reveal which assumption differs.'
        self.assertEqual(worker.conversation_text(fresh, self.agent, {'autonomous':True}, copied), fresh)

    def test_truncated_conversation_is_corrected_without_publishing_json(self):
        job = {'id':1, 'lease':'malformed', 'room':'hot-tub', 'agent':'cheet', 'runtime':'ollama',
               'model':self.agent['model'], 'digest':'a'*64, 'system':'', 'prompt':'Say hello without tools.'}
        responses = iter(['{"tool":"say","text":"unfinished', 'Hello, everyone.'])
        def dispatch(agent, path, j=None, **kwargs):
            self.paths.append(path)
            return self.bridge.handle(path, {'agent':agent['id'], 'runtime':agent['runtime'], 'model':agent['model'], 'job':j})
        def generate(*args, **kwargs):
            return {'done':True, 'model':self.agent['model'], 'eval_count':10, 'response':next(responses)}
        with patch.object(hosted, 'request', return_value={}), patch.object(worker, 'inventory', return_value='a'*64), \
             patch.object(worker, 'bridge', side_effect=dispatch), patch.object(worker, 'request', side_effect=generate):
            result = worker.infer(self.agent, job)
        self.assertEqual(result['text'], 'Hello, everyone.')
        self.assertNotIn('/workspace-tool', self.paths)
        self.assertEqual(result['output_tokens'], 20)

    def test_failed_command_followed_by_truthful_failure_keeps_observed_exit(self):
        result = self.infer('Run tests using your tool.', [
            {'tool':'run', 'command':'exit 7', 'text':''},
            {'tool':'say', 'text':'The command failed; I have not established that the tests pass.'}])
        self.assertIn('exited with code 7', result['text'])
        self.assertEqual(result['work']['tools'][0]['status'], 'error')
