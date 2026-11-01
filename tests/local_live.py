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
config["agents"] = [a for a in config["agents"] if a["runtime"] == "ollama"]
for source in config["memory"]["family"]:
    source["path"] = str(Path(source["path"]).resolve())
for agent in config["agents"]:
    agent.pop("workbench", None)  # This check isolates local model/memory continuity.
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
        assert profiles[1 - index]["profile"]["name"].casefold() not in reply["text"].casefold(), "reply must not borrow another visitor's name"
        assert "boat" in reply["text"].lower(), "reply must use the remembered introduction"
        assert reply["memory"]["format"] == "MEM8EV02"
        scopes = {r["scope"] for r in reply["memory"]["records"]}
        assert {"family", "agent:" + ["aye", "cheet"][index], "visitor:" + profile["profile"]["handle"]} <= scopes
    for reply in replies:
        print(json.dumps({k: reply[k] for k in ["sender_name", "text", "model", "model_digest", "elapsed_ms", "output_tokens"]}, ensure_ascii=False), flush=True)
    local.terminate()
    local.wait(timeout=10)
    local = None
    remembered = worker.request(origin, "/api/messages", {"token": profiles[0]["token"], "room": "model-lab", "ask_aye": "no",
        "text": "My keepsake is a copper lighthouse carved by my grandfather.", "client_id": secrets.token_hex(16)})
    for index in range(70):
        worker.request(origin, "/api/messages", {"token": profiles[index % 2]["token"], "room": "model-lab", "ask_aye": "no",
            "text": "Memory continuity check: a newer unrelated note about lunch.", "client_id": secrets.token_hex(16)})
    assert remembered["message"]["id"] not in [m["id"] for m in worker.request(origin, "/api/messages?room=model-lab")["messages"]]
    server.terminate()
    server.wait(timeout=10)
    server = subprocess.Popen(["target/debug/8c-chat"], env=env, stderr=subprocess.PIPE, text=True)
    assert "listening on" in server.stderr.readline()
    restored = worker.request(origin, "/api/introduce", {"token": profiles[0]["token"]})
    assert restored["profile"] == profiles[0]["profile"]
    local = subprocess.Popen(["python3", "worker.py", "--config", str(root / "agents.json"), "--chat", origin, "--state", str(root / "worker")], env=env, stdout=log, stderr=log)
    continuity = worker.request(origin, "/api/messages", {"token": profiles[0]["token"], "room": "model-lab", "agent": "aye",
        "text": "What keepsake did I tell you about earlier? Name the object and material in one short sentence.", "client_id": secrets.token_hex(16)})
    family = worker.request(origin, "/api/messages", {"token": profiles[1]["token"], "room": "model-lab", "agent": "cheet",
        "text": "What does LLL mean in our shared family context? One short sentence.", "client_id": secrets.token_hex(16)})
    later_ids = [continuity["message"]["id"], family["message"]["id"]]
    deadline = time.monotonic_ns() + 240_000_000_000
    later = []
    while time.monotonic_ns() < deadline:
        data = worker.request(origin, "/api/messages?room=model-lab")
        later = [m for m in data["messages"] if m.get("reply_to") in later_ids and m.get("origin") == "local-worker"]
        if len(later) == 2:
            break
        failed = [j for j in data["jobs"] if j["status"] == "failed"]
        if failed:
            raise RuntimeError(failed)
        time.sleep(2)
    assert len(later) == 2
    old_recall = next(m for m in later if m["reply_to"] == later_ids[0])
    assert "copper" in old_recall["text"].lower() and "lighthouse" in old_recall["text"].lower(), old_recall["text"]
    family_recall = next(m for m in later if m["reply_to"] == later_ids[1])
    assert "life long learning" in family_recall["text"].lower(), family_recall["text"]
    for reply in later:
        print(json.dumps(reply, ensure_ascii=False), flush=True)
    (root / "result.json").write_text(json.dumps({"ok": True, "replies": replies + later, "restart_restored": True, "beyond_recent_ring": True}, ensure_ascii=False, indent=2))
    print("PASS actual local Aye + Cheet, native shared family/individual/visitor/room recall, restart continuity beyond 64-message ring.", flush=True)
finally:
    if local:
        local.terminate()
        local.wait(timeout=10)
    server.terminate()
    server.wait(timeout=10)
    log.close()
    print("Artifacts:", root, flush=True)
