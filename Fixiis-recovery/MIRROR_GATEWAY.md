# Mirror Gateway — adopting Remote Browser's distribution / navigation / viewer architecture

Reference: `mystxcal/remote-browser` (read at HEAD of the clone; the applicable parts are
`packages/protocol/src/wire.ts`, `packages/gateway/src/hub/tabhub.ts`,
`packages/gateway/src/ws/{viewerconn,server}.ts`, `packages/gateway/src/browser/{agentlink,nav}.ts`,
`packages/gateway/src/input/relay.ts`, `packages/viewer/src/{mirror,resync,ws}.ts`).

**rrweb is NOT adopted.** Our capture pipeline (`dom_capture.py`: fast serializer, MutationObserver
delta, asset rewrite, form-value ops, coalescing, `data-mid` identity) stays the single recorder.
What we take from Remote Browser is the *shape of the system around the recorder*: a per-tab hub
that owns the canonical snapshot + bounded delta tail, an epoch/seq contract, drop-and-resync for
slow viewers, document identity for navigation, and an incremental renderer on the viewer side.

## 1. What Remote Browser does that we don't (gap analysis)

| Remote Browser | Us today | Adopted as |
|---|---|---|
| `TabHub` holds `meta + snapshot + deltas[]`, bounded at 5000 events / 10 MB, and *replays the tail to a joining viewer* | `DOMCaptureSession` keeps `last_sent_html` only; a new/reconnected viewer must wait for the next full capture | `mirror_gateway.MirrorHub` with bounded delta history + `join_payload()` |
| `seq` per tab, monotonic across snapshot **and** delta; viewer treats any gap as divergence and asks for a fresh epoch | `seq` stamped on frames, client counts gaps but only logs them; recovery relies on the next capture | hub-assigned `seq`, client gap detection → `resync-req` → **retransmit from history first**, snapshot only if the gap is unrecoverable |
| `epoch` bumps on new `docId`, forced snapshot, viewport change; every epoch opens with a snapshot | `gen` bumps on every full send; `doc` token carried on patches only | `epoch` + `document_id` (uuid per new document) on **every** frame, both directions |
| `ViewerConn`: check `bufferedAmount` **before** send, stall above 8 MB, drop deltas (never queue), poll drain, resume with `resync` + snapshot. Slow viewer = O(1) memory | capture `await`s `websocket.send_text` under `_ws_send_lock` — a slow viewer *does* block the capture pipeline today | per-viewer bounded queue + stall/ drain-resume policy in `MirrorViewer`; capture publishes to the hub and returns |
| `AgentLink` drops any message whose `docId` ≠ current; pending commands *reject on navigation* so an old-document mutation can't land on a new one | stale-delta protection is client-side (mid miss) and the `_ws_send_lock` re-check | hub drops frames carrying a stale `document_id`; snapshot requests are generation-checked |
| `nav.ts`: `frameNavigated` → loading + new epoch; `navigatedWithinDocument` → URL bar **only**, explicitly no epoch bump | URL watcher conflates SPA history changes with document changes (`_capture_navigation_once`) | `hub.begin_navigation()` / `hub.same_document_url()` / `hub.end_navigation()` / `hub.navigation_failed()` |
| `input/relay.ts`: per-tab serial tail, driver-only, **epoch gate** (input from a viewer whose acked epoch ≠ current is dropped), node id → live rect | `handle_click` resolves `data-mid`, no epoch gate, no per-viewer identity | `MirrorInputRelay`: per-tab queue, epoch gate with unlock timeout, mid → Browser A |
| `resync.ts`: bounded exponential backoff, 3 per 30 s window, 4th degrades that tab | 75 ms debounce, unbounded retries | `ViewerResyncPolicy` ported 1:1 |
| `Fanout`: serialize once, 30 ms delta batch, flush before a stalled peer's replay | each frame serialized once per send site, no batching | hub fanout with single serialization + per-tab batch window |

## 2. What we deliberately keep

- `dom_capture.py` capture engine, `_FAST_SERIALIZE_JS`, `_DELTA_OBSERVER_JS`, asset gate,
  `AssetManager`, SingleFile fallback tiers, coalescing, `data-mid` identity, progressive assets.
- The existing `full_document` / `dom_patch` / `asset_map` **body** keys on the wire (a megabyte-sized
  `html` must not be duplicated into a `payload` alias). New message types use `payload`.
  `client.html` normalizes both shapes.
- `resource_proxy/` (CSS rewrite, range, cache, auth) as the resource lane; the DOM channel never
  awaits it.
- One websocket per viewer at `/ws`; `session.websocket` remains the primary control socket.

## 3. File-by-file plan

