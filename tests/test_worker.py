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

    def run_mocked_work(self, transport, turns=1):
        """Exercise the real loop and spool, never an API or native/model process."""
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
        memory = Mock()
        memory.status.return_value = {"ready": True}
        memory.prepare.side_effect = lambda job: (job, {})
        memory.remember_generation.return_value = {"ref": "test-native-reference"}
        child = Mock(stdout=json.dumps({"text": "Test reply", "digest": "a" * 64}))
        with tempfile.TemporaryDirectory(prefix="8c-work-loop-") as directory:
            root = Path(directory)
            config = root / "agents.json"
            config.write_text(json.dumps({"agents": agents, "memory": {}}))
            with patch.dict(os.environ, {"WORKER_KEY": "k" * 32}), \
                 patch.object(worker.os, "umask"), \
                 patch.object(worker, "Memory", return_value=memory), \
                 patch.object(worker, "inventory", return_value="a" * 64), \
                 patch.object(worker, "sync_memory", return_value=True), \
                 patch.object(worker, "request", side_effect=transport), \
                 patch.object(worker.subprocess, "run", return_value=child), \
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
                return {"ok": True, "job": {"id": len(polls), "lease": "test-lease"}}
            self.assertEqual(path, "/api/worker/result")
            if body["id"] == 1:
                # The server accepted this result but its acknowledgement was lost;
                # it could grant the next lease if the worker incorrectly polled.
                raise OSError("lost acknowledgement")
            return {"ok": True}

        pending = self.run_mocked_work(transport)
        self.assertEqual(polls, ["aye"])
        self.assertIsNotNone(pending)
        self.assertEqual(pending["id"], 1)

    def test_work_rotates_agents_with_continuously_queued_jobs(self):
        polls = []

        def transport(_origin, path, body=None, **_kwargs):
            if path == "/api/worker/poll":
                polls.append(body["agent"])
                return {"ok": True, "job": {"id": len(polls), "lease": "test-lease"}}
            self.assertEqual(path, "/api/worker/result")
            return {"ok": True}

        self.assertIsNone(self.run_mocked_work(transport, turns=3))
        self.assertEqual(polls, ["aye", "cheet", "aye"])

    def test_reasoning_is_not_published_as_a_reply(self):
        self.assertEqual(worker.visible_text("<think>private reasoning</think> Hello"),"Hello")
        self.assertEqual(worker.visible_text("<think>unfinished reasoning"),"")


if __name__ == "__main__": unittest.main()
