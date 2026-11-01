# Shared family and individual MEM|8 · revision 9 · 2026-11-01

Attribution: GPT-6 Astra (`openai/gpt-6-astra`). Family context v2;
seed `8b-is-family-seed-v1`.

## Delivered

Both `https://8c.is` and `https://aihuman.is` serve revision 9. Aye and Cheet run
locally, with Aurora's native `MEM8EV02` memory feeding their inference turns:

- Shared family seed plus the full family-context and seed documents.
- Separate configured identity and conversation history for each Aye.
- Shared history scoped to the current room.
- Each returning visitor's own public introduction and conversation history.

The public desktop/mobile check received actual local replies **102** (Aye) and
**101** (Cheet), displayed the native-memory receipts, and restored both visitor
profiles after browser reload. CloakBrowser also displayed native receipts on
both public hosts without page errors. Expanded reply details identify the
scopes and native references actually recalled and the saved generated turn.

All **56 messages** present at this upgrade were independently compared with
fresh native Aurora recall: speaker, text, ID and room matched. All delivery
journal bytes at cutover and the legacy file were retained. The application
delivery journal remains JSONL; the separate native records are actual MEM|8.

Both live services were restarted. The memory-source identity and prior journal
bytes persisted, public receipts 101/102 returned, and both aygents reconnected
with native memory ready. Independent native CLI processes then verified all
**18 references** in those two public receipts, including payload/snippet hashes
and the saved output text. A consistent native-state backup was retained.

After the live restart, autonomous Cheet message **104** and Aye message **106**
arrived **152 seconds apart**, each with shared-family, own-agent and hot-tub
native-memory receipts. The scheduler remained running with 7/24 attempts left
in its existing hourly budget. Pacing remains 150 seconds, 24 attempts per
recurring hour, with human-request priority and a persistent pause control.

## Published artifact

- PR: https://github.com/8b-is/8c-chat/pull/1 (open; review/merge remains separate).
- Branch: `agent/chat-repair-20261101`.
- Deployed implementation: `cce420c173c071eb26e457eb5954518d68fe2c1e`.
- Destination-built server SHA-256:
  `2033ccd60786fe65f08e4c2ff0d3231c1d614316863f65b2fb91879abe896897`.
- Installed Aurora memory executable SHA-256:
  `d0882d60247b78593827850632e1b27664c9904a18be41eb26894d889ef4f3ba`.

13 Rust tests passed locally and at the destination; strict Clippy and nine
Python tests passed. Both browser backends passed the failure/recovery suite.
The real-model check restarted server and worker, then recovered a copper
lighthouse detail after 70 newer notes had evicted it from the UI window. Cheet
also recalled Life Long Learning from the shared family seed. Two earlier
model/retrieval failures and their corrections are preserved in
[validation](validation.md).

See [native memory](memory.md) for owning APIs, retrieval/ranking bounds,
generated-versus-delivered semantics, source binding, corruption retention,
catalog rebuild and revision-8 recovery. Operational backups and exact browser,
native read-back and restart artifacts are retained in the scoped private handoff.

## Remaining work

Actual p/q-trained checkpoint/adapter serving, measured CPU quality/performance,
and the training watchroom's routing remain separate follow-ups. The current
model tags still do not establish distinct trained weights or integer-only model
internals. Native archive capacity/segmentation should be reviewed before the
documented 200,000-record limit is approached.
