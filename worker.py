#!/usr/bin/env python3
"""8c local inference worker. Python stdlib; inference never leaves literal loopback.

HTTPS is used only for chat jobs/results. No model pulling or fallback. A single
locked spool retains a finished result until the server acknowledges it.
"""
import argparse
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from memory import Memory, MemoryFault

LIMIT = 1_048_576


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Redirect refused")


def endpoint(value, inference=False):
    url = urllib.parse.urlsplit(value)
    if url.username or url.password or url.query or url.fragment or url.path not in ("", "/"):
        raise ValueError("Endpoint must be an origin without credentials, path, query, or fragment")
    host = url.hostname
    if not host or url.port == 0:
        raise ValueError("Invalid endpoint")
    try:
        local = ipaddress.ip_address(host).is_loopback
    except ValueError:
        local = False
    if inference and (not local or host not in ("127.0.0.1", "::1") or url.scheme != "http"):
        raise ValueError("Inference requires http://127.0.0.1 or http://[::1]")
    if not inference and url.scheme != "https" and not (local and url.scheme == "http"):
        raise ValueError("Chat transport requires HTTPS (HTTP is allowed on literal loopback for tests)")
    return value.rstrip("/")


def request(origin, path, body=None, timeout=15, inference=False):
    origin = endpoint(origin, inference=inference)  # Validate before every connection.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
    req = urllib.request.Request(origin + path, data=data, headers={"Content-Type": "application/json"})
    with opener.open(req, timeout=timeout) as response:
        raw = response.read(LIMIT + 1)
        if len(raw) > LIMIT:
            raise ValueError("Response too large")
        value = json.loads(raw, parse_float=str)
        if not isinstance(value, dict):
            raise ValueError("Expected a JSON object")
        return value


def inventory(agent):
    if agent.get("runtime") != "ollama":
        raise ValueError("This worker supports the Ollama adapter; configure actual runtimes explicitly")
    local = endpoint(agent["endpoint"], inference=True)
    tags = request(local, "/api/tags", inference=True)
    model = next((m for m in tags.get("models", []) if m.get("name") == agent["model"]), None)
    if not model or not re.fullmatch(r"[0-9a-f]{64}", model.get("digest", "")):
        raise ValueError("Configured model is not installed locally")
    if not isinstance(model.get("size"), int) or model["size"] <= 0 or model.get("remote_model") or model.get("remote_host") or agent["model"].endswith(":cloud"):
        raise ValueError("Local weights required")
    if agent.get("digest") and agent["digest"] != model["digest"]:
        raise ValueError("Pinned model digest mismatch")
    # Ollama cloud stubs expose a remote model in /api/show. They are not local weights.
    shown = request(local, "/api/show", {"model": agent["model"]}, inference=True)
    if shown.get("remote_model") or shown.get("remote_host") or shown.get("details", {}).get("format") != "gguf":
        raise ValueError("Expected installed GGUF weights, not a remote model")
    return model["digest"]


def visible_text(text):
    # Reasoning sections are internal model output, not chat text.
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    if "<think>" in text:
        text = text.split("<think>", 1)[0]
    return text.strip()


def infer(agent, job):
    digest = inventory(agent)
    if job["digest"] != digest or job["model"] != agent["model"] or job["runtime"] != agent["runtime"] or job["agent"] != agent["id"]:
        raise ValueError("Job/model provenance mismatch")
    options = {"num_predict": 256, "num_ctx": 8192}
    if agent.get("cpu_only"):
        options["num_gpu"] = 0
    start = time.monotonic_ns()
    result = request(agent["endpoint"], "/api/generate", {
        "model": agent["model"], "prompt": job["prompt"], "system": job["system"], "stream": False,
        "options": options, "keep_alive": "5m",
    }, timeout=95, inference=True)
    elapsed = (time.monotonic_ns() - start) // 1_000_000
    if result.get("error") or not result.get("done") or result.get("model") != agent["model"]:
        raise ValueError("Local runtime did not finish a reply")
    text = visible_text(result.get("response", ""))
    if not text or len(text) > 4000:
        raise ValueError("Local model returned an empty or oversized reply")
    if inventory(agent) != digest:
        raise ValueError("Model changed during inference")
    tokens = result.get("eval_count", 0)
    if not isinstance(tokens, int) or tokens < 0:
        raise ValueError("Invalid token count")
    return {"text": text, "digest": digest, "elapsed_ms": elapsed, "output_tokens": tokens}


def save(path, value):
    temporary = path.with_suffix(".new")
    with temporary.open("w", encoding="utf-8") as output:
        json.dump(value, output, ensure_ascii=False)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)
    sync_directory(path.parent)


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def deliver(origin, key, pending):
    body = json.loads(pending.read_text())
    try:
        reply = request(origin, "/api/worker/result", {**body, "worker_key": key})
        if not reply.get("ok"):
            return False
    except urllib.error.HTTPError as error:
        raw = error.read(8192)
        try:
            reason = json.loads(raw).get("error", "")
        except ValueError:
            return False
        if error.code != 400 or reason not in ("Reply lease expired or cancelled", "Reply lease mismatch", "Unknown reply job"):
            return False
        # Preserve the rejected result locally for diagnosis; do not inject a late reply.
        os.replace(pending, pending.with_name(f"expired-{body['id']}-{body['lease'][:12]}.json"))
        sync_directory(pending.parent)
        print("Reply expired/cancelled; server exposes a retry", flush=True)
        return True
    pending.unlink()
    sync_directory(pending.parent)
    print("Reply acknowledged", flush=True)
    return True


