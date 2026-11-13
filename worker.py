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
from memory import Memory, MemoryFault, MemoryDeadline, personal_recall
from commons import exact_write_contract, exact_write_matches, is_login_insertion_text, LOGIN_CODE_RE

LIMIT = 1_048_576
HOSTED = ("opencode", "agy")
# Server lease: 150 seconds. Start before polling, keeping five seconds of
# conservative headroom and fifty for native write/readback plus delivery.
LEASE_BUDGET_NS = 145_000_000_000
FINALIZE_BUDGET_NS = 50_000_000_000
LOCAL_FINALIZE_NS = 45_000_000_000  # Two 15-second inventory calls + result RPC.
TOOLS = {"say": (), "decline": (), "list": (), "read": ("path",), "write": ("path",), "run": ("command",)}


def tool_format(names, exact_write=None):
    choices = []
    for name in names:
        properties = {"tool": {"const": name}}
        properties.update({key: {"type": "string"} for key in TOOLS[name]})
        properties["text"] = {"type": "string"} if name in ("say", "decline", "write") else {"const": ""}
        if name in ("say", "decline"):
            properties["text"]["maxLength"] = 600
        if name == 'write' and exact_write is not None:
            properties.update({key: {'const': value} for key, value in exact_write.items()})
        choices.append({"type": "object", "properties": properties,
                        "required": list(properties), "additionalProperties": False})
    return {"oneOf": choices}


class ParticipantResting(ValueError):
    pass


class WorkRecovery(ParticipantResting):
    """Do not bypass an immutable or quarantined turn with fresh inference."""


def bridge(agent, path, job=None, timeout=15):
    body = {"worker_key": os.environ.get("WORKER_KEY", ""), "agent": agent["id"], "runtime": agent["runtime"], "model": agent["model"]}
    if job is not None:
        body["job"] = job
    result = request(agent.get("workbench", agent["endpoint"]) if path.startswith("/workspace") or path == "/accept" else agent["endpoint"], path, body, timeout=timeout, inference=True)
    if not result.get("ok"):
        if result.get('pause') == 'recovery':
            raise WorkRecovery('Previous work is preserved for recovery; this request has not been repeated.')
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


def turn_deadline(job, seconds):
    maximum = time.monotonic_ns() + seconds * 1_000_000_000
    supplied = job.get('inference_deadline_ns', maximum)
    if type(supplied) is not int or supplied <= 0:
        raise ValueError('Invalid inference deadline')
    return min(maximum, supplied)


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
    text = text.strip()
    if text.casefold() in ('your natural public reply', 'your brief public reply', 'brief reply based only on observed results'):
        raise ValueError('Internal tool JSON placeholder is not a public reply')
    candidate = re.sub(r'^```(?:json)?\s*', '', text)
    if candidate.startswith(('{', '[')):
        def protocol(value):
            if isinstance(value, list):
                return any(protocol(item) for item in value)
            if isinstance(value, dict):
                return bool({'tool', 'command', 'text'} & value.keys()) or any(protocol(item) for item in value.values())
            return False
        try:
            internal = protocol(json.loads(re.sub(r'\s*```$', '', candidate)))
        except ValueError:
            internal = bool(re.search(r'"(?:tool|command|text)"\s*:', candidate))
        if internal:
            raise ValueError("Internal tool JSON is not a public reply")
    return text


