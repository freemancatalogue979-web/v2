"""Session gateway behaviour: python tests/test_mirror_gateway.py

The hub is the piece that decides whether a mirrored tab stays in sync, so
every assertion here is about ORDERING, BOUNDS and RECOVERY rather than about
page content (which dom_capture's own suites already cover):

  * a joining viewer is caught up from held state, not by waiting for a capture;
  * a sequence gap is repaired by retransmit, and only an unrecoverable gap by
    a fresh document;
  * a slow viewer degrades itself, never the capture pipeline;
  * frames for an old document cannot reach a new one.

Run with: python tests/test_mirror_gateway.py
"""
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mirror_gateway as mg  # noqa: E402

PASSED = 0
FAILED = []


def ok(cond, label):
    global PASSED
    if cond:
        PASSED += 1
    else:
        FAILED.append(label)


class FakeSocket:
    """A viewer's socket: records frames, optionally slow / wedged."""

    def __init__(self, *, delay_s=0.0, stall_after=None):
        self.frames = []
        self.delay_s = delay_s
        self.buffered_amount = 0
        self._stall_after = stall_after

    async def send_text(self, text):
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self._stall_after is not None:
            self.buffered_amount = self._stall_after
        self.frames.append(text)

    def dicts(self):
        return [json.loads(f) for f in self.frames]

    def kinds(self):
        return [d.get("message_type") for d in self.dicts()]


class CountingProvider:
    """Stands in for the capture pipeline; counts snapshot requests."""

    def __init__(self, *, fail_times=0, on_call=None):
        self.calls = []
        self.fail_times = fail_times
        self.on_call = on_call

    async def __call__(self, hub, reason):
        self.calls.append(reason)
        if self.on_call:
            self.on_call(hub, reason)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("capture unavailable")
        return True


async def drain(n=6):
    for _ in range(n):
        await asyncio.sleep(0)