def sync_memory(memory, origin, key):
    # Bounded catch-up; no job is claimed until the feed has reached its head.
    for _ in range(64):
        page = request(origin, "/api/worker/memory", {"worker_key": key, "after": memory.cursor})
        memory.ingest(page)
        if page.get("caught_up"):
            return True
    return False


def work(config, origin, directory):
    os.umask(0o077)
    origin = endpoint(origin)
    key = os.environ.get("WORKER_KEY", "")
    if len(key) < 32:
        raise ValueError("WORKER_KEY must contain at least 32 characters")
    configuration = json.loads(Path(config).read_text())
    agents = configuration["agents"]
    if not 1 <= len(agents) <= 16:
        raise ValueError("Configure 1–16 agents")
    for agent in agents:
        endpoint(agent["endpoint"], inference=True)
        if agent["runtime"] != "ollama":
            raise ValueError("Unsupported runtime")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "worker.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        memory = Memory(directory / "mem8", configuration["memory"], agents, Path(config).resolve().parent)
        pending = directory / "pending-result.json"
        inventory_cache = {}
        while True:
            try:
                if pending.exists():
                    if not deliver(origin, key, pending):
                        time.sleep(3)
                        continue
                if not sync_memory(memory, origin, key):
                    continue
                for agent in agents:
                    try:
                        cached = inventory_cache.get(agent["id"])
                        if cached is None or time.monotonic_ns() - cached[0] > 60_000_000_000:
                            cached = (time.monotonic_ns(), inventory(agent))
                            inventory_cache[agent["id"]] = cached
                        proof = {"agent": agent["id"], "model": agent["model"], "runtime": agent["runtime"], "digest": cached[1], "memory": memory.status()}
                        reply = request(origin, "/api/worker/poll", {**proof, "worker_key": key})
                        job = reply.get("job")
                        if not reply.get("ok") or not job:
                            continue
                        result = {**proof, "id": job["id"], "lease": job["lease"]}
                        try:
                            # A request may have arrived after the preceding feed page.
                            if not sync_memory(memory, origin, key):
                                raise MemoryFault("Memory catch-up exceeded this turn's budget")
                            prepared, receipt = memory.prepare(job)
                            # A separate bounded subprocess gives inference a whole-operation deadline,
                            # including inventory, connection, and response reading.
                            child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--infer"],
                                input=json.dumps({"agent": agent, "job": prepared}), text=True,
                                capture_output=True, timeout=115, check=True)
                            result.update(json.loads(child.stdout))
                            receipt["written"] = memory.remember_generation(agent, job, result, receipt)
                            result["memory"] = receipt
                        except MemoryFault:
                            result["error"] = "Native MEM|8 could not be verified. Your message is saved; please retry after memory recovery."
                            request(origin, "/api/worker/poll", {**proof, "memory": memory.status(False), "worker_key": key})
                        except subprocess.TimeoutExpired:
                            result["error"] = "Local inference exceeded its 115-second deadline. Please retry."
                        except (subprocess.CalledProcessError, ValueError):
                            result["error"] = "Local model could not produce a reply. Please retry."
                        save(pending, result)
                        deliver(origin, key, pending)
                        break
                    except (OSError, ValueError, KeyError) as error:
                        # Log only the error class: URLs/response text may contain private context.
                        print(f"Worker {agent['id']}: {type(error).__name__}; retrying", flush=True)
                time.sleep(3)
            except MemoryFault:
                for agent in agents:
                    cached = inventory_cache.get(agent["id"])
                    if cached:
                        try:
                            request(origin, "/api/worker/poll", {"worker_key": key, "agent": agent["id"], "model": agent["model"], "runtime": agent["runtime"], "digest": cached[1], "memory": memory.status(False)})
                        except (OSError, ValueError):
                            pass
                print("Native MEM8 unavailable; retaining source and retrying", flush=True)
                time.sleep(3)
            except (OSError, ValueError) as error:
                print(f"Worker transport: {type(error).__name__}; retaining result", flush=True)
                time.sleep(3)


if __name__ == "__main__":
    if sys.argv[1:] == ["--infer"]:
        payload = json.loads(sys.stdin.read(LIMIT))
        print(json.dumps(infer(payload["agent"], payload["job"])))
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--config", default="agents.example.json")
        parser.add_argument("--chat", default="https://8c.is")
        parser.add_argument("--state", default="/var/lib/8c-worker")
        args = parser.parse_args()
        work(args.config, args.chat, args.state)
