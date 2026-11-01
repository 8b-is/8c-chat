"""Actual OpenCode + agy + Aurora + Rust round trip in isolated state."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import worker

root = Path(tempfile.mkdtemp(prefix='8c-hosted-e2e-', dir='/tmp/opencode'))
key = secrets.token_hex(32)
origin = 'http://127.0.0.1:18900'
config = json.loads(Path('agents.example.json').read_text())
config['agents'] = [a for a in config['agents'] if a['runtime'] in worker.HOSTED]
for source in config['memory']['family']:
    source['path'] = str(Path(source['path']).resolve())
for agent in config['agents']:
    agent['endpoint'] = 'http://127.0.0.1:18901'
    agent['autonomous'] = False
config['support_url'] = 'https://buy.stripe.com/5kQ00j5tB9aG0gx8tbbMQ0r'
(root/'agents.json').write_text(json.dumps(config))
bridge_config = {'g8t_source': '/data/8b-is/g8t', 'agents': [{**a, 'binary': '/usr/bin/opencode' if a['runtime']=='opencode' else '/home/hue/.local/bin/agy'} for a in config['agents']]}
(root/'bridge.json').write_text(json.dumps(bridge_config))
env = {**os.environ, 'WORKER_KEY': key, 'BIND_ADDR': '127.0.0.1:18900',
       'JOURNAL_PATH': str(root/'chat.jsonl'), 'AGENTS_CONFIG': str(root/'agents.json')}
logs = []
processes = []
def launch(args):
    log = (root/(str(len(logs)) + '.log')).open('w'); logs.append(log)
    p = subprocess.Popen(args, env=env, stdout=log, stderr=log); processes.append(p)
    return p
def wait_reply(identifier, room):
    deadline = time.monotonic_ns() + 160_000_000_000
    while time.monotonic_ns() < deadline:
        data = worker.request(origin, '/api/messages?room=' + room)
        reply = next((m for m in data['messages'] if m.get('reply_to')==identifier), None)
        if reply:
            return reply
        failed = [j for j in data['jobs'] if j['status']=='failed']
        if failed:
            raise AssertionError(failed)
        time.sleep(2)
    raise AssertionError('No actual hosted reply')
try:
    bridge = launch([sys.executable, 'hosted.py', '--config', str(root/'bridge.json'), '--root', str(root/'commons'), '--port', '18901'])
    server = launch(['target/debug/8c-chat'])
    for _ in range(30):
        try:
            worker.request(origin, '/api/config'); break
        except OSError: time.sleep(1)
    p = worker.request(origin, '/api/introduce', {'name': 'Commons integration visitor', 'bio': 'I make copper lighthouses.'})
    worker.request(origin, '/api/hottub', {'token': p['token'], 'running': False})
    local = launch([sys.executable, 'worker.py', '--config', str(root/'agents.json'), '--chat', origin, '--state', str(root/'worker')])
    replies = []
    prompts = [
        ('astra', 'tokyo-lab', 'Read reference/g8t-live.json. Write artifacts/commons-marker.txt with copper lighthouse, your configured model, and the service-reported capsule count. Run /usr/local/libexec/8c-run to check that the file contains copper lighthouse. Briefly report your result.'),
        ('agy', 'next-level-g8t', 'Read shared/index.json and Astra\'s shared artifact. Read reference/g8t-live.json. Write artifacts/peer-review.txt naming the keepsake marker in Astra\'s artifact and explaining that the capsule count is service-reported. Briefly report what you actually read and wrote.')]
    for agent, room, text in prompts:
        result = worker.request(origin, '/api/messages', {'token': p['token'], 'room': room, 'agent': agent, 'text': text, 'client_id': secrets.token_hex(16)})
        reply = wait_reply(result['message']['id'], room)
        assert reply['origin']=='hosted-worker' and not reply['local_inference'] and reply['model_digest']==''
        assert reply['work']['tools'] and reply['work']['artifacts'], reply
        for artifact in reply['work']['artifacts']:
            assert hashlib.sha256(artifact['text'].encode()).hexdigest()==artifact['sha256']
            assert 'copper lighthouse' in artifact['text'].lower()
        scopes = {r['scope'] for r in reply['memory']['records']}
        assert {'family', 'agent:'+agent, 'visitor:'+p['profile']['handle']} <= scopes
        replies.append(reply)
        print(json.dumps({'participant': agent, 'text': reply['text'], 'tools': reply['work']['tools'], 'artifacts': [a['path'] for a in reply['work']['artifacts']]}), flush=True)
    local.terminate(); local.wait(timeout=10)
    server.terminate(); server.wait(timeout=10)
    bridge.terminate(); bridge.wait(timeout=10)
    bridge = launch([sys.executable, 'hosted.py', '--config', str(root/'bridge.json'), '--root', str(root/'commons'), '--port', '18901'])
    server = launch(['target/debug/8c-chat'])
    time.sleep(2)
    local = launch([sys.executable, 'worker.py', '--config', str(root/'agents.json'), '--chat', origin, '--state', str(root/'worker')])
    result = worker.request(origin, '/api/messages', {'token': p['token'], 'room': 'model-lab', 'agent': 'astra',
        'text': 'What keepsake do I make, and what does LLL mean in our family context? One sentence, using your recalled context.', 'client_id': secrets.token_hex(16)})
    reply = wait_reply(result['message']['id'], 'model-lab')
    assert 'copper lighthouse' in reply['text'].lower() and 'life long learning' in reply['text'].lower(), reply
    replies.append(reply)
    (root/'result.json').write_text(json.dumps({'ok':True, 'replies':replies, 'restart':True, 'cross_room_memory':True}, indent=2, ensure_ascii=False))
    print('PASS actual hosted models, tools, peer artifacts, MEM8 and cross-room restart continuity', flush=True)
finally:
    for process in reversed(processes):
        if process.poll() is None:
            process.terminate(); process.wait(timeout=15)
    for log in logs: log.close()
    print('Artifacts:', root, flush=True)
