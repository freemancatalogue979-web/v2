# Capture & mirror pipeline — performance investigation (2026-11-07)

Scope: why the live mirror feels slow even on light pages, why it sometimes
stops updating, and where full captures come from. Read-only investigation —
no code changed. All references are `file:line` in this directory.

## How a frame flows today (30 seconds)

Server (`dom_capture.py`, `session.py`) → websocket → client (`client.html`) → iframe.

* **Full document** (`full_document` frame): in-page serializer
  (`_capture_fast_meta`, dom_capture.py:1408) → server regex passes
  (dead-iframe strip, `<base>`, script strip, asset rewrite) → JSON → ws →
  client `renderPageSnapshot` (client.html:10850): ~7 regex passes over the
  whole HTML string, input/scroll state capture, blob URL, `iframe.src` reset
  → full re-parse/re-layout → `rebuildMirrorIdMap` (client.html:11470).
* **Delta** (`dom_patch` frame): in-page MutationObserver
  (`_DELTA_OBSERVER_JS`, dom_capture.py:3804) batches ops every 16 ms
  (`DELTA_FLUSH_MS`) → `__domDelta` binding → `_delta_route` → outbox →
  `_delta_writer` → client `applyDomPatch` (client.html:11559) applies ops
  in place against the `data-mid` map.

## Findings (ranked)

### F1 — The delta channel silently turns off, and then every interaction is a full capture
`_delta_active` is only set after a full capture that was *patch-capable*
(dom_capture.py:5548-5553, 5664). If the fast serializer ever falls back to
plain `outerHTML`, `_last_capture_supports_delta` is set `False`
(dom_capture.py:5216-5226) and from then on **every interaction triggers a
full serialize+send+iframe-rebuild** (the in-page trigger at dom_capture.py:4696-4721
only debounces to 0.08 s / 0.25 s — `INTERACTION_CAPTURE_MIN_INTERVAL_S`,
`SNAPSHOT_CAPTURE_MIN_INTERVAL_S`). One degenerate capture permanently
downgrades the page to full-capture-per-click. This matches "does full
capture… not effective".

### F2 — Full captures freeze the delta channel for their whole duration
`_delta_route` *holds* (never sends) every batch while `_send_inflight`
(dom_capture.py:5381-5396), and `_send_full` only clears that flag in its
`finally` — **after** the awaited progressive asset upgrade
(dom_capture.py:5685 → `_send_asset_upgrade` at 5747, which can fetch for up
to the 10 s global budget `_REWRITE_GLOBAL_BUDGET_S`, dom_capture.py:1737).
So on any page with uncached assets, the mirror shows the document and then
**stops updating for up to ~10 s** while assets download. That is the
"stops updating" symptom, and it is by design today.

### F3 — Resync loops: one lost/mismatched generation = full capture storm
Client drops a patch on gen/doc mismatch and asks for a resync
(client.html:11578-11587, `requestResync` 9938 with only a 75 ms debounce);
the server answers `resync_request` with a forced full capture
(api.py:12013-12016); that bumps `gen`, so all in-flight patches are dropped
server-side too (dom_capture.py:5340-5345) and any client that missed the
`full_document` (ws hiccup, `applyFullDocument` throw — the `_deltaGen`
update sits inside the same `try`, client.html:9194-9212) will mismatch again
on the next patch → another resync → loop of full captures.

### F4 — The observer watches *every* attribute on *every* node
`OBSERVE_CFG` (dom_capture.py:4361-4365) is `{subtree, childList, attributes,
characterData}` with **no `attributeFilter`**. JS-driven animations, spinners,
tickers and class toggles produce a mutation record per frame; past
`OP_CAP = 5000` ops (dom_capture.py:4132) the observer emits `overflow` →
full recapture (dom_capture.py:4640-4644) while more mutations pile up into
the hold buffer → resync storm on animated-but-light pages. Additionally a
1.2 s `setInterval` walks every `<style>` and reads `cssRules`
(dom_capture.py:4575), and `s` ops ship whole stylesheet text per batch.

### F5 — Per-full-capture round trips stack up on the CDP channel
Every `_send_full` does `_read_delta_seq()` (dom_capture.py:5477) and often
`_dom_checksum()` (5466) plus a `page.title()` fallback (5600) — each a CDP
round trip on the same channel that carries input dispatch (the channel whose
storms are documented as able to wedge the SB driver, session.py:1898).

### F6 — Navigation waits are heavy even for light pages
`_ensure_page_stable` (dom_capture.py:395-414) waits up to 2.5 s `load` +
**2.5 s `networkidle`** + 1 s readyState on every navigation
(`handle_navigation` dom_capture.py:6016-6030, URL watcher 5130-5140). A light
page that polls every <500 ms never reaches networkidle → every navigation
pays the full ~6 s. Plus `load_settle` fires a second full capture per
navigation by design (session.py:990-1015), and two pollers run
concurrently: `watch_url` every 100 ms (dom_capture.py:5063) and
`_page_transition_monitor` every 500 ms (session.py:2179) — the latter puts
the client into nav-pending (iframe hidden, patches buffered) and depends on
a `full_document` within 5 s (client.html:9991).

### F7 — Each `full_document` is expensive on the client even when small
`renderPageSnapshot` runs ~7 full-string regex passes (CSP strip,
chrome-error strip, `/assets/` absolutizing ×2, base inject, **script strip**
client.html:11094, viewport/referrer metas), captures all input state
(`querySelectorAll('input,textarea,select,[contenteditable]')`), scroll
positions, then blob + `iframe.src` reset = full re-parse/re-layout/repaint,
then a full `data-mid` map rebuild. The byte-identical skip
(`state._lastDocHtml`, client.html:10853) only helps exact duplicates; the
server's identical-snapshot skip only applies when `snapshot_only`
(dom_capture.py:5560) — and `snapshot_only` is true for every host except the
5 in `DOM_LIVE_HOSTS` (dom_capture.py:127-145, session.py:2898).

