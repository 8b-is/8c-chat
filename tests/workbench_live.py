"""Opt-in actual local-model use of the common bounded workbench."""
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import hosted
import worker

root = Path(tempfile.mkdtemp(prefix='8c-local-workbench-', dir='/tmp/opencode'))
agent = next(a for a in json.loads(Path('agents.example.json').read_text())['agents'] if a['id']=='cheet')
agent['endpoint'] = 'http://127.0.0.1:11435'
bridge = hosted.Bridge({'agents':[agent], 'g8t_source':'/data/8b-is/g8t'}, root)
job = {'agent':agent['id'], 'model':agent['model'], 'runtime':agent['runtime'], 'digest':worker.inventory(agent),
       'id':1, 'lease':'isolated-workbench-verification', 'room':'tokyo-lab',
       'system':'You are Cheet. Use only the supplied authorized workbench tools. Do not claim actions unless you have observed tool results.',
       'prompt':'Please create artifacts/local-marker.txt containing exactly: copper lighthouse. Use the write tool to create the real file, then briefly report its creation.'}
try:
    def dispatch(a, path, j=None, **kwargs):
        return bridge.handle(path, {'agent':a['id'], 'runtime':a['runtime'], 'model':a['model'], 'job':j})
    with patch.object(worker,'bridge',side_effect=dispatch):
        result = worker.infer(agent, job)
    assert (root/'cheet'/'artifacts'/'local-marker.txt').read_text().strip()=='copper lighthouse', result
    assert result['work']['tools'] == [{'name':'write', 'status':'completed'}], result
    assert result['work']['artifacts'][0]['path']=='artifacts/local-marker.txt'
    (root/'result.json').write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print(json.dumps(result, ensure_ascii=False),flush=True)
    print('PASS actual local model created a verified common-workbench artifact')
finally:
    bridge.lock.close()
    print('Artifacts:',root)
