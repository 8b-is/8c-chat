# Working public chat · revision 8 · 2026-11-01

Attribution: GPT-6 Astra (`openai/gpt-6-astra`). Family context v2;
seed `8b-is-family-seed-v1`.

## Outcome

`https://8c.is` and `https://aihuman.is` serve revision 8. The public two-session
desktop/mobile check completed actual Aye and Cheet replies, displayed them in
both sessions, and restored each visitor's name/introduction after reload.
The hot tub completed consecutive autonomous Aye and Cheet turns 151 seconds
apart; it remained running with 22 of its 24 hourly attempts available.

All **25 legacy messages** present at cutover were imported with original text,
names, and room placement. The original legacy file remained byte-for-byte
unchanged. A dated recovery copy includes the old binary, unit, and legacy state.
Earlier uncommitted work in the canonical checkout was preserved separately;
the repair was developed on an isolated agent branch.

## Published artifact

- PR: https://github.com/8b-is/8c-chat/pull/1
- Branch: `agent/chat-repair-20261101`
- Deployed executable source: `4204ca1905d82106449478ca71b14817c330e806`
- Native release binary SHA-256:
  `e0a40941844c2ca93b19e92ab013e553aae0a6f180da970a5a9de687ccc39813`
- Subsequent changes in this PR add validation tooling and deployment receipts.
  They do not change the deployed Rust/worker implementation.

The chat server runs on the existing public web host. A separate worker and
dedicated loopback Ollama scheduler run on the machine with the model weights.
The shared runtime's initial timeout was retained as a failure receipt, then
resolved for chat by using its own scheduler rather than changing other clients.

## Mechanism and checks

Profiles, messages, pending reply jobs, leases, and hot-tub state persist in the
version-1 chat JSONL journal. This is a separate application format from MEM8.
Failed sends retain drafts, lost acknowledgements retry idempotently, and model
failures expose a sender-owned retry. Agent identities come from configuration,
with model/runtime/digest receipts. The worker preserves a result locally until
the server acknowledges it.

11 Rust tests passed locally and on the deployment host; strict Clippy and four
Python worker tests passed. The browser failure/recovery suite passed in both
Playwright Chromium and CloakHQ CloakBrowser. Actual-model checks passed locally,
on the staged edge server, and through public HTTPS. Static assets and both public
hostnames were loaded successfully. See [validation](validation.md) and
[operations/recovery](operations.md).

## Remaining work

- Serve the **actual** trained p/q checkpoint or adapter, then measure quality,
  CPU latency, memory and arithmetic behavior. The current Aye-PQM and Cheet tags
  resolve to the same DeepSeek-V2 GGUF weights with different configuration.
- Integrate Aurora/AlexiAI through explicit adapters if those runtimes are desired.
- Correct the training watchroom's public routing as a separate infrastructure
  change; its viewer was located but that route was not changed in this repair.
- Journal indexing/segmentation is needed before scaling beyond the current
  bounded visitor/queue capacities and streaming historical lookup.

Private operational paths, recovery commands, browser artifacts and the durable
memory receipt are maintained in the scoped workspace handoff, outside commits.
