"""Concurrent, non-blocking delivery pipeline for the live mirror.

Why this exists
---------------
The mirror used to be one serialised chain: *detect a change -> wait for the
page to settle -> serialize the document -> rewrite its assets -> send*.  Every
link waited for the one in front of it, and the client sat on a transition
spinner for the whole chain even when the only thing it needed was the new URL.

That shape is wrong for the job.  The four things a mirrored page needs to say
are independent facts that become available at different times:

    1. where the page is now          (navigation)  - available *immediately*
    2. what its DOM looks like        (document)    - one serialize away
    3. what changed since            (patch)       - from the in-page observer
    4. what its assets resolve to    (asset_map)   - after network fetches

Nothing about (1) depends on (2), nothing about (2) depends on (4).  This
module is the delivery half of that observation: a small, ordered, priority
aware writer that a producer can hand a frame to and immediately walk away
from.  Producers never await each other, and a producer that is slow to finish
never holds a frame another producer has already produced.

The three rules
---------------
**Capture independently.**  :class:`SyncPipeline` knows nothing about browsers;
it only moves frames.  The independent *capture* lanes live next to the caller
(``DOMCaptureSession``) and are keyed by :func:`lane_for_reason`, so a
navigation capture does not queue behind an interaction capture of the page
being navigated away from.

**Deliver immediately.**  :meth:`publish` is synchronous and never blocks: it
enqueues and returns.  One writer task owns the socket (websockets do not
tolerate interleaved sends), so frames leave in a well-defined order without
any producer taking a lock.

**Apply progressively, and never let a slow thing block a fast one.**  Frames
carry two ordering stamps:

    ``nav_id``  - which navigation the frame belongs to (monotonic)
    ``seq``     - the order the producers handed their frames over (monotonic)

``seq`` is assigned at publish time and never rewritten, so a frame's number
says when its producer had it ready - not when the socket wrote it.  Control
frames may therefore leave with a number higher than a bulk frame still queued
behind them; that is the priority rule doing its job, and it is safe because a
navigation frame carries no document content.  Ordering decisions on the client
use ``nav_id`` (a newer navigation always wins), never ``seq``.

A control frame (navigation) may overtake *queued* bulk frames - it carries no
document content, so the only thing overtaking can cause is the client
discarding a document it had already been told was superseded.  Bulk frames
keep their FIFO order among themselves, which is what makes patches safe to
apply in arrival order.

The counterweight to all of that is de-duplication, not suppression: the
pipeline will drop a frame in exactly two places, and both are provable
no-ops for the client (see :meth:`publish`).
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, Optional

logger = logging.getLogger(__name__)

# Priority classes.  Control frames carry "this page moved / this capture
# failed" and are worth nothing unless they arrive first; bulk frames carry
# content and must stay in generation order among themselves.
CONTROL = "control"
BULK = "bulk"

# Independent capture lanes.  One capture may be in flight per lane, so a
# navigation never queues behind an interaction recapture of the page the user
# just left.
LANE_NAVIGATION = "navigation"
LANE_CONTENT = "content"

# Reasons that belong to the navigation lane.  Kept next to the lane logic
# rather than in the capture module so the mapping is testable on its own.
_NAVIGATION_REASON_PREFIXES = ("navigation", "nav_", "url_change", "history")
_NAVIGATION_REASONS = frozenset({
    "goto", "reload", "back", "forward", "tab_switch", "redirect",
    "document", "location", "url", "resync", "mirror_err", "recover",
    "desync",
})

# Frame types that make up the navigation pipeline.
_CONTROL_TYPES = frozenset({"navigation", "navigation_phase", "page_transition"})


def lane_for_reason(reason: Optional[str]) -> str:
    """Which capture lane a capture reason belongs to.

    Navigation reasons get their own lane: they are triggered by something the
    user just did, they invalidate anything older, and they must never wait for
    an interaction recapture of the page being left behind.
    """
    r = str(reason or "").strip().lower()
    if not r:
        return LANE_CONTENT
    if r.startswith("coalesce:"):
        r = r[len("coalesce:"):]
    if r in _NAVIGATION_REASONS:
        return LANE_NAVIGATION
    for prefix in _NAVIGATION_REASON_PREFIXES:
        if r.startswith(prefix):
            return LANE_NAVIGATION
    return LANE_CONTENT


def same_url(a: Optional[str], b: Optional[str]) -> bool:
    """URL equality that ignores a trailing empty fragment.

    ``/inbox`` and ``/inbox#`` are the same document in a browser; treating
    them as different made the URL watcher re-capture on every history
    round-trip.
    """
    if not a or not b:
        return False
    return str(a).rstrip("#") == str(b).rstrip("#")


class RawFrame:
    """A frame that is already serialized.

    The delta observer's batch arrives as ``JSON.stringify(...)`` from the page;
    parsing it in Python only to dump it again cost CPU and memory on every
    patch.  A RawFrame carries that text (plus its type, for priority and
    eviction decisions) through the queue untouched.
    """

    __slots__ = ("text", "kind")

    def __init__(self, text: str, kind: str = "dom_patch") -> None:
        self.text = text
        self.kind = kind

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"RawFrame({self.kind}, {len(self.text)} bytes)"


def _kind_of(frame: Any) -> str:
    if isinstance(frame, RawFrame):
        return frame.kind
    if isinstance(frame, dict):
        return str(frame.get("type") or "")
    return ""


class SyncPipeline:
    """Ordered, non-blocking frame delivery for one mirrored client.

    Usage::

        pipe = SyncPipeline(lambda: session.websocket, name="dom:c1")
        nav = pipe.begin_navigation(url, reason="history:pushState",
                                    same_document=True)
        pipe.publish(nav)                  # control: goes out first
        pipe.publish(pipe.stamp({"type": "dom_patch", "ops": ops}))
        await pipe.drain()                 # tests / shutdown only
        await pipe.close()

    ``get_websocket`` is a callable (not a socket) on purpose: a session can
    rebind its websocket when the client reconnects or the active tab changes,
    and the writer must follow that without being rebuilt.
    """

    def __init__(
        self,
        get_websocket: Optional[Callable[[], Any]] = None,
        *,
        sender: Optional[Callable[[Dict[str, Any]], Any]] = None,
        name: str = "",
        max_bulk: int = 256,
        max_control: int = 64,
    ) -> None:
        self._get_websocket = get_websocket
        self._sender = sender
        self.name = name or "sync"
        self.max_bulk = max(8, int(max_bulk))
        self.max_control = max(8, int(max_control))

        self._control: Deque[Any] = deque()
        self._bulk: Deque[Any] = deque()
        self._writer: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._owner_thread: Optional[int] = None
        self._closed = False
        self._sending = False

        # ---- ordering stamps -------------------------------------------
        self.seq: int = 0            # total frame order
        self.nav_id: int = 0         # which navigation we are on
        self.nav_url: Optional[str] = None
        self.prev_nav_url: Optional[str] = None
        self.nav_same_document: bool = False
        self.nav_doc_expected: bool = True
        self.nav_phase: str = "idle"
        self.nav_t: float = 0.0

        # ---- observability ---------------------------------------------
        self.sent: int = 0
        self.errors: int = 0
        self.dropped_overflow: int = 0
        self.dropped_unbound: int = 0

    # ------------------------------------------------------------------
    # Ordering stamps
    # ------------------------------------------------------------------
    def stamp(self, msg: Dict[str, Any], *, nav_id: Optional[int] = None,
              seq: Optional[int] = None) -> Dict[str, Any]:
        """Stamp a frame with its total order and the navigation it belongs to.

        ``seq`` is deliberately assigned at *publish* time, not at capture
        time: the client only needs to know the order things were sent in.
        """
        if seq is None:
            self.seq += 1
            seq = self.seq
        msg["seq"] = seq
        if nav_id is None:
            nav_id = self.nav_id
        if nav_id is not None:
            msg["nav_id"] = nav_id
        return msg

    def begin_navigation(
        self,
        url: Optional[str],
        *,
        prev_url: Optional[str] = None,
        same_document: bool = False,
        doc_expected: Optional[bool] = None,
        reason: str = "",
        source: str = "",
        phase: str = "detected",
        doc: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Open a new navigation and return its (already stamped) frame.

        Callers publish the frame immediately, then start their capture.  The
        returned frame is what the client uses to move its tab, its URL and -
        when a new document really is coming - its transition, all without
        waiting for a byte of DOM.
        """
        self.nav_id += 1
        self.prev_nav_url = self.nav_url
        self.nav_url = str(url) if url else None
        self.nav_same_document = bool(same_document)
        self.nav_doc_expected = (not same_document) if doc_expected is None else bool(doc_expected)
        self.nav_phase = phase or "detected"
        self.nav_t = time.time()

        frame: Dict[str, Any] = {
            "type": "navigation",
            "phase": self.nav_phase,
            "nav_id": self.nav_id,
            "url": self.nav_url,
            "prev_url": prev_url if prev_url is not None else self.prev_nav_url,
            "same_document": self.nav_same_document,
            "doc_expected": self.nav_doc_expected,
            "reason": reason,
        }
        if source:
            frame["source"] = source
        if doc:
            frame["doc"] = str(doc)[:256]
        return self.stamp(frame, nav_id=self.nav_id)

    def phase_frame(self, phase: str, *, nav_id: Optional[int] = None,
                    url: Optional[str] = None, detail: Optional[str] = None) -> Dict[str, Any]:
        """A lifecycle frame for an already-open navigation.

        Used for the two phases only the server can know about: the document
        committed (a capture is on its way but nothing is wrong) and failed
        (a capture gave up - the client must not keep a spinner up for it).
        """
        frame: Dict[str, Any] = {
            "type": "navigation",
            "phase": phase,
            "nav_id": self.nav_id if nav_id is None else nav_id,
            "url": url if url is not None else self.nav_url,
            "same_document": self.nav_same_document,
            "doc_expected": self.nav_doc_expected,
        }
        if detail:
            frame["detail"] = str(detail)[:200]
        return self.stamp(frame, nav_id=frame["nav_id"])

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------
    @property
    def pending(self) -> int:
        return len(self._control) + len(self._bulk) + (1 if self._sending else 0)

    def publish(self, msg: Dict[str, Any], *, control: Optional[bool] = None,
                wait: bool = False) -> Any:
        """Enqueue a frame.  Never awaits, never blocks, never raises.

        Returns True/False when ``wait`` is False (False only when the frame
        could not be taken at all — no running loop, no websocket, or a closed
        pipeline), or a Future that resolves when THIS frame has been written
        when ``wait`` is True.

        ``wait`` is for frames whose producer is already finished with its work
        (the full document, the asset edit list): it waits for that one frame,
        not for the queue, so a producer still never waits on another producer.
        Fast-path control frames (navigation) never pass it.
        """
        if self._closed or not isinstance(msg, (dict, RawFrame)):
            return False if not wait else None
        if control is None:
            control = self._is_control(msg)
        if not self._ensure_writer():
            self.dropped_unbound += 1
            return False if not wait else None
        loop = self._loop
        fut: Optional[asyncio.Future] = loop.create_future() if (wait and loop) else None
        if loop is not None and threading.get_ident() != self._owner_thread:
            # A browser callback landed on another thread (the SB backend runs
            # bindings off-thread on some builds).  Hand the frame to the
            # owning loop instead of touching its deques from here.
            try:
                loop.call_soon_threadsafe(self._enqueue, msg, bool(control), fut)
                return fut if wait else True
            except RuntimeError:
                return False if not wait else None
        self._enqueue(msg, bool(control), fut)
        return fut if wait else True

    async def publish_now(self, msg: Dict[str, Any], *, control: Optional[bool] = None,
                          timeout: float = 10.0) -> bool:
        """Publish a frame and wait for that frame alone to be written."""
        fut = self.publish(msg, control=control, wait=True)
        if fut is None:
            return False
        if isinstance(fut, bool):
            return fut
        try:
            return bool(await asyncio.wait_for(fut, timeout))
        except (asyncio.TimeoutError, asyncio.CancelledError):
            return False
        except Exception:
            return False

    def publish_navigation(self, url: Optional[str], **kwargs: Any) -> Optional[Dict[str, Any]]:
        """Open + publish a navigation in one call.  Returns the frame."""
        frame = self.begin_navigation(url, **kwargs)
        if not self.publish(frame, control=True):
            return None
        return frame

    def _is_control(self, msg: Any) -> bool:
        return _kind_of(msg) in _CONTROL_TYPES

    def next_seq(self) -> int:
        """Reserve the next send-order number.

        For RawFrame producers, which build their own text and therefore need
        the number before the frame is enqueued.
        """
        self.seq += 1
        return self.seq

    async def publish_text(self, text: str, *, kind: str = "dom_patch",
                           control: Optional[bool] = None,
                           timeout: float = 10.0) -> bool:
        """Publish pre-serialized JSON text, waiting for that frame alone."""
        frame = RawFrame(text, kind)
        fut = self.publish(frame, control=control, wait=True)
        if fut is None:
            return False
        if isinstance(fut, bool):
            return fut
        try:
            return bool(await asyncio.wait_for(fut, timeout))
        except (asyncio.TimeoutError, asyncio.CancelledError):
            return False
        except Exception:
            return False

    def _enqueue(self, msg: Dict[str, Any], control: bool,
                 fut: Optional[asyncio.Future] = None) -> None:
        if self._closed:
            if fut is not None and not fut.done():
                fut.set_result(False)
            return
        q = self._control if control else self._bulk
        limit = self.max_control if control else self.max_bulk
        if len(q) >= limit:
            self._evict(control)
            q = self._control if control else self._bulk
        q.append((msg, fut))

    def _evict(self, control: bool) -> None:
        """Make room by dropping the frame that costs the client least.

        A patch or an asset edit is by definition superseded by the next
        document and is safe to skip.  A document is not, so it is only
        dropped when the queue is nothing but documents - and that is logged,
        because it means the socket is slower than the capture loop.
        """
        q = self._control if control else self._bulk
        if not control:
            for i, (frame, fut) in enumerate(q):
                if _kind_of(frame) in ("dom_patch", "asset_map", "region_replace"):
                    del q[i]
                    self.dropped_overflow += 1
                    if fut is not None and not fut.done():
                        fut.set_result(False)
                    return
        dropped = q.popleft() if q else None
        if dropped is not None:
            self.dropped_overflow += 1
            frame, fut = dropped
            if fut is not None and not fut.done():
                fut.set_result(False)
            logger.warning(
                "sync pipeline %s: delivery backlog exceeded %d frames; dropped "
                "oldest %s frame (queue is not keeping up with capture)",
                self.name, self.max_control if control else self.max_bulk,
                _kind_of(frame),
            )

    # ------------------------------------------------------------------
    # Writer
    # ------------------------------------------------------------------
    def _ensure_writer(self) -> bool:
        """Make sure a writer task is draining the queue on this loop.

        The writer is a BURST task, not a daemon: it exists while there is
        something to send and returns when the queue is empty.  That keeps the
        pipeline loop-agnostic (nothing long-lived is bound to a loop that may
        be torn down), and it means an idle mirrored session owns no tasks at
        all.  ``publish`` is synchronous, so the "is the writer finished?"
        check and the decision to start one cannot interleave with the writer's
        own exit check.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No event loop yet (constructed during import, or called from a
            # teardown path).  Nothing can be delivered; the caller retries on
            # the next capture, which will be inside a loop.
            return False
        if self._closed:
            return False
        if self._writer is not None and not self._writer.done() and self._loop is loop:
            return True
        if self._writer is not None and not self._writer.done():
            # A writer from a previous loop (one-shot loops in tests, or a
            # recycled process).  Abandon it; frames it left queued are
            # delivered by the new one.
            try:
                self._writer.cancel()
            except Exception:
                pass
        self._loop = loop
        self._owner_thread = threading.get_ident()
        self._writer = loop.create_task(self._writer_loop(),
                                        name=f"sync-writer:{self.name}")
        return True

    async def _writer_loop(self) -> None:
        try:
            while True:
                if not self._control and not self._bulk:
                    # Nothing left: hand the loop back.  No await between the
                    # check and the references being dropped, so a publish can
                    # never slip into a queue nobody is draining.
                    self._writer = None
                    return
                msg, fut = self._control.popleft() if self._control else self._bulk.popleft()
                self._sending = True
                ok = False
                try:
                    await self._deliver(msg)
                    self.sent += 1
                    ok = True
                except asyncio.CancelledError:
                    if fut is not None and not fut.done():
                        fut.set_result(False)
                    raise
                except Exception as exc:  # socket gone, serialization, ...
                    self.errors += 1
                    logger.debug("sync pipeline %s: send failed for %s: %s",
                                 self.name, _kind_of(msg), exc)
                finally:
                    self._sending = False
                    if fut is not None and not fut.done():
                        fut.set_result(ok)
        except asyncio.CancelledError:
            raise
        finally:
            self._sending = False

    async def _deliver(self, msg: Any) -> None:
        if self._sender is not None:
            payload = msg.text if isinstance(msg, RawFrame) else msg
            result = self._sender(payload)
            if hasattr(result, "__await__"):
                await result
            return
        ws = self._get_websocket() if self._get_websocket is not None else None
        if ws is None:
            raise RuntimeError("no websocket bound")
        if getattr(ws, "closed", False):
            raise RuntimeError("websocket closed")
        text = msg.text if isinstance(msg, RawFrame) else json.dumps(msg, ensure_ascii=False)
        await ws.send_text(text)

    async def drain(self, timeout: float = 5.0) -> bool:
        """Wait until everything enqueued so far has been written.

        Used by shutdown and by tests.  Never used on a hot path: waiting is
        exactly what this module exists to avoid.
        """
        loop = asyncio.get_event_loop()
        deadline = loop.time() + max(0.0, timeout)
        while self.pending:
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(0)
        return True

    async def close(self, timeout: float = 2.0) -> None:
        """Flush what is queued (best effort), then stop the writer."""
        try:
            await self.drain(timeout)
        except Exception:
            pass
        self._closed = True
        for q in (self._control, self._bulk):
            while q:
                _msg, fut = q.popleft()
                if fut is not None and not fut.done():
                    fut.set_result(False)
        writer = self._writer
        self._writer = None
        if writer is not None and not writer.done():
            writer.cancel()
            try:
                await writer
            except (asyncio.CancelledError, Exception):
                pass

    def stats(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "nav_id": self.nav_id,
            "seq": self.seq,
            "queued_control": len(self._control),
            "queued_bulk": len(self._bulk),
            "sent": self.sent,
            "errors": self.errors,
            "dropped_overflow": self.dropped_overflow,
            "dropped_unbound": self.dropped_unbound,
            "nav_url": self.nav_url,
            "nav_same_document": self.nav_same_document,
        }


__all__ = [
    "SyncPipeline",
    "RawFrame",
    "CONTROL",
    "BULK",
    "LANE_NAVIGATION",
    "LANE_CONTENT",
    "lane_for_reason",
    "same_url",
]
