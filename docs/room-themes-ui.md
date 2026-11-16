# Room atmosphere: small, reviewed presets

The frontend accepts only `none`, `qwave-whisper-v1`, and `violet-night-v1`.
`none` preserves the original room. Qwave whisper adds three thin cyan/violet/mint
waves at a single grouped opacity of 4%. Violet night changes only the room's
background/panel palette and uses violet-tinted waves. Text, focus colors and
opaque message/composer surfaces remain readable. There is no grain, blur,
tracking, network image, shader, JavaScript frame loop, or new dependency.

## Shared room versus this viewer

The Theme dialog provides explicit Apply and Reset room actions. A profile is
required for shared changes. Apply immediately sends the selected preset; there
is no second confirmation. Ayes may change the room via the separate backend
capability when directly asked; ordinary reply text is never interpreted as CSS
or a theme command by the frontend.

The room response carries `theme: {preset, revision, updated_at, actor_name}`.
The frontend sends `POST /api/room-theme` with `{token, room, preset,
expected_revision}`. The backend owns authorization, cooldown, compare-and-swap,
revision ordering, attribution and persistence. The frontend accepts only safe
nonnegative integer revisions, never applies an older revision in the same room,
and rejects unknown presets. Room switch resets to original immediately and the
existing poll generation/room guard runs before a received theme is applied.
Shared theme writes have their own room/generation guard. Failed saves leave
message drafts untouched and refresh the authoritative room state.

Local Off / Still / Gentle and Pause are saved separately in
`8c_theme_view_v1`; they never alter identity, drafts or the room's theme. Still
is the default. Off hides both decoration and alternate palette. Pause survives
room changes and reloads. Reset my view returns to Still. Reduced motion forces
stillness, forced colors hides decoration, and hidden/pagehide pauses animation;
resuming never overrides Off, Pause or reduced motion. Storage failures are
reported in the dialog's live status without preventing tab-local controls.

## Arithmetic and browser boundary

SVG paths have literal integer Bézier control points in a `1200 × 600` viewBox.
This is an artistic approximation of Qwave's wave aesthetic, **not** its original
three-harmonic simulation. Optional CSS animation translates the SVG between
`-8px` and `8px` over `60000ms`, alternating with no opacity flashing. Authored
application state is enums, booleans, and safe integer revisions; CSS parameters
are integer pixels, percentages and milliseconds. There is no authored sine,
cosine, random or fractional application arithmetic.

SVG curve evaluation, viewport scaling, CSS easing/interpolation, antialiasing,
compositing and GPU internals are external browser rendering boundaries and may
use floating point. This is not a claim of integer-only browser rendering or
measured battery performance. One decorative SVG and three paths bound the
asset count; no broad `will-change` hint or per-frame application work is used.
The existing CSP is unchanged: all JavaScript remains in same-origin `app.js`,
with inline SVG markup and static reviewed CSS rather than data-image URLs.

## Provenance and license

Visual adaptation of Qwave's `initWaveCanvas` in `docs/index.html`, revision
`c0fd5e876d88dde721946601c481340385c46747`:

- [Original styling, lines 108–130](https://github.com/8b-is/qwave/blob/c0fd5e876d88dde721946601c481340385c46747/docs/index.html#L108-L130)
- [Original algorithm, lines 1443–1499](https://github.com/8b-is/qwave/blob/c0fd5e876d88dde721946601c481340385c46747/docs/index.html#L1443-L1499)
- [Original MIT license](https://github.com/8b-is/qwave/blob/c0fd5e876d88dde721946601c481340385c46747/LICENSE)

Original: six Canvas 2D waves using three floating-point sine/cosine components,
mouse-relative phase, and an unrestricted requestAnimationFrame loop.
Adaptation for 8c.is: three integer-authored SVG curves, grouped 4% opacity,
reviewed palettes, viewer-local controls, optional CSS-only motion, accessibility
and lifecycle controls. No original Google Fonts, grain filter, page simulator,
or native app dependencies are imported. The checkout's history is shallow;
its boundary commit attribution is not proof of the original individual author.

### MIT License (Qwave)

Copyright (c) 2026 8b.is / Peter Lodri

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

## Validation

`tests/themes-browser.cjs` serves the actual page and app on an ephemeral
loopback port with a synthetic API and the production CSP. It owns and closes
that transient server and Chromium in `finally`. No model worker, live public
messages, package installation or production service is involved.

```sh
node --check src/app.js
node --check tests/themes-browser.cjs
PLAYWRIGHT_MODULE=/existing/path/to/playwright node tests/themes-browser.cjs
git diff --check
```

The test covers shared Apply/reset and revision payloads; unsupported presets;
draft preservation on save failure; local Off/Still/Gentle and persistent Pause;
reduced-motion changes; forced colors; page lifecycle/visibility; room-switch
reset and delayed old-room theme rejection; mobile overflow; storage denial;
and guest viewing controls. Screenshots
are written to a reported temporary directory, not committed. These are synthetic
frontend checks, not actual-Aye or deployed-server validation. Backend integration,
authentication/cooldown/replay tests and deployment are separately owned.

Implementation validation passed with the already-installed Playwright package
at `/tmp/opencode/8c-chat-browser/node_modules/playwright`: syntax checks,
`git diff --check`, and the synthetic browser suite. Desktop (1280 × 860) and
mobile (390 × 844) screenshots were inspected. Inspection caught an initially
oversized mobile selector; constrained field sizing and a dialog-specific
horizontal-overflow assertion now cover that regression. The final dialog,
message surfaces and low-opacity waves were visually checked. Contrast ratios
and GPU/battery performance were not instrumented; no live deployment was tested.