### F8 — Document size inflation makes every capture bigger than the page
`CSS_EMBED_MAX_BYTES` = 3 MB of CSS inlined into the HTML and
`EMBED_MAX_BYTES` = 1 MB images as data URIs (dom_capture.py:225-243) — every
full capture re-serializes, re-JSON-dumps and re-sends those bytes and the
client re-parses them, even though `/assets/<hash>` + browser HTTP cache
would have been free after the first fetch.

## Quick wins (suggested order)

1. **Don't hold deltas during the asset upgrade**: move
   `_send_asset_upgrade` out of the `_send_inflight` window (or set the flag
   before it) — F2. One-line-ish change, biggest "stops updating" win.
2. **Re-arm `_delta_active` on the next successful fast capture** instead of
   letting one outerHTML fallback disable patches forever — F1. (Also:
   retry the fast serializer once before downgrading.)
3. **Add an `attributeFilter`** to `OBSERVE_CFG` (drop `style`/`class`? no —
   filter to a safe allowlist, or at least ignore `style` when a `class` op
   is present) and raise/handle `OP_CAP` more gracefully (coalesce instead of
   overflow) — F4.
4. **Skip the redundant CDP round trips** when meta already came with the
   capture (`_last_capture_meta`), and seed the checksum from it — F5.
5. **Cap navigation settle**: replace `networkidle` with a short
   "quiet for ~300 ms" in-page check, keep the 2.5 s only as a hard ceiling — F6.
6. **Deduplicate the two URL pollers** (watch_url vs _page_transition_monitor) — F6.
7. **Lower `CSS_EMBED_MAX_BYTES` / `EMBED_MAX_BYTES`** (or make inlining
   first-load-only) so recaptures stay small — F8.
8. **Rate-limit resyncs** client-side (e.g. 1 per 2 s, and only if the last
   resync didn't fix the mismatch) — F3.

## How to measure on a live box

The pipeline already logs the timings — grep the server log for:

* `dc send reason=… settle=… capture_ms=… rewrite_ms=… send_ms=… html_bytes=…`
  (dom_capture.py:5680) — one line per full capture;
* `[COLD] first document on screen: … ms` (dom_capture.py:5645);
* `dc delta outbox over budget` / `delta overflow -> full recapture`
  (dom_capture.py:5330, 4642);
* `dc skip(unchanged)` / `dc skip(identical snapshot)` — how often full
  captures are no-ops;
* client console: `applyDomPatch failed` / `requesting resync` lines
  (client.html:9684) and the WS frame sizes in devtools.

If `capture_ms` is small but `total_ms` is large, the time is in rewrite/send
(F8) or the client re-render (F7). If `reason=delta_overflow|resync|coalesce:*`
dominates, the delta channel is unhealthy (F1/F3/F4).

---

## Implementation status (2026-11-07)

All 8 quick wins are implemented on branch `arena/7fa9ed8a-v2`:

1. `_send_full` sends the document first and runs `_send_asset_upgrade` as a
   tracked background task (`_bg_tasks`), so `_send_inflight` clears immediately
   and `_delta_route` no longer holds patches for up to 10 s (dom_capture.py
   `_bg_upgrade`).
2. `_capture_fast_meta` is retried once before the outerHTML fallback, keeping
   `_delta_active` alive (dom_capture.py ~5288).
3. Observer: flush at `OP_CAP`, overflow only at `HARD_CAP = OP_CAP * 8`, with a
   re-entrancy-guarded `flush` wrapper around `flushInner` (dom_capture.py
   ~4178–4241).
4. `_send_full` reads the delta seq before the checksum and skips byte-identical
   re-sends via `_capture_pre_seq == _delta_seq_seen` (dom_capture.py ~5550–5559).
5. `_ensure_page_stable` waits are env-tunable constants
   (`DOM_STABLE_LOAD_TIMEOUT_MS` 1500 / `DOM_STABLE_NETWORKIDLE_TIMEOUT_MS` 800 /
   `DOM_STABLE_READY_TIMEOUT_MS` 800) instead of ~6 s hardcoded.
6. `DOMCaptureSession.on_url_changed` hook: `session._sync_dom_capture` wires it
   to `_on_capture_url_changed` (ws `page_transition` + navigation notification);
   `_page_transition_monitor` skips its 500 ms poll while the capture watcher
   (100 ms) is alive and hooked.
7. Embedding is first-load-only per URL (`_embed_done_urls`, capped 64);
   recaptures reference `/assets/...`. Swap dedupe is content-addressed
   (`from` + raw-content hash) so a recapture that resolves the same bytes as
   `/assets/<digest>` instead of a data: URI is not re-sent.
8. `requestResync` in client.html ignores a resync for a generation already
   requested within 5 s (`state._lastResyncGen` / `_lastResyncAt`).

Tests: capture pipeline 20, roundtrips 26, convergence 215, serializer 21,
asset upgrade 78 (py) + 31 (client), asset gate 22, inline assets 77, cssom 28,
boot speed 38, inline scripts 30 — all green. `tests/test_asset_upgrade.py` and
`tests/test_cssom_styles.cjs` were updated to the new contracts (background
asset upgrade drained via `send()` helper; first-load-only embed; guarded
flush wrapper).
