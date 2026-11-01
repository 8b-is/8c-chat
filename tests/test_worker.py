import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, HTTPServer
import worker


class WorkerTests(unittest.TestCase):
    def test_inference_boundary_rejects_dns_proxies_redirects_and_cloud(self):
        for url in ["http://localhost:11434", "https://example.com", "http://127.0.0.1.evil/", "http://127.0.0.1@elsewhere/", "http://127.0.0.1/path", "http://127.1", "http://2130706433", "http://0.0.0.0", "http://127.0.0.1?next=x"]:
            with self.assertRaises(ValueError): worker.endpoint(url, inference=True)
        self.assertEqual(worker.endpoint("http://[::1]:11434/", inference=True), "http://[::1]:11434")
        class Redirect(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302); self.send_header("Location", "http://example.com/"); self.end_headers()
            def log_message(self, *args): pass
        server=HTTPServer(("127.0.0.1",0),Redirect)
        thread=threading.Thread(target=server.serve_forever); thread.start()
        try:
            with patch.dict(os.environ, {"http_proxy":"http://127.0.0.1:1", "HTTP_PROXY":"http://127.0.0.1:1"}):
                with self.assertRaisesRegex(ValueError,"Redirect refused"):
                    worker.request(f"http://127.0.0.1:{server.server_port}","/",inference=True)
        finally: server.shutdown(); thread.join(); server.server_close()

    def test_cloud_stub_and_digest_mismatch_cannot_run(self):
        agent={"id":"aye","model":"aye:latest","runtime":"ollama","endpoint":"http://127.0.0.1:11434"}
        tags={"models":[{"name":"aye:latest","size":1024,"digest":"a"*64}]}
        with patch.object(worker,"request",side_effect=[tags,{"remote_model":"cloud","details":{"format":"gguf"}}]):
            with self.assertRaises(ValueError): worker.inventory(agent)
        with patch.object(worker,"request",return_value=tags):
            with self.assertRaises(ValueError): worker.inventory({**agent,"digest":"b"*64})

    def test_failed_result_delivery_keeps_spool_then_ack_removes_it(self):
        with tempfile.TemporaryDirectory(prefix="8c-worker-") as directory:
            path=Path(directory)/"pending-result.json"
            worker.save(path,{"id":3,"lease":"test-lease","text":"Hello 💙"})
            with patch.object(worker,"request",side_effect=OSError("offline")):
                with self.assertRaises(OSError): worker.deliver("https://8c.is","k"*32,path)
            self.assertEqual(json.loads(path.read_text())["text"],"Hello 💙")
            with patch.object(worker,"request",return_value={"ok":True,"duplicate":True}):
                self.assertTrue(worker.deliver("https://8c.is","k"*32,path))
            self.assertFalse(path.exists())

    def test_reasoning_is_not_published_as_a_reply(self):
        self.assertEqual(worker.visible_text("<think>private reasoning</think> Hello"),"Hello")
        self.assertEqual(worker.visible_text("<think>unfinished reasoning"),"")

    def test_unperformed_or_malformed_work_is_not_published(self):
        agent={'id':'cheet','name':'Cheet','runtime':'ollama','model':'local:latest',
               'endpoint':'http://127.0.0.1:11435','workbench':'http://127.0.0.1:8799'}
        job={'id':1,'agent':'cheet','runtime':'ollama','model':agent['model'],'digest':'a'*64,
             'system':'Answer the current visitor.','prompt':'Use the write tool to create artifacts/hello.txt.',
             'query':'Use the write tool to create artifacts/hello.txt.'}
        for response in ['{"tool":"run","command":"unfinished', '[]', '{"tool":"write","text":"missing path"}',
                         '{"tool":"say","text":"I created artifacts/hello.txt."}']:
            with self.subTest(response=response), patch.object(worker,'inventory',return_value='a'*64), \
                 patch.object(worker,'bridge',return_value={'context':'Authorized workbench'}) as bridge, \
                 patch.object(worker,'request',return_value={'done':True,'model':agent['model'],'eval_count':10,'response':response}):
                with self.assertRaises(ValueError): worker.infer(agent,job)
                self.assertEqual([call.args[1] for call in bridge.call_args_list],['/workspace-start'])
        with patch.object(worker,'inventory',return_value='a'*64), patch.object(worker,'bridge',side_effect=OSError('offline')), \
             patch.object(worker,'request') as model:
            with self.assertRaises(worker.ParticipantResting): worker.infer(agent,job)
            model.assert_not_called()


if __name__ == "__main__": unittest.main()
