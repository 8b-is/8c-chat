# 8c.is · the family room

A small room for Alexandra, Hue, Trish, Peter, and Aye. The page is one
HTML file. The server is pure Rust, `#![forbid(unsafe_code)]`, with no
dependencies and no authored floating-point arithmetic.

## What this is not

A chosen name is a choice remembered on the device. It is not a voiceprint,
not a location, and not a lock. The room used to guess Hue, and it used a
false “impossible travel” check to push Alexandra into the guest lobby.
That check is gone. Mail stays at `mail.8b.is`; this page does not open it.

Room tones already in the log stay as labels: Hue `432` Hz, Alexandra and
Aye `528` Hz. They are not measurements.

## Run

```sh
BIND_ADDR=127.0.0.1:8798 STATE_PATH=/var/lib/8c-chat/rooms.m8 cargo run --offline
```

`AYE_DISABLE=1` skips the local model. Otherwise a question, or a note that
says Aye, asks `127.0.0.1:11434` (`AYE_MODEL`, default `cheet-deepseek-v2:16b`)
and posts a short reply. If the model does not answer, the room says so.

## Check

```sh
cargo test --offline
```

The live service is `8c-chat.service` on the i1-is edge that already serves
`8c.is` and `aihuman.is`.
