#!/usr/bin/env python3
"""Actual Rust HTTP/journal + worker integration; inference/bridge are SYNTHETIC.

Run after cargo build. No models, providers, real workbench tools or live rooms.
Retains only newly-created fixture state under /tmp for inspection.
"""
import json
import os
from pathlib import Path
import select
import socket
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import worker
from reply_protocol import ReplyProtocolError, FAILURE, theme_presets

KEY = 'synthetic-room-theme-integration-worker-key'
MODEL = 'synthetic-theme-test:local'
ROOM = 'model-lab'


def main():
    binary = ROOT / 'target/debug/8c-chat'
    if not binary.is_file():
        raise SystemExit('Run cargo build --offline first.')
    state = Path(tempfile.mkdtemp(prefix='8c-theme-integration-'))
    print('SYNTHETIC inference; fixture directory:', state, flush=True)
    agent = dict(id='aye', name='Aye', model=MODEL, runtime='ollama',
                 endpoint='http://127.0.0.1:1', rooms=[ROOM], autonomous=False)
    config = state / 'agents.json'
    config.write_text(json.dumps({'memory': {'required': False}, 'agents': [agent]}))
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1', 0))
        port = reservation.getsockname()[1]
    origin = f'http://127.0.0.1:{port}'
    process = None

    def start(server_binary=binary):
        nonlocal process
        process = subprocess.Popen([str(server_binary)], env={**os.environ, 'BIND_ADDR': f'127.0.0.1:{port}',
            'JOURNAL_PATH': str(state/'chat.jsonl'), 'AGENTS_CONFIG': str(config), 'WORKER_KEY': KEY},
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        ready, _, _ = select.select([process.stderr], [], [], 10)
        assert ready, 'server startup timed out'
        line = process.stderr.readline()
        assert 'listening on' in line, line

    def stop():
        nonlocal process
        if process:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            process.stderr.close()
            process = None

    def api(path, value=None):
        return worker.request(origin, path, value)

    def room():
        return api('/api/messages?room='+ROOM)

    proof = dict(worker_key=KEY, agent='aye', model=MODEL, runtime='ollama', digest='a'*64)
    number = 0

    def claim(text):
        nonlocal number
        number += 1
        api('/api/messages', dict(token=profile['token'], room=ROOM, text=text,
                                 client_id=f'theme-integration-{number}', agent='aye'))
        job = api('/api/worker/poll', proof)['job']
        assert job and job['query'] == text
        return job

    def infer(job, response, reason='stop'):
        generated = dict(model=MODEL, response=response, done=True, done_reason=reason, eval_count=10)
        with patch.object(worker, 'inventory', return_value='a'*64), \
             patch.object(worker, 'request', return_value=generated), \
             patch.object(worker, 'bridge', side_effect=AssertionError('No workbench dispatch allowed')):
            return worker.infer(agent, job)

    def deliver(job, answer):
        body = {**proof, 'id': job['id'], 'lease': job['lease'], 'room': ROOM, **answer}
        body.pop('worker_key')
        pending = state / 'pending-result.json'
        worker.save(pending, body)
        assert worker.deliver(origin, KEY, pending, [agent])
        assert not pending.exists()
        return body

    try:
        start()
        profile = api('/api/introduce', dict(name='Synthetic theme visitor'))
        # Exact failure classes from the reported conversation, never executed.
        for raw in ['{"tool":"run","command":"echo unfinished',
                    '[{"tool":"write","path":"artifacts/response.txt","text":"Hello"}]']:
            job = claim('Hey guys')
            try:
                infer(job, raw)
                raise AssertionError('Protocol leak was accepted')
            except ReplyProtocolError:
                deliver(job, {'error': FAILURE})
            assert not any(m['sender_kind'] == 'aye' for m in room()['messages'])

        job = claim('Hello again')
        deliver(job, infer(job, 'Hello! This is a synthetic conversational reply.'))
        assert room()['messages'][-1]['text'].startswith('Hello!')

        job = claim('Please give this room the qwave background')
        assert job['room_theme_capability']['presets'] == list(theme_presets(job))
        generated = infer(job, '{"tool":"set_room_theme","preset":"qwave-whisper-v1"}')
        body = deliver(job, generated)
        snapshot = room()
        assert snapshot['theme']['preset'] == 'qwave-whisper-v1'
        assert snapshot['theme']['revision'] == 1
        assert snapshot['messages'][-1]['room_theme'] == snapshot['theme']
        assert 'Qwave Whisper' in snapshot['messages'][-1]['text']
        assert not snapshot['messages'][-1]['text'].startswith(('{', '['))
        duplicate = api('/api/worker/result', {**body, 'worker_key': KEY})
        assert duplicate['duplicate'] and duplicate['published']
        assert room()['theme']['revision'] == 1
        assert duplicate['message']['text'] == snapshot['messages'][-1]['text']

        job = claim('Reset the room background')
        # A real human setting races with the generated result; stale CAS must fail.
        reset = api('/api/room-theme', dict(token=profile['token'], room=ROOM,
                    preset='none', expected_revision=1))
        assert reset['theme']['revision'] == 2
        deliver(job, infer(job, '{"tool":"set_room_theme","preset":"none"}'))
        snapshot = room()
        assert snapshot['theme']['revision'] == 2
        assert any(j['id'] == job['id'] and j['status'] == 'failed' for j in snapshot['jobs'])
        assert snapshot['messages'][-1]['origin'] == 'room-theme'

        stop()
        journal = (state/'chat.jsonl').read_bytes()
        source = (state/'chat.memory-source').read_bytes()
        start()
        assert room()['theme'] == snapshot['theme']
        assert (state/'chat.jsonl').read_bytes().startswith(journal)
        assert (state/'chat.memory-source').read_bytes() == source
        assert api('/api/health')['ui_revision'] == 11
        if len(sys.argv) == 2:
            legacy = Path(sys.argv[1]).resolve()
            assert legacy.is_file(), 'Pass a built revision-10 server for the rollback check'
            stop()
            start(legacy)
            assert api('/api/health')['ui_revision'] == 10
            api('/api/messages', dict(token=profile['token'], room=ROOM, text='Synthetic old-binary append',
                                     client_id='legacy-rollback-note', ask_aye='no'))
            stop()
            after_old = (state/'chat.jsonl').read_bytes()
            assert after_old.startswith(journal)
            start()
            assert room()['theme'] == snapshot['theme']
            assert room()['messages'][-1]['text'] == 'Synthetic old-binary append'
            assert (state/'chat.jsonl').read_bytes().startswith(after_old)
            assert (state/'chat.memory-source').read_bytes() == source
            print('PASS: actual revision-10 replay/append and revision-11 recovery; matching-worker rollout still required.')
        print('PASS: protocol rejection, ordinary answer, theme capability/effect, duplicate ACK, stale failure, restart and source preservation; no real inference.')
    finally:
        stop()


if __name__ == '__main__':
    main()
