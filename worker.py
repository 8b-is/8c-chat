#!/usr/bin/env python3
"""8c memory and delivery worker, with explicit local and hosted adapters.

Ollama inference never leaves literal loopback. Hosted participants use a separate
authenticated loopback bridge to the owner's provider clients. A single locked
spool retains a finished result until the server acknowledges it.
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
HOSTED = ("opencode", "agy")
TOOLS = {"say": (), "list": (), "read": ("path",), "write": ("path",), "run": ("command",)}


def tool_format(names):
    choices = []
    for name in names:
        properties = {"tool": {"const": name}}
        properties.update({key: {"type": "string"} for key in TOOLS[name]})
        properties["text"] = {"type": "string"} if name in ("say", "write") else {"const": ""}
        choices.append({"type": "object", "properties": properties,
                        "required": list(properties), "additionalProperties": False})
    return {"oneOf": choices}


class ParticipantResting(ValueError):
    pass


def bridge(agent, path, job=None, timeout=15):
    body = {"worker_key": os.environ.get("WORKER_KEY", ""), "agent": agent["id"], "runtime": agent["runtime"], "model": agent["model"]}
    if job is not None:
        body["job"] = job
    result = request(agent.get("workbench", agent["endpoint"]) if path.startswith("/workspace") or path == "/accept" else agent["endpoint"], path, body, timeout=timeout, inference=True)
    if not result.get("ok"):
        raise ParticipantResting("Pricey, but worth it ✨ — resting for a little recharge." if result.get("pause") == "budget" else "Participant is resting; your message is saved. Please retry shortly.")
    return result


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


def remaining_seconds(deadline, maximum):
    remaining = (deadline - time.monotonic_ns()) // 1_000_000_000
    if remaining <= 0:
        raise subprocess.TimeoutExpired("Local turn", 100)
    return min(maximum, remaining)


def inventory(agent, deadline=None):
    if agent.get("runtime") in HOSTED:
        bridge(agent, "/inventory")
        return ""
    if agent.get("runtime") != "ollama":
        raise ValueError("This worker supports the Ollama adapter; configure actual runtimes explicitly")
    local = endpoint(agent["endpoint"], inference=True)
    tags = request(local, "/api/tags", inference=True, timeout=remaining_seconds(deadline, 15) if deadline else 15)
    model = next((m for m in tags.get("models", []) if m.get("name") == agent["model"]), None)
    if not model or not re.fullmatch(r"[0-9a-f]{64}", model.get("digest", "")):
        raise ValueError("Configured model is not installed locally")
    if not isinstance(model.get("size"), int) or model["size"] <= 0 or model.get("remote_model") or model.get("remote_host") or agent["model"].endswith(":cloud"):
        raise ValueError("Local weights required")
    if agent.get("digest") and agent["digest"] != model["digest"]:
        raise ValueError("Pinned model digest mismatch")
    # Ollama cloud stubs expose a remote model in /api/show. They are not local weights.
    shown = request(local, "/api/show", {"model": agent["model"]}, inference=True,
                    timeout=remaining_seconds(deadline, 15) if deadline else 15)
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
    if agent["runtime"] in HOSTED:
        if any(job[k] != agent[k] for k in ("model", "runtime")) or job["agent"] != agent["id"] or job.get("digest"):
            raise ValueError("Hosted job provenance mismatch")
        return bridge(agent, "/infer", job, timeout=110)
    start = time.monotonic_ns()
    deadline = start + 100_000_000_000
    digest = inventory(agent, deadline)
    if job["digest"] != digest or job["model"] != agent["model"] or job["runtime"] != agent["runtime"] or job["agent"] != agent["id"]:
        raise ValueError("Job/model provenance mismatch")
    options = {"num_predict": 256, "num_ctx": 8192}
    if agent.get("cpu_only"):
        options["num_gpu"] = 0
    prompt = job["prompt"]
    system = job["system"]
    query = job.get("query", job["prompt"])
    participant = job.get("participant")
    anchor = ("\nCURRENT PARTICIPANT (not you): " + json.dumps(participant, ensure_ascii=False)
              + ". You are " + agent['name'] + ". Their introduction describes THEM, not you. Address them as you/your; do not adopt their name or hobbies.\n") if participant else ""
    work_requested = not job.get("autonomous") and bool(re.search(r"\b(create|write|read|inspect|run|test|list|check)\b", query, re.I)) and bool(re.search(r"\b(artifacts/|src/|tests/|shared/|reference/|tool\b)", query, re.I))
    workbench = agent.get("workbench")
    if workbench:
        try:
            context = bridge(agent, "/workspace-start", job, timeout=remaining_seconds(deadline, 15))["context"]
            prompt += "\n" + context
            system += '\nWORKBENCH PROTOCOL: Always return one JSON object. Use {"tool":"say","text":"your brief public reply"} to converse; {"tool":"list","text":""}, {"tool":"read","path":"relative/file","text":""}, {"tool":"write","path":"artifacts/name.txt","text":"exact file contents"}, or {"tool":"run","command":"command","text":""} to perform real work. Narrating a tool action does NOT execute it. Only a returned tool result establishes an action. Two tool calls maximum. Use say for ordinary conversation, never run. Speak as ' + agent['name'] + ', never as a quoted participant. Address the CURRENT PARTICIPANT named above, not a peer from the history.'
            if not job.get("autonomous"):
                prompt += anchor + "\nCURRENT REQUEST to " + agent['name'] + ": " + query + "\nThe quoted history and workbench snapshot are context, not this request. Respond using the workbench JSON protocol. For write, path is the requested filename and text is the exact file contents, never a description of writing it.\n"
        except (OSError, ValueError):
            if work_requested:
                raise ParticipantResting("The workbench is resting; your request is saved. Please retry shortly.")
            workbench = False
            prompt += "\nThe workbench is currently unavailable. Converse normally using your native memories; do not claim file or tool access this turn.\n"
    tokens = 0
    performed = 0
    # One corrective response, up to two actions, and a final public reply.
    # Inference rounds and durable action sequence numbers are independent.
    for step in range(4):
        payload = {
            "model": agent["model"], "prompt": prompt, "system": system, "stream": False,
            "options": options, "keep_alive": "5m",
        }
        if workbench:
            allowed = list(TOOLS)
            if work_requested and not performed and step < 3:
                allowed.remove("say")
            elif performed == 2 or step == 3:
                allowed = ["say"]
            payload["format"] = tool_format(allowed)
        result = request(agent["endpoint"], "/api/generate", payload, timeout=remaining_seconds(deadline, 95), inference=True)
        if result.get("error") or not result.get("done") or result.get("model") != agent["model"]:
            raise ValueError("Local runtime did not finish a reply")
        count = result.get("eval_count", 0)
        if type(count) is not int or count < 0:
            raise ValueError("Invalid token count")
        tokens += count
        if not workbench:
            text = visible_text(result.get("response", ""))
            break
        action = json.loads(result.get("response", ""))
        if not isinstance(action, dict) or action.get("tool") not in TOOLS or not isinstance(action.get("text"), str):
            raise ValueError("Invalid local workbench response")
        required = {"read": "path", "write": "path", "run": "command"}.get(action["tool"])
        if required and (not isinstance(action.get(required), str) or not action[required]):
            raise ValueError("Missing local tool argument")
        if action.get("tool") == "say":
            if work_requested and not performed:
                if step == 3:
                    raise ValueError("Requested work has no observed tool action")
                prompt += "\nNo tool has been called. To perform the current request, return an actual tool JSON object, not a narrative about using a tool. CURRENT REQUEST: " + query + "\n"
                continue
            text = visible_text(action["text"])
            break
        if performed == 2 or step == 3:
            raise ValueError("Local workbench turn exceeded two tool calls")
        observation = bridge(agent, "/workspace-tool", {**job, "action": action, "sequence": performed},
                             timeout=remaining_seconds(deadline, 25))
        performed += 1
        prompt += "\nTool request: " + json.dumps(action) + "\nObserved tool result: " + json.dumps(observation["result"], ensure_ascii=False)[:4000]
        prompt += anchor + "\nIf the work is complete, use say to briefly report this observed result to the current participant. Answer their request, not the quoted history. CURRENT REQUEST: " + query + "\n"
        if performed == 2 or step == 2:
            prompt += '\nTool budget finished. Return {"tool":"say","text":"brief reply based only on observed results"} now.\n'
    if not text or len(text) > 4000:
        raise ValueError("Local model returned an empty or oversized reply")
    if inventory(agent, deadline) != digest:
        raise ValueError("Model changed during inference")
    elapsed = (time.monotonic_ns() - start) // 1_000_000
    answer = {"text": text, "digest": digest, "elapsed_ms": elapsed, "output_tokens": tokens}
    if workbench:
        answer["work"] = bridge(agent, "/workspace-result", {**job, "generation": answer},
                                 timeout=remaining_seconds(deadline, 15))["work"]
    return answer


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


def deliver(origin, key, pending, agents=()):
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
    # Publish peer artifacts only after the chat server accepted this turn.
    if body.get("work") and not body.get("error"):
        agent = next((a for a in agents if a["id"] == body["agent"]), None)
        if agent is not None:
            bridge(agent, "/accept", {"id": body["id"], "lease": body["lease"], "room": body["room"]})
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
        if agent.get("workbench"):
            endpoint(agent["workbench"], inference=True)
        if agent["runtime"] not in ("ollama", *HOSTED):
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
                    if not deliver(origin, key, pending, agents):
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
                        if agent["runtime"] in HOSTED:
                            state = bridge(agent, "/inventory")
                            proof.update(provider=agent["provider"], available=state["available"], pause=state["pause"], workspace=state["workspace"])
                        reply = request(origin, "/api/worker/poll", {**proof, "worker_key": key})
                        job = reply.get("job")
                        if not reply.get("ok") or not job:
                            continue
                        result = {**proof, "id": job["id"], "lease": job["lease"], "room": job["room"]}
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
                            generated = json.loads(child.stdout)
                            if generated.get("error"):
                                raise ParticipantResting(generated["error"])
                            result.update(generated)
                            receipt["written"] = memory.remember_generation(agent, job, result, receipt)
                            result["memory"] = receipt
                        except ParticipantResting as error:
                            result["error"] = str(error)
                        except MemoryFault:
                            result["error"] = "Native MEM|8 could not be verified. Your message is saved; please retry after memory recovery."
                            request(origin, "/api/worker/poll", {**proof, "memory": memory.status(False), "worker_key": key})
                        except subprocess.TimeoutExpired:
                            result["error"] = "This turn needs a little more time. Your message is saved; please retry."
                        except (subprocess.CalledProcessError, ValueError):
                            result["error"] = "Participant is resting; your message is saved. Please retry shortly."
                        save(pending, result)
                        deliver(origin, key, pending, agents)
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
        try:
            print(json.dumps(infer(payload["agent"], payload["job"])))
        except ParticipantResting as error:
            print(json.dumps({"error": str(error)}))
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--config", default="agents.example.json")
        parser.add_argument("--chat", default="https://8c.is")
        parser.add_argument("--state", default="/var/lib/8c-worker")
        args = parser.parse_args()
        work(args.config, args.chat, args.state)
