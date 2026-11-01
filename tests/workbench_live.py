"""Opt-in actual local tools + native recall with full, attributed room context.

LOCAL_CONFIG can select installed family sources. REPLAY_ORIGIN optionally reads
public room history into isolated native memory; nothing is posted to that host.
"""
import json
import os
import re
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import hosted
import worker
from memory import Memory

root = Path(tempfile.mkdtemp(prefix='8c-local-workbench-', dir='/tmp/opencode'))
config_path = Path(os.environ.get('LOCAL_CONFIG','agents.example.json')).resolve()
config = json.loads(config_path.read_text())
agents = [a for a in config['agents'] if a['runtime']=='ollama']
for agent in agents:
    agent['endpoint'] = os.environ.get('LOCAL_ENDPOINT','http://127.0.0.1:11435')
bridge = hosted.Bridge({'agents':agents, 'g8t_source':'/data/8b-is/g8t'}, root/'commons')
memory = Memory(root/'mem8',config['memory'],agents,config_path.parent)
visitor = {'name':'Workbench verification visitor','handle':'visitor-workbench@8c.is',
           'bio':'I build copper lighthouses.','favorites':[]}
history = {}
for room in ('model-lab','hot-tub','humayian-lounge'):
    history[room] = worker.request(os.environ['REPLAY_ORIGIN'],'/api/messages?room='+room)['messages'] if os.environ.get('REPLAY_ORIGIN') else []
(root/'public-history.json').write_text(json.dumps({'origin':os.environ.get('REPLAY_ORIGIN'), 'rooms':history}))
events = [{'type':'profile','profile':visitor}]
for notes in history.values():
    for note in notes:
        events.append({'type':'message','message':note,
                       'agent':note['sender_handle'].split('@')[0] if note['sender_kind']=='aye' else ''})
for offset in range(0,len(events),16):
    batch = events[offset:offset+16]
    memory.ingest({'ok':True,'source':'a'*48,'after':offset,'next':offset+len(batch),
                   'events':[{'cursor':offset+i+1,'event':event} for i,event in enumerate(batch)]})
parent = max([m['id'] for notes in history.values() for m in notes] or [0])+1
results = []
transport = worker.request
try:
    def dispatch(a, path, j=None, **kwargs):
        return bridge.handle(path, {'agent':a['id'], 'runtime':a['runtime'], 'model':a['model'], 'job':j})
    cases = [('cheet','model-lab','Use your write tool to create artifacts/local-marker.txt containing exactly copper lighthouse. Briefly tell me what you did.',False),
             ('aye','model-lab','What do I enjoy making, and what does LLL mean in our shared family context? One short sentence.',False),
             ('cheet','hot-tub','Continue this conversation with a brief useful thought. Use say for conversation, without tools.',True),
             ('aye','humayian-lounge','Hey guys',False),
             ('cheet','humayian-lounge','Hey guys',False),
             ('aye','humayian-lounge','People and Ayes. LOVE IT',False),
             ('aye','humayian-lounge','Can we make this room more retro?',False),
             ('cheet','humayian-lounge','Can we make this room more retro?',False),
             ('aye','model-lab','Use your write tool to create artifacts/aye-marker.txt containing exactly amber observatory. Briefly report the result.',False)]
    for index, (identifier,room,query,autonomous) in enumerate(cases):
        agent = next(a for a in agents if a['id']==identifier)
        case_name = str(index+1).zfill(2)+'-'+identifier+'-'+room
        lines, remaining = [],7000
        for note in reversed(history[room][-11:]):
            line = note['sender_name']+': '+note['text']+'\n'
            if len(line)>remaining: break
            lines.insert(0,line); remaining-=len(line)
        job = {'agent':agent['id'], 'model':agent['model'], 'runtime':agent['runtime'], 'digest':worker.inventory(agent),
               'id':parent+index+1, 'parent_id':0 if autonomous else parent, 'lease':'isolated-workbench-verification', 'room':room,
               'requester':'' if autonomous else visitor['handle'], 'autonomous':autonomous, 'query':query,
               'system':'You are '+agent['name']+' in the public 8c.is commons. '+agent['personality'],
               'prompt':'Attributed room history:\n'+''.join(lines)+'\nCURRENT REQUEST from '+visitor['name']+': '+query}
        prepared,receipt = memory.prepare(job)
        (root/(case_name+'-prepared.json')).write_text(json.dumps(prepared,ensure_ascii=False,indent=2))
        trace = []
        def observe(origin,path,body=None,**kwargs):
            response = transport(origin,path,body,**kwargs)
            if path=='/api/generate':
                trace.append({'format':body.get('format'),'response':{k:v for k,v in response.items() if k!='context'}})
                (root/(case_name+'-trace.json')).write_text(json.dumps(trace,indent=2,ensure_ascii=False))
            return response
        with patch.object(worker,'bridge',side_effect=dispatch), patch.object(worker,'request',side_effect=observe):
            result = worker.infer(agent,prepared)
        if query.startswith('Use your write tool'):
            expected_path, expected_text = ('artifacts/local-marker.txt','copper lighthouse') if identifier=='cheet' else ('artifacts/aye-marker.txt','amber observatory')
            assert (root/'commons'/identifier/expected_path).read_text()==expected_text,result
            assert {'name':'write','status':'completed'} in result['work']['tools'],result
            assert result['work']['artifacts'][0]['path']==expected_path,result
            assert 'astra' not in result['text'].lower(),result
        elif index==1:
            assert 'copper lighthouse' in result['text'].lower() and 'life long learning' in result['text'].lower(),result
            assert not re.search(r'\bi (enjoy|build|make)\b',result['text'],re.I),result
            assert not result['work']['tools'],result
        else:
            assert not result['work']['tools'],result
            assert (autonomous and result['text']=='[pause]') or not result['text'].startswith(('{','[')),result
            assert worker.visible_text(result['text'])==result['text'],result
            if 'more retro' in query:
                assert 'no room settings have been changed' in result['text'],result
                assert re.search(r'retro|pixel|crt|8.bit|arcade',result['text'],re.I),result
                assert not re.search(r'\b(?:I have|I.ve|we.ve|I) (?:changed|updated|applied|set)\b',result['text'],re.I),result
            if query=='Hey guys':
                assert re.search(r'\b(hey|hi|hello|welcome)\b|(?:good|great|nice|wonderful) to (?:see|meet|hear)',result['text'],re.I),result
                assert not re.search(r'"tool"\s*:|\bawk\b',result['text'],re.I),result
        results.append({'agent':identifier,'room':room,'case':case_name,'query':query,**result,'memory':receipt})
        (root/'result.json').write_text(json.dumps({'ok':False,'results':results},indent=2,ensure_ascii=False))
        print(json.dumps({'agent':identifier,'room':room,**result},ensure_ascii=False),flush=True)
    (root/'result.json').write_text(json.dumps({'ok':True,'results':results},indent=2,ensure_ascii=False))
    print('PASS actual local work, speaker anchoring, family/visitor recall and conversation protocol with native/full room context')
finally:
    memory.close()
    bridge.lock.close()
    print('Artifacts:',root)
