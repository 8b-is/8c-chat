# Production review fixes · 2026-11-01

## Outcome

Deployed and verified on **https://8c.is** and **https://aihuman.is**.

- Delayed favorite/profile updates no longer replace the currently displayed person.
- Long names fit narrow phone headers and message bubbles without horizontal overflow.
- Invalid legacy rooms are rejected before contaminating the replacement journal.
- A failed reply or peer-publication acknowledgement cannot be overwritten by the next agent's result.
- Worker polling rotates after claimed turns instead of indefinitely favoring the first agent.

Production was newer than the checkout initially reviewed. This release ports the
fixes onto the actual deployed baseline, retaining local Aye/Cheet, hosted
Astra/Agy, workbench safeguards, receipt anchors, whole-turn deadline budgeting,
and shared funding. It does **not** install the older revision-9 worker.

Only `/usr/local/bin/8c-chat` on the edge and
`/usr/local/share/8c-chat/worker.py` on the worker host were replaced. Configuration,
native-memory implementation, hosted bridge, funding services, and receipt
migration code were unchanged. The hosted bridge was not restarted.

## Release identity

The protocol/UI marker remains **10**; exact artifacts identify this patch:

| Artifact | SHA-256 |
| --- | --- |
| Destination-built server | `d4b1460bd355d72033ba69980fc616845c538eb39c2ddae180208d6099f7d788` |
| Worker | `0ccdb922710d95b7b91876e38cf2bcd832f8775f9fbc27cf9ed9b9591679bd81` |
| Served HTML | `f2187e5622b4cb8b081210bd5853530a0c576f85b06e79c53ce2b1de8374897c` |
| Served JavaScript | `c27396447ab72bcb5226858c44d254948148aee984351a73150816758752bfd4` |
| Frozen source archive | `68ab40fc2f3e2469040498dc104870f1072c2ebdc108b8354939e9df25e91e85` |

- Base: `1273c3e171df4ff41ee00e78fe458be99608bf28`, whose installed worker/bridge/
  memory/helper hashes were verified before deployment. Its server sources match
  the deployed `7424764` revision-10 server sources.
- Isolated release branch: `agent/production-review-fixes-20261101`.
- Source workspace: `/data/.agent-workspaces/8c-chat-production-review-fixes-20261101`.
- Edge build source: `/usr/local/src/8c-chat-production-review-20261101`.
- Applied changes remain in that isolated worktree; the frozen source, patch,
  and per-file manifest are retained under
  `/data/.agent-state/8c-production-review-20261101/`.

**Do not redeploy the older main checkout over production.** Reconcile it with
this newer deployed baseline first. No merge or push was performed by this
production rollout.

## Validation

- **15 Rust tests** passed locally and on the destination; locked/offline native
  release build used the destination's existing Rust 1.99.1 toolchain.
- Strict Clippy, formatting, and patch-whitespace checks passed.
- **105 Python tests** passed, including native Aurora persistence checks,
  hosted/workbench safety, receipt migration, deadline handling, worker fairness,
  pending-result retention, and peer-publication acknowledgement retry.
- Expanded isolated desktop/mobile browser regression suite passed.
- Three negative live-browser-harness tests passed.
- The exact destination-built binary passed isolated introduction, message
  acceptance, duplicate retry, process restart, profile restoration, all-room
  reads, and byte-exact asset checks.
- Before installation, the same binary successfully replayed a private copy of
  the frozen production journal and memory-source sidecar. No stage test used
  production state as its writable journal.
- Both public hostnames passed fresh-browser desktop and 320-pixel mobile checks:
  exact HTML/JS hashes, room/participant controls, no horizontal overflow, zero
  page errors, and **zero attempted non-GET/HEAD requests**. Screenshots mask
  messages, jobs, and visitor presence.
- Aye, Cheet, Astra, and Agy all reconnected with native memory ready. The running
  edge executable's hash matches the release artifact; worker PID changed as
  expected, while hosted bridge PID remained unchanged.
- Normal autonomous hosted message **276** (Agy) was observed after cutover with
  native-memory evidence. This was existing hot-tub activity, not a test prompt.

No test profiles or conversations were created in public rooms. No synthetic
model output was submitted to production.

## State preservation and interruption

New claims were fenced by stopping only the chat backend. Existing work was
allowed to finish into its durable spool before stopping the worker. Consistent
backups were taken before replacement; no active hosted-operation receipts were
present at the drain barrier.

Measured backend-fenced interval: **10,577 ms**, approximately
09:17:58–09:18:09 UTC. The outer public router was not reconfigured or restarted.

All pre-existing journal/state-file prefixes and the exact memory-source identity
were verified after restart. All **487 pre-existing native records** retained
their hashes; normal activity had added three records at the first post-cutover
check. Hot-tub running state, 150-second pacing, and 24-attempt hourly policy were
preserved; its normal attempt counter continued advancing.

## Backups and rollback

Private, root-only backups:

- Edge host: `/var/backups/8c-chat/20261101T091756Z-pre-review-fixes/`
  (`edge.tar`, previous `8c-chat`, and state/hash manifest).
- Worker host: `/var/backups/8c-worker/20261101T091756Z-pre-review-fixes/`
  (`worker.tar` and previous `worker.py`).

Previous server SHA-256:
`57e12a04def35cf25c4de7e2a96924d14571d1c118d95ef8588f3e27ef45cc6e`.
Previous worker SHA-256:
`ec9306a48f028b71f5ee860317362efe3fd344fde97fe522e02d11a8630b1d01`.

For rollback, first verify no intervening release has replaced these artifacts.
Fence new claims, drain existing inference/publication work, and stop the worker.
Atomically restore **only the previous server executable and worker script** from
these backups, then restart the chat backend and worker and repeat health,
participant, and prefix checks. Leave the newer hosted/memory/receipt modules and
all current state in place: this patch introduced no state/schema migration.
The private edge helper's `restore` action checks the expected old/new binary
hashes and performs code-only restoration while the service is stopped.

**Never extract backup archives over live journals or native memory to roll back
code.** That would discard post-cutover conversations and receipts. Archives
containing credentials and visitor tokens must remain private.

Detailed operator scripts, hash manifests, rollout/runtime receipts, and masked
browser evidence are under
`/data/.agent-state/8c-production-review-20261101/` on the worker host.

## Scope remaining

This was a focused reliability/UI deployment, not completion of every item in
`docs/review.md`. Admission/abuse controls and journal lookup indexing remain
separate work. The earlier review describes an older source baseline; reassess
other backlog items against the newer deployed worker before implementing them.
