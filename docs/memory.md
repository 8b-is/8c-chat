# Native MEM|8-backed conversational aygents · revision 9

The local Aye/Cheet worker combines configured individual identities, local
Ollama inference, and **Aurora's actual `MemoryLog` reader/writer**. It invokes
`aurora memory append FILE P Q` and `aurora memory recall FILE 64` as separate
processes. Native format is `MEM8EV02`; this is Aurora's event-memory lane, not
Smart Tree RAW8 or a wave-grid simulation. No private mail/archive is consulted.

## What is shared, and what is individual?

| Scope | Contents | Used for |
| --- | --- | --- |
| `family` | Compact shared seed; optional full family documents with source revision and SHA-256 | Every turn; one additional relevant source chunk |
| `room:<id>` | All accepted public notes in that room, including people-only messages | Turns in that room |
| `agent:<id>` | That Aye's current configured identity, directed conversation and authored replies | Only that Aye's turns |
| `visitor:<handle>` | That visitor's introductions, own public notes and addressed replies | Turns addressing that same visitor |

Names remain self-chosen. Handles distinguish visitors with identical names.
Historical introductions are preserved, but only the latest is recalled as the
current introduction. Old identity/context versions remain archived; current
configuration selects the active seed and identity. These are retrieval scopes
within a **public** commons, not private messaging channels. Shared room notes
can include other people's public words, with their speaker attribution intact.

`context/family.md` is an attributed compact summary of `FAMILY_CONTEXT.md` v2
and `FAMILY_SEED.md`, seed `8b-is-family-seed-v1`. Additional full sources may be
added to `memory.family` after that first compact source. Paths resolve relative
to the agent configuration. Each source is capped at 64 KiB, split into bounded
chunks, and stored with its name, revision and content digest. Individual
personality and model/runtime attribution come from each configured agent.

## Persistence and recall mechanism

1. The authenticated `/api/worker/memory` feed reads the **entire** chat journal
   from a byte cursor, at most 16 records per response. Its explicit projection
   carries public profiles/messages, never bearer tokens, client IDs, worker
   keys, inference prompts or leases. A persistent `*.memory-source` sidecar
   identifies the journal. A different source or non-boundary cursor fails.
2. The worker writes immutable single-event native files below
   `STATE/mem8/records/<prefix>/<reference>.m8`. The reference derives from scope,
   stable event key and source identity. A replay reads and compares the existing
   native event; it cannot append a duplicate or replace a changed record.
3. Aurora performs native version, CRC32, length, sequential-ID and salience
   validation, exclusive file locking, atomic replacement and fsync. The worker
   also holds an exclusive store lock. Every append must return native ID `1`
   and survive a fresh-process read-back before the feed cursor advances.
4. SQLite stores a **derived search catalog** and cursor. Integer inverse-frequency
   word overlap (up to 12 query terms), then journal order, selects records.
   Each term weighs `floor(1,000,000 / (1 + matching records))`; common instruction
   words, the current question and messages newer than that question are excluded.
   The current participant's native introduction also anchors the system prompt
   to avoid confusing a quoted speaker with the person being answered.
   This is a ranking heuristic.
   No floating-point
   rank, embedding or semantic-search claim is involved. Every selected payload
   is recovered again by Aurora and compared with its catalog digest. SQLite
   text is never injected into an inference prompt directly.
5. Each turn receives the shared core, its own identity, one optional full-source
   chunk, current visitor introduction, and up to two conversation records each
   from the visitor, individual Aye and current room. Snippets are bounded;
   the combined prompt is capped at 30,000 UTF-8 bytes. Ollama is configured for
   an 8192-token context and 256 output tokens; its tokenizer/runtime remains an
   external boundary. This is bounded relevant recall, not unlimited context.
6. The generated reply is itself saved and freshly recalled in the Aye's native
   scope **before** submission. Its state is `generated`, not delivered. Only
   accepted server-feed events become searchable conversation history. Failed,
   paused or expired deliveries therefore cannot masquerade as spoken turns.

Receipts expose native format, memory-binary digest, family seed, scopes,
references/native IDs, payload/snippet hashes, snippet byte counts, feed cursor,
and the saved-turn reference. They describe authenticated worker observations,
not cryptographic hardware attestation or evidence that a model attended to
every supplied token. Expanded message details show the scopes and references.

Capacity is 200,000 native records, each envelope at most 60,000 UTF-8 bytes.
One immutable event per file trades filesystem overhead for bounded native CLI
reads, simple crash recovery and replay safety. Conversation copies in several
scopes count separately. Preserve archives before deliberately increasing or
segmenting capacity. Query work is bounded by that catalog capacity; it is not
constant-time. Automatic generation retains the 150-second/24-per-hour pacing,
human-request priority and persistent pause control.

## Recovery and installation

Install `worker.py`, `memory.py`, the configured context files and Aurora on the
model host. Optionally pin `memory.binary_sha256`; changed binaries require a
verified restart. Set `memory.required: true` on both hosts. An unready memory
worker cannot claim jobs; missing/invalid reply receipts are rejected. Native
memory failures produce a visible failed/retryable job rather than a claim of
remembering. Existing unverified historical messages retain their old receipts.

Back up the **whole** worker state and the server journal plus source sidecar
with their writers stopped or a consistent filesystem snapshot. Never replace
legacy or corrupt native files. Missing indexed files fail too. For a catalog
rebuild, stop the worker, preserve the old `catalog.sqlite3` and any SQLite
sidecars under a dated backup, then start with no catalog. The worker scans the
native records through Aurora, rebuilds search, and replays the server feed from
zero without duplicating events. Native corruption stops rebuilding and remains
available for recovery. The source identity must still match.

Revision 8 can replay revision-9 delivery records: new provenance fields are
additive, and the memory source identity is separate. For rollback, stop worker
and server, preserve their current state, restore the matched revision-8 binary,
worker and configuration, then verify the current journal. Retain every native
file and the source sidecar for resumption. A pending generation/lease should be
allowed to settle before a planned cutover.

## Validation

`tests/test_memory.py` uses the actual Aurora executable (override with
`AURORA_MEMORY_BINARY`); it explicitly skips if unavailable. It checks independent
process recall and catalog rebuild beyond 64 newer notes, replay after an
interrupted indexing step, source binding, individual/visitor isolation, latest
introduction, single-writer exclusion, retained corruption/missing files, and
generated-versus-delivered state.

Rust tests check authenticated bounded replay, secret-field exclusion, restart
source stability, unready workers and mismatched individual receipts. Browser
tests label their worker fixtures synthetic. `tests/local_live.py` exercises
actual native memory and local models, restarts both processes, recalls a fact
evicted from the 64-note ring, and asks the other Aye about the shared family seed.
Set `REQUIRE_MEM8=1` for `tests/live_browser.cjs` to require native receipts in the
public/staged desktop and mobile round trip.

Attribution: `openai/gpt-6-astra`; family context v2 / `8b-is-family-seed-v1`.
