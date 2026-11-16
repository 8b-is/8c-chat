# Room-theme and public-reply validation — revision 11

2026-11-01 · OpenAI GPT-6 Astra via DeepSeek Harness.
Family context v2 / `8b-is-family-seed-v1`.

**Prepared for integration, not deployed.** The owner chose the existing 8c repair
stream to combine this work with its ongoing recovery/deadline changes and perform
one coordinated rollout. No production service, configuration, history or memory
was modified by this work. No packages were installed and no Git push was made.

## Checks actually passed

- **26 Rust tests**, strict offline Clippy, debug and release builds.
- **63 Python tests**: protocol and spool fixtures, worker transport, bridge peer
  publication, funding, actual isolated command-sandbox checks and actual native
  Aurora memory persistence. No live model/provider was called by these tests.
- Actual Rust HTTP/journal + Python worker integration, with explicitly synthetic
  inference: both reported JSON leak shapes fail without dispatch; normal prose
  works; a theme action is accepted only with a server capability; effect and
  factual reply are committed together; duplicate delivery is idempotent; stale
  effects fail without a success message; restart preserves styles and source ID.
- Actual revision-10 binary built from `7424764`: replayed the new journal and
  appended a synthetic ordinary message. Revision 11 then recovered the theme,
  new message and identical memory-source identity. Prior journal bytes remained
  an unchanged prefix. This is **journal-reader compatibility**, not permission to
  mix worker/server revisions or run old workers on new in-flight theme jobs.
- Browser suite against the actual Rust backend and production frontend: existing
  two-visitor/draft/retry behavior, synthetic Aye theme action, room isolation,
  actor display, manual reset and viewer-local Off persisted across reload.
- Separate production-frontend/synthetic-API browser suite: allowlist/CAS requests,
  stale room/POST responses, draft retention, Off/Still/Gentle/Pause persistence,
  reduced-motion changes, forced colors, hidden/pagehide behavior, unavailable
  storage, guest controls and mobile/dialog overflow.
- Node syntax checks and Git whitespace checks.

Screenshots were inspected at 1280×860 and 390×844. Decoration is very faint and
text/composer surfaces stay opaque. These images are test fixtures, not evidence
of live Aye inference. Numerical contrast, GPU and battery measurements were not
performed.

## Review findings addressed

The first intent hint used unordered keyword co-occurrence and would grant a
capability to “Why did you change the background?” or quoted past instructions.
It now requires a finite initial imperative/polite request, with negative and
quoted/explanatory forms declined; regression cases cover these boundaries.
It remains deliberately conservative and English-only, not general NLP authority.

A further fence regression ensured a malformed inline Markdown opener cannot
swallow the first line containing a tool object/array and let it become public.
Ordinary data JSON and explanatory code remain readable; explicit `say.text` is
literal and never recursively dispatched. Ambiguous naked tool examples fail
closed rather than being executed or silently rewritten.

Accepted peer-bundle text now comes from the server's acknowledged public message,
not a hosted bridge's cached raw protocol envelope. Original generated receipts
are retained. Rejected/stale ACKs and duplicate rejections cannot publish peers.

## Integration requirements

- Preserve the other repair stream's newer session recovery, finalized-operation
  replay, deadlines and deterministic observed-work reporting. Do not replace its
  newer worker/bridge wholesale with this baseline-derived tree. During final
  verification that repair independently deployed `1273c3e`; its installed worker
  and bridge matched that continuation checkout. Reconcile against the current
  committed deployment, not the old `7424764` worker used as this branch's base.
- Reconcile the reviewed theme capability/action with its constrained output
  schema. Theme effects are server settings, never filesystem/shell actions.
- Keep worker `reply_protocol.py`, private-spool guard, server publication ACK and
  bridge `public_text` acceptance semantics together. Local bookkeeping markers
  must not enter public API payloads or become action authorization.
- The server derives room/author from the leased job, independently checks the
  preset and saved revision, and commits the effect with its action receipt.
  Failed effects ACK `published=false`; duplicate ACKs preserve that state.
- Drain in-flight work and preserve consistent journal, memory-source, MEM8,
  pending spool and workbench receipts before rollout. Preserve quarantined bytes;
  never repair or replay the pasted commands/arrays to manufacture success.
- Do not silently rewrite historical malformed public replies.
- Re-run the combined suites and actual-model checks after reconciliation. Then
  verify the existing public URL, desktop/mobile behavior, restart recovery and
  matching installed modules. No such production/combined-model check is claimed
  by this handoff.
