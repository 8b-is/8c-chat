import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from http.server import BaseHTTPRequestHandler, HTTPServer
import worker


class WorkerTests(unittest.TestCase):
    def test_tools_stay_closed_unless_the_server_boolean_is_true(self):
        self.assertFalse(worker.tools_allowed({}))
        self.assertFalse(worker.tools_allowed({"tools_ready": "true"}))
        self.assertFalse(worker.tools_allowed({"tools_ready": 1}))
        self.assertFalse(worker.tools_allowed({"tools_ready": False}))
        self.assertTrue(worker.tools_allowed({"tools_ready": True}))

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

    def run_mocked_work(self, transport, turns=1, acceptance=None):
        """Run the real loop/spool with isolated files and no model/native/network I/O."""
        class StopWork(BaseException):
            pass

        sleeps = 0

        def sleep(_seconds):
            nonlocal sleeps
            sleeps += 1
            if sleeps >= turns:
                raise StopWork()

        agents = [{"id": name, "model": name + ":latest", "runtime": "ollama",
                   "endpoint": "http://127.0.0.1:11434"} for name in ("aye", "cheet")]
        if acceptance is not None:
            agents[0].update(runtime="opencode", provider="test-provider",
                             workbench="http://127.0.0.1:8799")
        memory = Mock()
        memory.status.return_value = {"ready": True}
        generated = {"text": "Test reply", "digest": "a" * 64, "memory": {"ready": True}}
        if acceptance is not None:
            generated["work"] = {"artifact": "synthetic-test-artifact"}

        def bridge(agent, path, job=None, **_kwargs):
            if path == "/inventory":
                return {"available": True, "pause": "", "workspace": "test-workspace"}
            self.assertEqual(path, "/accept")
            self.assertIsNotNone(acceptance)
            return acceptance(agent, job)

        with tempfile.TemporaryDirectory(prefix="8c-work-loop-") as directory:
            root = Path(directory)
            config = root / "agents.json"
            config.write_text(json.dumps({"agents": agents, "memory": {}}))
            with patch.dict(os.environ, {"WORKER_KEY": "k" * 32}), \
                 patch.object(worker.os, "umask"), \
                 patch.object(worker, "Memory", return_value=memory), \
                 patch.object(worker, "inventory", return_value="a" * 64), \
                 patch.object(worker, "sync_memory", return_value=True), \
                 patch.object(worker, "generate_job", return_value=generated), \
                 patch.object(worker, "bridge", side_effect=bridge), \
                 patch.object(worker, "request", side_effect=transport), \
                 patch.object(worker.time, "sleep", side_effect=sleep):
                with self.assertRaises(StopWork):
                    worker.work(config, "https://example.invalid", root / "state")
            pending = root / "state" / "pending-result.json"
            return json.loads(pending.read_text()) if pending.exists() else None

    def test_work_retains_pending_before_polling_another_agent_on_delivery_error(self):
        polls = []

        def transport(_origin, path, body=None, **_kwargs):
            if path == "/api/worker/poll":
                polls.append(body["agent"])
                return {"ok": True, "job": {"id": len(polls), "lease": "test-lease", "room": "model-lab"}}
            self.assertEqual(path, "/api/worker/result")
            if body["id"] == 1:
                raise OSError("lost acknowledgement")
            return {"ok": True}

        pending = self.run_mocked_work(transport)
        self.assertEqual(polls, ["aye"])
        self.assertIsNotNone(pending)
        self.assertEqual(pending["id"], 1)

    def test_work_retries_same_result_before_resuming_rotated_agents(self):
        actions, results = [], []

        def transport(_origin, path, body=None, **_kwargs):
            if path == "/api/worker/poll":
                actions.append(("poll", body["agent"]))
                return {"ok": True, "job": {"id": 1 if body["agent"] == "aye" else 2,
                                             "lease": "lease-" + body["agent"], "room": "model-lab"}}
            self.assertEqual(path, "/api/worker/result")
            actions.append(("result", body["id"]))
            results.append(body)
            if len(results) == 1:
                raise OSError("lost acknowledgement")
            return {"ok": True}

        self.assertIsNone(self.run_mocked_work(transport, turns=2))
        self.assertEqual(actions, [("poll", "aye"), ("result", 1), ("result", 1),
                                   ("poll", "cheet"), ("result", 2)])
        self.assertEqual(results[0], results[1])

    def test_work_rotates_agents_with_continuously_queued_jobs(self):
        polls = []

        def transport(_origin, path, body=None, **_kwargs):
            if path == "/api/worker/poll":
                polls.append(body["agent"])
                return {"ok": True, "job": {"id": len(polls), "lease": "test-lease", "room": "model-lab"}}
            self.assertEqual(path, "/api/worker/result")
            return {"ok": True}

        self.assertIsNone(self.run_mocked_work(transport, turns=3))
        self.assertEqual(polls, ["aye", "cheet", "aye"])

    def test_work_retains_hosted_result_until_peer_acceptance_then_rotates(self):
        for error_type in (OSError, worker.ParticipantResting):
            for turns in (1, 2):
                with self.subTest(error=error_type.__name__, turns=turns):
                    actions, results, accepted = [], [], []

                    def transport(_origin, path, body=None, **_kwargs):
                        if path == "/api/worker/poll":
                            actions.append(("poll", body["agent"]))
                            if body["agent"] == "aye":
                                self.assertEqual(body["provider"], "test-provider")
                                self.assertTrue(body["available"])
                            return {"ok": True, "job": {"id": 1 if body["agent"] == "aye" else 2,
                                "lease": "lease-" + body["agent"], "room": "model-lab"}}
                        self.assertEqual(path, "/api/worker/result")
                        actions.append(("result", body["id"]))
                        results.append(body)
                        return {"ok": True, "duplicate": len(results) > 1 and body["id"] == 1}

                    def acceptance(agent, job):
                        actions.append(("accept", job["id"]))
                        accepted.append(job)
                        self.assertEqual(job["room"], "model-lab")
                        self.assertEqual(job["lease"], "lease-" + agent["id"])
                        if len(accepted) == 1:
                            raise error_type("peer acknowledgement unavailable")
                        return {"ok": True}

                    pending = self.run_mocked_work(transport, turns=turns, acceptance=acceptance)
                    expected = [("poll", "aye"), ("result", 1), ("accept", 1)]
                    if turns == 1:
                        self.assertIsNotNone(pending)
                        self.assertEqual(pending["id"], 1)
                        self.assertTrue(pending["work"])
                    else:
                        expected += [("result", 1), ("accept", 1), ("poll", "cheet"),
                                     ("result", 2), ("accept", 2)]
                        self.assertIsNone(pending)
                        self.assertEqual(results[0], results[1])
                        self.assertEqual(accepted[0], accepted[1])
                    self.assertEqual(actions, expected)

    def test_reasoning_is_not_published_as_a_reply(self):
        self.assertEqual(worker.visible_text("<think>private reasoning</think> Hello"),"Hello")
        self.assertEqual(worker.visible_text("<think>unfinished reasoning"),"")

    def test_unperformed_or_malformed_work_is_not_published(self):
        agent={'id':'cheet','name':'Cheet','runtime':'ollama','model':'local:latest',
               'endpoint':'http://127.0.0.1:11435','workbench':'http://127.0.0.1:8799'}
        job={'id':1,'agent':'cheet','runtime':'ollama','model':agent['model'],'digest':'a'*64,
             'system':'Answer the current visitor.','prompt':'Use the write tool to create artifacts/hello.txt.',
             'query':'Use the write tool to create artifacts/hello.txt.','tools_ready':True}
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
