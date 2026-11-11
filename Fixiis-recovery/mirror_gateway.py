"""Mirror session gateway — page distribution, navigation generations and viewer fan-out.

This module is the seam between our capture engine and the sockets that carry
it.  It is a deliberate port of the architecture in ``mystxcal/remote-browser``
(``packages/gateway/src/hub/tabhub.ts``, ``ws/viewerconn.ts``, ``ws/server.ts``,
``browser/nav.ts``, ``viewer/src/resync.ts``) onto our DOM protocol.  Remote
Browser records with rrweb; **we do not** — ``dom_capture.py`` stays the only
recorder.  What is adopted is everything around the recorder:

* a per-tab hub that owns the canonical snapshot plus a **bounded ordered delta
  tail**, so a newly connected viewer is caught up from state instead of waiting
  for the next full capture (``MirrorHub.join_payload``);
* ``epoch`` + ``seq`` semantics: every frame carries document identity and a
  monotonic per-tab sequence; a gap is divergence, and divergence is repaired
  by **retransmit from history first**, by a fresh snapshot only if the tail is
  no longer contiguous;
* slow-viewer handling: check the backlog *before* sending, stall the viewer,
  drop deltas instead of growing a queue, resume with a resync — a stalled
  viewer costs O(1) memory and never slows the capture pipeline;
* document lifecycle: navigation opens a new ``document_id``, frames stamped for
  another document are dropped at the hub, and same-document URL changes update
  the chrome without an epoch bump (SPA routing must never rebuild the mirror);
* input gating: forwarded input from a viewer that has not applied the current
  epoch is dropped — the whole "clicked on a stale layout" bug class.

No FastAPI, no Playwright, no third-party imports: the hub is pure asyncio so a
test harness can drive it with a fake socket and a synthetic capture stream.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Frame kinds (``message_type`` on the wire, ``kind`` in-process)
# ---------------------------------------------------------------------------

#: A complete document. Opens an epoch; clears the delta tail.
KIND_SNAPSHOT = "snapshot"
#: Ordered DOM patch ops for the current document.
KIND_DELTA = "delta"
#: Progressive asset edits for a document that shipped before its assets.
KIND_ASSET = "asset"
#: Navigation chrome state: url / loading / canBack / canFwd / error.
KIND_CHROME = "chrome"
#: "Discard everything you hold for this tab; a snapshot follows."
KIND_RESYNC = "resync"
#: Retransmission of frames the hub still holds in its bounded history.
KIND_REPLAY = "replay"
#: Everything that is not mirror state (LPV events, chat, permissions, …).
KIND_CONTROL = "control"

#: Wire names a pre-gateway client already understands. ``full_document`` is our
#: snapshot and ``dom_patch`` our delta, so both names keep being emitted and an
#: old viewer keeps working against the new server.
WIRE_NAMES: Dict[str, str] = {
    KIND_SNAPSHOT: "full_document",
    KIND_DELTA: "dom_patch",
    KIND_ASSET: "asset_map",
    KIND_CHROME: "mirror_chrome",
    KIND_RESYNC: "mirror_resync",
    KIND_REPLAY: "mirror_replay",
    KIND_CONTROL: "mirror_control",
}

#: Kind a viewer can lose and recover from. A snapshot, chrome or resync frame
#: is structural: skipping one of those silently desynchronizes a tab, so they
#: are never dropped for backpressure — a viewer that cannot take them is
#: stalled instead.
DROPPABLE_KINDS = frozenset({KIND_DELTA, KIND_ASSET})

#: Kinds recorded in the bounded delta history (retransmittable state).
HISTORICAL_KINDS = frozenset({KIND_DELTA, KIND_ASSET})

# Backpressure thresholds, from remote-browser's viewerconn.ts: they are sized
# for a socket kernel buffer, not for our page sizes, which is the point — the
# number that matters is "the pipe is full", not "the page is big".
STALL_BYTES = 8 * 1024 * 1024
RESUME_BYTES = 1 * 1024 * 1024

# Resync storm policy (resync.ts): bounded exponential backoff, and once the
# window budget is spent the tab degrades instead of thrashing the capture
# pipeline forever.
RESYNC_WINDOW_S = 30.0
MAX_RESYNCS_PER_WINDOW = 3
RESYNC_BACKOFF_BASE_S = 0.25
RESYNC_BACKOFF_MAX_S = 5.0


def new_document_id() -> str:
    """Identity for one document in one tab.

    Remote Browser's agent keeps a per-document ``docId`` counter and the
    gateway drops anything stamped for another id.  We mint an opaque id per new
    document so the same rule survives the three cases a URL comparison cannot
    tell apart: a reload that keeps the URL, back/forward traversal, and
    ``pushState`` on the same document.
    """
    return uuid.uuid4().hex[:16]


# ---------------------------------------------------------------------------
# Frames
# ---------------------------------------------------------------------------

@dataclass
class MirrorFrame:
    """One outbound mirror message, in the shape the hub owns.

    ``raw`` holds the already-escaped JSON *text* of body fields the producer
    serialized in the page (a ``dom_patch`` op array) or in the capture tier (a
    megabyte ``html`` blob).  Splicing that text is why one snapshot costs
    exactly one serialization; re-parsing the page's JSON in Python and dumping
    it again is the CPU cost the capture pipeline was explicitly built to avoid.
    """

    kind: str
    payload: Dict[str, Any] = field(default_factory=dict)
    raw: Dict[str, str] = field(default_factory=dict)
    seq: int = 0
    epoch: int = 0
    document_id: str = ""
    tab_id: str = ""
    session_id: str = ""
    #: Producer-side stage timings, forwarded verbatim for §9 measurement.
    timing: Dict[str, float] = field(default_factory=dict)
    text: Optional[str] = None
    created_at: float = field(default_factory=time.monotonic)
    #: Wall-clock millisecond stamp, for the viewer's end-to-end measurement.
    #: ``created_at`` is monotonic and cannot be compared against anything on the
    #: other machine; this one can.  The viewer still reports the number as
    #: skew-corrected rather than exact, because clock skew is real and unmeasured.
    wall_ts: float = field(default_factory=lambda: time.time() * 1000.0)
    #: Number of DOM operations the frame carries (history budget accounting).
    event_count: int = 1
    #: ``"nav"`` for frames that came from a navigation, ``"update"`` for
    #: ordinary DOM churn. Navigation is measured separately so a 900 ms page
    #: switch cannot pollute the number for a keystroke.
    lane: str = "update"

    def wire_type(self) -> str:
        return WIRE_NAMES.get(self.kind, self.kind)

    def size(self) -> int:
        return len(self.text) if self.text is not None else 0

    def to_dict(self) -> Dict[str, Any]:
        """The flat wire dict: identity fields plus the body at the top level.

        The body stays flat on purpose.  Duplicating a multi-megabyte ``html``
        under a ``payload`` alias would double the frame, so ``payload`` carries
        the body only for the small hub-native kinds; ``client.html`` normalizes
        either shape to the same internal frame.
        """
        out: Dict[str, Any] = {
            "type": self.wire_type(),
            "message_type": self.kind,
            "session_id": self.session_id,
            "tab_id": self.tab_id,
            "document_id": self.document_id,
            "epoch": self.epoch,
            "seq": self.seq,
        }
        if self.timing:
            out["timing"] = dict(self.timing)
        out["ts"] = self.wall_ts
        if self.kind in (KIND_SNAPSHOT, KIND_DELTA, KIND_ASSET, KIND_REPLAY, KIND_RESYNC):
            # ``gen`` is the pre-gateway generation counter, kept as an alias so
            # a cached client's stale-frame guard needs no change.
            out["gen"] = self.epoch
        for key, value in self.payload.items():
            out[key] = value
        if self.kind in (KIND_CHROME, KIND_RESYNC, KIND_REPLAY, KIND_CONTROL):
            out["payload"] = dict(self.payload)
        return out

    def encode(self) -> str:
        """Serialize once.  Called by the hub's fan-out, never per viewer."""
        if self.text is not None:
            return self.text
        body = self.to_dict()
        if not self.raw:
            self.text = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
            return self.text
        head = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        tail = ",".join(f'"{key}":{text}' for key, text in self.raw.items())
        sep = "" if head == "{}" else ","
        self.text = head[:-1] + sep + tail + "}"
        return self.text


