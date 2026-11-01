# Local engineering review

## Assessment

Keep the current separation between the browser, durable chat server, and local
inference worker. The loopback-only model boundary, explicit public-memory scopes,
lease checks, retained drafts, and recovery tests are good foundations. I would
not rewrite these solely for stylistic consistency.

This was a source review and local regression-test pass, not a deployment or an
exhaustive security audit. No production services, profiles, journals, or native
memory archives were changed. The pre-existing `.aye_consciousness.m8` was left
alone. Actual-model inference and public-host end-to-end checks were not rerun.

## Fixes made

- **Wrong-person UI after an account switch** — `src/app.js` now ignores a delayed
  favorite/profile response if the active bearer token has changed. Previously,
  the old profile could replace the new person's display while requests used the
  new person's token. A delayed-response browser regression reproduced this.
- **Narrow-screen overflow** — `src/page.html` bounds the identity button, allows
  room headings to wrap, and wraps long message authors. Browser tests now check
  the actual configured viewport width and the message pane's own overflow; an
  `innerWidth` comparison alone missed mobile viewport expansion.
- **Legacy-import journal corruption** — `src/chat.rs` rejects an unknown legacy
  room before appending it. Previously the invalid message was durable before
  in-memory validation failed, so subsequent startup failed too. The regression
  checks original-byte preservation, replayable accepted prefixes, and
  duplicate-free resumption with a separately reviewed source copy.
- **Pending-result preservation and worker fairness** — `worker.py` prevents
  another agent from overwriting the pending result after a delivery exception,
  and rotates the next polling agent after a claimed job. The original fixed
  ordering could repeatedly serve Aye without ever polling Cheet. The server's
  lease rules limit the overwrite case to a first job already accepted or no
  longer valid; loss of a still-valid reply was not demonstrated. Mocked loop
  tests cover both paths without invoking a model.

## Changes still recommended

### High priority: admission and abuse controls

`Chat::introduce` accepts anonymous profile creation into a permanent 4,096-entry
capacity. Its global allowance of 30 attempts per minute limits speed, not total
abuse; malformed attempts also consume the shared allowance. Without effective
external admission controls, one source can deny new visitors access. Define a
trusted reverse-proxy rate-limit/admission policy, capacity monitoring, and a
non-destructive profile-lifecycle policy. Do not silently delete identities to
free space. Existing deployment-side protections were not assessed here.

### Correctness: byte-aware prompt budgeting for accepted Unicode messages

A delayed review finding confirmed a mismatch between `Chat::post`, which accepts
4,000 Unicode characters, and `Memory.prepare`, which caps the assembled prompt
at 30,000 UTF-8 bytes. For example, the API accepts 4,000 non-BMP emoji (16,000
bytes), but `Chat::prompt` includes that text both in recent history and in the
current-speaker instruction. Those two copies alone exceed the worker's limit.
The resulting failure is misleadingly reported as a native-memory verification
problem; retrying the unchanged saved prompt cannot resolve it.

This remains unfixed. Include the current message only once and budget the whole
prompt in UTF-8 bytes, reserving its space before selecting history and recall.
Removing duplication alone is insufficient because other Unicode history can
still exceed the total budget. Add cross-layer maximum-length Unicode tests and
distinguish prompt-capacity errors from memory-integrity failures. This finding
is established from source and byte counts, not a live-model test.

### Before growth: indexed idempotency lookups

A new `client_id` misses the 512-entry recent cache and calls
`Store::find_request`, which scans the entire journal under the global chat
mutex. This affects every new send, not just old retries. As history grows,
message acceptance can delay room polling and worker acknowledgements. Add a
validated durable/rebuildable request index or a segmented store, retaining
old-ID retry and crash-recovery guarantees. Merely enlarging the recent cache
will not eliminate scans for new IDs.

### Reliability hardening: a whole-job lease budget

The inference subprocess has a 115-second timeout and the server grants a
150-second lease, but post-claim history synchronization, native recall, generated
memory writes, and result delivery also consume lease time. Individual operation
bounds do not form an overall deadline. Budget the complete claimed-job path,
reserve time for durable result delivery, and test slow-memory/network cases.
This is a remaining design risk, not a measured live outage from this review.

### Configuration upgrades: version family provenance in record identity

Another delayed finding concerns `Memory.__init__`: family record keys contain
source position, text digest, and chunk number, but their immutable payloads also
contain the configured revision and source filename. Changing only the revision
or renaming an otherwise identical source therefore reuses a key with a different
payload. `Memory.put` rejects it as `Replayed memory differs`, preventing startup
against the existing store. Include complete versioned provenance in the record
identity while preserving old native records, and test metadata-only upgrades
against populated stores. This source-confirmed issue remains unfixed; a runtime
upgrade reproduction was not performed in this pass.

### Transport hardening: total HTTP deadlines outside inference

`worker.request` uses urllib's socket timeout, not a whole-operation deadline.
Inventory, history synchronization, polling, and result delivery outside the
inference subprocess run in the sole worker thread. A slowly trickled response
can hold it far beyond the nominal 15-second timeout despite the response-size
cap. Bound the complete connect/read operation, including HTTP error bodies, and
test slow/truncated responses. This remains a source-identified hardening task;
no slow-peer experiment or deployed transport assessment was performed.

A smaller test-portability issue also remains: `tests/test_memory.py` defaults
its temporary-directory parent to `/tmp/opencode`, which need not exist on a
clean installation. Prefer the standard temporary-directory default unless an
explicit override is supplied.

## Validation completed

- `cargo test --offline`: **14 passed**.
- `cargo clippy --offline --all-targets -- -D warnings`: passed.
- `cargo fmt --check` and `cargo build --offline`: passed.
- `python3 -m unittest discover -s tests -v`: **12 passed**, including all five
  native-memory tests against the installed Aurora executable; no skips.
- `node --check src/app.js` and `node --check tests/browser.cjs`: passed.
- Desktop/mobile Playwright suite: passed, including delayed identity changes,
  48-character names at 320 pixels, message-pane overflow, and prior recovery
  scenarios. The new identity and overflow checks failed before the fixes.
- Patch whitespace checks across the complete review changes: passed.

Added coverage resides in `src/chat.rs`, `tests/test_worker.py`, and
`tests/browser.cjs`. Browser model responses and worker-loop tests are explicitly
synthetic; no actual-model inference claim is made.

Workspace auto-sync created checkpoint `472c45f` during the review and bumped
`Cargo.toml` to 0.9.1. The validation build refreshed `Cargo.lock` to match; that
automatic version change was retained. No manual commit, push, or deployment was
performed as part of this review.
