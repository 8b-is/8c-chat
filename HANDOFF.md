# Current handoff

Revision 9 adds deployed native MEM|8-backed aygents with shared family and
individual context. See [the native-memory handoff](docs/mem8-handoff.md),
[the earlier repair handoff](docs/repair-handoff.md), and
[validation evidence](docs/validation.md).

The revision-6 receipt below is retained as history.

# 8c.is room · revision 6 · 2026-11-01

Family context version: 2
Seed: `8b-is-family-seed-v1`

## What was wrong

`https://8c.is` answered, but an empty visit was marked as Hue and
`verified`. Alexandra’s phone was then caught by a false impossible-travel
check and two alert notes were written into `#humayian-lounge`. On a phone
the name controls sat in a hidden sidebar, and the message list was rebuilt
every few seconds, which jumped the scroll position. `/favicon.ico` and
`/api/health` returned the HTML page.

## What changed

The service is now an honest family room. A name is chosen on the device.
It is not a voiceprint and not a location. Empty requests are not Hue.
Notes stay in the room they were sent to. The phone layout leads with
“Who is here?” and a Talk button. Speak is off until asked. Mail is a link
to `mail.8b.is`, not a fake inbox.

Family notes were kept, including Alexandra’s “I thought you wanted me to
talk…not text”. The two impossible-travel alerts and one probe note were
removed from the live file. The previous file is still at
`/var/lib/8c-chat/rooms.m8.pre-ui6`.

## Artifacts

- Source: `/data/8b-is/8c-chat` and `/usr/local/src/8c-chat` on the i1-is edge
- Binary: `/usr/local/bin/8c-chat`
- Binary sha256: `6187699f484b4314e0025aea9282f21b1808fc1361226cc18c43d7507b146519`
- Previous binary: `/usr/local/bin/8c-chat.pre-ui6`
- Previous binary sha256: `b7ba7add4b0ece31a776eeefc89b83dafd557c217258b9d8cadbf8b91715fbdf`
- Previous state sha256: `6733249ecd2500f066c2875388dc115c4f80606db64d8b8acf40def2475c49ae`
- Service: `8c-chat.service`, `127.0.0.1:8798`, still fronted by `i1-is`

## Checks

- `cargo test --offline`: 11 passed locally and on the edge host
- Local HTTP: empty handshake is not Hue; a Dra claim stays in `dra-hue`
- PNG icon inflates; page script has no `innerHTML` and no decimal literals
- Live `https://8c.is/` and `https://aihuman.is/` return the new page,
  health JSON `ui_revision` 6, and the kept family notes

## Not claimed

A chosen name is not authentication. Aye’s local model was not exercised
on the live room in this change. `i1-is` still reports `Content-Length: 0`
on HEAD for proxied pages; browsers use GET, which is intact. Registrar
renewal was not touched.