**`mirror_gateway.py` (new)** — no fastapi/playwright import; pure asyncio.
- `MirrorHub`: `session_id`, `tab_id`, `document_id`, `epoch`, `seq`; `snapshot` (latest full
  document frame), `history` (bounded `deque` of `(seq, epoch, kind, text)`), `publish()`,
  `join_payload()`, `request_snapshot(reason)` with retry/backoff, `begin/end/failed_navigation`,
  `same_document_url`, `attach/detach viewer`, `note_input`, `resync(viewer)` (retransmit-or-snapshot),
  `ack_epoch`, `stats()`.
- `MirrorViewer`: bounded `asyncio.Queue`, `buffered_amount`/`backlog` stall rule, replay cursor
  (skip frames the viewer already applied), pump task.
- `MirrorFrame` + `encode_frame()` — the one place an envelope is serialized (per broadcast, not
  per viewer).
- `ViewerResyncPolicy` (ported constants), `PipelineMetrics` (capture/queue/serialize/send/render).
- `HubSocket`: duck-typed `send_text/send_json/buffered_amount` shim so capture code that still
  holds a "websocket" publishes into the hub instead of the wire.

**`dom_capture.py`** — capture publishes, never writes to a socket directly.
- `DOMCaptureSession.hub` attribute; `_emit()` replaces the five `websocket.send_text`/`send_json`
  sites; no hub ⇒ old direct path (rollback-safe).
- Frame emission carries `kind`, `document_id`, `epoch`, and our `gen` (kept as a legacy alias).
- `document_id` minted per new document (`_new_document_id()`), bumped in `_send_full` when the
  doc token changes; `_on_delta` drops frames whose doc token ≠ current document.
- Perf stamps (`capture_ms`, `rewrite_ms`, `serialize_ms`) ride on the frame for the hub metrics.
- `note_navigation_*` hooks call into the hub; nav follow-ups stay but are hub-cadence aware.

**`session.py`**
- `_sync_dom_capture()` builds/keeps one hub per `(session, active tab)`; viewer (re)attach and
  detach on `reattach_websocket`/`cleanup`; `_send_metadata` and permission frames go through the
  primary viewer.
- Navigation lifecycle from page events: `framenavigated`/`popup`/`close` → hub document changes;
  `handle_navigation` failures → `hub.navigation_failed()` (viewer shows the failure, keeps the old
  document instead of a blank mirror).
- `MirrorInputRelay` gates forwarded input by acked epoch; `handle_input/click/keypress/input_sync`
  route through it (scroll + focus added).
- **Removed**: `log_keystroke` writers, `__neoLogInput` / `__neoLogFormData` bindings,
  `captureCredentialFields`, the credential keylog store and its admin read surface. No password,
  payment or keystroke data is collected or transmitted by the mirror anymore.

**`api.py`**
- `/ws`: after `session_info`, `hub.attach_viewer(websocket)` → the viewer immediately receives
  `join_payload()` (latest snapshot + tail), no wait for the next capture; detach in `finally`.
- Inbound protocol: `resync_request` → `hub.resync()`, `view_ack`, `mirror_perf`, `nav`
  (go/back/fwd/reload/newtab/close/activate), `scroll`, `focus`, `visible` (visibility → throttle).
  `resume` in `init` replays the bounded tail instead of a full re-capture when it is still valid.
- `GET /api/diagnostics/pipeline` (+ per-session slice) for §9 measurements.
- Keylog endpoints return `removed: true` (410 for writes) instead of serving stored secrets.

**`client.html`** — incremental renderer, not a document re-writer.
- `MirrorProtocol.normalize(message)` → `{kind, tab, documentId, epoch, seq, payload}` for both the
  new envelope and legacy flat frames.
- `MirrorCursor`: per-tab `{epoch, documentId, nextSeq}`; duplicate/out-of-order → ignore; gap →
  `resync-req` with `ViewerResyncPolicy` (backoff, 3/30 s, degrade); `replay` frames re-enter the
  same ordered path.
- `full_document`/`snapshot` → blob swap as today (structure/CSS/resource refs preserved, scripts
  inert); `dom_patch`/`delta`/`replay` → `applyDomPatch` in place (no `document.open/write` on the
  patch path — the remaining `document.write` at the LPV path is a different, non-mirror screen).
- `chrome` → nav transition affordance (url/loading/back-fwd) without freezing or buffering patches;
  same-document URL changes never rebuild.