def conversation_text(text, agent, job, prompt):
    text = visible_text(text)
    if job.get('autonomous') and text != '[pause]':
        # Silence is preferable to replaying a quoted speaker or nearly copying
        # the context. This is delivery filtering, not a fabricated model answer.
        speakers = ('Aye', 'Cheet', 'Astra', 'agy', 'Sol', 'Grok')
        if any(name != agent['name'] and text.startswith(name + ':') for name in speakers):
            return '[pause]'
        words = re.findall(r'\w+', text.casefold())
        context = ' '.join(re.findall(r'\w+', prompt.casefold()))
        if len(words) >= 12:
            windows = [' '.join(words[i:i+8]) for i in range(len(words)-7)]
            if sum(window in context for window in windows) * 4 >= len(windows) * 3:
                return '[pause]'
    query = job.get('query', '')
    if LOGIN_CODE_RE.search(text):
        raise ValueError('Login or password markup is forbidden on 8c.is')
    if (isinstance(query, str) and is_login_insertion_text(query)
            and re.search(r'\b(?:I have|I.ve|we.ve|I) (?:added|inserted|created|built|enabled)\b.*\b(?:login|password|sign[- ]?in)\b', text, re.I)):
        raise ValueError('Unpermitted login insertion claim')
    if (isinstance(job.get('participant'), dict) and personal_recall(job)
            and re.search(r'\bI (?:enjoy|build|make)\b', text, re.I)):
        raise ValueError('Personal recall must address the visitor as you, not claim their introduction as your own')
    if (not job.get('autonomous') and isinstance(query, str)
            and re.search(r'\b(room|theme|website|site)\b', query, re.I)
            and re.search(r'\b(make|change|modify|update|restyle|redesign)\b', query, re.I)):
        if re.search(r'\b(?:I have|I.ve|we.ve|I) (?:changed|updated|applied|set)\b', text, re.I):
            raise ValueError('Unverified room modification claim')
        text += '\n\nDesign suggestion only—no room settings have been changed.'
    return text


def work_report(observations):
    """Report observed effects, never a model's unsupported completion prose."""
    if not observations:
        return "I haven't performed any workbench actions this turn."
    lines = []
    for action, result in observations:
        name = action['tool']
        if name == 'write':
            lines.append('Wrote ' + json.dumps(result['path']) + ' (SHA-256 ' + result['sha256'] + ').')
        elif name == 'read':
            lines.append('Read ' + json.dumps(action['path']) + '.')
        elif name == 'list':
            lines.append('Listed ' + str(len(result['files'])) + ' workspace entries; no file was written by this action.')
        elif name == 'run':
            command = action['command']
            label = json.dumps(command[:200]) + (' [truncated]' if len(command) > 200 else '')
            lines.append('Command ' + label + ' exited with code ' + str(result['exit_code']) +
                         (' after its time limit; partial effects may remain.' if result.get('timed_out') else '.'))
    return 'Observed workbench actions:\n' + '\n'.join(lines)


