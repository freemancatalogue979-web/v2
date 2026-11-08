#!/usr/bin/env python3
"""The delivery pipeline: ordered, priority aware, never a waiting room.

    PYTHONPATH=. python3 tests/test_sync_pipeline.py

The mirrored view needs four independent facts - the new URL, the document, the
patches, the resolved assets - and they become available at different times.  The
pipeline exists so a producer can hand a frame over and walk away: a slow
capture must never hold back a frame another producer has already produced.

What these tests pin:

  * ``publish`` returns immediately (no awaits, no locks, no raising);
  * a navigation control frame overtakes QUEUED bulk frames, because a document
    that has not been written yet is worth nothing compared to the URL;
  * bulk frames keep their FIFO order among themselves (that is what makes
    applying patches in arrival order safe);
  * ``nav_id`` and ``seq`` are monotonic and every frame is stamped before it
    can be seen by anyone;
  * a producer that waits for its own frame (``publish_now``) waits for that
    frame only - not for frames other producers have not produced yet;
  * the queue is bounded, and what it drops under pressure is the frame the
    client loses least by losing (a patch or an asset edit, never silently a
    document);
  * the writer is a burst task: it exits when the queue drains, so an idle
    session owns no tasks and nothing is bound to a dead event loop.
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sync_pipeline as sp  # noqa: E402

PASSED = 0


def ok(cond, label):
    global PASSED
    assert cond, label
    PASSED += 1


class FakeSocket:
    """Records frames in send order; can be made slow to expose blocking."""

    def __init__(self, delay=0.0):
        self.sent = []
        self.delay = delay
        self.closed = False

    async def send_text(self, text):
        if self.delay:
            await asyncio.sleep(self.delay)
        self.sent.append(text)

    def types(self):
        import json
        return [json.loads(t)["type"] for t in self.sent]


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# 1. publish is synchronous and never raises
# ---------------------------------------------------------------------------
def test_publish_returns_immediately():
    async def main():
        ws = FakeSocket(delay=0.05)
        pipe = sp.SyncPipeline(lambda: ws, name="t")
        t0 = asyncio.get_event_loop().time()
        result = pipe.publish({"type": "dom_patch", "ops": [["t", "1", 0, "x"]]})
        elapsed = asyncio.get_event_loop().time() - t0
        ok(result is True, "publish() should report that the frame was taken")
        ok(elapsed < 0.01,
           "publish() blocked for %.1f ms - it must only enqueue (%.0f ms send is "
           "still running)" % (elapsed * 1000, ws.delay * 1000))
        ok(pipe.pending >= 1, "the frame is not queued")
        await pipe.drain(2.0)
        ok(ws.types() == ["dom_patch"], "the frame was never written: %r" % (ws.types(),))
        await pipe.close()

    run(main())


def test_publish_without_a_loop_is_a_no_op_not_a_crash():
    pipe = sp.SyncPipeline(lambda: FakeSocket(), name="nolOOP")
    # Called from synchronous code with no running loop (import time, teardown).
    ok(pipe.publish({"type": "dom_patch"}) is False,
       "without a running loop the frame cannot be taken and must say so")


# ---------------------------------------------------------------------------
# 2. priority: navigation overtakes queued bulk, bulk stays FIFO
# ---------------------------------------------------------------------------
def test_navigation_overtakes_queued_bulk():
    async def main():
        ws = FakeSocket(delay=0.01)
        pipe = sp.SyncPipeline(lambda: ws, name="prio")
        # A big document and a patch are queued, none of them written yet...
        pipe.publish({"type": "full_document", "url": "https://a/", "html": "x"})
        pipe.publish({"type": "dom_patch", "ops": [["r", "1"]]})
        # ...and the page moves, before the writer got to either.  That fact is
        # worth more than both: the client can move its URL on it now.
        nav = pipe.begin_navigation("https://b/", reason="url_change", same_document=False)
        pipe.publish(nav, control=True)
        await pipe.drain(2.0)
        types = ws.types()
        ok(types.index("navigation") == 0,
           "the navigation frame did not overtake queued bulk frames: %r" % (types,))
        ok(types.index("dom_patch") > types.index("navigation"),
           "the patch should follow the newer navigation: %r" % (types,))
        await pipe.close()

    run(main())


def test_bulk_frames_keep_their_order():
    async def main():
        ws = FakeSocket(delay=0.002)
        pipe = sp.SyncPipeline(lambda: ws, name="fifo")
        for i in range(6):
            pipe.publish({"type": "dom_patch", "ops": [["t", str(i), 0, str(i)]]})
        await pipe.drain(2.0)
        order = [t for t in ws.types()]
        ok(order == ["dom_patch"] * 6, "bulk frames were reordered: %r" % (order,))
        import json
        ops = [json.loads(t)["ops"][0][1] for t in ws.sent]
        ok(ops == ["0", "1", "2", "3", "4", "5"],
           "patches left in a different order than they were published: %r" % (ops,))
        await pipe.close()

    run(main())


# ---------------------------------------------------------------------------
# 3. stamps: nav_id and seq are monotonic and present on every frame
# ---------------------------------------------------------------------------
def test_stamps_are_monotonic():
    async def main():
        ws = FakeSocket()
        pipe = sp.SyncPipeline(lambda: ws, name="stamps")
        n1 = pipe.begin_navigation("https://a/", reason="url_change")
        pipe.publish(n1, control=True)
        pipe.publish(pipe.stamp({"type": "full_document", "url": "https://a/", "html": "<html>"}))
        pipe.publish(pipe.stamp({"type": "dom_patch", "ops": []}))
        n2 = pipe.begin_navigation("https://b/", reason="history:pushState", same_document=True)
        pipe.publish(n2, control=True)
        await pipe.drain(2.0)
        import json
        frames = [json.loads(t) for t in ws.sent]
        seqs = [f["seq"] for f in frames]
        ok(len(set(seqs)) == len(seqs), "two frames share a seq: %r" % (seqs,))
        ok(seqs[0] < seqs[-1], "the second navigation did not advance seq: %r" % (seqs,))
        bulk_seqs = [f["seq"] for f in frames if f["type"] != "navigation"]
        ok(bulk_seqs == sorted(bulk_seqs),
           "bulk frames left in a different order than they were produced: %r" % (bulk_seqs,))
        navs = [f for f in frames if f["type"] == "navigation"]
        bulk = [f for f in frames if f["type"] != "navigation"]
        ok([f["nav_id"] for f in navs] == [1, 2],
           "navigation ids are not monotonic in send order: %r" % (navs,))
        ok([f["nav_id"] for f in bulk] == [1, 1],
           "the document/patch must stay attached to the navigation it was "
           "captured for: %r" % ([f["nav_id"] for f in bulk],))
        ok(navs[1]["same_document"] is True and navs[1]["doc_expected"] is False,
           "a same-document navigation must say so, and must not promise a document: %r"
           % (navs[1],))
        ok(navs[0]["prev_url"] is None and navs[1]["prev_url"] == "https://a/",
           "prev_url should chain the announcements: %r" % (navs,))
        await pipe.close()

    run(main())


def test_phase_frame_belongs_to_its_navigation():
    async def main():
        ws = FakeSocket()
        pipe = sp.SyncPipeline(lambda: ws, name="phase")
        pipe.begin_navigation("https://a/", reason="url_change")
        pipe.publish(pipe.phase_frame("failed", detail="capture_failed"), control=True)
        await pipe.drain(2.0)
        import json
        frame = json.loads(ws.sent[0])
        ok(frame["type"] == "navigation" and frame["phase"] == "failed",
           "the failure phase did not reach the client: %r" % (frame,))
        ok(frame["nav_id"] == 1, "the failure must name the navigation it is about")
        await pipe.close()

    run(main())


# ---------------------------------------------------------------------------
# 4. publish_now waits for ITS frame, not for the queue
# ---------------------------------------------------------------------------
def test_waiting_for_your_own_frame_does_not_wait_for_other_producers():
    async def main():
        ws = FakeSocket(delay=0.01)
        pipe = sp.SyncPipeline(lambda: ws, name="own")
        # Producer A drops a frame nobody is waiting for.
        pipe.publish({"type": "dom_patch", "ops": []})
        t0 = asyncio.get_event_loop().time()
        # Producer B publishes and wants to know ITS frame went out.
        done = await pipe.publish_now({"type": "full_document", "html": "<html>"})
        elapsed = asyncio.get_event_loop().time() - t0
        ok(done is True, "publish_now() reported failure for a frame that was written")
        ok(elapsed < 0.2, "publish_now waited %.0f ms for two 10 ms sends" % (elapsed * 1000))
        ok(ws.types() == ["dom_patch", "full_document"],
           "the frames were not written in publish order: %r" % (ws.types(),))
        await pipe.close()

    run(main())


def test_a_broken_socket_reports_failure_instead_of_hanging():
    async def main():
        class Dead:
            closed = False

            async def send_text(self, text):
                raise RuntimeError("socket is gone")

        pipe = sp.SyncPipeline(lambda: Dead(), name="dead")
        ok(await pipe.publish_now({"type": "full_document"}, timeout=1.0) is False,
           "a failed write must resolve the producer's future, not leave it hanging")
        ok(pipe.errors == 1, "the failure was not counted: %r" % (pipe.stats(),))
        await pipe.close()

    run(main())


def test_no_websocket_bound_is_a_clean_failure():
    async def main():
        pipe = sp.SyncPipeline(lambda: None, name="unbound")
        ok(await pipe.publish_now({"type": "full_document"}, timeout=1.0) is False,
           "no socket: the producer's future must resolve False")
        await pipe.close()

    run(main())


# ---------------------------------------------------------------------------
# 5. bounded backlog: patches are what goes
# ---------------------------------------------------------------------------
def test_backlog_pressure_drops_patches_not_documents():
    async def main():
        ws = FakeSocket(delay=0.02)
        pipe = sp.SyncPipeline(lambda: ws, name="overflow", max_bulk=8)
        pipe.publish({"type": "full_document", "html": "<html>"})
        for i in range(12):
            pipe.publish({"type": "dom_patch", "ops": [["r", str(i)]]})
        ok(pipe.dropped_overflow > 0,
           "the backlog was not bounded (queue %d, cap %d)" % (len(pipe._bulk), pipe.max_bulk))
        ok(any(str(f.get("type")) == "full_document" for f, _ in pipe._bulk),
           "a document was dropped while patches were still queued - the client "
           "loses content it can never recover")
        await pipe.drain(2.0)
        ok(ws.types()[0] == "full_document", "the document must survive the pressure")
        await pipe.close()

    run(main())


# ---------------------------------------------------------------------------
# 6. the writer is a burst task, not a daemon
# ---------------------------------------------------------------------------
def test_writer_exits_when_the_queue_drains():
    async def main():
        ws = FakeSocket()
        pipe = sp.SyncPipeline(lambda: ws, name="burst")
        pipe.publish({"type": "dom_patch", "ops": []})
        ok(pipe._writer is not None, "no writer was started for a queued frame")
        await pipe.drain(2.0)
        ok(pipe._writer is None,
           "the writer stayed alive with an empty queue - an idle session would own "
           "a task forever (and one bound to a dead loop in tests)")
        # ...and a later frame starts a new one.
        pipe.publish({"type": "dom_patch", "ops": []})
        await pipe.drain(2.0)
        ok(len(ws.sent) == 2, "the pipeline stopped accepting frames: %r" % (ws.types(),))
        await pipe.close()

    run(main())


def test_close_flushes_then_stops():
    async def main():
        ws = FakeSocket(delay=0.01)
        pipe = sp.SyncPipeline(lambda: ws, name="close")
        pipe.publish({"type": "full_document", "html": "<html>"})
        await pipe.close()
        ok(ws.types() == ["full_document"],
           "close() must keep the promise that queued frames still go out: %r" % (ws.types(),))
        ok(pipe.publish({"type": "dom_patch"}) is False,
           "a closed pipeline must refuse frames instead of queueing them forever")

    run(main())


# ---------------------------------------------------------------------------
# 7. reason -> lane mapping (the independent capture lanes)
# ---------------------------------------------------------------------------
def test_lane_mapping():
    cases = {
        "navigation": sp.LANE_NAVIGATION,
        "url_change": sp.LANE_NAVIGATION,
        "goto": sp.LANE_NAVIGATION,
        "tab_switch": sp.LANE_NAVIGATION,
        "nav_settle": sp.LANE_NAVIGATION,
        "coalesce:navigation": sp.LANE_NAVIGATION,
        "interaction:click:button": sp.LANE_CONTENT,
        "click": sp.LANE_CONTENT,
        "delta_overflow": sp.LANE_CONTENT,
        "nav_settle_but_not_really": sp.LANE_NAVIGATION,
        "": sp.LANE_CONTENT,
        None: sp.LANE_CONTENT,
    }
    for reason, expected in cases.items():
        got = sp.lane_for_reason(reason)
        ok(got == expected, "reason %r mapped to %s, expected %s" % (reason, got, expected))


def test_url_equality_ignores_a_trailing_hash():
    ok(sp.same_url("https://a/inbox", "https://a/inbox#"), "empty fragment is the same URL")
    ok(not sp.same_url("https://a/in#one", "https://a/in#two"), "different fragments differ")
    ok(not sp.same_url(None, "https://a/"), "None is not a URL match")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("sync pipeline: %d checks passed" % PASSED)
