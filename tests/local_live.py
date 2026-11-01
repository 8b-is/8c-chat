"""Opt-in actual-model round trip; creates isolated chat/worker state, then restarts chat.

Run from the repository root after cargo build. LOCAL_ENDPOINT may point to a
dedicated literal-loopback Ollama instance. This is deliberately outside unittest.
"""
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

root = Path(tempfile.mkdtemp(prefix="8c-real-e2e-", dir=os.environ.get("TMPDIR", "/tmp/opencode")))
key = secrets.token_hex(32)
origin = "http://127.0.0.1:18899"
config = json.loads(Path("agents.example.json").read_text())
for agent in config["agents"]:
    agent["endpoint"] = os.environ.get("LOCAL_ENDPOINT", agent["endpoint"])
(root / "agents.json").write_text(json.dumps(config))
env = {**os.environ, "BIND_ADDR": "127.0.0.1:18899", "JOURNAL_PATH": str(root / "chat.jsonl"),
       "AGENTS_CONFIG": str(root / "agents.json"), "WORKER_KEY": key}
server = subprocess.Popen(["target/debug/8c-chat"], env=env, stderr=subprocess.PIPE, text=True)
local = None
log = (root / "worker.log").open("w")
try:
    assert "listening on" in server.stderr.readline()
    profiles = [worker.request(origin, "/api/introduce", {"name": name, "bio": "I make little wooden boats."})
                for name in ["Éva", "Milo"]]
    worker.request(origin, "/api/hottub", {"token": profiles[0]["token"], "running": False})
    local = subprocess.Popen(["python3", "worker.py", "--config", str(root / "agents.json"),
                              "--chat", origin, "--state", str(root / "worker")], env=env, stdout=log, stderr=log)
    ids = []
    for index, agent in enumerate(["aye", "cheet"]):
        data = worker.request(origin, "/api/messages", {"token": profiles[index]["token"], "room": "model-lab",
            "agent": agent, "text": "Hello! What is my name, and what do I enjoy making? Answer in one sentence.",
            "client_id": secrets.token_hex(16)})
        ids.append(data["message"]["id"])
    deadline = time.monotonic_ns() + 240_000_000_000
    replies = []
    while time.monotonic_ns() < deadline:
        data = worker.request(origin, "/api/messages?room=model-lab")
        replies = [m for m in data["messages"] if m.get("reply_to") in ids and m.get("origin") == "local-worker"]
        if len(replies) == 2:
            break
        failed = [j for j in data["jobs"] if j["status"] == "failed"]
        if failed:
            raise RuntimeError(failed)
        time.sleep(2)
    assert len(replies) == 2, "two actual local replies required"
    for index, profile in enumerate(profiles):
        reply = next(m for m in replies if m["reply_to"] == ids[index])
        assert profile["profile"]["name"].casefold() in reply["text"].casefold(), "reply must address the current participant"
        assert "boat" in reply["text"].lower(), "reply must use the remembered introduction"
    for reply in replies:
        print(json.dumps({k: reply[k] for k in ["sender_name", "text", "model", "model_digest", "elapsed_ms", "output_tokens"]}, ensure_ascii=False), flush=True)
    server.terminate()
    server.wait(timeout=10)
    server = subprocess.Popen(["target/debug/8c-chat"], env=env, stderr=subprocess.PIPE, text=True)
    assert "listening on" in server.stderr.readline()
    restored = worker.request(origin, "/api/introduce", {"token": profiles[0]["token"]})
    assert restored["profile"] == profiles[0]["profile"]
    data = worker.request(origin, "/api/messages?room=model-lab")
    assert len([m for m in data["messages"] if m.get("origin") == "local-worker"]) == 2
    (root / "result.json").write_text(json.dumps({"ok": True, "replies": replies, "restart_restored": True}, ensure_ascii=False, indent=2))
    print("PASS actual local Aye + Cheet, simultaneous requests, persisted profiles and messages after restart.", flush=True)
finally:
    if local:
        local.terminate()
        local.wait(timeout=10)
    server.terminate()
    server.wait(timeout=10)
    log.close()
    print("Artifacts:", root, flush=True)