- `view_ack` per applied epoch; input is not forwarded until the current epoch is applied (matches
  the server gate, so a stale layout can't be clicked).
- `mirror_perf` samples (≤1/s) report receive/patch-apply/render paint times back to the hub.

## 4. Invariants

1. Capture never awaits a viewer: `publish()` is O(1) into bounded structures; the socket write
   happens on the viewer's pump task.
2. Every frame carries `session_id`, `tab_id`, `document_id`, `epoch`, `seq`.
3. A frame whose `document_id` is not the hub's current document is dropped at the hub, not shipped.
4. A new viewer gets snapshot + tail; a gap is repaired by retransmit before it is repaired by a
   fresh capture. Nothing is dropped silently: every drop increments a named counter.
5. Scripts in the mirror stay inert; Browser A is the only JavaScript runtime.

## 5. Verification

- `tests/test_mirror_gateway.py` — asyncio harness, no browser: ordering, bounded history,
  join payload, gap retransmit, unrecoverable gap → snapshot, slow-viewer stall/drop/resume,
  stale-document drop, epoch bump + input gate, resync storm degradation, capture non-blocking.
- `tests/test_viewer_protocol.cjs` — extracts `MirrorProtocol`/`MirrorCursor`/`ViewerResyncPolicy`
  from `client.html` into a vm sandbox: envelope normalization (both shapes), gap/dup/order,
  patch-only application (no document rebuild), backoff window, degrade-on-storm.
- `tests/test_no_keylog.py` — the credential/keystroke collection surface is gone from
  `session.py`/`api.py` and the mirror protocol has no field that can carry one.
- Existing suites re-run unchanged: `test_capture_pipeline.py`, `test_capture_roundtrips.py`,
  `test_asset_upgrade.py`, `test_capture_convergence.cjs`, `test_mirror_scroll.cjs`,
  `test_tab_identity.cjs`, `test_duplicate_mids.cjs`.

## 6. Implementation status (after the client/relay turn)

Delivered, and measured rather than asserted:

| Plan item | Where | State |
| --- | --- | --- |
| Hub: per-tab canonical state, bounded tail, join payload, resync budget, stall/resume | `mirror_gateway.py` | done — `tests/test_mirror_gateway.py`, 96 checks |
| Capture publishes into the hub (no second engine, one serialization per frame) | `dom_capture.py` `_emit_*`, `_send_full`, `_on_delta` | done — convergence suite still green (215 checks) |
| Navigation lanes: `begin/end/failure/same_document_url`, token-based document identity | `dom_capture.py` + `MirrorHub` | done |
| Session: hub per tab, viewer attach/detach, tab-switch handoff, page-close teardown | `session.py` | done |
| Reverse interaction relay (click/key/scroll/focus/form) with per-tab serialization + epoch gate | `MirrorInputRelay` + `session._relay_input` | done — 4 new relay cases in the gateway suite |
| Credential/keystroke collection deleted; hard-coded secret exclusion | `session.py`, `api.py`, `dom_capture.py`, `client.html` | done — `tests/test_credential_policy.py`, 63 checks |
| Viewer protocol: envelope+legacy normalization, per-tab cursor, gap→resync, `view_ack`, `mirror_perf`, `mirror_nav`/`mirror_focus` | `client.html` | done — `tests/test_mirror_protocol.cjs`, 85 checks |
| `mirror_*` settings | `config.py` | done (`MIRROR_GATEWAY`, `MIRROR_HISTORY_FRAMES`, `MIRROR_HISTORY_BYTES`, `MIRROR_BATCH_MS`, `MIRROR_VIEWER_QUEUE`) |
| Pipeline diagnostics endpoint (per-stage, navigation separate) | `GET /api/diagnostics/pipeline` | done — reports means, no targets |
| Asset-lane documentation | `resource_proxy/` | unchanged by design: the DOM channel never awaits a resource |

Deliberate deviations from the plan, each for a reason worth keeping on the record:

1. **No resume cursor on reconnect.** The plan allowed carrying `{epoch, next_seq}` across a
   reconnect. A reconnecting transport cannot prove what it last *rendered*, and resuming from an
   unverified position is how a mirror stays permanently one patch wrong. `reattach_websocket`
   therefore asks the hub for the join payload; the client dedupes by `(epoch, seq)`, so the cost is
   a few redundant frames instead of a wrong page. An explicit `resume` from the viewer is still
   honoured, because there the viewer is *telling* us what it acked.
2. **Envelope frames stay flat on the wire.** `MirrorFrame.to_dict()` puts identity and body at the
   top level (a multi-megabyte `html` is never duplicated under `payload`), and `MirrorProtocol
   .normalize()` maps gateway kinds onto the legacy handler names. Rewriting every renderer in
   `client.html` would have been churn with no user-visible benefit.
3. **Credential values travel as keystrokes, not as state.** Absolute-value field sync
   (`input_sync`) is the reliable channel for ordinary inputs and stays. For a credential-shaped
   field the client instead sends the individual characters the user pressed, keeps no baseline in
   the typed-value store, and never logs a value. Typing into a login form on Browser A still
   works — a mirror that cannot type a password is not a mirror — while the value is never
   accumulated, echoed, or archived anywhere.
4. **Latency legs use the clock that can measure them.** Durations inside one process use that
   process's monotonic clock; the one leg that crosses machines (send → receive) uses wall clocks on
   both ends and is reported as skew-limited. The pre-existing `C - B` that subtracted a server
   `perf_counter()` from a client `performance.now()` printed a meaningless number, and is gone.

Known pre-existing failures in this checkout, unrelated to this work and verified identical at
`73f06b0`: `tests/test_capture_roundtrips.py`, `tests/test_lpv_mono_font.cjs`,
`tests/test_mirror_font.cjs`, `tests/test_mirror_scroll.cjs`. The last three fail because
`client.html` in this recovery branch carries *stub* definitions for
`ensureMirrorScrolling` / `pageScrolls` / `nudgeScrollRescue` / the font theming helper, so their
tests cannot find the real block. Restoring those is a separate change.

## 7. Latency reporting and the input gate (after the first live visit)

The first real end-to-end visit produced:

```
[LATENCY] seq=3  gen=1 capture_ms=%.0f network_ms=%.0f ... 917.5 144.46 20.8 -1794440401020.1
[LATENCY] seq=18 gen=3 capture_ms=%.0f network_ms=%.0f ... 172.6 8983.3 42.8 -1794440392904.8
```

Three separate defects were hiding in that output, and only one of them was page speed:

1. **`%.0f` is not a `console.log` format specifier.** Chrome implements
   `%s %d %i %f %o %O %j %c`; `%.0f` is emitted literally and the arguments are
   appended at the end unformatted. The numbers were therefore not labelled by
   their position in the string — the log was unreadable rather than wrong.
   Format strings are gone from both `[LATENCY]` lines; numbers are rounded where
   they are built (`toFixed(0)`) and concatenated.
2. **`total_ms` subtracted two clocks that have nothing to do with each other** —
   the viewer's `performance.now()` (ms since *that page* loaded) minus a
   server-side timestamp. Hence `-1794440401020`. A total is now the **sum of the
   measured legs**, and is omitted when a leg could not be measured.
3. **`network_ms = 8983` was clock skew, not the network.** Once the send stamp
   became a wall clock (so the viewer could compare it at all), the difference
   between two machines' clocks entered that leg directly: a viewer whose clock is
   9 s off reports a 9 s wire time. There was no skew sampler to subtract, so one
   was added: `mirror_clock_req` → `{server_time_ms}` once every 5 s, giving
   `offset = server + rtt/2 − local` (median of the last 8 samples, an absurd RTT
   discarded). The legs are now `capture` / `rewrite` / `publish` / `wire` (offset
   corrected) / `render`, `skew` and `rtt` are printed alongside anything derived
   from them, and `wire=?` is printed rather than a number when no sample exists.

What the numbers *do* say about speed: ~917 ms for the first full document of a
real page, ~172 ms for the next, ~20–43 ms to render. Distribution contributed
~0 — those are capture-side durations. The split is now reported as
`capture=` vs `rewrite=` so the next optimization targets whichever is large
instead of guessing.

Two changes that affect felt latency, from the gateway side rather than the report:

* **The input gate no longer holds everything.** It waited up to 600 ms for the
  epoch ack on *every* action and then discarded a click. During a navigation that
  reads as a dead page. The ceiling is now 120 ms (a round trip, not a capture),
  and the wait is graded: pointer presses / submit may be dropped (a mid from a
  superseded document can name a different node), keystrokes wait briefly and then
  **run anyway** (a lost character is unrecoverable, a 120 ms-late one is
  invisible), and scroll/wheel/mousemove never wait at all. A dropped press is
  announced to the viewer on the control lane (`mirror_input_dropped`) so the UI
  can say "page is loading" instead of appearing to eat the click.
* **A capture no longer holds the relay tail.** The post-click and post-mouseup
  recaptures were awaited inside the relayed handler, so every subsequent action
  queued behind a full snapshot. They are now scheduled (`_schedule_capture`); the
  capture session already coalesces concurrent requests, and ordering between
  *actions* is unaffected because the action itself still runs on the tail.
* `view_ack` is coalesced: immediate when the epoch changes (that is the ack the
  gate waits for), otherwise at most one per 250 ms carrying the newest applied
  frame. Previously the viewer sent one upstream message per applied patch.
