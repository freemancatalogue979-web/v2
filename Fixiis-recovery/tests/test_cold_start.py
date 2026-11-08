#!/usr/bin/env python3
"""The first page: it has the least time and the most to do.

Everything else in the capture pipeline makes a page that is already on screen
better.  The first page gets none of that: a visitor is staring at the spinner
and the only thing that ends it is a document.

Two things were holding it back, and neither had anything to do with the page:

  * three browser round trips (title, UA, checksum) that the capture could
    answer for free — see tests/test_capture_roundtrips.py;
  * the session wrote the visitor's profile to disk BEFORE starting the
    capture, so the first capture waited behind bookkeeping it does not read.

This pins the second one, and the single INFO line that makes the number
visible in a real deployment instead of a guess.

    PYTHONPATH=. python3 tests/test_cold_start.py
"""
import asyncio
import logging
import sys
import types

sys.path.insert(0, "/home/user/v2/Fixiis-recovery")

PASSED = 0


def ok(cond, label):
    global PASSED
    assert cond, label
    PASSED += 1


def install_fake_http():
    mod = types.ModuleType("httpx")

    class C:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *e):
            return False

        async def get(self, url, timeout=None):
            r = types.SimpleNamespace()
            r.url = url
            r.status_code = 200
            r.content = b"wOF2" + b"0" * 32
            r.headers = {"content-type": "font/woff2"}
            return r

    class Limits:
        def __init__(self, *a, **k):
            pass

    mod.AsyncClient = C
    mod.Limits = Limits
    sys.modules["httpx"] = mod


install_fake_http()
import dom_capture  # noqa: E402

URL = "https://site.example/page"
HTML = ("<!DOCTYPE html><html><head><title>T</title></head><body>"
        + "<p>x</p>" * 80 + "</body></html>")


class Page:
    url = URL

    async def title(self):
        return "T"

    async def evaluate(self, *a, **k):
        return "1:1:1"


class WS:
    def __init__(self):
        self.sent = []

    async def send_text(self, text):
        self.sent.append(text)


def make_session():
    dom_capture._GLOBAL_ASSET_MANAGER = dom_capture.AssetManager()
    dom_capture._ASSET_GATE = None
    ws = WS()
    session = dom_capture.DOMCaptureSession(Page(), websocket=ws, client_id="cold")
    session._last_capture_supports_assets = True
    session._last_capture_supports_delta = True
    session._delta_installed = False
    session._last_capture_meta = {"title": "T", "checksum": "1:1:1", "ua": "UA"}

    async def capture(settle=None):
        return HTML

    session.capture_page = capture
    return session, ws


def run(fn):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(fn())
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# 1. the timeline exists, and says something
# ---------------------------------------------------------------------------
def test_the_first_document_is_timed_once():
    session, ws = make_session()
    ok(session._cold_first_sent_at is None, "the cold clock started already running")
    ok(session._t_created > 0, "no creation stamp")

    records = []

    class Grab(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = Grab()
    dom_capture.logger.addHandler(handler)
    level = dom_capture.logger.level
    dom_capture.logger.setLevel(logging.INFO)
    try:
        run(lambda: session._send_full(URL, reason="navigation"))
        run(lambda: session._send_full(URL, reason="click"))
    finally:
        dom_capture.logger.removeHandler(handler)
        dom_capture.logger.setLevel(level)

    cold = [r for r in records if "[COLD]" in r.getMessage()]
    ok(len(cold) == 1,
       "the cold-start line must be logged exactly once per session, got %d" % len(cold))
    line = cold[0].getMessage()
    ok("first document on screen" in line, "the line does not say what it measured: %r" % line)
    ok("ms capture" in line and "ms send" in line,
       "the line does not break down where the time went: %r" % line)
    ok(session._cold_first_sent_at is not None, "the clock was not stopped")
    ok(session._cold_first_sent_at >= session._t_created, "the clock went backwards")
    ok(session._cold_first_capture_at is not None, "the capture start was not stamped")


def test_the_marks_are_reset_per_session_not_per_capture():
    session, ws = make_session()
    run(lambda: session._send_full(URL, reason="navigation"))
    first = session._cold_first_sent_at
    run(lambda: session._send_full(URL, reason="click"))
    ok(session._cold_first_sent_at == first,
       "a later capture moved the first-document mark")


# ---------------------------------------------------------------------------
# 2. the first capture no longer waits for bookkeeping
# ---------------------------------------------------------------------------
def test_the_capture_starts_before_the_profile_write():
    src = open("/home/user/v2/Fixiis-recovery/session.py", encoding="utf-8",
               errors="replace").read()
    i_capture = src.index('asyncio.create_task(self.capture_remote_page(reason="initial_connect"')
    i_profile = src.index("await self._save_profile_info()")
    ok(i_capture < i_profile,
       "the profile write is still awaited before the first capture starts — a "
       "visitor pays for our bookkeeping in the only window where they have "
       "nothing on screen")
    # ...and the heartbeat, which is also bookkeeping.
    i_heartbeat = src.index("self._heartbeat_task = asyncio.create_task")
    ok(i_capture < i_heartbeat, "the heartbeat is still started before the capture")


def test_the_first_capture_is_still_forced():
    """Order changed; the capture itself must not have been weakened."""
    src = open("/home/user/v2/Fixiis-recovery/session.py", encoding="utf-8",
               errors="replace").read()
    ok('reason="initial_connect", force=True' in src,
       "the initial capture is no longer forced — an unchanged page could be skipped")


# ---------------------------------------------------------------------------
# 3. the measurement itself
# ---------------------------------------------------------------------------
def test_the_bench_shows_fewer_round_trips():
    sys.path.insert(0, "/home/user/v2/Fixiis-recovery/tests")
    import bench_cold_start as bench  # noqa: E402
    old = bench.measure(4.0, modern=False)
    new = bench.measure(4.0, modern=True)
    ok(new["round_trips"] < old["round_trips"],
       "the cold path did not shed any round trips (%d -> %d)"
       % (old["round_trips"], new["round_trips"]))
    ok(new["ms"] < old["ms"],
       "the cold path got no faster (%d ms -> %d ms)" % (old["ms"], new["ms"]))
    ok(new["ms"] <= max(12, old["ms"] // 3),
       "expected the cold path to be at least 3x cheaper at 4 ms per round "
       "trip: %d ms -> %d ms" % (old["ms"], new["ms"]))


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("cold start: %d checks passed" % PASSED)
