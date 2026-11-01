# Operating revision 8

## Processes and configuration

Run one Rust server per journal and one worker per worker-state directory. Both
hold OS-level exclusive locks; starting a second writer fails. The server uses
16 HTTP threads, a 32-connection queue, an eight-second whole-request deadline,
16 KiB headers and a 64 KiB body cap. It binds loopback by default; use your
existing HTTPS reverse proxy for public access.

Server environment:

| Variable | Meaning |
| --- | --- |
| `BIND_ADDR` | Default `127.0.0.1:8798` |
| `JOURNAL_PATH` | Default `/var/lib/8c-chat/chat-v1.jsonl` |
| `AGENTS_CONFIG` | Agent JSON file; defaults to the compiled example |
| `WORKER_KEY` | Shared secret, at least 32 characters, for worker routes |
| `LEGACY_PATH` | Optional read-only import of revision-6 `rooms.m8` |

Store the key in a service environment file readable only by the service owner.
Keep it out of source control and public logs. A worker service should use the
same environment file, invoke `python3 /installed/path/worker.py --config ...
--chat https://your-host --state /var/lib/8c-worker`, and restart on failure. The
worker reaches Ollama's loopback API on **its own host**. No inference tunnel is
needed on the web server.

`deploy/8c-model.service` is an optional dedicated Ollama scheduler on port 11435,
using an already installed `/var/lib/ollama` model store. Adjust that path for your
installation. Set the worker agent endpoints to port 11435 when using it. This
isolates chat's bounded queue from other model clients. `deploy/8c-worker.service`
is a systemd worker example; install its script/config paths and environment file
before enabling it. Both services must live on the model host.

## Journal and recovery

`chat-v1.jsonl` is the chat application's own UTF-8 JSONL event format. It is
**not MEM8**. One newline-terminated JSON object is one durable transaction:

- `profile`: a remembered profile, including its secret bearer token.
- `message`: a human/imported message and its optional reply job, saved together.
- `job`: a queue state/lease transition.
- `result`: an Aye message and completed job, saved together.
- `tub`: scheduler state, optional scheduled job, and atomic pause cancellation.
- `import_complete`: marks a completed one-time legacy import.

Each append is synced before success is returned. On an I/O error, the append is
rolled back to its previous length; failed rollback disables further writes.
Records have a 128 KiB cap. Restart fails on incomplete, malformed, unknown-type,
or oversized records and retains the original file. There is no silent reset.

Back up the journal and worker state with the processes stopped (or use a
consistent filesystem snapshot). A damaged tail needs inspection: retain a copy
of the entire original and a separate copy of the rejected bytes, identify the
last complete accepted transaction, and restore only after checking the affected
messages/jobs. Reopen the recovered journal and verify the affected room before
resuming the worker. Do not replace a journal with an empty file to make it load.

The journal stores all accepted history; the UI shows the most recent 64 notes
per room. Duplicate-send checks use the last 512 requests in RAM and a streaming
journal lookup for older IDs. That lookup uses bounded per-record memory but is
linear in journal size. Profile capacity is 4096, live reply capacity 32, and
recent job capacity 256. Do not compact away idempotency records or legacy import
receipts. Large deployments need a separately validated indexed/segmented store.

## Queue and runtime evidence

Jobs are durable before the browser receives success. A poll grants a random
150-second lease to one worker; the worker's complete inference subprocess has a
115-second deadline. Unclaimed jobs fail visibly after 300 seconds. The sender
can request a new lease through **Retry reply**. A late or cancelled lease cannot
post a message. A full queue still saves the human message with a failed/retryable
reply record. Automated turns wait for pending human jobs.

Before accepting more work, the worker delivers any pending result from its
atomic local spool. A lost acknowledgement retries that same result. The server
acknowledges duplicate completed leases without posting again. Expired results
are retained under an `expired-<job>-<lease>.json` filename in the private worker
directory for diagnosis. Those files can contain conversation text.

Public receipts contain model name and digest, runtime, personality revision,
integer elapsed milliseconds, and token count. They report the authenticated
worker's validated local runtime observation; they are not a hardware attestation
or proof of model internals. Optional CPU mode selects Ollama's CPU setting;
measure the resulting backend and latency before making performance claims.

## Revision-6 migration and deployment

1. Build and test on the destination architecture. Exercise a temporary server,
   temporary journal, and actual worker before changing the public listener.
2. Preserve the current binary, unit configuration, and **all** existing state.
3. Stop the old server briefly so it cannot accept writes during import. Copy its
   latest `rooms.m8` to a dated recovery location. Configure a new journal path
   and set `LEGACY_PATH` to the old file. The importer reads legacy `MSG` records
   as-is; old identity guesses and tokens are not adopted as profiles.
4. Start revision 8 and check health, room contents, asset headers, and two
   independent browser sessions through the public HTTPS hostname.
5. Start the worker on the inference host with the matching secret/config. Verify
   actual Aye and Cheet responses, both model digests, remembered introductions,
   queue completion and hot-tub pacing. Recheck after process restarts.

If rollback is needed, stop the worker and server, retain the new journal and
spool, restore the previous binary/unit, and start the old service against its
preserved legacy state. New revision-8 messages remain in the new journal; the old
UI cannot display them. Export/reconcile them before any later migration. Never
overwrite the new journal with a legacy file.

## API

- `GET /api/health`, `GET /api/config`
- `GET /api/messages?room=<id>` (optional `Authorization: Bearer <profile-token>`)
- `POST /api/introduce`: `{name, bio}` or `{token}` to restore
- `POST /api/profile`: `{token, bio, favorites}`
- `POST /api/messages`: `{token, room, text, client_id, agent, ask_aye}`
  (`ask_aye: "no"` is people-only; `agent: "auto"` selects a room follower)
- `POST /api/retry`: `{token, id}`
- `POST /api/hottub`: `{token, running}`
- `POST /api/worker/poll`: `{worker_key, agent, model, runtime, digest}`
- `POST /api/worker/result`: the worker identity plus `{id, lease, text,
  elapsed_ms, output_tokens}` or `{id, lease, error}`

Requests use JSON objects, structural parsing, bounded Unicode-aware text, and
integer IDs within JavaScript's safe integer range. Worker keys/profile tokens
are never included in public room payloads or URLs. Public user-facing errors
must remain visible in the browser; a connection error must not clear a draft.
