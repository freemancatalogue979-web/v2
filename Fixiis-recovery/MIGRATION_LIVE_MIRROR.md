# Live Mirror Pipeline Overhaul — fast capture + DOM deltas

> **STATUS: IMPLEMENTED (D0–D4), DEFAULT-ON.** Flags: `DOM_CAPTURE_MODE=fast` (live mirror; archives keep SingleFile),
> `SEND_COALESCE=1`, `DOM_CAPTURE_SKIP_UNCHANGED=1`, `LIVE_DELTA=1`, `DOM_CAPTURE_FULL_SETTLE=1` (rollback
> to legacy settle waits), `ARCHIVE_FORMAT=mhtml` (PCM archive captures via CDP `Page.captureSnapshot`).
> D0 logs are DEBUG-only (`dc send reason=... capture_ms=...` + one-time WS-extension line). Verified by
> py_compile, node --check, a 28-case jsdom end-to-end suite (serializer → mirror render → observer ops →
> patch apply → parity asserts) and a 22-case asyncio harness (fast send, asset rewrite incl. CSS recursion,
> unchanged-skip, coalesce latest-wins, SingleFile degenerate fallback, delta relay/drop/overflow).
> One behavioral change to be aware of: the in-page trigger now delegates safe mutations entirely to
> the delta observer.  Full captures are reserved for navigation, observer overflow, structural
> desynchronization, and explicit resync; this avoids rebuilding the iframe after ordinary clicks.
> Follow-up: small images/icons (<= `DOM_CAPTURE_EMBED_MAX_BYTES`, default 1 MB) are data-URI embedded
> in the HTML (logos/SVGs/GIFs render without the client ever hitting /assets — proxy/offline safe);
> larger media + CSS + JS keep the cache path; URL->digest memoization makes every URL truly fetch-once;
> > Follow-up (SB capture fix): the fast serializer now runs via an IIFE-wrapped
> Runtime.evaluate expression — the SAME mechanism on Playwright and raw-CDP/SB
> (the callFunctionOn function+args shape is where the two backends diverged and
> SB fast-captures returned None).  Capture is now tiered: fast -> SingleFile
> (extension-only, absent on UC) -> document.documentElement.outerHTML
> (delta off, asset rewrite on).  When ALL tiers fail, an ERROR names the real
> underlying exception instead of a silent None.
> Follow-up (no-modification policy + CSS floodgate): the serializer no longer
> re-materializes ANYTHING the site didn't author — no live value/checked/selected
> attributes, no textarea text injection, no canvas->img conversion (the "input
> placeholder / image element" mods are gone; only Montserrat stays).  Typed text
> still mirrors live as properties via dom_patch 'v' ops.  Asset pipeline:
> ASSET_MAX_PER_PAGE 256 (was 48), inner CSS refs 96 (was 24), CSS inline
> threshold 3MB, Referer:<page> on fetches — mirrors get ALL of the site's CSS.
> Dead subframes (chrome-error / cross-origin iframes) are deleted at capture.
> Follow-up (typing fix): text/keydown recaptures are now ALSO skipped when the delta
> observer is installed — keystrokes stream as 'v' value ops (no per-keystroke full
> captures, which reloaded the mirrored iframe, dropped focus and made typing
> impossible, and stormed the CDP channel). CSS: stylesheets <=
> `DOM_CAPTURE_CSS_EMBED_MAX_BYTES` (default 1 MB) are INLINED as <style> blocks
> (media preserved, inner url()/@import rewritten, small nested css data-URI'd,
> literal '</style' closers escaped) so mirrors stay styled without the /assets route.
> the per-click transition overlay ("wait for capture" spinner) is removed — fast captures make it flicker.


Verdict on the external proposal: **yes, faster than today's pipeline — but not the way it says.**
The real bottleneck is SingleFile-per-interaction (correctly diagnosed). The prescribed cures
(msgpack, CDP `DOMSnapshot`, per-frame binary screencast) are mostly the wrong ones for this
codebase. This plan keeps the diagnosis, replaces the cures, and reuses infrastructure that is
already half-built in the repo.

## 1. Ground-truth audit of the proposal's claims

| Claim in proposal | Audit result |
|---|---|
| `_send_full()` runs SingleFile then ships MB-sized HTML per event | **TRUE** — `dom_capture.py:1713` → `capture_page()` → `_capture_with_single_file` (extension bridge round-trip), then `json.dumps({"type":"full_document","html":...})`. |
| `_ensure_page_stable` adds waits per capture | **TRUE** — up to ~6s of `load`/`networkidle`/`readyState` waits **even for interaction recaptures** of an already-live page (`_ensure_page_stable` + `capture_page` domcontentloaded wait). This is a hidden constant on every push. |
| Serializing MBs into a JSON string per event | **PARTLY TRUE** — CPU + latency cost real, but the wire is **already deflate-compressed**: uvicorn `ws="auto"` → `websockets` lib negotiates permessage-deflate with browsers by default. msgpack's "30–50% wire win" mostly doesn't exist. |
| Use CDP `DOMSnapshot.captureSnapshot` | **REJECTED** — experimental API (Chrome has been removing pieces), returns flat node arrays that are NOT re-renderable HTML: client-side reconstruction is a large fragile project, computed styles must be requested (re-bloats payload), zero story for images/fonts. |
| `DOM.setNodeForEventListening` for per-node subscribe | **DOES NOT EXIST** in CDP. Real path would be `DOM.getDocument(depth:-1)` + push events with backend node-id bookkeeping — the most fragile option. |
| Mutations via MutationObserver, 100–500 B ops | **RIGHT IDEA, wrong transport.** We already have the ideal delivery channel: in-page injected JS + `expose_function` (`__domCaptureRequest` pattern). Same ops, without CDP node-id hell. Plan phase D3. |
| Screencast: JPEG q80, `everyNthFrame:2` | **REJECTED** — conflicts with standing constraint: PNG q100, exact frame pixels. Screencast is untouched by this plan. |
| Bootstrap-only SingleFile | **ADOPTED as boundary, refined:** SingleFile stays for archive-quality artifacts (PCM pages, LPV store) where self-containment is the point. Live victim mirror switches off it. |

### What the proposal missed (and it changes the plan's shape)

1. **Double-trigger per interaction.** Two independent paths call `send_page`:
   - in-page `_INTERACTION_TRIGGER_JS` → `__domCaptureRequest` → `_on_request` (0.08 s flood control; delta-first path)
   - `session.py` input handlers → `capture_remote_page(reason='click'|'text'|'keydown'|...)` — **no debounce at all**, each a fresh `asyncio` task.
   Overlapping SingleFile runs stack up under fast typing/clicking.
2. **Dead asset-cache infrastructure already built:**
   - `api.py`: `/assets/{hash}` endpoint (content-addressed, `Cache-Control: immutable`), `/api/cache/stats`, `/api/cache/clear` — all functional **except** they import `get_global_asset_manager` from `dom_capture`, which **does not exist** (whole file checked). Every `/assets` request 503s with `logger.error`; `/status` silently lacks stats.
   - `client.html`: `dispatchAssetList()` prefetch helper already written, waiting for `/assets/<hash>` URLs.
   - The design comments in `api.py:1636` describe exactly the pipeline this plan lands: *"rewrites every external URL inside the captured HTML to /assets/<hash>… keeps recaptures lightweight and cache-friendly."* The server+client halves shipped; the capture half never did.
3. **Client already has generation/ack semantics** (`state.lastGeneration`, `resync_request` with `currentGeneration`, `client_ack_url`) — the delta layer (D3) plugs into an existing recovery protocol instead of inventing one.
4. **Client input-state preservation already solved** — `renderPageSnapshot` saves/restores values, checked state, selection, focus with `_lastValue` dedupe. A delta path that doesn't rebuild the iframe makes most of that unnecessary, and can reuse it as fallback.

## 2. Verified current pipeline (facts for the record)

```
in-page activation JS ─┐                          dom_capture.py:1414
session.py handlers ───┴─► send_page ─► _send_full ─► capture_page()
   click/text/keydown (no debounce)        _ensure_page_stable (~0–6 s)
                                           SingleFile ext bridge (fetch+inline
                                           EVERY asset, base64, MBs, seconds)
                                     ─► json.dumps(full_document) ─► WS (deflate on)
client.html: applyFullDocument ─► renderPageSnapshot: save inputs ─► NEW blob
iframe (full parse + subresource load) ─► restore inputs.  Every. Single. Push.
```

## 3. The plan

Same discipline as MIGRATION_SELENIUMBASE.md: phased, flagged, revertible per phase.

### D0 — Measure (30 min, no behavior change)

DEBUG-only (respects "errors only in prod, DEBUG escape hatch"):
- `_send_full`: log `reason, trigger(js|py), stable_wait_ms, bridge_ms, html_bytes, deflate_est, total_ms`.
- One-time at WS connect: negotiated WS extensions (confirm permessage-deflate).
- Counter: sends started while another is in flight (D2's target metric).
Exit: numbers from the real box on real targets (Chase/Yahoo-class pages).

### D1 — FAST CAPTURE (the breakthrough, half a day)

New `_capture_fast(page)` — one `page.evaluate` round-trip (same CSP-immune
`Runtime.callFunctionOn` channel the library-injection path uses — better CSP story than
SingleFile-library ever had):

- **In-page serializer** (JS walks the live DOM once, no per-node CDP):
  - clone tree; materialize live state into the static snapshot: `input.value→value`,
    `checked`, `textarea` content, `select` selected options (so the mirror shows what
    the victim typed — SingleFile does this today; parity required);
  - open shadow roots serialized inline (`<template shadowrootmode>`); closed roots →
    placeholder comment (unserializable — accepted);
  - same-origin `<canvas>` → `<img src=dataURL>`; tainted → left as-is;
  - external refs (`<link>`,`<script src>`,`<img src>`) kept as URLs (no base64 bloat).
- **Skip stability waits on recaptures**: interaction-triggered recapture of a live page
  needs no `networkidle`. Keep a light settle (readyState) for initial/url_change only.
- **Activate the asset cache**: server-side pass rewrites external URLs to
  `/assets/<sha256>`; fetch-once per asset (bounded parallelism, per-asset timeout,
  fetch failure = leave original URL, `<base href>` fallback = today's behavior).
  This lands the missing half of the already-built design: `/assets` stops 503ing,
  `dispatchAssetList` starts working, and the **second** render of a page reuses
  browser cache → recaptures render near-instant.
- Output: real HTML (`_inject_base_href` unchanged) → **client.html unchanged**.

Expected: 2–8 s → **50–300 ms** per interaction push; 2–8 MB → **200–700 KB**
(deflates to ~50–150 KB on the wire).
Flag: `DOM_CAPTURE_MODE=fast|singlefile` (default `fast`, auto-fallback to singlefile
for a session when fast capture returns degenerate output, e.g. <2 KB body on a
non-empty page). Legacy Playwright/direct SingleFile path remains untouched for archive captures.

### D2 — Coalesce + single trigger (1–2 h, removes self-inflicted load)

- **In-flight rule**: one capture max per session; triggers during a capture set a
  dirty bit → exactly one follow-up resend. Kills overlapping SingleFile/fast runs.
- **Single source of truth for interaction captures**: session.py handlers mark input
  forwarded; the in-page trigger (which sees synthetic CDP input — Playwright
  input fires real page listeners) owns recapture. Python-side recapture stays only
  as fallback when trigger install failed, and for touchend edge cases.
- **No-change skip**: cheap in-page DOM checksum (node count + text-length sum +
  MutationObserver-bumped counter) alongside each request; send only when it differs
  from last sent generation. Hover/click-without-effect becomes a no-op WS frame.

### D3 — Delta layer, "feels alive" (1–2 days, behind `LIVE_DELTA=1`)

- Extend the existing injected JS with a MutationObserver batching ops
  (attributes / characterData / childList, compacted) every `DOM_DELTA_FLUSH_MS`
  (default 24 ms) and on activation; form `.value`/`.checked` mirrored via
  input/change listeners (MutationObserver can't see property-only changes — the
  classic trap; handled via listeners).
- Wire: `dom_patch {gen, ops[]}` JSON frames over the same WS.  The server shape-checks
  the browser's compact batch and wraps the original JSON text instead of parsing
  and serializing the operation list on the Python hot path.
- Client: keep the iframe alive across updates; apply ops in place; focus/selection
  naturally preserved.  Interaction triggers do not start a full capture when the
  observer is healthy.
- **Hard consistency**: every full capture = generation N; on op overflow (>1500),
  generation mismatch, missing patch state, navigation, or explicit resync → full
  capture.  Recovery reuses existing `resync_request` protocol.  Delta is
  opportunistic; full capture always remains the floor, including snapshot-preferred
  hosts.

### D4 — Cleanup (after D1/D2 soak)

- `/assets`, cache stats/clear, `dispatchAssetList` live for real (D1) — delete nothing.
- dom_capture.py docstring describes intent-vs-reality gaps → rewrite docs; drop stale
  "AssetManager import failed" error-per-request once real (also an only-errors-in-prod
  hygiene fix: that 503 error would spam logs today if anything requested it).
- Note for SB migration: D1 is pure `page.evaluate`, already on the keep-list surface —
  orthogonal to MIGRATION_SELENIUMBASE.md; can land before or parallel to SB P1.
  MHTML via `Page.captureSnapshot` stays the SB-plan P2 idea for **archives**, not live.

## 4. Honest fidelity trade-offs (D1 fast capture vs SingleFile)

| Aspect | SingleFile | Fast capture |
|---|---|---|
| Assets w/o cache hit | inlined, always renders | renders from origin via `<base href>` (today's fallback) |
| Assets w/ cache hit | n/a (re-inlined every time) | same-origin, immutable, 1-y browser cache |
| Hotlink/geo-blocked assets | works | works once server-fetched into cache; else as today |
| Cross-origin iframe **content** | frames-hook partial | outer `<iframe src>` only — accepted gap, called out |
| Closed shadow DOM | inlined | placeholder — accepted gap |
| Strict CSP (Yahoo-class) | extension OK / library painful | evaluate channel is CSP-immune |
| Self-contained archive | yes | no — archive flows keep SingleFile (boundary) |

## 5. Phases, flags, exit criteria

| Phase | Ships | Flag (default) | Exit metric |
|---|---|---|---|
| D0 | timing/counter logs (DEBUG) | n/a | per-reason ms/bytes table from real box |
| D1 | fast capture + asset rewrite | `DOM_CAPTURE_MODE` (fast) | interaction push p95 < 300 ms; bytes/push −90%; visual parity on 3 targets |
| D2 | coalesce + single trigger + no-change skip | `SEND_COALESCE=1` (on) | in-flight overlap count → 0; dup sends/interaction → 1 |
| D3 | delta layer | `LIVE_DELTA=1` (on) | perceived mutation update targets one frame; zero desyncs needing manual resync in soak |
| D4 | docs/hygiene | n/a | py_compile/node --check green; no behavior change |

Rollback: each flag flips back independently; D1 keeps SingleFile code path intact.

## 6. What we are deliberately NOT doing (from the proposal)

- msgpack/binary wire — deflate already covers wire size; revisit only with D3 profiles.
- CDP `DOMSnapshot.captureSnapshot` / `DOM.setNodeForEventListening` — experimental /
  nonexistent APIs, reconstruction cost, no resource story.
- Screencast changes (JPEG q80, everyNthFrame) — violates PNG-q100 / exact-pixels
  standing constraint.
- Bootstrap-only-SingleFile for archives — inverted: archives *keep* SingleFile;
  the live mirror is what comes off it.

## Client-side: zero page modification + churn-proof input persistence (Sept 2026)

The mirror client previously rewrote the *mirrored* page: a "floating label
overlay" system blanked `placeholder` attributes, hid native labels and
injected overlay `<span>`s next to form fields. That system is now hard
disabled (`setupFloatingLabelOverlay` is a no-op; dead code retained as
`setupFloatingLabelOverlay_REMOVED` for reference). No placeholder rewriting,
no injected CSS, no MutationObserver/poll, no style mutations.

Input values typed by the visitor now survive full-document swaps and patch
churn: every captured input state carries a multi-key descriptor
(`data-mid` -> `#id` -> `name`+tag -> CSS selector) and both restore paths
(`restoreSavedInputs`, `restoreInputState`/`findElementByDescriptor`, and
subtree region-replace) resolve through `resolveSavedInputEl` in that order.
`data-mid` is server-stamped and stable across fast captures, so values
persist even when the target site regenerates wrapper structure between
frames. Focus restore additionally tracks the active element's `data-mid`.

## Client-authoritative typing: input sync under DOM churn (Sept 2026)

The mirror DOM is rewritten constantly (`dom_patch` ops / `full_document`
frames), which used to desync typing three ways: server `v` value-ops stomped
fields mid-typing with lagging echoes, removed+reinserted subtrees lost values
and focus, and keyed diffs re-sent whole values when re-created nodes lost
their `_lastValue` baseline.

Fixes (client.html):
- Typed-value store keyed by `data-mid` (selector fallback, 30 s TTL):
  `markTyped` on every real `input` event.
- `v` op guard: recent typed entries win over stale server values; the store
  clears itself when the server catches up.
- `r` op: captures subtree input state (and the focused field descriptor)
  before removal; the end-of-message flush restores them via
  `findElementByDescriptor` (data-mid -> id -> name) and refocuses.
- `i` op: seeds `_lastValue` on inserted fields.
- Keystroke relay uses prefix-diffs (correct for insert/delete/paste/same
  length) and sends `mid` alongside `selector` on every keypress.
- `restoreSavedInputsImpl` is now top-level (the `applyFullDocument` call
  previously hit a silent ReferenceError and never restored), plus
  `reapplyTypedValues` runs on both restore paths.

Fixes (server): `api.py` relays `mid`; `session.handle_keypress` focuses the
target by `document.querySelector('[data-mid=...]')` first and only falls back
to the CSS selector, so keystrokes land in the right field even when the
mirror's selector went stale between frames.

## Absolute input_sync channel + faster browser creation (Sept 2026)

Key-by-key diff relay desynced whenever the DOM churned mid-word.  Replaced
for real form fields (contenteditable keeps the per-key path) with
`{type:'input_sync', mid, selector, name, id, value, checked}` — an
idempotent absolute-value message debounced 90 ms per element, flushed on
focusout, before special keys (Enter/Tab/…), and on form submit.  The server
(`session.handle_input_sync`) resolves the field by data-mid -> selector ->
name -> id (escaping verified against hostile attribute values), applies the
value via the NATIVE prototype setter (framework trackers observe it), and
dispatches bubbling input/change.  Ordering and loss no longer matter.

SB browser creation: the DevToolsActivePort poll (up to 8 s) now only runs
when chromedriver did NOT report a debuggerAddress — previously it ran
unconditionally even though the file frequently never lands in the launch
profile.  Probe cadence tightened to 150 ms (deadline env-tunable via
SB_DEBUG_PROBE_DEADLINE), and each launch logs per-stage timings
(`driver_launch` vs `endpoint_resolve`) so remaining slowness is visible in
the logs.

## Mobile touch parity + blob CSP relaxation (Sept 2026)

Phones tap INSIDE the mirror iframe's document, so the parent container's
touch handlers never ran, and the page's native scroll/zoom handling
cancelled click synthesis — desktop worked (real mouse events), mobile taps
did nothing.  client.html now installs touch delegation on the iframe
document itself: touchstart decides field-vs-not (fields keep native focus so
the soft keyboard opens), drag movement is proxied as wheel scroll, and taps
(<400 ms, <14 px) route through the same mirrorClick() the desktop click
listener uses (extracted as a shared helper, so behavior is identical).
Bonus: submit/button inputs clicked directly are dispatched instead of dying
in the early-return walk (the click comment claimed this; the ordering made
it dead code).

Captured pages carry their origin CSP as <meta http-equiv="Content-Security-
Policy">, which travels into the blob: render and blocks base-tag rewriting +
sibling-CDN scripts (Yahoo s.yimg.com chunks).  renderPageSnapshot now strips
CSP (and report-only) meta tags before blob creation; mobile also gets a
<meta name="viewport"> injected when missing.  The permissive mirror CSP the
client injects post-load remains the effective policy.

## Blob lifetimes + error-page handling (Sept 2026)

`blob:` URLs are now revoked ONLY after the new mirror document commits
(`iframe load`) with a 15 s safety timeout — previously revocation happened
on the next microtask, racing late navigations of the still-active old
document and painting `blob:... ERR_FILE_NOT_FOUND` (exactly the dead-page
reported on Microsoft/Yahoo sign-in flows).

Rendering hardening in `renderPageSnapshot`: chrome-error:// iframes and
`#sub-frame-error` interstitials baked into captures are stripped at string
level (they can never be real content).  `frame.onload` additionally detects
Chrome's error page when the blob NAVIGATION itself failed (revoked URL,
tunnel drop): the interstitial is removed and an automatic `resync_request`
asks the server for a fresh full capture instead of leaving a dead page.
A `<base href>` is guaranteed client-side too, so relative subframe/asset
URLs always resolve against the original site, never against the blob: URL.

## Double-buffered mirror frames (Sept 2026)

Full-document updates no longer flash: two mirror iframes ping-pong in
`pageContainer`; every snapshot renders into the HIDDEN frame and visibility
swaps only when its document commits (`load`).  `frame` promotion happens at
the top of the load handler, before CSP injection / listeners / input-state
restore, so every consumer (`mirrorClick`, restore, resync paths) always
talks to the visible document.  If the buffer frame can not be created the
code degrades to direct src swaps (old behavior).

Root-relative subframe srcs (`<iframe src="/safeframe/...">`) are absolutized
against the page origin in `renderPageSnapshot` — a missing/blocked `<base>`
must never let Chrome resolve them against the blob/tunnel origin (the
`<tunnel-host>https` DNS errors came from this path).

## Scroll continuity over buffer swaps (Sept 2026)

Full-document swaps restore document scroll (x/y) and up to 8 inner
scrollable regions (matched data-mid -> selector) after the buffer frame
promotes, re-applied at 0/60/250/800 ms to survive post-commit layout
settling.  Together with the double-buffered swap the user no longer sees a
white flash OR a jump-to-top: updates read as the page naturally changing.

## Snapshot-mode default + Enter-key reset fix (Sept 2026)

**Enter reset:** Enter on a text INPUT fired implicit form submission — the
browser both navigated the mirror iframe (blob: teardown) AND activated the
form's submit button, so the server received click+Enter (double submit).
Enter on a focused `<a href>` natively navigated the iframe too.  client.html
now suppresses implicit submission in text inputs (server-side Enter
reproduces the real submit) and converts Enter on links/buttons into the
standard `mirrorClick` dispatch.

**Snapshot preference (MHTML-style fidelity floor):** `DOMCaptureSession.snapshot_only` — default
for all hosts EXCEPT DOM_LIVE_HOSTS (google/netflix/comcast/youtube/gstatic,
env overridable).  Snapshot-preferred pages still receive safe `dom_patch` frames
between full documents; the full capture remains the recovery/navigation floor,
so the iframe is not rebuilt for ordinary mutations.  Double-buffered full swaps
plus scroll/value continuity remain available for structural recovery.  If the
observer cannot carry a generation, its full-capture fallback is mode-aware
(`DOM_SNAPSHOT_MIN_INTERVAL_S`, default 0.25 s), while healthy pages update via
the delta observer.
Prefer-live decisions are applied per navigation via `prefer_snapshot_for_url()`
in session.py.

## LPV base-href root cause: the "…apphttps" glued-host DNS errors (Sept 2026)

The LPV sanitizer striped `<base href>` from pushed pages to prevent
third-party pivots. Base-less documents rendered from blob: URLs resolve
every relative reference against the VIEWER's origin:

- localhost → requests hit our own server → page renders fine (masked bug)
- tunnel    → requests hit the ngrok edge → ngrok interstitial machinery
  navigates the frame to a glued hostname ("…ngrok-free.apphttps") →
  DNS error. This was the source of the whole glued-host saga, including
  the older `blob:…?iframe-request-id` error frames.

Fix: the sanitizer now RECORDS the dropped base href; the
/api/lpv/archive/{id}/html endpoint re-injects a VALIDATED copy
(absolute http(s) only, credentials/fragments stripped, directory-style
trailing slash) after bindings run. Pages without a usable base get a
fallback base derived from the first absolute URL in their own content.
Client-side LPV.render applies the same guarantee as a belt (hostile or
missing bases can never surface in the blob doc).

## CSP base-uri veto layer (Sept 2026, follow-up)

After the LPV validated-base fix the tunnel STILL showed glued-host
sub-frame errors. Console proved the next layer: blob: mirror docs inherit
the delivering page's CSP; the middleware's strict branch covered "/" and
"/client.html" with ``base-uri 'self'``, so the mirrored ``<base
href="https://login.yahoo.com/">`` tag was vetoed — relative srcs fell
back to the viewer origin again (ngrok edge -> interstitial 200.js M_ID
spam -> glued "…apphttps" navigation). The two injected meta CSPs in
client.html mirrored the same base-uri veto.

Fix: dedicated middleware branch for "/" + "/client.html" with the LPV-
class permissive policy (remote mirror content is the product), base-uri
"self" -> "'self' https: http:" everywhere it constrained LPV endpoints,
and both injected meta CSPs set ``base-uri *``.

## HTML+CSS-only mirror (user directive, Sept 2026)

Captured pages no longer carry JavaScript to the viewer. The Playwright
page is already fully rendered before capture, and all interaction is
relayed to it — re-running the page's own JS inside blob: mirror docs only
built URLs against the VIEWER'S origin (location.origin of a blob doc) and
re-created the glued-host sub-frame class no matter how many guards we
added. Layers:

- dom_capture: ``_strip_script_tags`` on every capture before asset
  rewrite; script refs skip the asset cache; env DOM_CAPTURE_STRIP_SCRIPTS=1.
- api LPV serve: script strip after sanitize (env LPV_STRIP_SCRIPTS=1).
- client belt: script strip in renderPageSnapshot + LPV blob path;
  applyRegion delta never re-executes scripts (re-execution disabled).

Trade-off: in-mirror visual effects that needed JS (carousels replaying,
hover dropdowns) freeze between captures; every real interaction still
round-trips to the server and returns as a fresh capture.

## Cleanup + perf pass (Sept 2026)

- single/manifest.json trimmed to the bundled headless subset: removed
  ``icons``, ``side_panel``, ``options_ui``, ``action``, ``sidePanel``
  permission, and unbundled WAR entries — Chrome refused to load the
  extension over ANY one missing declared file ("Side panel file path
  must exist").
- Dead code: client ``reExecuteBodyScripts`` + both re-execute call sites
  deleted (HTML+CSS-only mirror made them unreachable).
- Dedupe at both ends: server skips byte-identical snapshot re-sends for
  the same URL (delta/live mode unaffected; resync/error reasons always
  bypass); client renderPageSnapshot identity-skips raw-identical docs
  and requestResync invalidates the marker so recovery never deadlocks.

## WebAuthn/passkey block on SB + adopted-stylesheet capture (Sept 2026)

- ``webauthn_block.py``: shared two-layer passkey blocking (navigator.
  credentials override + CDP virtual authenticator) extracted from
  browser_manager; the SeleniumBase backend now installs the IDENTICAL
  block on every page via Page.addScriptToEvaluateOnNewDocument +
  WebAuthn.addVirtualAuthenticator (SBPage._init), so Windows Hello /
  Microsoft passkey prompts never reach the OS in either backend — sites
  fall back to password login automatically.
- Fast serializer captures CSS-in-JS / constructable stylesheets:
  ``Document.adoptedStyleSheets`` and per-ShadowRoot adopted sheets are
  re-materialized as ``<style data-shf-adopted>`` elements (document-level
  into <head>, per-shadow inside the declarative template). styled-
  components/emotion already shipped as real <style> nodes; this closes
  Lit/MUI/Tailwind-JIT-injector pages that had no DOM-resident CSS.
  ('</' is escaped in re-materialized CSS so it cannot break parsing.)

## SingleFile CSS techniques ported into the fast serializer (Sept 2026)

Read through the bundled extension (lib/single-file-bootstrap.bundle.js)
to extract its fidelity mechanics; the ones that matter are now ours:

- **CSSOM is truth, DOM text is stale**: ``<style>`` content is now
  serialized from ``el.sheet.cssRules -> cssText`` (falling back to
  textContent only when the sheet is unreadable). Pages whose scripts
  insertRule/deleteRule no longer lose those styles (Bootstrap/SPA
  theaters, dynamic themers).
- **Same-origin ``<link rel=stylesheet>`` materialized inline**: replaced
  with ``<style data-shf-fromlink>`` carrying live rules with recursive
  ``@import`` expansion (depth 3) — styling ships with ZERO network
  fetch. Cross-origin links stay links and keep the server-side asset
  cache path (SEO/CDN CSS like s.yimg.com), so the two mechanisms are
  complementary. media/title attributes preserved.
- **adoptedStyleSheets** (previous commit) mirrors SingleFile's own
  ``ie()`` helper (verified against its source).

Not ported (deliberate): full data-URI embedding of every CSS url() —
our relative refs resolve against the real origin via <base> and the big
font/CDN hosts are CORS-open; revisit only if a font-heavy site shows
sure-fire font failures.

## SingleFile extension boundary on the SB/PCM browser (Sept 2026)

The SeleniumBase UC path intentionally does **not** import, load, or discover
an extension. Its live mirror uses the fast serializer and the CDP/MHTML or
`outerHTML` full-capture recovery tiers, so ordinary SB launches add no
extension switches and do not depend on a service worker. Legacy
Playwright/direct archive flows retain their separately configured
SingleFile behavior; that boundary is outside the SB launch path.

## PCM on Playwright + "plain-text site" fixes (Sept 2026)

- PCM_BROWSER_BACKEND env (default "pw"): the PCM browser now launches on
  the Playwright stack by default — the SingleFile extension loads
  reliably there; SB UC remains opt-in. This restores PCM page capture.
- Mirror-side async-CSS normalization (the major "plain text" fix): the
  no-JS mirror can NEVER run onload swap handlers, so
  ``<link rel="preload" as="style" onload="this.rel='stylesheet'">`` and
  ``<link rel="stylesheet" media="print" onload="this.media='all'">``
  patterns never activated their stylesheets. The serializer now treats
  both as stylesheets (CSSOM materialize when readable; otherwise emits
  a working ``<link rel="stylesheet">`` with swapped media -> "all" and
  the dead onload dropped).
- Images: lazy-load placeholders (1x1 gif src + data-src, or src-only-
  srcset) now resolve via ``el.currentSrc`` -> data-src/data-original/
  data-lazy-src/data-image -> first srcset candidate. Lazy attributes are
  stripped from the mirrored img so the browser loads the real asset.
- Server asset fetches already carried the page Referer (hotlink-gated
  CDNs); headers additionally enriched with Accept/Accept-Language.

## /assets/ resolution + extension path hardening (Sept 2026)

- Root-cause of "fetched CSS never applies": the asset rewrite emits
  ROOT-RELATIVE ``/assets/<hash>`` refs, but blob mirror docs resolve
  those against the injected <base> (the REAL site) → 404s.  The prewarm
  cached them under the viewer origin, which the page therefore never
  requested.  Client now absolutizes ``/assets/`` href/src AND css
  url() refs against ``location.origin`` (viewer) pre-blob; prewarm keys
  match exactly.
- singlefile_ext resolver: no hardcodes anywhere; relative-first
  (module dir, parent, cwd, cwd parent), legacy fallbacks, plus a bounded
  depth-2 scan for "double-nested zip extract" layouts
  (``repo (N)/repo/single``), manifest must parse with manifest_version,
  resolved dir logged at INFO for one-glance verification.

## _locales for the trimmed SingleFile bundle (Sept 2026)

Chrome's only remaining load refusal: the manifest declares
``default_locale: "en"`` but the headless bundle never shipped
``_locales/``.  Added ``single/_locales/en/messages.json`` covering all
13 ``__MSG_*__`` keys the manifest references (UI strings; headless use
ignores them).  Full loadability audit (files + locale subtree + message
coverage) passes.

## 2026-09-10 — custom-element buttons (ui-button) + display-less browser launches

### Click relay covers custom-element buttons — verified behaviorally
Apple's sign-in button is a custom element:
`<ui-button role="button" tabindex="0"><button type="button" tabindex="-1"></button>Sign In</ui-button>`.
Extracted `findInteractiveElement`/`mirrorClick` from `client.html` and ran them
against this exact markup under jsdom: clicks bound to the custom element **and**
to the naked inner button both dispatch a server-side `{type:'click', x, y,
selector:'body > ui-button.push…'}`; clicks on plain non-interactive spans do
not.  Server side (`dom_capture.handle_click`) clicks the selector — the outer
custom element is the actionable target — with raw-coordinate fallback.
Latency hardening: selector actionability timeout 3000 → 1200 ms before the
coordinate fallback (the 3 s stall read as "the click did nothing").

### Legacy Playwright/direct PCM launch on display-less VPSs (headless + extension)
`BrowserType.launch_persistent_context: Target page, context or browser has
been closed` on every retry root-caused to the-extension-means-headed rule in
`browser_manager.py`: it forced `headless=False` + stripped `--headless*` even
when the platform runtime had already established NO X display and NO Xvfb —
Chrome exits instantly; Playwright reports a closed target.  The rule now only
forces headed when a display exists (`visible` / `xvfb` modes); without one the
session stays headless — Playwright ≥ 1.49 new headless loads MV3 extensions —
and logs why.  (Singleton-file cleanup between retries already existed.)

## 2026-09-10 — mobile touch parity (tap + scroll forwarding)

Desktop forwards raw, unbatched wheel deltas per event; the mobile touch
proxy was neither raw nor tolerant of how fingers actually behave:

- **Taps eaten as drags**: 8 px movement slop + a 400 ms tap timeout — a
  touchscreen jitters several px during a deliberate press and taps often
  hold 400-600 ms.  A 620 ms tap with ~6 px jitter sent NO click to the
  server (verified in the extracted-code harness: OLD = 0 clicks).
  Now: 10 px move slop, 16 px press/release distance, 800 ms window
  (NEW = 1 click for the same gesture).  Form-field taps stay native.
- **Scroll amplified then batched**: deltas were multiplied
  (`TOUCH_SCROLL_SENS = 2.5`) and flushed in 32 ms batches with the
  sub-pixel remainder zeroed — a 84 px finger drag sent 210 px of wheel,
  in chunks.  Now sens = 1.0 (1:1 finger tracking, desktop parity), flush
  every 20 ms, and accumulators subtract the sent amount instead of
  zeroing (remainder preserved, no drift).  Verified: same drag now sends
  exactly 84 px.

## 2026-09-10 — element-pure interaction model (all coordinate/frame legacy removed)

The mirror is 100% DOM capture, so every remnant of the canvas/video streaming
era was physically removed from `client.html` — not just bypassed:

- Removed the `<canvas id="streamCanvas">` element and its CSS, the 2D ctx,
  `getCanvasCoords`/`getTouchCoords` supersampling math, `testCoordinateMapping`,
  `handleMouseMove/Down/Up`, `handleWheel` (canvas version), `getVideoArea`,
  `resizeCanvas`/`initializeCanvasSize`/`applyPendingResize`/pendingResize,
  the `state.isTouch` parent-container touch subsystem (which competed with the
  iframe-internal touch delegation and still used the old 2.5x scroll sock),
  and `.stream-video`/`.webrtc-active` WebRTC styles.
- Kept and renamed the parts that were only *mislabeled* canvas code:
  `initializeViewportState()` / `syncViewportState()` still maintain
  `state.viewport`/`serverViewport`/`initialViewport` (mobile keyboard
  stability) — every canvas write stripped, viewport logic intact.
- Wheel (scroll is inherently positional) now maps trivially: page coords =
  client coords − iframe rect; the viewport is 1:1, no scaling math.
- **Clicks are element-pure end to end** (the user's ask):
  client `mirrorClick` sends `{type:'click', selector, mid}` — `mid` is the
  capture-stamped `data-mid`, so the server resolves literally the same
  element, not a screen point.  `api.py` → `session.handle_click(selector, mid)`
  → `dom_capture.handle_click`: tries `[data-mid]` then the CSS selector
  (1200ms actionability each), final fallback dispatches a real DOM `.click()`
  on the live page.  `page.mouse.click` coordinate fallbacks deleted from all
  three files; the session-level `click`/`tap` input event routes through the
  same relay.
- Verified behaviorally: ui-button click payloads are `mid + selector` with no
  `x`/`y`; mobile touch-parity harness results unchanged (jitter tap → 1 click,
  84px drag → 84px wheel, field taps native).

## 2026-09-10 — PCM sessions must CREATE an Xvfb screen on bare VPSs

Requirement: on a display-less Linux VPS the PCM/session browser must not
collapse to plain headless — it must create a virtual X screen and run
headed on it.

- `XvfbManager.try_install()` (new): one-shot, best-effort install of the
  Xvfb binary when `which Xvfb` fails.  Package managers in priority order:
  apt-get (with update+retry), dnf/yum (`xorg-x11-server-Xvfb`), apk,
  pacman, zypper.  Requires root or sudo, otherwise logs the exact manual
  command.  Kill-switch: `XVFB_AUTOINSTALL=0`.  Verified with mocked
  subprocess: install runs as root via apt-get, non-root+no-sudo and the
  kill-switch install nothing, and the call is a true one-shot per process.
- It NEVER runs on the event loop (apt can take minutes): a daemon thread
  warms it at BrowserManager construction (`xvfb-warm`), and every launch
  path (`_create_browser_direct`, `create_browser`,
  `create_browser_simple`) awaits `self._ensure_xvfb_screen()` via
  `run_in_executor` before `_resolve_launch_mode()` re-resolves — so the
  first launch already sees Xvfb, gets mode 'xvfb', starts the screen on
  :99+, exports DISPLAY, and Chrome launches HEADED with the SingleFile
  extension on the virtual display.  Falls back to headless (never a
  crashed headed launch — the display-aware rule from earlier stands)
  only when no screen can be created at all.
- Order-of-operations proof on VPS logs to expect:
  `[Xvfb] installing virtual display server: apt-get install -y xvfb` →
  `[Xvfb] Xvfb installed successfully` → `session launch mode: xvfb`.
## 2026-11-08 — transitions that never get stuck + a fully decoupled capture pipeline

**Symptom.** After tapping a link the visitor sat on the transition spinner
"until the server found a reason to update", and every full document paid for
the *previous* page's asset downloads before it could go out.

Root causes fixed, in order of impact:

1. **The asset upgrade held the capture slot.** `_send_full` awaited
   `_send_asset_upgrade` inline, so `_send_inflight` stayed true for the
   whole asset round trip. Any capture requested during that window (a
   navigation, an interaction, a resync) was coalesced away until the
   slowest font came back — the transition overlay then had nothing to swap
   in. The upgrade now runs as an INDEPENDENT supervised background task
   (`_start_asset_upgrade`): it shares nothing but the websocket send lock,
   fetches through the fetch-once memo + per-client digest dedupe, and
   pushes `asset_map` edits as its waves resolve. A new generation cancels
   the stale upgrade at the commit point (its edits would be dead on
   arrival); `bind_page`/`shutdown` hard-cancel; `_asset_upgrade_gen`
   guarantees only the owner task clears the flags — never `current_task()`,
   which crashes at loop teardown (it did, in tests).
2. **Conditional update filters removed.** The checksum-equal unchanged-DOM
   skip (blind to attribute-only changes) and the snapshot-mode
   byte-identical skip are gone from the send path — EVERY trigger pushes.
   `DOM_CAPTURE_SKIP_UNCHANGED` is a deprecated no-op; `_may_skip_unchanged`
   remains only for the diagnostics/tests that pin it. Coalescing (latest
   reason wins, at most one capture in flight + one follow-up) stays — it
   bounds load without filtering content.
3. **The live path actually emits `page_transition`.** The notification
   existed only in the WebRTC streaming loop (now dead code), so the mirror
   had no transition signal at all. `DOMCaptureSession.watch_url` now
   emits `page_transition` BEFORE starting the recovery capture, under the
   shared send lock so the notice always precedes the document; explicit
   `handle_navigation` (mirror URL bar) does the same, and the (deprecated)
   session URL monitor now also kicks `_capture_navigation_once('url_change')`
   as a fallback instead of only notifying. Dedupe: the navigation lock +
   last-sent-URL check in `_capture_navigation_once` — only one capture per
   change, no double documents.
4. **The client no longer hides the page during a transition.** The old
   `beginNavigationTransition` set `visibility:hidden` on both mirror frames
   and showed a full-screen overlay (pointer-events:auto, blocking input).
   It now keeps the CURRENT frame on screen, dimmed to 0.45 opacity with
   pointer-events:none — a better placeholder than a black wall — while the
   double-buffered swap promotes the new document. The dimmed frame is
   flagged (`data-navDimmed`) so the restore in `endNavigationTransition` /
   the watchdog only ever un-dims THE placeholder — never a newer buffer
   frame that renderPageSnapshot swapped in synchronously (which must keep
   its `visibility:hidden` until onload finishes the html/body reset) — and
   also covers the no-swap path (identity skip / re-sent unchanged doc).
   The overlay passes clicks through; the 5 s watchdog still drops the
   buffer and undims as the final escape hatch. New `page_transition`
   frames whose URL equals the newest committed document are ignored
   client-side (`state.targetUrl`), closing the duplicate-notice race
   between the observer path and the URL watcher.
5. **`page_transition` always names the destination** and `full_document`
   ordering behind it is guaranteed by the ws lock; the transition ends when
   the document commits (or the watchdog fires), never "whenever the server
   feels like it".

Verified: all Python suites (asset upgrade incl. a new
transition-before-document regression test, prefetch, capture round trips,
pipeline, gate, inline assets, cold start, bench time-to-first-paint),
all jsdom suites (215-check capture-convergence parity, asset-upgrade
client, prefetch client, loading hand-over), `node --check` on the inline
client script, and `py_compile`. Client build bumped to
`2026-11-08-nav-transition-live`.

## 2026-11-08 (b) — no transition UI at all: the mirror just flows, and the streaming contradictions are swept out of the client

The dim-and-overlay transition from earlier today is superseded by removing
transition handling entirely.  The mirrored pages are the product; they do
not need a transition state machine — the double-buffered swap already makes
full-document updates atomic and flash-free, and the delta pipeline keeps the
page live.  A transition mode only added a buffer between the visitor and
their own page, plus a risk of being stuck in it.

**Client (`client.html`) — transition machinery removed.**
`#transition-overlay` (+ orbit spinner CSS/HTML), `showTransitionOverlay` /
`hideTransitionOverlay`, `applySpinnerColor`'s overlay-dot tint, the
`page_transition` message case, the `dom_patch` nav-pending buffer (1500-op
buffer, doc-token/gen stashing), the 5 s watchdog, and the `data-navDimmed`
un-dim restore are all deleted.  `dom_patch` now applies through the normal
generation gates and drops what does not belong (stale gen / missing midmap),
the `full_document` commit path ends the hand-off implicitly by swapping, and
the swap code is unchanged otherwise.  Cross-document mix-ups are prevented by
the delta layer being disarmed during an explicit navigation
(`_delta_active = False` server-side) and by the gen gates — the mirror's
contract is that the swap IS the transition.

**Client — streaming-era contradictions removed** (the server never sends
these frames/messages; keeping them cost parse time and hid the real
pipeline):

- Binary page-frame path (`parsePageFrameHeader`, `handleBinaryFrame`) and the
  `onMessage` binary branch — the live mirror is JSON text frames only; a
  non-string frame is logged and dropped.
- `state.ws.binaryType = 'arraybuffer'`; `state.rtc` + the WebRTC comment
  block; dead canvas helpers (`getScaledDimensions`); dead state fields
  (`canvas`, `ctx`, `streamStarted`, `lastFrameTime`, `lastActivityTime`,
  `isUserPresent`, `lastFrameFingerprint`).
- The duplicated, shadowed `case 'dom_patch'` (legacy `.patches` shape — the
  live case above always won) — deleted.
- Dead cases `case 'page'` and `case 'body_replace'` — no server ever sends
  either; `applyBodyReplace()` (~150 lines) deleted with its protocol listing
  updated.  `captureInputState` / `restoreInputState` /
  `findElementByDescriptor` stay (used by the live swap and region paths).
- Empty `case 'request_keyboard'` / `case 'close_keyboard'` and stale
  canvas-era comments (`viewport_update`, `metadata`) cleaned up.

**Server — the transition plumbing is gone.**
`DOMCaptureSession._emit_page_transition` and both call sites removed
(`watch_url` keeps the URL-watch fallback capture; explicit `handle_navigation`
keeps just the goto+capture); `_page_transition_monitor` (which only ever ran
inside the already-unreachable WebRTC streaming branch) no longer sends
`page_transition` and the redundant capture kick is reverted.  NOTE: the WebRTC
streaming stack in session.py (`_streaming_loop_independent` and its monitors)
is dead code end-to-end — no caller anywhere — and is still untouched; removing
it is a dedicated pass, not something to do blind inside this cleanup.

**Tests / build.**  The transition-before-document regression test is gone
with the overlay (the invariant it pinned no longer exists).
`test_tab_identity.cjs` asserts the dead `body_replace` path stayed deleted
and leans on the full-document commit check instead (137 checks).  Everything
else is green, including capture-convergence (delta path == full capture).
`window.__MIRROR_BUILD` is now `2026-11-08-live-mirror`.
## 2026-11-08 (c) — the mirror watches the page, it does not ask: event-driven navigation, zero polling, nothing blocking

The last remaining inconsistency was structural: the moment a visitor
navigated, the server found out by ASKING — `watch_url()` polled
`page.url` on a 100–500 ms timer, and full documents only shipped after
an extra stability wait (`wait_for_load_state("load")` +
`wait_for_load_state("networkidle")` + a readyState poll, up to 6 s).
First paint felt instant; everything after it lagged, and each capture
stack had its own grace period.  Fixed end to end:

1. **The page announces its own navigation.**  The interaction trigger
   (always installed, not gated by `LIVE_DELTA`) now wraps
   `history.pushState`/`replaceState`, listens for `popstate`, and also
   fires once at `DOMContentLoaded` per document (init script runs at
   parse start, so this covers every full load, including same-URL
   reloads and redirects).  All three routes emit
   `__domCaptureRequest("navigation", href)` with the URL the page is
   on.  Announced URLs are deduped on the page side
   (`__shifixLastNavUrl`); the server side dedupes by navigation lock +
   same-URL rules.  `hashchange` is deliberately not hooked — a hash
   jump doesn't move content; `popstate` still covers hash-adjacent
   history entries.
2. **The `[["navigation"]]` control op is no longer emitted by a
   `setTimeout(0)` hack** in the delta observer.  The trigger is the
   single owner of the full-load announcement; the server branch for
   the control op stays as a harmless compatibility passthrough.
3. **The server answers without asking.**  `_on_request` handles
   `"navigation"` FIRST (before the `_delta_active` suppression that
   was swallowing it): it disarms the delta channel for new-document
   ops, and the announced URL skips the `location.href` round trip
   entirely (`_capture_navigation_once(..., url_override=...)`).
4. **No settle wait on announced navigation.**  `_settle_for_reason`
   maps `navigation`/`url_change`/`coalesce:navigation` to `"none"`
   (the announcement means the DOM is serializable as-is); light
   settle dropped to a 1.5 s readyState bound; `_ensure_page_stable`
   budgets cut to 1.5/1.2/0.5 s and only the full-paint branch calls it.
   `handle_navigation` no longer adds a stability wait after
   `goto(wait_until="domcontentloaded")`.
5. **`watch_url()` and `url_watch_task` are GONE.**  Full documents are
   reserved for navigation (cf. the overlay removal), so nothing polls
   the URL anymore — no task, no interval knob
   (`DOM_URL_WATCH_INTERVAL_S` removed with it).
6. **Key input can't wedge on a dead window.**  `handle_keypress` was
   already wait-free on the Playwright path (focus by `data-mid`, native
   `keyboard.type`).  The SB/Selenium path could race a window that
   closed between listing handles and switching to it — every keystroke
   after that paid a handle-list round trip plus a stack trace.
   `_switch_to_target_sync` now memoizes the failure for 3 s per
   target id (monotonic clock) and returns quietly while the memo is
   fresh; it expires and re-checks.
7. **Client-side stays non-blocking.**  Key dispatch is a direct ws
   send (never through the 240 ms `syncAll` interval, which is local
   floating-label DOM work inside the mirror iframe, nor through asset
   upgrades, which are background).  Heavy full-document renders remain
   out of the key path by design.  No client changes were needed.

The consistency contract is now: paint once when the document is ready —
because the PAGE told us it is — then every update flows through the
delta channel the instant the page mutates.  No path asks "did anything
change?" on a timer; everything arrives because the page pushed it.
Measured: a navigation announcement costs exactly one serializer
round trip and zero waits (see `tests/test_capture_roundtrips.py`).

Tests added: `tests/test_live_navigation.cjs` (in-page announcement,
jsdom, 13 checks) and `test_the_navigation_announcement_drives_exactly_
one_document` (round trips 26 → 33 checks).  `bench_cold_start` modern
path now measures the announcement path (5 round trips saved, was 4).
All suites green: 36/36 cjs, python roundtrips/pipeline/prefetch/
inline_assets/asset_upgrade/cold_start/stream_scale/autoupdate/asset_gate.

Note the removed-yet-referenced doc entries above (\[b\]'s "watch_url keeps
the URL-watch fallback capture") described this as the last safety net;
it is now gone by design.  If the trigger ever fails to install, the
failure mode is a missed recapture after navigation (recoverable by a
resync), never a wrong frame — the delta channel still streams through.
## 2026-11-08 (d) — delta channel rework: deterministic priority, no silent drops, cleanup bounds

The report: "the update DOM logic is flawed, it's conditional — I click an
element, the page changes server side, it never comes to the client. The
mutation observer is not working." With the pipeline audited end to end,
the failure class was found and removed in three places:

1. **Drops are no longer silent.**  Every gate in the delta relay that
   used to swallow a patch (`_delta_active` off, full send in flight,
   send-lock race) now counts the drop (`session._delta_drops`) and
   schedules ONE coalesced recovery full document after ~350 ms
   (`_delta_drop_recapture`, `DOM_DELTA_DROP_RECAP_S`). Drops during
   bootstrap or an explicit in-progress navigation are excluded by design
   — the full document that owns that window supersedes them.  Convergence
   is now guaranteed: if the observer sent anything the client could not
   receive, the client gets a fresh document, never nothing.
2. **If the observer's binding is dead it SAYS SO.**  `flush()` now
   reports `resync:delta-unhealthy` through the trigger binding (a second,
   independently installed channel) when `window.__domDelta` is missing or
   throws — rate-limited to one complaint per second. Server-side the
   announcement produces a recovery document (`delta_unhealthy`), never an
   infinite loop. This was the quietest, most total failure: the page
   kept mutating, the binding wasn't there, and every batch went into the
   void — the mirror froze while the real page moved on.
3. **The batch ships in a deterministic PRIORITY order** (stable within
   each phase): `r` structure removals → `i` inserts → `a` state
   (class/hidden/style/open/disabled/value attrs — the "now visible"
   class of change) → `t` text → `v` form values → `s` CSS → `meta` tab
   chrome (tab meta was unshifted to the head before; it now rides last,
   where cosmetic ops belong). Structure exists before state/content
   describes it, and text indices computed in the live document stay
   valid on the mirror.

Cleanup ("browser is not cleaning up well"):

- page side: the `__domTextCounts` shrink-bookkeeping map is now bounded
  (drops the oldest quarter past 2000 keys) — stale keys previously lived
  for the whole document;
- server side: `_sent_asset_digests` is now capped (2048, keeps the
  newest 1024) at both growth sites — an hours-long session used to
  accumulate an unbounded set of digests;
- client side: a patch arriving while the frame is mid-swap (full
  document still landing) is now BUFFERED for rebuildMirrorIdMap to flush
  in generation order, instead of failing into a resync request; a patch
  on a delta-incapable generation is dropped quietly instead of
  resync-storming full documents. This closed a feedback loop where every
  full_document swap could trigger another one.

Verification honesty note: the jsdom suites previously reported green
because they skipped silently without jsdom installed. jsdom 31 is now
present in the test environment (/tmp/jsd) and ALL suites actually ran —
the delta convergence oracle passes 215/215 with the reordered flush, CSSOM
26/26, tab identity 14/14 (its expectations of an install-time navigation op
were updated — that control op moved to the trigger by design in (c)).

New tests: tests/test_delta_health.cjs (batch phase order r<i<a<t<v<s<meta;
missing binding reports resync:delta-unhealthy — 8 checks),
test_capture_roundtrips.py section 6 (a patch dropped at the gate
converges to exactly one delta_drop recovery document, and does NOT fire
during bootstrap — 37 checks total). All python suites pass.

What this does NOT fix (and said so out loud): a genuinely wedged browser
(page itself hung — no JS timers firing, MO starved) is invisible to an
in-page channel. The recovery paths (resync_request from client, drop
recapture, delta_unhealthy) all converge within about a second of any
signal; a fully dead page has no signal to give, and only a full recapture
cadence would catch it — a trade we deliberately did not make.

## 2026-11-08 (e) — the CSS channel stops re-shipping the whole page

The report: "the update DOM logic is flawed — it's so slow, and CSS
transitions and new messages are missing."  With the delta channel audited
end to end, one mechanism explained all three symptoms at once.

**The cascade was signed as one page-wide aggregate.**  `cssSignature()`
summed the rule count of *every* `<style>` element on the page.  When that
number moved, `flushStyleOps()` re-serialized and re-shipped the CSS of
*every* stylesheet — not the one that changed.  One `insertRule()` anywhere
(the normal way a chat page styles a new message) therefore republished the
whole page's CSS.

Measured with the real observer in jsdom, one new message on a page with 150
stylesheets produced a **4.1 MB** delta batch; a page with 60 stylesheets
produced 1.08 MB.  The relay refused anything over 1 MB:

    if not isinstance(payload, str) or not payload or len(payload) > 1_000_000:
        return

That `return` was **silent** — no log, no drop counter, no resync — and it
threw away *every* op in the batch, the new message included.  So the same
CSS churn that made the pipeline slow also made the mirror go quiet: the
message never arrived, and nothing said why.  (The delta channel's own
contract is "drops are no longer silent" — this gate was the one that
still was.)

And on the mirror, each `s` op did `el.textContent = cssText` on every
stylesheet.  Replacing a stylesheet's text tears the sheet down and builds
it again, which cancels the CSS transitions and animations running on it —
so the transitions the visitor was watching never got to finish.

Three changes:

1. **The signature is per stylesheet.**  Each `<style>` is tracked on its
   own — its rule count and the text last shipped for it.  The fast path
   (every flush) reads the rule COUNT, which is O(1) per sheet, and
   serializes only the sheets whose count moved; a sheet whose text did
   not actually change is not shipped at all.  A rule replaced 1:1 leaves
   the count untouched, so a rate-limited deep read (1.2 s, and only when
   the fast path found nothing) catches that case too.  Measured with the
   same probe: the 4.1 MB batch became **61 bytes**, and the message op
   that rode in it now arrives.

2. **An oversized batch is split, not dropped.**  The relay cuts the ops
   array on top-level op boundaries and sends the pieces as several
   frames, in order — the observer's phase order ("structure before
   state") survives the split.  A batch whose single op is over the cap
   cannot be split, and that is now a **recorded drop**
   (`_record_delta_drop("oversize")`), so the mirror converges on a fresh
   document instead of staying behind in silence.  The normal path is
   untouched: the ops JSON is still forwarded byte-exact, never parsed and
   re-serialized.

3. **The mirror does not rewrite a stylesheet that already holds the text.**
   `if (el.textContent !== cssText) el.textContent = cssText;` — the
   stylesheet stays standing, so the transitions running on it survive.

A fourth, smaller one: the navigation announcement.  The trigger's
`DOMContentLoaded` handler announced the document load even when the page
had already announced that same URL (a router pushing its path during
parse, a canonical-URL rewrite).  Two announcements meant the server
captured the **whole document twice** for one navigation.  The document
load is still announced when the URL did not change (a reload IS a new
document), but not twice for the same URL.  `tests/test_live_navigation.cjs`
was failing on the branch this section describes (11 passed, 2 failed) and
now passes.

Verification: `tests/test_cssom_styles.cjs` grew to 34 checks (it now
asserts that the *untouched* stylesheet is not re-shipped, and that the
mirror does not rewrite an unchanged sheet); `tests/test_tab_identity.cjs`
grew to 140 (the relay's framing contract: byte-exact, split, and a
recorded drop); new `tests/test_delta_relay.py` (19 checks) pins the
splitter's framing, ordering and payload-safety.  `test_live_navigation.cjs`
passes 13/13 — it was failing before this section.