async def wait_until(pred, timeout=1.5):
    """Wait for a condition instead of guessing how many loop turns a pump needs."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.005)
    return bool(pred())


def frames_of(sock, kind):
    return [d for d in sock.dicts() if d.get("message_type") == kind]


async def test_identity_and_ordering():
    hub = mg.MirrorHub(session_id="s1", tab_id="t1")
    doc = hub.document_id
    f1 = hub.publish(mg.KIND_SNAPSHOT, {"html": "<html></html>", "url": "https://a/"},
                     document_id=doc, lane="nav")
    ok(f1 is not None and f1.seq == 1, "snapshot is frame 1")
    ok(f1.epoch == 1, "first snapshot opens epoch 1")
    d1 = hub.publish(mg.KIND_DELTA, {"ops": [["t", 1, "x"]]}, document_id=doc)
    d2 = hub.publish(mg.KIND_DELTA, {"ops": [["t", 2, "y"]]}, document_id=doc)
    ok((f1.seq, d1.seq, d2.seq) == (1, 2, 3), "seq is monotonic across snapshot AND delta")
    ok(d1.epoch == d2.epoch == 1, "deltas stay inside the epoch")
    body = json.loads(d1.encode())
    ok(body["session_id"] == "s1" and body["tab_id"] == "t1", "identity fields on the wire")
    ok(body["document_id"] == doc and body["message_type"] == "delta",
       "document_id + message_type on the wire")
    ok(body["gen"] == 1 and body["type"] == "dom_patch",
       "legacy gen/type aliases still emitted for cached clients")
    ok(body["ops"] == [["t", 1, "x"]], "body stays flat (payload is not duplicated)")
    await hub.close()


async def test_raw_field_splicing_is_valid_json():
    hub = mg.MirrorHub(session_id="s", tab_id="t")
    raw_ops = '[["i",5,"<p>hi</p>"],["a",6,"class","\\"x\\""]]'
    frame = hub.publish(mg.KIND_DELTA, {"url": "https://a/"},
                        raw={"ops": raw_ops}, document_id=hub.document_id)
    parsed = json.loads(frame.encode())
    ok(parsed["ops"] == [["i", 5, "<p>hi</p>"], ["a", 6, "class", '"x"']],
       "raw ops text splices into a parseable frame")
    ok(parsed["url"] == "https://a/", "envelope fields survive the splice")
    await hub.close()


async def test_join_payload_catches_viewer_up():
    hub = mg.MirrorHub(session_id="s", tab_id="t")
    doc = hub.document_id
    hub.publish(mg.KIND_SNAPSHOT, {"html": "<html>a</html>", "url": "https://a/"},
                document_id=doc, lane="nav")
    for i in range(4):
        hub.publish(mg.KIND_DELTA, {"ops": [["t", i, "v"]]}, document_id=doc)
    # history holds the tail even with no viewer attached
    ok(len(hub.history) == 4, "deltas are recorded while nobody is watching")
    sock = FakeSocket()
    hub.attach_viewer(sock, "v1")
    ok(await wait_until(lambda: len(sock.frames) >= 7), "join payload was written out")
    kinds = sock.kinds()
    ok(kinds[0] == mg.KIND_RESYNC and kinds[1] == mg.KIND_SNAPSHOT,
       "join = resync then the latest snapshot")
    ok(kinds.count(mg.KIND_DELTA) == 4, "join carries every held delta after the snapshot")
    tail = frames_of(sock, mg.KIND_DELTA)
    ok([t["seq"] for t in tail] == sorted(t["seq"] for t in tail), "tail order preserved")
    ok(kinds[-1] == mg.KIND_CHROME, "join ends with chrome state")
    ok(all(t["document_id"] == doc for t in tail), "held deltas keep their document identity")
    await hub.close()


async def test_join_without_snapshot_requests_one():
    provider = CountingProvider()
    hub = mg.MirrorHub(session_id="s", tab_id="t", snapshot_provider=provider)
    sock = FakeSocket()
    hub.attach_viewer(sock, "v1")
    await drain()
    kinds = sock.kinds()
    ok(kinds == [mg.KIND_RESYNC], "no snapshot held -> resync frame only")
    await drain()
    ok(provider.calls[:1] == ["resync"], "joining with nothing held asks for a snapshot")
    await hub.close()


async def test_gap_retransmit_before_snapshot():
    provider = CountingProvider()
    hub = mg.MirrorHub(session_id="s", tab_id="t", snapshot_provider=provider)
    doc = hub.document_id
    snap = hub.publish(mg.KIND_SNAPSHOT, {"html": "<html/>", "url": "u"},
                       document_id=doc, lane="nav")
    deltas = [hub.publish(mg.KIND_DELTA, {"ops": [["t", i, "v"]]}, document_id=doc)
              for i in range(5)]
    sock = FakeSocket()
    viewer = hub.attach_viewer(sock, "v1")
    await drain()
    provider.calls.clear()
    # Viewer claims it only got through seq 3 of the epoch; 4 and 5 must be
    # retransmitted out of history without touching the capture pipeline.
    viewer.cursor_next_seq = 3
    ok(await wait_until(lambda: len(sock.frames) >= 8), "join payload fully written first")
    mark = len(sock.frames)
    result = hub.resync(viewer, epoch=snap.epoch, last_seq=3)
    ok(await wait_until(lambda: len(sock.frames) >= mark + 3), "retransmitted frames written")
    await asyncio.sleep(0.02)
    ok(result == "replay", "recoverable gap is repaired by retransmit")
    ok(provider.calls == [], "a retransmit costs zero captures")
    expected = [d.seq for d in deltas if d.seq > 3]
    after = [json.loads(f) for f in sock.frames[mark:]]
    replayed = [f["seq"] for f in after if f.get("message_type") == mg.KIND_DELTA]
    ok(replayed == expected,
       f"exactly the frames past the viewer's cursor are resent ({replayed} vs {expected})")
    ok(hub.metrics.counters["retransmit_frames"] == len(expected), "retransmit counted")
    # A gap that reaches behind the snapshot cannot be repaired cheaply.
    viewer.cursor_next_seq = 0
    result2 = hub.resync(viewer, epoch=snap.epoch, last_seq=snap.seq - 1)
    await drain()
    ok(result2 == "snapshot", "gap behind the epoch's snapshot needs a fresh document")
    ok(provider.calls == ["resync"], "unrecoverable gap asks the capture for a snapshot")
    await hub.close()


async def test_stale_document_frames_are_dropped():
    hub = mg.MirrorHub(session_id="s", tab_id="t")
    old_doc = hub.document_id
    hub.publish(mg.KIND_SNAPSHOT, {"html": "<html/>", "url": "u"}, document_id=old_doc)
    pending = hub.begin_navigation("https://b/")
    await drain()
    ok(hub.pending_document_id == pending, "navigation pending a new document")
    dropped = hub.publish(mg.KIND_DELTA, {"ops": [["t", 1, "old"]]}, document_id=old_doc)
    ok(dropped is None, "a delta for the previous document is refused")
    ok(hub.metrics.counters["frames_dropped_stale_doc"] == 1, "the drop is counted, not silent")
    # The new document's frames are admissible the moment it is announced.
    kept = hub.publish(mg.KIND_DELTA, {"ops": [["t", 2, "new"]]}, document_id=pending)
    ok(kept is not None, "the pending document's frames are accepted")
    snap = hub.publish(mg.KIND_SNAPSHOT, {"html": "<html>b</html>", "url": "https://b/"},
                       document_id=pending, lane="nav")
    ok(hub.document_id == pending, "the snapshot commits the new document")
    ok(snap.epoch == 2, "committing a navigation bumps the epoch")
    ok(len(hub.history) == 0, "the tail restarts under the new document")
    await hub.close()


async def test_chrome_frames_during_navigation():
    hub = mg.MirrorHub(session_id="s", tab_id="t")
    sock = FakeSocket()
    hub.attach_viewer(sock, "v1")
    await drain()
    hub.begin_navigation("https://b/")
    await drain()
    chrome = [d for d in sock.dicts() if d["message_type"] == mg.KIND_CHROME][-1]
    ok(chrome["loading"] is True and chrome["url"] == "https://b/",
       "navigation start announces loading + url")
    ok(chrome.get("navigating") is True, "the frame says a document is being replaced")
    hub.same_document_url("https://b#/tab")
    await drain()
    spa = [d for d in sock.dicts() if d["message_type"] == mg.KIND_CHROME][-1]
    ok(spa["url"] == "https://b#/tab" and spa.get("same_document") is True,
       "same-document URL change updates the bar")
    ok(hub.epoch == 0, "SPA routing never bumps the epoch")
    hub.navigation_failed("https://c/", "net::ERR_NAME_NOT_RESOLVED")
    await drain()
    fail = [d for d in sock.dicts() if d["message_type"] == mg.KIND_CHROME][-1]
    ok(fail.get("nav_failed") is True and "ERR_NAME" in fail["error"],
       "navigation failure is reported to the viewer")
    ok(hub.pending_document_id is None and hub.loading is False,
       "a failed navigation releases the pending document")
    await hub.close()


async def test_slow_viewer_does_not_block_capture():
    """The §6/§7 property: publishing is O(1) whatever the viewer is doing."""
    hub = mg.MirrorHub(session_id="s", tab_id="t", viewer_max_queue=8)
    doc = hub.document_id
    hub.publish(mg.KIND_SNAPSHOT, {"html": "<html/>", "url": "u"}, document_id=doc)
    slow = FakeSocket(delay_s=0.2, stall_after=20 * 1024 * 1024)
    viewer = hub.attach_viewer(slow, "v-slow")
    await drain()
    t0 = time.perf_counter()
    for i in range(60):
        hub.publish(mg.KIND_DELTA, {"ops": [["t", i, "v"]]}, document_id=doc)
    elapsed = time.perf_counter() - t0
    ok(elapsed < 0.05, f"60 publishes against a wedged socket took {elapsed*1000:.1f} ms")
    ok(viewer.stalled, "a saturated viewer is marked stalled")
    ok(hub.metrics.counters["frames_dropped_stalled"] > 0,
       "stalled drops are counted")
    # Fast viewer is unaffected by the slow one.
    fast = FakeSocket()
    hub.attach_viewer(fast, "v-fast")
    await drain()
    hub.publish(mg.KIND_DELTA, {"ops": [["t", 99, "z"]]}, document_id=doc)
    ok(await wait_until(lambda: any(json.loads(f).get("ops") == [["t", 99, "z"]]
                                    for f in fast.frames)),
       "a healthy viewer keeps receiving while a stalled one catches up")
    await hub.close()


async def test_structural_frames_are_never_dropped():
    hub = mg.MirrorHub(session_id="s", tab_id="t", viewer_max_queue=4)
    doc = hub.document_id

    class Paused(FakeSocket):
        def __init__(self):
            super().__init__()
            self.blocked = True

        async def send_text(self, text):
            while self.blocked:
                await asyncio.sleep(0.01)
            self.frames.append(text)

    sock = Paused()
    viewer = hub.attach_viewer(sock, "v1")
    await drain()
    for i in range(30):
        hub.publish(mg.KIND_DELTA, {"ops": [["t", i, "v"]]}, document_id=doc)
    ok(viewer.queue.full(), "the viewer's queue really is full")
    chrome = hub.publish(mg.KIND_CHROME, {"loading": False, "url": "https://x/"},
                         document_id=doc)
    ok(chrome is not None, "a structural frame is never refused because the queue was full")
    ok(hub.metrics.counters["frames_dropped_queue_full"] > 0,
       "the deltas that were shed to admit it are counted, not silently lost")
    sock.blocked = False
    ok(await wait_until(lambda: any(json.loads(f).get("seq") == chrome.seq
                                    for f in sock.frames)),
       "the shed-deltas path still delivers the chrome frame to the viewer")
    await hub.close()


async def test_stalled_viewer_resumes_by_replaying_state():
    """Full loop: saturated → stall → drain → hub hands it the catch-up payload."""
    hub = mg.MirrorHub(session_id="s", tab_id="t", viewer_max_queue=4,
                       snapshot_provider=CountingProvider())
    doc = hub.document_id
    hub.publish(mg.KIND_SNAPSHOT, {"html": "<html1/>", "url": "u1"}, document_id=doc)

    class Wedge(FakeSocket):
        """Accepts writes but never drains until told to."""

        def __init__(self):
            super().__init__(delay_s=0.02)
            self.blocked = True

        async def send_text(self, text):
            while self.blocked:
                await asyncio.sleep(0.01)
            self.buffered_amount = 0
            self.frames.append(text)

    sock = Wedge()
    sock.buffered_amount = 20 * 1024 * 1024      # over STALL_BYTES
    viewer = hub.attach_viewer(sock, "v1")
    await drain()
    for i in range(30):
        hub.publish(mg.KIND_DELTA, {"ops": [["t", i, "v"]]}, document_id=doc)
    ok(viewer.stalled, "a wedged socket with a full queue is stalled")
    dropped_while_stalled = hub.metrics.counters["frames_dropped_stalled"]
    t0 = time.perf_counter()
    for i in range(30):
        hub.publish(mg.KIND_DELTA, {"ops": [["t", 100 + i, "v"]]}, document_id=doc)
    ok(time.perf_counter() - t0 < 0.05, "publishing to a stalled viewer stays O(1)")
    ok(hub.metrics.counters["frames_dropped_stalled"] > dropped_while_stalled,
       "every delta dropped while stalled is counted")

    before = len(sock.frames)
    sock.blocked = False
    for _ in range(200):
        await asyncio.sleep(0.01)
        if not viewer.stalled:
            break
    ok(not viewer.stalled, "the viewer resumes once the socket drains")
    for _ in range(100):
        await asyncio.sleep(0.01)
        if len(sock.frames) > before:
            break
    kinds = [json.loads(f).get("message_type") for f in sock.frames[before:]]
    ok(mg.KIND_RESYNC in kinds and mg.KIND_SNAPSHOT in kinds,
       "resume reuses the canonical join payload (resync + snapshot + tail)")
    await hub.close()


async def test_resync_storm_degrades():
    provider = CountingProvider()
    hub = mg.MirrorHub(session_id="s", tab_id="t", snapshot_provider=provider)
    sock = FakeSocket()
    viewer = hub.attach_viewer(sock, "v1")
    await drain()
    results = []
    for i in range(5):
        viewer.resync._sent.append(time.monotonic() - i * 0.001)
        results.append(hub.resync(viewer, epoch=99, last_seq=0, reason="patch_failed"))
    ok("degraded" in results, "the fourth request in the window degrades instead of looping")
    ok(hub.metrics.counters["resync_degrades"] == 1, "degrade is counted once")
    ok(hub.resync(viewer, epoch=99, last_seq=0) == "degraded",
       "a degraded tab stops asking")
    await hub.close()


async def test_bounded_history_trims_and_requests_snapshot():
    calls = []

    def provider(hub, reason):
        calls.append(reason)

    hub = mg.MirrorHub(session_id="s", tab_id="t", max_history_frames=3,
                       snapshot_provider=provider)
    doc = hub.document_id
    for i in range(10):
        hub.publish(mg.KIND_DELTA, {"ops": [["t", i, "v"]]}, document_id=doc)
    ok(len(hub.history) == 3, "history stays inside its frame bound")
    ok(hub.metrics.counters["history_trims"] >= 7, "trims are counted")
    ok(await wait_until(lambda: "trim" in calls),
       "a trimmed floor asks the capture for a fresh document")
    await hub.close()


async def test_input_gate():
    hub = mg.MirrorHub(session_id="s", tab_id="t", input_gate_s=0.2)
    doc = hub.document_id
    snap = hub.publish(mg.KIND_SNAPSHOT, {"html": "<html/>", "url": "u"},
                       document_id=doc, lane="nav")
    sock = FakeSocket()
    viewer = hub.attach_viewer(sock, "v1")
    await drain()
    allowed, why = hub.allows_input("v1")
    ok(allowed and why == "", "a viewer that already holds the epoch may act")
    hub.publish(mg.KIND_SNAPSHOT, {"html": "<html2/>", "url": "u"}, document_id=doc)
    viewer.acked_epoch = snap.epoch
    allowed2, why2 = hub.allows_input("v1")
    ok(not allowed2 and why2 == "epoch-unapplied",
       "input is gated while the viewer is behind the new epoch")
    hub.ack("v1", hub.epoch, None)
    ok(hub.allows_input("v1")[0], "the epoch ack unlocks input")
    hub.publish(mg.KIND_SNAPSHOT, {"html": "<html3/>", "url": "u"}, document_id=doc)
    viewer.acked_epoch = hub.epoch - 1
    await asyncio.sleep(0.25)
    ok(hub.allows_input("v1")[0], "a lost ack cannot lock the viewer out forever")
    await hub.close()


async def test_delta_batching():
    hub = mg.MirrorHub(session_id="s", tab_id="t", batch_ms=20)
    doc = hub.document_id
    sock = FakeSocket()
    hub.attach_viewer(sock, "v1")
    await drain()
    before = len(sock.frames)
    for i in range(5):
        hub.publish(mg.KIND_DELTA, {"ops": [["t", i, "v"]]}, document_id=doc)
    ok(len(sock.frames) == before, "deltas are held for the batch window")
    await asyncio.sleep(0.05)
    ok(len(sock.frames) == before + 5, "the batch flush delivers them all")
    hub.publish(mg.KIND_CHROME, {"loading": False, "url": "u"}, document_id=doc)
    for _ in range(20):
        await asyncio.sleep(0.005)
        if len(sock.frames) >= before + 6:
            break
    kinds = sock.kinds()[before:]
    ok(kinds.count(mg.KIND_DELTA) == 5, "the batch flushed all five deltas")
    ok(kinds[-1] == mg.KIND_CHROME, "a structural frame is never reordered behind its deltas")
    await hub.close()


async def test_snapshot_request_retries_with_backoff():
    provider = CountingProvider(fail_times=1)
    hub = mg.MirrorHub(session_id="s", tab_id="t", snapshot_provider=provider,
                       snapshot_retry_s=0.02, snapshot_retry_max_s=0.1)
    hub.request_snapshot("nav")
    ok(await wait_until(lambda: len(provider.calls) >= 2, 0.5),
       "a failed snapshot request is retried instead of abandoned")
    ok(provider.calls[0] == "nav", "first request goes out immediately")
    # A successful snapshot resets the backoff (remote-browser's clearSnapshotRetry(True)).
    hub.publish(mg.KIND_SNAPSHOT, {"html": "<html/>", "url": "u"},
                document_id=hub.document_id, lane="nav")
    ok(hub._retry_delay_s == hub.snapshot_retry_s, "landing a snapshot resets the backoff")
    await hub.close()


async def test_hubsocket_adapter_normalizes_capture_frames():
    """The adapter is how the untouched capture path reaches the hub."""
    hub = mg.MirrorHub(session_id="s", tab_id="t")
    sock = FakeSocket()
    hub.attach_viewer(sock, "v1")
    await drain()
    adapter = mg.HubSocket(hub, None)
    await adapter.send_json({"type": "full_document", "html": "<html/>", "url": "https://a/",
                             "gen": 1, "seq": 1, "reason": "navigation"})
    await adapter.send_text(json.dumps({"type": "dom_patch", "gen": 1, "seq": 2,
                                        "ops": [["t", 1, "v"]]}))
    await adapter.send_json({"type": "session_info", "session_id": "s"})
    ok(await wait_until(lambda: len(sock.frames) >= 4), "adapter frames reached the socket")
    frames = sock.dicts()
    kinds = [f.get("message_type") for f in frames]
    ok(mg.KIND_SNAPSHOT in kinds, "capture full_document became a snapshot frame")
    ok(mg.KIND_DELTA in kinds, "capture dom_patch became a delta frame")
    snap = [f for f in frames if f.get("message_type") == mg.KIND_SNAPSHOT][0]
    ok(snap["tab_id"] == "t" and snap["session_id"] == "s", "adapter stamped identity")
    ok(snap["html"] == "<html/>" and snap["url"] == "https://a/",
       "the capture body survives normalization untouched")
    ok(snap["seq"] == hub.snapshot_seq, "the hub owns the sequence number, not the producer")
    ok(mg.KIND_DELTA in kinds and json.loads(sock.frames[kinds.index(mg.KIND_DELTA)])["ops"],
       "the adapter's delta really carries its ops")
    ok("session_info" in json.dumps(frames[-1]),
       "unrecognized traffic is passed through as control, not dropped")
    await hub.close()


async def test_snapshot_opens_epoch_and_clears_superseded_queue():
    hub = mg.MirrorHub(session_id="s", tab_id="t", viewer_max_queue=64)
    doc = hub.document_id
    hub.publish(mg.KIND_SNAPSHOT, {"html": "<html/>", "url": "u"}, document_id=doc, lane="nav")
    sock = FakeSocket(delay_s=0.0)
    viewer = hub.attach_viewer(sock, "v1")
    await drain()
    for i in range(3):
        hub.publish(mg.KIND_DELTA, {"ops": [["t", i, "v"]]}, document_id=doc)
    new_doc = hub.begin_navigation("https://b/")
    hub.publish(mg.KIND_SNAPSHOT, {"html": "<html b/>", "url": "https://b/"},
                document_id=new_doc, lane="nav")
    ok(hub.epoch == 2, "second document = second epoch")
    ok(hub.snapshot_frame.payload["url"] == "https://b/", "hub holds the newest document")
    ok(viewer.cursor_epoch == 2, "cursor follows the epoch")
    await hub.close()


# ---------------------------------------------------------------------------
# MirrorInputRelay — the B→A lane
# ---------------------------------------------------------------------------

async def test_relay_serializes_structural_actions_per_tab():
    """Two clicks in the same tab cannot interleave; the second waits for the first.

    Ordering matters more than speed here: a visitor who clicks "next" twice and
    gets pages 3 and 2 in that order has a mirror that lies about the page.
    """
    hub = mg.MirrorHub(session_id="s", tab_id="t", viewer_max_queue=64)
    doc = hub.document_id
    hub.publish(mg.KIND_SNAPSHOT, {"html": "<html/>"}, document_id=doc, lane="nav")
    sock = FakeSocket()
    viewer = hub.attach_viewer(sock, "v1")
    await wait_until(lambda: len(sock.frames) >= 1)
    hub.ack("v1", epoch=hub.epoch, seq=hub.seq)      # viewer is current: gate open

    relay = mg.MirrorInputRelay(hub, gate_timeout_s=0.05)
    order = []

    async def slow(i):
        order.append("start-%d" % i)
        await asyncio.sleep(0.03)
        order.append("end-%d" % i)

    await relay.submit("click", lambda: slow(1), viewer_id="v1")
    await relay.submit("click", lambda: slow(2), viewer_id="v1")
    await relay.drain(timeout_s=2.0)
    ok(order == ["start-1", "end-1", "start-2", "end-2"],
       "structural actions run one after another, never overlapping: %r" % (order,))
    ok(relay.stats["executed"] == 2, "both actions executed")
    await hub.close()


async def test_relay_holds_structural_input_until_the_epoch_is_acked():
    """A click against a document the viewer has not drawn yet is dropped, not applied.

    This is the anti-ghost-input rule: the visitor's cursor is over a button in
    epoch 4, the page has navigated to epoch 5, and "close the modal" must not be
    delivered to a page that no longer has one.
    """
    hub = mg.MirrorHub(session_id="s", tab_id="t")
    doc = hub.document_id
    hub.publish(mg.KIND_SNAPSHOT, {"html": "a"}, document_id=doc, lane="nav")
    sock = FakeSocket()
    viewer = hub.attach_viewer(sock, "v1")
    await wait_until(lambda: len(sock.frames) >= 1)
    hub.ack("v1", epoch=hub.epoch, seq=hub.seq)

    relay = mg.MirrorInputRelay(hub, gate_timeout_s=0.06, wait_poll_s=0.01)
    ran = []
    # A newer epoch opens and the viewer has not acked it yet.
    new_doc = hub.begin_navigation("https://b/")
    hub.publish(mg.KIND_SNAPSHOT, {"html": "b"}, document_id=new_doc, lane="nav")
    ok(hub.allows_input("v1")[0] is False, "input is gated while the viewer is behind")
    verdict = await relay.submit("click", lambda: ran.append(1), viewer_id="v1")
    ok(verdict == "stale", "gated structural action reports 'stale'")
    ok(not ran, "nothing was executed against an unacked document")
    ok(relay.stats["dropped_stale"] == 1, "the drop is counted, not silent")

    # force=True is the operator's escape hatch (reload, explicit re-click).
    verdict = await relay.submit("click", lambda: ran.append(2), viewer_id="v1", force=True)
    await relay.drain(timeout_s=1.0)
    ok(verdict == "ok" and ran == [2], "forced submission runs anyway and is counted")
    ok(relay.stats["forced"] == 1, "forced count")

    # Once the viewer acks the new epoch, the same action runs normally.
    hub.ack("v1", epoch=hub.epoch, seq=hub.seq)
    verdict = await relay.submit("click", lambda: ran.append(3), viewer_id="v1")
    await relay.drain(timeout_s=1.0)
    ok(verdict == "ok" and ran == [2, 3], "acked viewer unblocks input")
    await hub.close()


async def test_relay_passes_geometry_actions_through_and_survives_failures():
    """mousemove/scroll are not gated, and a raising handler must not wedge the tail."""
    hub = mg.MirrorHub(session_id="s", tab_id="t")
    doc = hub.document_id
    hub.publish(mg.KIND_SNAPSHOT, {"html": "a"}, document_id=doc, lane="nav")
    relay = mg.MirrorInputRelay(hub, gate_timeout_s=0.05, wait_poll_s=0.01)
    seen = []
    # 'mousemove' is not in STRUCTURAL: no viewer ack is demanded for pointer
    # geometry, or the mirror would feel dead whenever a viewer is catching up.
    verdict = await relay.submit("mousemove", lambda: seen.append("move"), viewer_id="v1")
    await relay.drain(timeout_s=1.0)
    ok(verdict == "ok" and seen == ["move"], "geometry actions bypass the gate")

    ok(await relay.submit("click", lambda: (_ for _ in ()).throw(RuntimeError("boom")),
                          viewer_id="v1") == "ok", "a failing action is accepted")
    await relay.drain(timeout_s=1.0)
    ok(relay.stats["failed"] == 1, "failure counted")
    after = []
    await relay.submit("click", lambda: after.append(1), viewer_id="v1")
    await relay.drain(timeout_s=1.0)
    ok(after == [1], "the per-tab tail keeps running after a failure")
    await hub.close()


async def test_relay_without_hub_executes_inline():
    """Gateway off: the relay must not swallow input — sessions still work."""
    hub = mg.MirrorHub(session_id="s", tab_id="t")
    relay = mg.MirrorInputRelay(None, gate_timeout_s=0.02)
    ran = []
    verdict = await relay.submit("click", lambda: ran.append(1))
    await relay.drain(timeout_s=1.0)
    ok(verdict == "skipped", "no hub reports 'skipped' so callers run inline")
    ok(ran == [], "the relay itself did nothing")
    await hub.close()



async def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        try:
            await t()
        except Exception as exc:  # a raised assertion is a failed check
            FAILED.append(f"{t.__name__} raised {type(exc).__name__}: {exc}")
    if FAILED:
        print(f"FAIL  mirror gateway: {PASSED} passed, {len(FAILED)} failed")
        for f in FAILED:
            print("  - " + f)
        return 1
    print(f"PASS  mirror gateway: {PASSED} checks across {len(tests)} cases")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
