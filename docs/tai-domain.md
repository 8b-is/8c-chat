# Text AI at tai.is

The public home is `https://tai.is`, branded **Text AI**. The edge also accepts
`www.tai.is`; legacy chat host routes remain available when their DNS resolves.
The inference worker uses `--chat https://tai.is`.

This migration changes the public domain and presentation, not the journal
schema, room IDs, worker key, service names, or historical identity handles.
Existing `8c-chat` service/state paths and `8c_*` browser-storage keys remain
intentional compatibility names. The server continues using its existing journal
and memory-source sidecar. Never create an empty journal to change a hostname.

Browser storage is scoped to the origin. Visitors arriving from `8c.is` may need
to introduce themselves again on `tai.is`; server history is preserved. Existing
profile tokens remain valid with the same server, but browsers cannot silently
transfer another origin's storage.

Identity and mail remain available at `/@market` and `/@mail?domain=tai.is`.
Their `/api/market/*` and `/api/mail/*` routes are handled by the edge before chat
proxying. The page links to both services.

The deployed inference endpoints use Smart Tree with native model backends.
Historical `runtime: "ollama"` values identify the compatibility API protocol;
they do not require the removed Ollama package. Do not reinstall the old
`deploy/8c-model.service` launcher as part of this domain change.

Validation includes the Rust tests and strict Clippy, Python tests, desktop/mobile
browser tests against a separate journal, and a destination-host staging check.
Public migration checks must additionally verify TLS, Text AI branding, the
health/config endpoints, preserved history, the worker's authenticated memory
feed, and the retained identity/mail paths. Avoid posting test messages into
public rooms unless the user explicitly requests them.
