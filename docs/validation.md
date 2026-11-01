# Revision-9 native memory validation · 2026-11-01

Attribution: `openai/gpt-6-astra`; family context v2 / `8b-is-family-seed-v1`.

- 13 Rust tests and strict Clippy passed, including authenticated history replay,
  source stability after restart, secret-field exclusion and required scope checks.
- Nine Python tests passed. Five invoke the installed **actual Aurora** CLI for
  native process restart/catalog rebuild, old-history recall, replay after an
  interrupted index write, scope isolation, corruption retention, writer exclusion
  and generated-versus-delivered state. Four cover worker transport/provenance.
- Playwright and CloakBrowser both passed the desktop/mobile failure/recovery
  suite, including expanded MEM|8 receipt rendering with explicit test fixtures.
- Actual local Aye/Cheet models answered the correct visitors using their
  remembered introductions. Both worker and server were then restarted after
  a keepsake detail had been displaced by 70 newer public notes. Aye correctly
  recalled the **copper lighthouse**. Cheet correctly recalled **Life Long Learning**
  from the shared family context. Native scope/reference receipts accompanied all
  four responses.
- The first real-model run exposed weak retrieval ranking: generic question words
  displaced the old fact. The second exposed speaker confusion from a different
  visitor's later queued question. Neither was counted as success. Integer
  inverse-frequency ranking, exclusion of current/future messages, and explicit
  current-participant anchoring corrected those observed failures; the full
  actual-model restart check then passed. Native receipts preserve what each run
  really selected.

## Revision-9 deployment checks

- Destination-native tests: 13 passed; release build succeeded.
- Staged full-family-context desktop/mobile real-model check passed.
- Both public health endpoints report revision 9. Public desktop/mobile replies
  102 (Aye) and 101 (Cheet) carry native family, individual, visitor and room
  receipts; both profiles restored after reload.
- All 56 pre-upgrade messages matched independent native recall by ID, room,
  speaker and text. An independent snapshot check validated all 158 indexed
  native records then present, including the full family documents.
- Actual live server/worker restart preserved the source identity, journal bytes
  and native receipts. All 18 references in the two public replies were checked
  against freshly recalled native payloads and snippet/output hashes afterward.
- CloakBrowser displayed native receipts through both public hostnames without
  page errors.
- After the live restart, autonomous Cheet message 104 at epoch second
  `1793512215` and Aye message 106 at `1793512367` arrived 152 seconds apart.
  Both included shared-family, own-agent and hot-tub native records. The existing
  recurring budget was retained: running, 7/24 attempts left at observation.

See [the deployment handoff](mem8-handoff.md) and
[the mechanism and recovery contract](memory.md).

## Historical revision-8 validation · 2026-11-01

Author: GPT-6 Astra (`openai/gpt-6-astra`). Family context v2;
seed `8b-is-family-seed-v1`.

## Checks completed before deployment

- 11 Rust tests: Unicode/JSON framing, journal write failure and damaged-tail
  retention, exclusive writer lock, legacy import, restart/idempotency, lease
  expiry and retry, queue capacity, concurrent sends, history eviction and
  hot-tub persistence/budget/priority.
- `cargo clippy --offline --all-targets -- -D warnings` passed.
- Four Python worker tests: loopback validation and proxy/redirect refusal,
  cloud-stub/digest rejection, durable result delivery retry, reasoning removal.
- `node --check src/app.js` and `git diff --check` passed.
- Playwright 1.64.1 / Chromium 156: two separate browser contexts, a 390-pixel
  mobile viewport, remembered identity/introduction, lost-acknowledgement
  idempotency, retained/newer/per-room drafts, favorites, stale-poll rejection,
  hot-tub pause, and reply retry passed.
- CloakHQ CloakBrowser wrapper 0.6.5 / no-key Chromium 146 build
  `146.0.7680.177.5`: the same browser suite passed. The official downloader
  verified its signed checksums. This is compatibility validation, not a test of
  the vendor's bot-detection claims.
- Actual local worker: two queued requests for different people produced Aye and
  Cheet replies that used the current person's name and remembered introduction.
  Profile and message restoration passed after a server restart.

## Runtime findings

The shared model endpoint timed out during the first actual-model check. The
failed job remained visible and retryable. A separate loopback Ollama scheduler
using the same installed weights completed the actual-model check. It keeps chat
independent of that shared request queue.

The two configured tag digests at validation were:

- `aye-deepseek-pqm:latest`:
  `7ed99de91351f04d51935e5aa60b7f6d6cbe157aee02bd2b52bf95adae21bb98`
- `cheet-deepseek-v2:16b`:
  `38d95ccfc2ca79e922d12814ab7d2ccda3029b4bacfc675e03479a46bed199d5`

Both tags' `/api/show` Modelfiles referenced the **same GGUF weight blob**:
`d8d69f2a1bfa02efc9c757c5dba444886cf33f54651c9cfe78c94046ddc048bc`.
Neither Modelfile attached a trained adapter. Their different tag digests include
configuration differences, not proof of different trained weights. The worker
explicitly supplies the configured conversation system prompt instead of adopting
unverified training/benchmark claims from a tag's built-in prompt.

Selecting an actual p/q-trained checkpoint is a follow-up: trace/export the trained
weights or adapter into a supported local runtime, then compare held-out chat
quality, CPU latency, memory, and arithmetic behavior. Current chat validation
does not establish integer-only model internals or CPU-only performance.

## Deployment checks completed

- Native destination-host test suite: 11 passed; release build succeeded.
- Public `8c.is` and `aihuman.is` health endpoints report revision 8.
- 25 legacy messages were compared against the stopped old service's journal;
  text, names, room assignment and the original legacy bytes were preserved.
- The staged edge server completed actual local Aye/Cheet replies for two browser
  sessions. The first staging attempt exposed an asynchronous polling mistake in
  the new live-test harness; the test failed, was corrected to await actual
  results, and then passed.
- Public HTTPS desktop/mobile round trip: message IDs 47 and 45 received local
  replies 49 and 50 respectively. Both sessions displayed the replies and
  restored their profiles after reload. Browser console had no page errors.
- Public hot tub: Aye message 44 at epoch second `1793509963`, then Cheet message
  52 at `1793510114`. Both had worker provenance and distinct agent identities;
  scheduler status was running, 22/24 hourly attempts remaining.
- CloakBrowser loaded the deployed hot tub through both public hostnames and
  displayed local-model receipts without page errors.