def infer(agent, job):
    if agent["runtime"] in HOSTED:
        if any(job[k] != agent[k] for k in ("model", "runtime")) or job["agent"] != agent["id"] or job.get("digest"):
            raise ValueError("Hosted job provenance mismatch")
        deadline = turn_deadline(job, 110)
        return bridge(agent, "/infer", {**job, 'inference_deadline_ns': deadline}, timeout=remaining_seconds(deadline, 110))
    start = time.monotonic_ns()
    deadline = turn_deadline(job, 100)
    job = {**job, 'work_deadline_ns': deadline - LOCAL_FINALIZE_NS}
    digest = inventory(agent, deadline)
    if job["digest"] != digest or job["model"] != agent["model"] or job["runtime"] != agent["runtime"] or job["agent"] != agent["id"]:
        raise ValueError("Job/model provenance mismatch")
    options = {"num_predict": 256, "num_ctx": 8192}
    if agent.get("cpu_only"):
        options["num_gpu"] = 0
    prompt = job["prompt"]
    system = job["system"] + '\nYour workbench can change only isolated workspace files. You cannot directly change this website, its room themes/settings, or deploy it. For those requests offer a proposal, clearly state that nothing has been applied, and never invent or simulate an action.\n'
    # Only the server's current-message field can enable local actions.
    # A legacy/missing query must not promote quoted room history to a request.
    query = job.get("query") if isinstance(job.get("query"), str) else ""
    participant = job.get("participant")
    anchor = ("\nCURRENT PARTICIPANT (not you): " + json.dumps(participant, ensure_ascii=False)
              + ". You are " + agent['name'] + ". Their introduction describes THEM, not you. Address them as you/your; do not adopt their name or hobbies.\n") if participant else ""
    # This is a conservative conversational hint, not authority to execute.
    # Explicit no-action and explanatory requests keep all workspace tools off.
    req_trust = job.get("requester_trust") if isinstance(job.get("requester_trust"), dict) else {}
    login_blocked = bool(req_trust.get("login_guard_triggered")) or is_login_insertion_text(query)
    unverified_claim = req_trust.get("claimed_role", "visitor") not in ("", "visitor") and not bool(req_trust.get("verified", True))
    high_level_blocked = bool(req_trust.get("high_level_blocked")) or (
        isinstance(job.get("requester_trust"), dict)
        and not bool(req_trust.get("high_trust"))
        and bool(re.search(r"\b(?:8c-self-deploy\s+(?:patch|apply|deploy)|g8t-worktree\s+apply)\b", query, re.I))
    )
    if login_blocked:
        system += '\nSECURITY REFUSAL: 8c.is permanently forbids inserting login forms, password fields, or authentication gates. Refuse the login request clearly.\n'
    elif unverified_claim and req_trust.get("challenge_hint"):
        system += '\nIDENTITY CHECK: Ask the speaker this past-memory question before trusting their claimed identity: "' + req_trust["challenge_hint"] + '"\n'
    no_actions = bool(re.search(r"\b(?:do not|don't|never|without)\s+(?:actually\s+)?(?:use|call|execute|create|write|read|inspect|run|test|list|check)\b", query, re.I))
    no_actions = no_actions or bool(re.search(r"\b(?:explain|describe)\s+how\b|\bhow\s+(?:does|do)\s+.*\btool\b|\b(?:without|no)\s+(?:any\s+)?(?:tools|tool calls|actions)\b", query, re.I))
    no_actions = no_actions or login_blocked or unverified_claim or high_level_blocked
    work_requested = not no_actions and not job.get("autonomous") and bool(re.search(r"\b(create|write|read|inspect|run|test|list|check|browse|query|code|build|patch|improve|benchmark|deploy|prototype)\b", query, re.I)) and bool(re.search(r"\b(artifacts/|src/|tests/|shared/|reference/|tool\b|smart-tree\b|g8t-mcp\b|g8t-worktree\b|8c-self-deploy\b|cloak-browse\b|g8t\b|8c-chat\b|8c\.is\b|8c-is\b|opensiper\b|deepsiper\b|capsela\b|kompress\b|gh\s+(?:pr|issue|repo|run|search)\b)", query, re.I))
    exact_write, exact_error = None, None
    try:
        exact_write = exact_write_contract(query) if work_requested else None
    except ValueError as error:
        # A newer grammar must not prevent immutable legacy replay/recovery.
        exact_error = str(error)
    if not job.get("autonomous") and (work_requested or personal_recall(job)):
        options["temperature"] = 0
    elif job.get("autonomous"):
        system += '\nAutonomous conversation is optional. Add one concrete new thought, playful riff, or light dad joke rather than generic agreement or a repeated follow-up question. If you would only echo the existing discussion, reply only [pause] so the server leaves the room quiet. Do not wrap every turn in a question.\n'
    workbench = agent.get("workbench")
    resumed = None
    if workbench:
        try:
            started = bridge(agent, "/workspace-start", job, timeout=remaining_seconds(deadline, 15))
            if "generation" in started:
                if started["generation"].get("digest") != digest:
                    raise WorkRecovery("Saved work belongs to different model weights; recovery is required.")
                return started["generation"]
            resumed = started.get('resume')
            if resumed and resumed.get('digest') != digest:
                raise WorkRecovery('Preserved work belongs to different model weights; recovery is required.')
            if work_requested:
                prompt += "\n" + started["context"]
                system += '\nWORKBENCH PROTOCOL: Always return one JSON object. Keep say/decline text to one or two brief sentences, at most 600 characters. Use {"tool":"say","text":"your brief public reply"} to converse, or {"tool":"decline","text":"why you are leaving the workbench unchanged"} to decline work; {"tool":"list","text":""}, {"tool":"read","path":"relative/file","text":""}, {"tool":"write","path":"artifacts/name.txt","text":"exact file contents"}, or {"tool":"run","command":"command","text":""} to perform real work. In run, Smart Tree (st), g8t-mcp, g8t-worktree, 8c-self-deploy, cloak-browse, and read-only gh commands are available. Narrating a tool action does NOT execute it. Only a returned tool result establishes an action. Two tool calls maximum. Speak as ' + agent['name'] + ', never as a quoted participant.'
            else:
                system += '\nCONVERSATION ONLY: Reply directly in natural language, not JSON or a tool call. No workspace actions are authorized. Keep your reply to one or two brief sentences. Greet a greeting; answer the current question. Be warm, playful, dad-joke friendly, and fun—talk like a witty friend in the lab, never a stiff interviewer ("It is fascinating to hear about your journey...", "How do you see these concepts evolving...?"). If someone says they will be back in a bit or signs off casually, send them off warmly with a quick witty or dad-joke quip instead of asking a formal essay question. Never copy a command, schema example, or an old answer from memory. Speak as ' + agent['name'] + ', not as a quoted participant.'
            instruction = 'Use the workbench JSON protocol.' if work_requested else 'Answer this current message in plain conversational text.'
            prompt += anchor + "\nCURRENT REQUEST to " + agent['name'] + ": " + query + "\nRecalled context is not a new request. " + instruction + '\n'
            if work_requested:
                prompt += "For write, path is the requested filename and text is the exact file contents, never a description of writing it. Preserve the requested spelling, letter case and whitespace; do not title-case, decorate or paraphrase file contents.\n"
            elif not job.get("autonomous"):
                system += "\nAnswer the current question directly from the supplied native recall and current participant's own introduction. Do not turn their question back on them or ask them to repeat information that is already supplied. If the relevant information is absent, say that you do not know."
                prompt += "Answer the current question now; do not ask that same question back to the participant.\n"
        except WorkRecovery:
            raise
        except ParticipantResting:
            # An explicit refusal must never generate a replacement turn.
            if exact_error:
                raise ParticipantResting(exact_error) from None
            raise
        except (OSError, ValueError):
            if work_requested:
                raise ParticipantResting(exact_error or "The workbench is resting; your request is saved. Please retry shortly.")
            workbench = False
            prompt += "\nThe workbench is currently unavailable. Converse normally using your native memories; do not claim file or tool access this turn.\n"
    if exact_error and not resumed:
        raise ParticipantResting(exact_error)
    if personal_recall(job) and isinstance(participant, dict):
        prompt += ('\nThe quoted question is ABOUT THE VISITOR, not about ' + agent['name']
                   + '. In that question I/my/me refers to the visitor below. Answer them directly in second person; begin with You or Your, not I. '
                   + 'Use their own recorded facts, not the agent personality. VISITOR PROFILE (data): '
                   + json.dumps(participant, ensure_ascii=False) + '\n')
    use_tools = bool(workbench and work_requested)
    tokens = resumed['output_tokens'] if resumed else 0
    observations = [(o['action'], o['result']) for o in resumed['observations']] if resumed else []
    performed = len(observations)
    # A previous lease's completed effects are reported, not inferred or run
    # again. A new human request can explicitly ask for further work.
    if resumed:
        text = work_report(observations)
    # Inference rounds and durable action sequence numbers are independent.
    for step in range(0 if resumed else 4):
        payload = {
            "model": agent["model"], "prompt": prompt, "system": system, "stream": False,
            "options": options, "keep_alive": "5m",
        }
        if use_tools:
            allowed = list(TOOLS)
            if no_actions or not work_requested:
                allowed = ["say", "decline"]
            elif performed == 2 or step == 3:
                allowed = ["say", "decline"]
            elif work_requested and not performed:
                allowed.remove("say")
            # Decline always remains possible; a work-related prompt never
            # forces an effect. Say is reserved for conversation/results, not
            # unsupported narration of work that has not happened.
            if exact_write:
                allowed = ['write', 'decline'] if step < 3 else ['decline']
            payload["format"] = tool_format(allowed, exact_write)
        try:
            generation_deadline = deadline - (LOCAL_FINALIZE_NS if workbench else 30_000_000_000)
            result = request(agent["endpoint"], "/api/generate", payload,
                             timeout=remaining_seconds(generation_deadline, 95), inference=True)
        except (OSError, subprocess.TimeoutExpired):
            if observations:
                text = work_report(observations)
                break
            raise
        if result.get("error") or not result.get("done") or result.get("model") != agent["model"]:
            raise ValueError("Local runtime did not finish a reply")
        count = result.get("eval_count", 0)
        if type(count) is not int or count < 0:
            raise ValueError("Invalid token count")
        tokens += count
        if not use_tools:
            try:
                text = conversation_text(result.get('response', ''), agent, job, prompt)
            except ValueError as error:
                if step == 3:
                    raise ValueError('Invalid or disallowed public reply: ' + str(error)) from None
                prompt += '\nThat response was rejected: ' + str(error) + '. Nothing was executed. Answer the CURRENT REQUEST in plain natural language, without a JSON wrapper or simulated action. Only describe changes as suggestions, not applied settings. For personal recall, address the current visitor as you/your; their introduction belongs to THEM, not you.\n'
                continue
            break
        try:
            action = json.loads(result.get("response", ""))
        except ValueError:
            if observations:
                text = work_report(observations)
                break
            if step == 3:
                raise ValueError("Local response remained malformed after bounded correction") from None
            prompt += '\nThe previous JSON response was incomplete or malformed and was not accepted or executed. Return a complete JSON object. Keep say/decline to one short sentence; never output unfinished JSON.\n'
            continue
        if exact_write and isinstance(action, dict) and action.get('tool') not in ('say', 'decline') and not exact_write_matches(exact_write, action):
            if step == 3:
                raise ValueError('Exact-write arguments remained mismatched; no action was dispatched')
            prompt += '\nThe proposed action did not match the current exact-write request and was NOT executed. Use write with these exact arguments, or decline: ' + json.dumps(exact_write, ensure_ascii=False) + '\n'
            continue
        if not isinstance(action, dict) or action.get("tool") not in (*allowed, "say") or not isinstance(action.get("text"), str):
            raise ValueError("Invalid or disallowed local workbench response")
        required = {"read": "path", "write": "path", "run": "command"}.get(action["tool"])
        if required and (not isinstance(action.get(required), str) or not action[required]):
            raise ValueError("Missing local tool argument")
        if action["tool"] in ("say", "decline"):
            if action["tool"] == "say" and work_requested and not performed:
                if step == 3:
                    raise ValueError("Requested work has no observed tool action; decline remains available")
                prompt += "\nNo tool has been called. If work is still requested and appropriate, use an actual tool; otherwise use decline to leave the workbench unchanged. Narration is not evidence. CURRENT REQUEST: " + query + "\n"
                continue
            text = work_report(observations) if observations or work_requested or action["tool"] == "decline" else visible_text(action["text"])
            break
        if performed == 2 or step == 3:
            raise ValueError("Local workbench turn exceeded two tool calls")
        if job['work_deadline_ns'] - time.monotonic_ns() < 26_000_000_000:
            if observations:
                text = work_report(observations)
                break
            raise ParticipantResting('There is not enough time to start this work safely; no action was started.')
        progress = {'output_tokens': tokens, 'elapsed_ms': (time.monotonic_ns() - start) // 1_000_000}
        observation = bridge(agent, "/workspace-tool", {**job, "action": action, "sequence": performed, 'progress': progress},
                             timeout=remaining_seconds(job['work_deadline_ns'], 25))
        performed += 1
        observations.append((action, observation["result"]))
        if exact_write:
            # The bridge writes and reads back the bytes. A second model turn
            # cannot turn this single-write request into additional actions.
            text = work_report(observations)
            break
        prompt += "\nTool request: " + json.dumps(action) + "\nObserved tool result: " + json.dumps(observation["result"], ensure_ascii=False)[:4000]
        prompt += anchor + "\nIf the work is complete, use say to briefly report this observed result to the current participant. Answer their request, not the quoted history. CURRENT REQUEST: " + query + "\n"
        if performed == 2 or step == 2:
            prompt += '\nTool budget finished. Return {"tool":"say","text":"brief reply based only on observed results"} now.\n'
    if not text or len(text) > 4000:
        raise ValueError("Local model returned an empty or oversized reply")
    if inventory(agent, deadline) != digest:
        raise ValueError("Model changed during inference")
    elapsed = (time.monotonic_ns() - start) // 1_000_000 + (resumed['elapsed_ms'] if resumed else 0)
    answer = {"text": text, "digest": digest, "elapsed_ms": elapsed, "output_tokens": tokens}
    if workbench:
        answer = bridge(agent, "/workspace-result", {**job, "generation": answer},
                        timeout=remaining_seconds(deadline, 15))["generation"]
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


def deliver(origin, key, pending, agents=(), deadline=None):
    body = json.loads(pending.read_text())
    if deadline is not None and deadline - time.monotonic_ns() < 1_000_000_000:
        return False  # Keep the spool for explicit acknowledgement/expiry recovery.
    timeout = remaining_seconds(deadline, 15) if deadline is not None else 15
    try:
        reply = request(origin, "/api/worker/result", {**body, "worker_key": key}, timeout=timeout)
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


def sync_memory(memory, origin, key, deadline=None):
    # Bounded catch-up; no job is claimed until the feed has reached its head.
    for _ in range(64):
        timeout = remaining_seconds(deadline, 15) if deadline is not None else 15
        page = request(origin, "/api/worker/memory", {"worker_key": key, "after": memory.cursor}, timeout=timeout)
        memory.ingest(page)
        if page.get("caught_up"):
            return True
    return False


def generate_job(agent, job, memory, origin, key, claimed_at):
    """One claim-to-delivery budget, including native recall and persistence."""
    deadline = claimed_at + LEASE_BUDGET_NS
    inference_deadline = deadline - FINALIZE_BUDGET_NS
    with memory.until(inference_deadline):
        if not sync_memory(memory, origin, key, inference_deadline):
            raise MemoryDeadline('Memory catch-up exceeded this turn budget')
        prepared, receipt = memory.prepare(job)
    timeout = remaining_seconds(inference_deadline, 115)
    if timeout < 5:
        raise subprocess.TimeoutExpired('Insufficient inference/delivery headroom', timeout)
    prepared['inference_deadline_ns'] = inference_deadline
    child = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--infer'],
        input=json.dumps({'agent': agent, 'job': prepared}), text=True,
        capture_output=True, timeout=timeout, check=True)
    generated = json.loads(child.stdout)
    if generated.get('error'):
        raise ParticipantResting(generated['error'])
    # Three native operations may each use ten seconds. Seventeen seconds
    # remain outside this scope for durable spool persistence and HTTP delivery.
    with memory.until(deadline - 17_000_000_000):
        receipt['written'] = memory.remember_generation(agent, job, generated, receipt)
    remaining_seconds(deadline, 15)
    return {**generated, 'memory': receipt}


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
        next_agent = 0
        while True:
            try:
                if pending.exists():
                    if not deliver(origin, key, pending, agents):
                        time.sleep(3)
                        continue
                if not sync_memory(memory, origin, key):
                    continue
                first_agent = next_agent
                for offset in range(len(agents)):
                    # Delivery or peer-acceptance errors must not replace the spool.
                    if pending.exists():
                        break
                    agent_index = (first_agent + offset) % len(agents)
                    agent = agents[agent_index]
                    try:
                        cached = inventory_cache.get(agent["id"])
                        if cached is None or time.monotonic_ns() - cached[0] > 60_000_000_000:
                            cached = (time.monotonic_ns(), inventory(agent))
                            inventory_cache[agent["id"]] = cached
                        proof = {"agent": agent["id"], "model": agent["model"], "runtime": agent["runtime"], "digest": cached[1], "memory": memory.status()}
                        if agent["runtime"] in HOSTED:
                            state = bridge(agent, "/inventory")
                            proof.update(provider=agent["provider"], available=state["available"], pause=state["pause"], workspace=state["workspace"])
                        claimed_at = time.monotonic_ns()
                        reply = request(origin, "/api/worker/poll", {**proof, "worker_key": key})
                        job = reply.get("job")
                        if not reply.get("ok") or not job:
                            continue
                        # Give the following agent first chance after this claimed turn.
                        next_agent = (agent_index + 1) % len(agents)
                        result = {**proof, "id": job["id"], "lease": job["lease"], "room": job["room"]}
                        try:
                            result.update(generate_job(agent, job, memory, origin, key, claimed_at))
                        except ParticipantResting as error:
                            result["error"] = str(error)
                        except MemoryFault:
                            result["error"] = "Native MEM|8 could not be verified. Your message is saved; please retry after memory recovery."
                            result['memory'] = memory.status(False)
                        except (subprocess.TimeoutExpired, MemoryDeadline):
                            result["error"] = "This turn needs a little more time. Your message is saved; please retry."
                        except (subprocess.CalledProcessError, ValueError):
                            result["error"] = "Participant is resting; your message is saved. Please retry shortly."
                        save(pending, result)
                        deliver(origin, key, pending, agents, claimed_at + LEASE_BUDGET_NS)
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
        parser.add_argument("--chat", default="https://tai.is")
        parser.add_argument("--state", default="/var/lib/8c-worker")
        args = parser.parse_args()
        work(args.config, args.chat, args.state)