# ---------------------------------------------------------------------------
# Metrics (§9 — every stage measured, navigation kept separate)
# ---------------------------------------------------------------------------

class StageCounter:
    """Count / mean / max / last for one pipeline stage, in O(1) memory."""

    __slots__ = ("count", "total_ms", "max_ms", "bytes_", "last_ms")

    def __init__(self) -> None:
        self.count = 0
        self.total_ms = 0.0
        self.max_ms = 0.0
        self.bytes_ = 0
        self.last_ms = 0.0

    def record(self, ms: float, bytes_: int = 0) -> None:
        if ms != ms or ms < 0:      # NaN/negative guard: one bad clock sample
            return                   # must not poison a mean anyone reads
        self.count += 1
        self.total_ms += ms
        if ms > self.max_ms:
            self.max_ms = ms
        self.last_ms = ms
        self.bytes_ += bytes_

    def as_dict(self) -> Dict[str, Any]:
        return {
            "count": self.count,
            "mean_ms": round(self.total_ms / self.count, 2) if self.count else 0.0,
            "max_ms": round(self.max_ms, 2),
            "last_ms": round(self.last_ms, 2),
            "bytes": self.bytes_,
        }


class PipelineMetrics:
    """Stage timings for the whole pipeline, bucketed by lane.

    Stages: ``capture`` (Browser A serialization), ``rewrite`` (asset pass),
    ``serialize`` (envelope), ``queue`` (publish → admitted to the viewer's
    bounded queue), ``send`` (socket write), and the viewer-reported
    ``receive`` / ``patch`` / ``paint`` plus the end-to-end ``paint`` span as
    ``end_to_end``.  A number is only ever claimed here if a sample recorded it.
    """

    STAGES = ("capture", "rewrite", "serialize", "queue", "send",
              "receive", "patch", "paint", "end_to_end")

    def __init__(self) -> None:
        self._lanes: Dict[str, Dict[str, StageCounter]] = {
            lane: {stage: StageCounter() for stage in self.STAGES}
            for lane in ("nav", "update", "any")
        }
        self.counters: Dict[str, int] = {
            "frames_published": 0,
            "frames_delivered": 0,
            "frames_dropped_stale_doc": 0,
            "frames_dropped_stalled": 0,
            "frames_dropped_queue_full": 0,
            "frames_dropped_replayed": 0,
            "snapshots": 0,
            "deltas": 0,
            "retransmit_frames": 0,
            "resyncs": 0,
            "resync_degrades": 0,
            "viewer_stalls": 0,
            "history_trims": 0,
            "input_gated": 0,
        }

    def stage(self, stage: str, ms: float, *, lane: str = "any", bytes_: int = 0) -> None:
        if stage not in self.STAGES:
            return
        for key in (lane, "any"):
            bucket = self._lanes.get(key)
            if bucket is not None:
                bucket[stage].record(ms, bytes_)

    def bump(self, name: str, amount: int = 1) -> None:
        if amount <= 0:
            return
        self.counters[name] = self.counters.get(name, 0) + int(amount)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "counters": dict(self.counters),
            "lanes": {
                lane: {stage: counter.as_dict() for stage, counter in stages.items()
                       if counter.count}
                for lane, stages in self._lanes.items()
            },
        }


# ---------------------------------------------------------------------------
# Resync budget (shared arithmetic for hub and viewer)
# ---------------------------------------------------------------------------

class ResyncBudget:
    """Bounded exponential backoff with a rolling window, per tab.

    Ported from ``packages/viewer/src/resync.ts``: the first request leaves
    immediately, later ones wait 250/500/1000 ms, and the fourth request inside
    a 30 s window stops asking and degrades — an endless resync loop is worse
    for everybody than a mirror that says "live updates paused".
    """

    def __init__(self, *, window_s: float = RESYNC_WINDOW_S,
                 max_per_window: int = MAX_RESYNCS_PER_WINDOW,
                 backoff_base_s: float = RESYNC_BACKOFF_BASE_S,
                 backoff_max_s: float = RESYNC_BACKOFF_MAX_S,
                 now: Callable[[], float] = time.monotonic) -> None:
        self.window_s = float(window_s)
        self.max_per_window = int(max_per_window)
        self.backoff_base_s = float(backoff_base_s)
        self.backoff_max_s = float(backoff_max_s)
        self._now = now
        self._sent: List[float] = []
        self.total = 0
        self.degraded = False
        self.pending = False

    def _prune(self) -> None:
        cutoff = self._now() - self.window_s
        while self._sent and self._sent[0] <= cutoff:
            self._sent.pop(0)

    def backoff_s(self) -> float:
        self._prune()
        if not self._sent:
            return 0.0
        delay = self.backoff_base_s * float(2 ** (len(self._sent) - 1))
        return min(self.backoff_max_s, delay)

    def can_request(self) -> bool:
        if self.degraded:
            return False
        self._prune()
        return len(self._sent) < self.max_per_window

    def note_sent(self) -> None:
        self._sent.append(self._now())
        self.total += 1

    def should_degrade(self) -> bool:
        self._prune()
        return (not self.degraded) and len(self._sent) >= self.max_per_window

    def degrade(self) -> None:
        self.degraded = True

    def reset(self) -> None:
        """An epoch landed: the divergence is over, so forget the storm."""
        self._sent.clear()
        self.degraded = False
        self.pending = False

    def as_dict(self) -> Dict[str, Any]:
        self._prune()
        return {
            "recent": len(self._sent),
            "total": self.total,
            "degraded": self.degraded,
            "pending": self.pending,
        }


# ---------------------------------------------------------------------------
# Viewer connection
# ---------------------------------------------------------------------------

class MirrorViewer:
    """One Browser B (or admin mirror) attached to a hub.

    The capture pipeline offers a serialized frame into a **bounded** queue and
    returns; this class's pump task is the only thing that awaits a socket
    write.  That is the entire backpressure story: a viewer on a bad connection
    cannot slow the recording browser down, and the worst it can do to itself is
    fall far enough behind to need a resync.
    """

    def __init__(self, viewer_id: str, sink: Any, *, hub: "MirrorHub",
                 max_queue: int = 512, stall_bytes: int = STALL_BYTES,
                 resume_bytes: int = RESUME_BYTES, drain_poll_s: float = 0.03,
                 send_timeout_s: float = 5.0) -> None:
        self.viewer_id = viewer_id
        self.sink = sink
        self.hub = hub
        self.max_queue = int(max_queue)
        self.stall_bytes = int(stall_bytes)
        self.resume_bytes = int(resume_bytes)
        self.drain_poll_s = float(drain_poll_s)
        self.send_timeout_s = float(send_timeout_s)
        self.queue: "asyncio.Queue[Optional[str]]" = asyncio.Queue(maxsize=self.max_queue)
        self.stalled = False
        self.closed = False
        self.joined = False
        #: Replay cursor — what this viewer has been told to apply.
        self.cursor_epoch: int = -1
        self.cursor_next_seq: int = 0
        #: Epoch the viewer has acked; drives input gating.
        self.acked_epoch: int = -1
        self.acked_at: float = 0.0
        self.sent_frames = 0
        self.dropped_frames = 0
        self.last_sent_at: float = 0.0
        self.resync = ResyncBudget()
        self._pump: Optional[asyncio.Task] = None
        self._resume: Optional[asyncio.Task] = None

    # -- lifecycle -------------------------------------------------------
    def start(self) -> None:
        if self._pump is None or self._pump.done():
            self._pump = asyncio.create_task(self._pump_loop())

    async def close(self) -> None:
        self.closed = True
        for task in (self._resume, self._pump):
            if task is not None and not task.done():
                task.cancel()
        self._resume = None
        self._pump = None

    # -- producer side (never awaits the socket) -------------------------
    def buffered_amount(self) -> int:
        try:
            return int(getattr(self.sink, "buffered_amount", 0) or 0)
        except Exception:
            return 0

    def backlog(self) -> int:
        """Rough bytes already owed to this viewer (queue + kernel buffer)."""
        return self.buffered_amount() + self.queue.qsize() * 4096

    def offer(self, frame: MirrorFrame) -> bool:
        """Enqueue ``frame``.  Returns False when it was dropped, never blocks."""
        if self.closed:
            return False
        # Replay suppression: a viewer that already applied these sequences
        # must not be handed them again (a stall-then-resume is the case this
        # exists for; remote-browser's per-viewer cursor does the same job).
        if frame.kind in (KIND_DELTA, KIND_ASSET) and self.cursor_epoch == frame.epoch:
            if frame.seq + max(1, frame.event_count) <= self.cursor_next_seq:
                self.hub.metrics.bump("frames_dropped_replayed")
                return False
        text = frame.encode()
        if self.stalled and frame.kind in DROPPABLE_KINDS:
            self.dropped_frames += 1
            self.hub.metrics.bump("frames_dropped_stalled")
            return False
        if frame.kind == KIND_SNAPSHOT:
            # A snapshot supersedes everything queued for the state it replaces.
            self._drain_superseded()
        try:
            self.queue.put_nowait(text)
        except asyncio.QueueFull:
            if frame.kind in DROPPABLE_KINDS:
                self.dropped_frames += 1
                self.mark_stalled("queue_full")
                self.hub.metrics.bump("frames_dropped_queue_full")
                return False
            # A structural frame must not be lost: shed queued deltas to admit
            # it, which is cheaper and rarer than stalling the whole pipeline.
            if self._drop_droppable_tail(max(4, self.max_queue // 4)):
                try:
                    self.queue.put_nowait(text)
                except asyncio.QueueFull:
                    self.dropped_frames += 1
                    self.mark_stalled("queue_full")
                    self.hub.metrics.bump("frames_dropped_queue_full")
                    return False
            else:
                self.dropped_frames += 1
                self.mark_stalled("queue_full")
                self.hub.metrics.bump("frames_dropped_queue_full")
                return False
        if self.buffered_amount() > self.stall_bytes:
            self.mark_stalled("buffered")
        self.hub.metrics.stage("queue", (time.monotonic() - frame.created_at) * 1000.0,
                               lane=frame.lane, bytes_=len(text))
        return True

    def offer_text(self, text: str) -> bool:
        """Ship a frame that is already serialized (join payloads, retransmits).

        This is the *recovery* path, so a full queue must not stall the viewer
        on the very frames that are catching it up — that oscillates (stall →
        resume → stall) and never converges.  Room is made by discarding queued
        deltas instead; the join payload is already bounded upstream by the
        hub's history limits.
        """
        if self.closed:
            return False
        try:
            self.queue.put_nowait(text)
            return True
        except asyncio.QueueFull:
            self._drop_droppable_tail(max(4, self.max_queue // 4))
            try:
                self.queue.put_nowait(text)
                return True
            except asyncio.QueueFull:
                self.dropped_frames += 1
                self.hub.metrics.bump("frames_dropped_queue_full")
                return False

    def _drain_superseded(self) -> None:
        """Drop queued frames a snapshot is about to replace; keep chrome."""
        drained: List[str] = []
        while True:
            try:
                drained.append(self.queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        if not drained:
            return
        keep = [d for d in drained if '"message_type":"chrome"' in d]
        for item in keep:
            try:
                self.queue.put_nowait(item)
            except asyncio.QueueFull:
                break
        self.dropped_frames += max(0, len(drained) - len(keep))

    def _drop_droppable_tail(self, count: int) -> int:
        """Make room by discarding up to ``count`` queued deltas (recoverable by
        retransmit).  Returns how many were actually dropped."""
        kept: List[str] = []
        while True:
            try:
                kept.append(self.queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        dropped = 0
        for item in kept:
            if dropped < count and '"message_type":"delta"' in item:
                dropped += 1
                self.dropped_frames += 1
                continue
            try:
                self.queue.put_nowait(item)
            except asyncio.QueueFull:
                break
        return dropped

    def mark_stalled(self, why: str) -> None:
        if self.stalled:
            return
        self.stalled = True
        self.hub.metrics.bump("viewer_stalls")
        logger.debug("[HUB] viewer=%s stalled (%s); deltas dropped until it drains",
                     self.viewer_id, why)
        if self._resume is None or self._resume.done():
            self._resume = asyncio.create_task(self._resume_when_drained())

    async def _resume_when_drained(self) -> None:
        while not self.closed:
            await asyncio.sleep(self.drain_poll_s)
            if self.buffered_amount() <= self.resume_bytes and self.queue.empty():
                break
        if self.closed:
            return
        self.stalled = False
        self.hub.resume_viewer(self)

    # -- pump ------------------------------------------------------------
    async def _pump_loop(self) -> None:
        while not self.closed:
            try:
                text = await self.queue.get()
            except asyncio.CancelledError:
                return
            if text is None:
                return
            t0 = time.perf_counter()
            try:
                await asyncio.wait_for(self.sink.send_text(text), timeout=self.send_timeout_s)
                self.sent_frames += 1
                self.last_sent_at = time.monotonic()
                self.hub.metrics.stage("send", (time.perf_counter() - t0) * 1000.0)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                # A failed write means the socket is gone or wedged.  Detach:
                # the endpoint's own loop sees the disconnect, and the hub must
                # never raise into the capture path.
                logger.debug("[HUB] viewer=%s send failed (%s); detaching",
                             self.viewer_id, type(exc).__name__)
                self.closed = True
                try:
                    asyncio.ensure_future(self.hub.detach_viewer(self.viewer_id))
                except Exception:
                    pass
                return

    def advance_cursor(self, frame: MirrorFrame) -> None:
        if frame.kind == KIND_SNAPSHOT:
            self.cursor_epoch = frame.epoch
            self.cursor_next_seq = frame.seq + 1
        elif frame.kind in (KIND_DELTA, KIND_ASSET, KIND_REPLAY):
            self.cursor_epoch = frame.epoch
            self.cursor_next_seq = max(self.cursor_next_seq,
                                       frame.seq + max(1, frame.event_count))
        elif frame.kind == KIND_RESYNC:
            self.cursor_epoch = -1
            self.cursor_next_seq = 0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "viewer_id": self.viewer_id,
            "stalled": self.stalled,
            "joined": self.joined,
            "queued": self.queue.qsize(),
            "sent": self.sent_frames,
            "dropped": self.dropped_frames,
            "buffered_bytes": self.buffered_amount(),
            "acked_epoch": self.acked_epoch,
            "cursor": {"epoch": self.cursor_epoch, "next_seq": self.cursor_next_seq},
            "resync": self.resync.as_dict(),
        }


# ---------------------------------------------------------------------------
# The hub
# ---------------------------------------------------------------------------

SnapshotProvider = Callable[["MirrorHub", str], Any]


class MirrorHub:
    """Per-session, per-tab distribution state for one mirrored document.

    Canonical state = the latest snapshot frame + the ordered delta tail after
    it, bounded by frame count, byte budget, event count and document identity.
    Recovery is deliberately cheap: retransmit what is still held, otherwise
    open a fresh epoch.  There is no diff-repair path — a mirror that
    disagrees with Browser A is replaced, never negotiated with.
    """

    def __init__(self, *, session_id: str, tab_id: str = "main",
                 snapshot_provider: Optional[SnapshotProvider] = None,
                 max_history_frames: int = 400,
                 max_history_bytes: int = 6 * 1024 * 1024,
                 max_delta_events: int = 5000,
                 batch_ms: float = 0.0,
                 input_gate_s: float = 1.5,
                 snapshot_retry_s: float = 0.25,
                 snapshot_retry_max_s: float = 5.0,
                 viewer_max_queue: int = 512,
                 metrics: Optional[PipelineMetrics] = None) -> None:
        self.session_id = str(session_id)
        self.tab_id = str(tab_id)
        self.snapshot_provider = snapshot_provider
        self.max_history_frames = int(max_history_frames)
        self.max_history_bytes = int(max_history_bytes)
        self.max_delta_events = int(max_delta_events)
        self.batch_ms = float(batch_ms)
        self.input_gate_s = float(input_gate_s)
        self.snapshot_retry_s = float(snapshot_retry_s)
        self.snapshot_retry_max_s = float(snapshot_retry_max_s)
        self.viewer_max_queue = int(viewer_max_queue)
        self.metrics = metrics or PipelineMetrics()

        # ---- document / ordering identity ----
        self.document_id: str = new_document_id()
        self.pending_document_id: Optional[str] = None
        self.epoch: int = 0
        self.seq: int = 0
        self._epoch_opened_at: float = 0.0

        # ---- canonical state ----
        self.snapshot_frame: Optional[MirrorFrame] = None
        self.snapshot_seq: int = 0
        # (seq, epoch, kind, serialized text, event count)
        self.history: Deque[Tuple[int, int, str, str, int]] = deque()
        self.history_bytes: int = 0
        self.history_events: int = 0
        self.url: str = ""
        self.title: str = ""
        self.loading: bool = False
        self.can_back: bool = False
        self.can_fwd: bool = False
        self.nav_error: str = ""
        self._last_input_at: float = float("-inf")

        # ---- viewers ----
        self.viewers: Dict[str, MirrorViewer] = {}
        self._viewer_seq: int = 0

        # ---- snapshot request bookkeeping ----
        self._snapshot_requested = False
        self._snapshot_reason = ""
        self._retry_delay_s = self.snapshot_retry_s
        self._retry_task: Optional[asyncio.Task] = None
        self._batch_task: Optional[asyncio.Task] = None
        self._pending_batch: List[MirrorFrame] = []
        self._closed = False

    # -- publishing ------------------------------------------------------
    def publish(self, kind: str, payload: Optional[Dict[str, Any]] = None, *,
                raw: Optional[Dict[str, str]] = None,
                document_id: Optional[str] = None,
                lane: Optional[str] = None,
                timing: Optional[Dict[str, float]] = None,
                event_count: int = 1,
                fanout: bool = True) -> Optional[MirrorFrame]:
        """Assign identity, record in history, fan out.  Never awaits I/O.

        Returns the frame, or ``None`` when it was dropped — and every drop
        reason is a named counter, because "mutations were silently discarded"
        is the failure mode this module exists to remove.
        """
        if self._closed:
            return None
        payload = dict(payload or {})
        raw = dict(raw or {})
        event_count = max(1, int(event_count or 1))

        if document_id and self._is_stale_document(document_id):
            self.metrics.bump("frames_dropped_stale_doc")
            logger.debug("[HUB] drop stale %s frame doc=%s (current=%s)",
                         kind, str(document_id)[:8], self.document_id[:8])
            return None

        if kind == KIND_SNAPSHOT:
            self._commit_document(document_id)
            # A snapshot OPENS an epoch (remote-browser's openPendingSnapshot):
            # the viewer's next accepted state is fully defined by it, and the
            # delta tail restarts underneath.
            self.epoch += 1
            self._epoch_opened_at = time.monotonic()
            self._pending_batch.clear()
        elif document_id:
            self.document_id = document_id

        self.seq += 1
        frame = MirrorFrame(
            kind=kind,
            payload=payload,
            raw=raw,
            seq=self.seq,
            epoch=self.epoch,
            document_id=self.document_id,
            tab_id=self.tab_id,
            session_id=self.session_id,
            timing=dict(timing or {}),
            event_count=event_count,
            lane=lane or ("nav" if self.loading else "update"),
        )
        t0 = time.perf_counter()
        text = frame.encode()
        self.metrics.stage("serialize", (time.perf_counter() - t0) * 1000.0,
                           lane=frame.lane, bytes_=len(text))
        self.metrics.bump("frames_published")
        if kind == KIND_SNAPSHOT:
            self.metrics.bump("snapshots")
        elif kind == KIND_DELTA:
            self.metrics.bump("deltas")

        if kind == KIND_SNAPSHOT:
            self.snapshot_frame = frame
            self.snapshot_seq = frame.seq
            self._reset_history()
            self.loading = False
            self.nav_error = ""
            for key in ("url", "title"):
                value = payload.get(key)
                if isinstance(value, str) and value:
                    setattr(self, key, value)
        elif kind in HISTORICAL_KINDS:
            self._remember(frame, text, event_count)

        if fanout:
            self._fanout(frame)
        else:
            self.remember_published(frame)
        return frame

    def _fanout(self, frame: MirrorFrame) -> None:
        """Deliver to every viewer from one serialization.

        ``frame.encode()`` ran once above; each viewer receives the same ``str``.
        Per-viewer serialization is the cost remote-browser calls out explicitly
        ("NEVER JSON.stringify per viewer"), and it is exactly what a
        multi-megabyte snapshot cannot afford to multiply by viewer count.
        """
        if not self.viewers:
            return
        if self.batch_ms > 0 and frame.kind == KIND_DELTA:
            # Coalesce ordinary patches into one write per tick.  A structural
            # frame flushes the batch first (remote-browser's publish(): flush
            # the tab, then broadcast) so a chrome/snapshot frame can never
            # overtake the deltas it is meant to follow.
            self._pending_batch.append(frame)
            if self._batch_task is None or self._batch_task.done():
                self._batch_task = asyncio.create_task(self._flush_batch_later())
            return
        if self._pending_batch:
            self.flush_batch()
        for viewer in list(self.viewers.values()):
            if viewer.offer(frame):
                viewer.advance_cursor(frame)
                self.metrics.bump("frames_delivered")

    async def _flush_batch_later(self) -> None:
        try:
            await asyncio.sleep(self.batch_ms / 1000.0)
        except asyncio.CancelledError:
            pass
        self.flush_batch()

    def flush_batch(self) -> None:
        frames, self._pending_batch = self._pending_batch, []
        task, self._batch_task = self._batch_task, None
        if task is not None and not task.done():
            task.cancel()
        for frame in frames:
            for viewer in list(self.viewers.values()):
                if viewer.offer(frame):
                    viewer.advance_cursor(frame)
                    self.metrics.bump("frames_delivered")

    def remember_published(self, frame: MirrorFrame) -> None:
        """History-only recording for a frame the producer already wrote out.

        Used while a session runs without viewers — the tail still has to exist
        for the first viewer that attaches, and a viewer that appears mid-delta
        must not have to wait for the next capture to be caught up.
        """
        if frame.kind in HISTORICAL_KINDS:
            self._remember(frame, frame.encode(), frame.event_count)
        elif frame.kind == KIND_SNAPSHOT:
            self.snapshot_frame = frame
            self.snapshot_seq = frame.seq
            self._reset_history()
            self.loading = False

    # -- history ---------------------------------------------------------
    def _remember(self, frame: MirrorFrame, text: str, event_count: int = 1) -> None:
        events = max(1, int(event_count))
        self.history.append((frame.seq, frame.epoch, frame.kind, text, events))
        self.history_bytes += len(text)
        self.history_events += events
        if (len(self.history) > self.max_history_frames
                or self.history_bytes > self.max_history_bytes
                or self.history_events > self.max_delta_events):
            self._trim_history()

    def _trim_history(self) -> None:
        """Bound the tail, and ask for a snapshot when the floor moved.

        Trimming the oldest frames means a viewer that far behind can no longer
        be retransmitted — so, exactly like remote-browser's delta-limit reset,
        a fresh document becomes the only valid resume point and is requested
        (deferred while the page is actively being typed into).
        """
        over = (len(self.history) > self.max_history_frames
                or self.history_bytes > self.max_history_bytes
                or self.history_events > self.max_delta_events)
        if not over:
            return
        wait_s = 0.0
        if (self.history_events > self.max_delta_events
                and self._last_input_at != float("-inf")):
            # Defer a routine trim while the tab is busy: replacing the document
            # mid-keystroke is what drops focus and makes typing unusable.
            idle_s = time.monotonic() - self._last_input_at
            wait_s = max(0.0, 0.75 - idle_s)
        while (self.history and (len(self.history) > self.max_history_frames
                                  or self.history_bytes > self.max_history_bytes
                                  or self.history_events > self.max_delta_events)):
            dropped = self.history.popleft()
            self.history_bytes = max(0, self.history_bytes - len(dropped[3]))
            self.history_events = max(0, self.history_events - dropped[4])
            self.metrics.bump("history_trims")
        if wait_s > 0:
            asyncio.ensure_future(self._deferred_trim(wait_s))
        else:
            self.request_snapshot("trim")

    async def _deferred_trim(self, delay_s: float) -> None:
        try:
            await asyncio.sleep(delay_s)
        except asyncio.CancelledError:
            return
        self.request_snapshot("trim")

    def _reset_history(self) -> None:
        self.history.clear()
        self.history_bytes = 0
        self.history_events = 0

    # -- document identity -----------------------------------------------
    def _is_stale_document(self, document_id: str) -> bool:
        if document_id == self.document_id:
            return False
        if self.pending_document_id and document_id == self.pending_document_id:
            return False
        return True

    def _commit_document(self, document_id: Optional[str]) -> None:
        if document_id and document_id != self.document_id:
            # Navigation completed: the new document owns the tab.  Frames still
            # in flight for the previous document are stale by construction, so
            # they stop being admissible the moment this snapshot lands.
            self.document_id = document_id
        self.pending_document_id = None

    def begin_navigation(self, url: str = "", *, document_id: Optional[str] = None) -> str:
        """A new document is starting — stop the old one from corrupting it.

        ``epoch`` is deliberately NOT bumped here.  Remote Browser bumps it when
        the fresh snapshot arrives, so an interrupted navigation cannot leave a
        viewer waiting on an epoch that never opens.  What does happen now: the
        previous document's frames become stale (dropped at the hub), the
        retransmittable tail is cleared, and viewers are told the tab is
        loading so they can show a transition without freezing.
        """
        self.pending_document_id = document_id or new_document_id()
        self.loading = True
        self.nav_error = ""
        if url:
            self.url = url
        self._reset_history()
        self.publish(KIND_CHROME, {
            "loading": True, "navigating": True, "url": self.url,
            "title": self.title, "canBack": self.can_back, "canFwd": self.can_fwd,
            "document_id": self.pending_document_id,
        }, document_id=self.pending_document_id, lane="nav")
        self.request_snapshot("nav")
        return self.pending_document_id

    def same_document_url(self, url: str, *, title: str = "") -> None:
        """``pushState`` / ``replaceState`` / fragment change: URL bar yes, epoch no."""
        if url:
            self.url = url
        if title:
            self.title = title
        self.publish(KIND_CHROME, {
            "loading": False, "url": self.url, "title": self.title,
            "canBack": self.can_back, "canFwd": self.can_fwd,
            "same_document": True,
        }, lane="update")

    def end_navigation(self, *, url: str = "", can_back: bool = None,
                       can_fwd: bool = None) -> None:
        if url:
            self.url = url
        if can_back is not None:
            self.can_back = bool(can_back)
        if can_fwd is not None:
            self.can_fwd = bool(can_fwd)
        self.publish(KIND_CHROME, {
            "loading": False, "url": self.url, "title": self.title,
            "canBack": self.can_back, "canFwd": self.can_fwd,
        }, lane="update")

    def navigation_failed(self, url: str, error: str) -> None:
        """Navigation broke; the mirror keeps the document it already has.

        Clearing the tab instead would turn a DNS failure into a lost session
        while Browser A still has a live page the operator needs to see.
        """
        self.pending_document_id = None
        self.loading = False
        self.nav_error = str(error or "")[:300]
        self.publish(KIND_CHROME, {
            "loading": False, "url": self.url or url, "title": self.title,
            "canBack": self.can_back, "canFwd": self.can_fwd,
            "nav_failed": True, "attempted_url": url, "error": self.nav_error,
        }, lane="nav")

    # -- snapshot requests ------------------------------------------------
    def request_snapshot(self, reason: str = "resync") -> bool:
        """Ask the capture pipeline for a fresh document.  Coalesced, retrying.

        A provider failure does not strand the mirror: the retry loop re-asks
        with bounded exponential backoff, which is how remote-browser keeps a
        wedged page from becoming "no updates ever again" the way a one-shot
        request would.
        """
        if self._closed or self.snapshot_provider is None:
            return False
        if self._snapshot_requested:
            return False
        self._snapshot_requested = True
        self._snapshot_reason = reason
        try:
            asyncio.ensure_future(self._run_snapshot(reason))
        except RuntimeError:         # no running loop (sync tests, shutdown)
            logger.debug("[HUB] snapshot request %s has no event loop", reason)
            self._snapshot_requested = False
            return False
        return True

    async def _run_snapshot(self, reason: str) -> None:
        t0 = time.perf_counter()
        try:
            result = self.snapshot_provider(self, reason) if self.snapshot_provider else None
            if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
                await result
            self.metrics.stage("capture", (time.perf_counter() - t0) * 1000.0,
                               lane="nav" if reason == "nav" else "update")
            self.snapshot_ready(reason)
        except Exception as exc:
            logger.debug("[HUB] snapshot provider failed (%s): %s", reason, exc)
            self.snapshot_failed(reason)

    def snapshot_ready(self, reason: str = "") -> None:
        self._snapshot_requested = False
        self._retry_delay_s = self.snapshot_retry_s
        for viewer in self.viewers.values():
            viewer.resync.reset()
            if self.snapshot_frame is not None:
                viewer.joined = True

    def snapshot_failed(self, reason: str = "") -> None:
        self._snapshot_requested = False
        if self._closed or reason not in ("nav", "resync", "trim"):
            return
        if self._retry_task is not None and not self._retry_task.done():
            return
        delay = self._retry_delay_s
        self._retry_delay_s = min(self.snapshot_retry_max_s, max(0.05, delay * 2))
        try:
            self._retry_task = asyncio.ensure_future(self._retry_snapshot_later(reason, delay))
        except RuntimeError:
            self._retry_task = None

    async def _retry_snapshot_later(self, reason: str, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        if self._closed:
            return
        self.request_snapshot(reason)

    # -- viewers ----------------------------------------------------------
    def attach_viewer(self, sink: Any, viewer_id: Optional[str] = None, *,
                      resume_from: Optional[Dict[str, Any]] = None) -> MirrorViewer:
        """Register a viewer and catch it up immediately.

        A join must not wait for the next capture: ``join_payload`` ships the
        latest valid snapshot plus every delta still held after it — the
        property that removes "new Browser B stares at a spinner", and the reason
        ordinary DOM changes never require a repeated full-page capture.
        """
        if viewer_id is None:
            self._viewer_seq += 1
            viewer_id = f"viewer-{self._viewer_seq:02d}"
        existing = self.viewers.get(viewer_id)
        if existing is not None:
            try:
                asyncio.ensure_future(existing.close())
            except RuntimeError:
                pass
        viewer = MirrorViewer(viewer_id, sink, hub=self, max_queue=self.viewer_max_queue)
        self.viewers[viewer_id] = viewer
        viewer.start()
        # A join payload already contains everything this hub holds, so the
        # pending batch is flushed first: otherwise it would land on the new
        # viewer twice (once from the flush, once from the tail).
        self.flush_batch()
        if resume_from:
            try:
                viewer.cursor_epoch = int(resume_from.get("epoch", -1))
                viewer.cursor_next_seq = int(resume_from.get("next_seq", 0))
            except (TypeError, ValueError):
                pass
        for text in self.join_payload(viewer):
            viewer.offer_text(text)
        if self.snapshot_frame is not None:
            # The join payload *is* this epoch: an ack for it is implied.  A
            # viewer is only gated from the moment a NEWER epoch opens.
            viewer.joined = True
            viewer.cursor_epoch = self.epoch
            viewer.cursor_next_seq = self.seq + 1
            viewer.acked_epoch = self.epoch
        return viewer

    async def detach_viewer(self, viewer_id: str) -> None:
        viewer = self.viewers.pop(viewer_id, None)
        if viewer is not None:
            await viewer.close()

    def viewer_id_for(self, sink: Any) -> Optional[str]:
        viewer = self.viewer_for(sink)
        return viewer.viewer_id if viewer is not None else None

    def viewer_for(self, sink: Any) -> Optional[MirrorViewer]:
        for viewer in self.viewers.values():
            if viewer.sink is sink:
                return viewer
        return None

    def resume_viewer(self, viewer: MirrorViewer) -> None:
        """A stalled viewer drained: give it the canonical catch-up payload."""
        if viewer.closed:
            return
        self.flush_batch()
        for text in self.join_payload(viewer, force_resync=True):
            viewer.offer_text(text)

    def join_payload(self, viewer: Optional[MirrorViewer] = None, *,
                     force_resync: bool = False) -> List[str]:
        """Frames a joining (or recovered) viewer needs, in order."""
        out: List[str] = []
        snapshot = self.snapshot_frame
        if snapshot is None:
            out.append(self._synthetic(KIND_RESYNC, {
                "reason": "no-snapshot", "url": self.url,
            }).encode())
            self.request_snapshot("resync")
            return out
        out.append(self._synthetic(KIND_RESYNC, {
            "reason": "stalled" if force_resync else "join",
            "url": self.url, "title": self.title,
            "document_id": self.document_id, "epoch": self.epoch,
        }).encode())
        out.append(snapshot.encode())
        start_seq = self.snapshot_seq + 1
        if viewer is not None and not force_resync and viewer.cursor_epoch == self.epoch:
            start_seq = max(start_seq, viewer.cursor_next_seq)
        for seq, _epoch, kind, text, _events in self.history:
            if seq < start_seq:
                continue
            out.append(text)
        out.append(self._synthetic(KIND_CHROME, {
            "loading": self.loading, "url": self.url, "title": self.title,
            "canBack": self.can_back, "canFwd": self.can_fwd,
            "document_id": self.document_id, "epoch": self.epoch,
            "join_complete": True,
        }).encode())
        return out

    def resync(self, viewer: Optional[MirrorViewer], *, epoch: Any = None,
               last_seq: Any = None, reason: str = "gap") -> str:
        """Recover a viewer: retransmit when possible, otherwise re-snapshot.

        Returns ``"replay"``, ``"snapshot"`` or ``"degraded"``.  This is the
        function that makes "Browser B fell behind" a non-event: the common gap
        costs a few small frames out of history, and only an unrecoverable one
        pays for a document capture.
        """
        self.metrics.bump("resyncs")
        if viewer is None:
            self.request_snapshot("resync")
            return "snapshot"
        budget = viewer.resync
        if budget.degraded:
            return "degraded"
        try:
            epoch_i = int(epoch) if epoch is not None else None
        except (TypeError, ValueError):
            epoch_i = None
        try:
            last_i = int(last_seq) if last_seq is not None else viewer.cursor_next_seq - 1
        except (TypeError, ValueError):
            last_i = viewer.cursor_next_seq - 1
        if self.replay_from(viewer, epoch=epoch_i, last_seq=last_i, reason=reason):
            return "replay"
        if budget.should_degrade():
            budget.degrade()
            self.metrics.bump("resync_degrades")
            viewer.offer(self._synthetic(KIND_CHROME, {
                "loading": False, "url": self.url, "title": self.title,
                "live_paused": True, "reason": str(reason)[:120],
            }))
            return "degraded"
        budget.note_sent()
        self.request_snapshot("resync")
        return "snapshot"

    def replay_from(self, viewer: MirrorViewer, *, epoch: Optional[int], last_seq: int,
                    reason: str = "gap") -> bool:
        """Cheap recovery: retransmit the still-held tail.  True if repaired."""
        snapshot = self.snapshot_frame
        if snapshot is None:
            return False
        if epoch is not None and int(epoch) != self.epoch:
            return False
        if last_seq < self.snapshot_seq:
            # The gap reaches behind the snapshot that opened this epoch: only a
            # fresh document can repair it.
            return False
        if self.history and self.history[0][0] > last_seq + 1:
            # The oldest frames we hold are newer than what the viewer needs:
            # there is a hole inside the history itself.
            return False
        wanted = [text for (seq, _epoch, _kind, text, _n) in self.history
                  if seq > last_seq]
        if not wanted:
            # Nothing was missed — the viewer only needs to be told to resume.
            viewer.joined = True
            viewer.resync.reset()
            return True
        self.metrics.bump("retransmit_frames", len(wanted))
        for text in wanted:
            viewer.offer_text(text)
        try:
            viewer.cursor_next_seq = last_seq + 1 + len(wanted)
        except (TypeError, ValueError):
            pass
        viewer.resync.reset()
        return True

    def _synthetic(self, kind: str, payload: Dict[str, Any]) -> MirrorFrame:
        self.seq += 1
        payload = dict(payload)
        payload.pop("document_id", None)
        frame = MirrorFrame(kind=kind, payload=payload, seq=self.seq, epoch=self.epoch,
                            document_id=self.document_id, tab_id=self.tab_id,
                            session_id=self.session_id)
        return frame

    # -- control frames ----------------------------------------------------
    def send_control(self, message: Dict[str, Any]) -> None:
        """Ship a non-mirror message (permissions, session_info, chat, …).

        Control traffic bypasses the mirror envelope but shares each viewer's
        bounded queue, so a slow viewer cannot block an admin flow either.
        """
        if not isinstance(message, dict):
            return
        text = json.dumps(message, ensure_ascii=False)
        for viewer in list(self.viewers.values()):
            viewer.offer_text(text)

    def broadcast_text(self, text: str) -> None:
        for viewer in list(self.viewers.values()):
            viewer.offer_text(text)

    # -- input gating (remote-browser D7) ---------------------------------
    def allows_input(self, viewer_id: Optional[str]) -> Tuple[bool, str]:
        """Whether input from ``viewer_id`` may reach Browser A right now.

        The gate has a deadline: a lost ``view_ack`` must not lock a visitor out
        of the page forever, so after ``input_gate_s`` input is allowed on the
        assumption the epoch landed.  That keeps the property (no clicks on a
        stale layout) without the failure mode (a dropped ack wedges a session).
        """
        viewer = self.viewers.get(viewer_id or "")
        if viewer is None:
            return True, ""
        if viewer.acked_epoch >= self.epoch:
            return True, ""
        if not self._epoch_opened_at:
            return True, ""
        if (time.monotonic() - self._epoch_opened_at) > self.input_gate_s:
            return True, "gate-timeout"
        self.metrics.bump("input_gated")
        return False, "epoch-unapplied"

    def note_input(self, viewer_id: Optional[str] = None) -> None:
        """Accepted driver activity.  Defers routine trims; feeds metrics."""
        self._last_input_at = time.monotonic()

    def ack(self, viewer_id: Optional[str], epoch: Any, seq: Any = None) -> None:
        viewer = self.viewers.get(viewer_id or "")
        if viewer is None:
            return
        try:
            epoch_i = int(epoch)
        except (TypeError, ValueError):
            return
        viewer.acked_epoch = max(viewer.acked_epoch, epoch_i)
        if seq is not None:
            try:
                viewer.cursor_next_seq = max(viewer.cursor_next_seq, int(seq) + 1)
            except (TypeError, ValueError):
                pass
        if viewer.acked_epoch >= self.epoch:
            viewer.joined = True
            viewer.resync.reset()

    def note_render(self, *, ms: float, stage: str = "patch", lane: str = "update") -> None:
        """Record a viewer-reported timing sample (§9, C/D half of the chain)."""
        try:
            self.metrics.stage(stage, float(ms), lane=lane)
        except (TypeError, ValueError):
            return

    # -- misc --------------------------------------------------------------
    def stats(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "tab_id": self.tab_id,
            "document_id": self.document_id,
            "pending_document_id": self.pending_document_id or "",
            "epoch": self.epoch,
            "seq": self.seq,
            "url": self.url,
            "title": self.title,
            "loading": self.loading,
            "nav_error": self.nav_error,
            "history_frames": len(self.history),
            "history_bytes": self.history_bytes,
            "history_events": self.history_events,
            "max_history_frames": self.max_history_frames,
            "max_history_bytes": self.max_history_bytes,
            "snapshot_bytes": self.snapshot_frame.size() if self.snapshot_frame else 0,
            "snapshot_seq": self.snapshot_seq,
            "viewers": [v.as_dict() for v in self.viewers.values()],
            "metrics": self.metrics.as_dict(),
        }

    async def close(self) -> None:
        self._closed = True
        self.flush_batch()
        for task in (self._batch_task, self._retry_task):
            if task is not None and not task.done():
                task.cancel()
        self._batch_task = None
        self._retry_task = None
        for viewer in list(self.viewers.values()):
            await viewer.close()
        self.viewers.clear()


class MirrorInputRelay:
    """Node-aware interaction forwarding: per-tab serial tail plus the epoch gate.

    Ported from ``packages/gateway/src/input/relay.ts``.  Two properties matter
    and neither is optional:

    * **Ordering.** A double click, a type-then-enter, a scroll-then-click — all
      of them are only meaningful in the order the visitor performed them, so
      every action for a tab runs on one tail and never interleaves.
    * **Epoch agreement.** Input is executed against the *authoritative* page.
      If the viewer is still looking at a document that has been replaced, the
      node it is pointing at may no longer mean what it meant; the gate waits
      briefly for the viewer to apply the epoch and then drops the action
      (reporting why) rather than clicking something arbitrary.

    Browser A stays the only place website JavaScript runs: this dispatches into
    the real page, and the resulting mutations come back through the normal
    capture path.  The mirror never executes a copied script.
    """

    #: Actions whose target identity can be stale; geometry-only ones are not.
    STRUCTURAL = frozenset({"click", "tap", "key", "text", "value", "submit",
                            "nav", "focus", "form"})

    def __init__(self, hub: MirrorHub, *, gate_timeout_s: float = 0.6,
                 wait_poll_s: float = 0.02) -> None:
        self.hub = hub
        self.gate_timeout_s = float(gate_timeout_s)
        self.wait_poll_s = float(wait_poll_s)
        self.stats: Dict[str, int] = {
            "submitted": 0, "executed": 0, "gated_wait": 0,
            "dropped_stale": 0, "failed": 0, "forced": 0,
        }
        self._tail: Optional[asyncio.Task] = None

    async def submit(self, action: str, handler, *, viewer_id: Optional[str] = None,
                     force: bool = False) -> str:
        """Queue one interaction.  Returns ``ok`` / ``stale`` / ``skipped``.

        Returns as soon as the action is *queued*: an endpoint loop that awaits
        every forwarded gesture would let a slow page action hold up the next
        one — and the visitor's own input thread is not the place to discover
        that a website is slow.
        """
        self.stats["submitted"] += 1
        if self.hub is None:
            return "skipped"
        allowed, _why = self.hub.allows_input(viewer_id)
        if not allowed:
            if force:
                self.stats["forced"] += 1
            else:
                self.stats["gated_wait"] += 1
                if not await self._wait_for_gate(viewer_id):
                    if action in self.STRUCTURAL:
                        self.stats["dropped_stale"] += 1
                        return "stale"
        prev, self._tail = self._tail, None

        async def _run() -> None:
            if prev is not None:
                try:
                    await asyncio.shield(prev)
                except (asyncio.CancelledError, Exception):
                    pass
            try:
                result = handler()
                if asyncio.iscoroutine(result):
                    await result
                self.stats["executed"] += 1
                self.hub.note_input(viewer_id)
            except Exception as exc:
                self.stats["failed"] += 1
                logger.debug("[RELAY] %s failed: %s", action, exc)

        self._tail = asyncio.ensure_future(_run())
        return "ok"

    async def _wait_for_gate(self, viewer_id: Optional[str]) -> bool:
        deadline = time.monotonic() + self.gate_timeout_s
        while time.monotonic() < deadline:
            await asyncio.sleep(self.wait_poll_s)
            allowed, _why = self.hub.allows_input(viewer_id)
            if allowed:
                return True
        return False

    async def drain(self, timeout_s: float = 5.0) -> None:
        """Await the queued tail (shutdown, tests, and "the click landed" flows)."""
        deadline = time.monotonic() + timeout_s
        while self._tail is not None and time.monotonic() < deadline:
            try:
                await asyncio.wait_for(asyncio.shield(self._tail),
                                       timeout=max(0.0, deadline - time.monotonic()))
            except (asyncio.TimeoutError, asyncio.CancelledError):
                return
            except Exception:
                return
            if self._tail.done():
                break


class HubSocket:
    """Duck-typed websocket that publishes into a hub instead of onto a socket.

    ``DOMCaptureSession`` was written against ``websocket.send_text/send_json``.
    Rather than re-plumb the capture engine, it can be handed this: the same
    awaits, the same shapes, the same ``buffered_amount`` its own backpressure
    check reads — and behind it a hub that owns identity, ordering, history and
    fan-out.  Passing ``hub=`` directly is the zero-adaptation path and is what
    a session uses; this shim exists so any caller that still holds "the"
    websocket feeds the same pipeline instead of a second one.
    """

    __slots__ = ("hub", "sink", "closed", "buffered_amount", "client_state")

    def __init__(self, hub: MirrorHub, sink: Any = None) -> None:
        self.hub = hub
        self.sink = sink
        self.closed = False
        self.buffered_amount = int(getattr(sink, "buffered_amount", 0) or 0)
        self.client_state = getattr(sink, "client_state", None)

    async def send_text(self, text: str) -> None:
        self.publish_text(text)

    async def send_json(self, message: Dict[str, Any]) -> None:
        self.publish_dict(message)

    def publish_text(self, text: str) -> None:
        try:
            message = json.loads(text)
        except Exception:
            self.hub.broadcast_text(text)
            return
        if not isinstance(message, dict):
            self.hub.broadcast_text(text)
            return
        self.publish_dict(message)

    def publish_dict(self, message: Dict[str, Any]) -> None:
        kind = _KIND_BY_WIRE.get(str(message.get("type") or ""))
        if kind is None:
            self.hub.send_control(message)
            return
        payload = {k: v for k, v in message.items()
                   if k not in ("type", "gen", "seq")}
        document_id = message.get("document_id") or None
        timing = message.get("timing") if isinstance(message.get("timing"), dict) else {}
        reason = str(message.get("reason") or "")
        lane = "nav" if reason.startswith(("navigation", "nav_", "goto", "back",
                                          "forward", "reload")) else "update"
        events = len(message.get("ops") or []) or 1
        raw: Dict[str, str] = {}
        # A frame whose body is already JSON text (the patch path) keeps it as
        # text so the adapter does not pay for a parse + dump of every op.
        if kind == KIND_DELTA and "ops" in message and not isinstance(message["ops"], (list, dict)):
            raw["ops"] = str(message["ops"])
            payload.pop("ops", None)
        self.hub.publish(kind, payload, raw=raw, document_id=document_id,
                         lane=lane, timing=timing, event_count=events)


_KIND_BY_WIRE = {
    "full_document": KIND_SNAPSHOT,
    "dom_patch": KIND_DELTA,
    "asset_map": KIND_ASSET,
    "mirror_chrome": KIND_CHROME,
    "page_transition": KIND_CHROME,
}

__all__ = [
    "DROPPABLE_KINDS",
    "HISTORICAL_KINDS",
    "HubSocket",
    "KIND_ASSET",
    "KIND_CHROME",
    "KIND_CONTROL",
    "KIND_DELTA",
    "KIND_REPLAY",
    "KIND_RESYNC",
    "KIND_SNAPSHOT",
    "MAX_RESYNCS_PER_WINDOW",
    "MirrorFrame",
    "MirrorHub",
    "MirrorViewer",
    "PipelineMetrics",
    "RESYNC_BACKOFF_BASE_S",
    "RESYNC_WINDOW_S",
    "ResyncBudget",
    "WIRE_NAMES",
    "new_document_id",
]
