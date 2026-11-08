#!/usr/bin/env python3
"""Concurrent navigation pipeline: announce immediately, capture beside it.

    PYTHONPATH=. python3 tests/test_navigation_pipeline.py

The site the visitor is on changes URL, and the mirrored client should know that
in the same tick - not after the page has loaded, settled, been serialized,
asset-rewritten and sent.  So the two facts travel on two pipelines:

    navigation   (URL / route)      -> published the instant it is seen
    document     (serialized DOM)   -> captured independently, delivered when ready

and the acceptance criteria for this file are the criteria for the rework:

  1. a URL change reaches the client without waiting for a full DOM snapshot;
  2. new content starts streaming as soon as it exists (a partial document
     beats a spinner, and a settled follow-up converges it);
  3. a navigation capture is not held back by an interaction capture still
     running on the page being left;
  4. a slow capture's document is DROPPED when a newer navigation supersedes
     it - never allowed to overwrite the newer page;
  5. rapid successive navigations end with the newest page, once;
  6. a same-document route change (pushState/popstate/hash) updates the URL,
     keeps the patch stream open and never rebuilds the iframe;
  7. patches are delivered while a capture is in flight, and patches from a
     superseded document are dropped instead of applied to the wrong page;
  8. a navigation that cannot produce a document says so, so the client can
     leave its transition instead of waiting out a watchdog.
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dom_capture  # noqa: E402

PASSED = 0


def ok(cond, label):
    global PASSED
    assert cond, label
    PASSED += 1


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class FakeWebSocket:
    def __init__(self):
        self.sent = []
        self.closed = False
        self.t0 = None

    async def send_text(self, text):
        if self.t0 is None:
            self.t0 = asyncio.get_event_loop().time()
        self.sent.append((asyncio.get_event_loop().time(), json.loads(text)))

    def types(self):
        return [m["type"] for _t, m in self.sent]

    def frames(self, kind=None):
        return [m for _t, m in self.sent if kind is None or m["type"] == kind]

    def first(self, kind):
        got = self.frames(kind)
        return got[0] if got else None

    def clear(self):
        self.sent = []


class FakePage:
    """A page that knows its URL, and can be told how to behave."""

    def __init__(self, url="https://a.example/", capture_delay=0.0, partial=False):
        self.url = url
        self.capture_delay = capture_delay
        self.partial = partial
        self.checksum = "1:1:1"
        self.ready = True
        self.calls = []
        self.bindings = {}
        self.goto_calls = 0
        self.on_goto = None

    async def evaluate(self, expr, *args):
        self.calls.append(str(expr)[:60])
        if str(expr).strip() == "location.href":
            return self.url
        return self.checksum

    async def wait_for_function(self, expr, timeout=None):
        """Like the real one: WAIT for the condition, then give up."""
        if "readyState" not in str(expr):
            return None
        limit = (timeout if isinstance(timeout, (int, float)) else 1000) / 1000.0
        waited = 0.0
        while not self.ready:
            if waited >= limit:
                raise RuntimeError("Timeout %s ms exceeded" % timeout)
            await asyncio.sleep(0.005)
            waited += 0.005
        return None

    async def wait_for_load_state(self, *a, **k):
        return None

    async def title(self):
        return "Title"

    async def add_init_script(self, script=None, **k):
        return None

    async def expose_function(self, name, fn):
        self.bindings[name] = fn

    async def goto(self, url, **k):
        self.goto_calls += 1
        self.calls.append("goto:" + url)
        if self.on_goto:
            self.on_goto(url)


def make_session(url="https://a.example/", capture_delay=0.0, partial=False,
                 delta_ready=False):
    """A DOMCaptureSession wired to fakes: no browser, no httpx, no api."""
    ws = FakeWebSocket()
    page = FakePage(url, capture_delay=capture_delay, partial=partial)
    session = dom_capture.DOMCaptureSession(page, websocket=ws, client_id="nav")
    # Pretend the in-page machinery is installed, so nothing tries to talk to
    # a browser that is not there.
    session._interaction_trigger_installed = True
    session._interaction_binding_installed = True
    session._interaction_init_script_installed = True
    session._delta_binding_installed = True
    session._delta_init_script_installed = True
    session._delta_installed = delta_ready
    session._delta_active = delta_ready
    session._assets_ok = False          # keep the asset fetcher out of these tests
    session._last_capture_supports_assets = False

    async def capture_page(settle=None):
        session.calls = getattr(session, "calls", 0) + 1
        if page.capture_delay:
            await asyncio.sleep(page.capture_delay)
        session._last_capture_meta = {
            "doc": "doc:" + page.url,
            "checksum": "c:" + page.url,
            "title": "Title",
        }
        # Stands in for capture_page's bounded readyState wait: a page that is
        # still parsing ships early and says so.
        session._last_capture_partial = not page.ready
        session._last_capture_supports_assets = False
        session._last_capture_supports_delta = True
        return "<!DOCTYPE html><html><body>%s</body></html>" % page.url

    session.capture_page = capture_page
    return session, page, ws


async def install_delta(session, page):
    """Install the real relay closure on the fake page."""
    # make_session() pre-marks the bindings as installed so nothing tries to
    # talk to a browser; this test wants the real closure, so install it.
    session._delta_binding_installed = False
    session._delta_init_script_installed = False
    await dom_capture._install_delta_observer(page, session)
    ok("__domDelta" in page.bindings, "the delta relay was never exposed")
    return page.bindings["__domDelta"]


# ---------------------------------------------------------------------------
# 1. the URL travels alone, first
# ---------------------------------------------------------------------------
def test_url_change_is_announced_before_any_capture():
    async def main():
        session, page, ws = make_session("https://a.example/", capture_delay=0.05)
        # The URL poller sees the change because the page is already there.
        page.url = "https://b.example/"
        frame = await session._on_navigation_signal(
            "https://b.example/", same_document=False, source="url_watch")
        ok(frame is not None, "the navigation was not announced at all")
        await session._pipeline.drain(1.0)

        types = ws.types()
        ok(types == ["navigation"],
           "a URL change must reach the client with no document attached: %r" % (types,))
        nav = ws.first("navigation")
        ok(nav["url"] == "https://b.example/" and nav["prev_url"] is None,
           "the announcement named the wrong page: %r" % (nav,))
        ok(nav["doc_expected"] is True and nav["same_document"] is False,
           "a cross-document navigation must promise a document: %r" % (nav,))
        ok(nav["nav_id"] == 1 and isinstance(nav["seq"], int),
           "the frame is not versioned: %r" % (nav,))

        # The document follows on its own pipeline, unprompted.
        await asyncio.sleep(0.15)
        await session._pipeline.drain(1.0)
        doc = ws.first("full_document")
        ok(doc is not None, "the document never followed the announcement")
        ok(doc["url"] == "https://b.example/", "the document is for the wrong page")
        ok(doc["nav_id"] == nav["nav_id"],
           "the document is not attached to the navigation it answers: %r vs %r"
           % (doc.get("nav_id"), nav["nav_id"]))
        ok(doc.get("partial") is not True, "a fully parsed page was marked partial")
        await session.shutdown()

    run(main())


def test_announcement_does_not_wait_for_a_slow_capture():
    async def main():
        session, page, ws = make_session("https://a.example/", capture_delay=0.3)
        task = asyncio.ensure_future(session.send_page(reason="interaction:click"))
        await asyncio.sleep(0.02)          # the capture is now deep in flight
        page.url = "https://b.example/"
        t0 = asyncio.get_event_loop().time()
        frame = await session._on_navigation_signal(
            "https://b.example/", same_document=False, source="url_watch")
        await session._pipeline.drain(1.0)
        elapsed = asyncio.get_event_loop().time() - t0
        ok(frame is not None, "no announcement was made")
        ok(elapsed < 0.12,
           "the announcement waited %.0f ms for an unrelated capture" % (elapsed * 1000))
        ok(ws.types()[:1] == ["navigation"],
           "the client heard about the page change only after the capture: %r" % (ws.types(),))
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        await session.shutdown()

    run(main())


# ---------------------------------------------------------------------------
# 2. capture lanes are independent, and stale documents are dropped
# ---------------------------------------------------------------------------
def test_navigation_capture_is_not_blocked_by_a_content_capture():
    async def main():
        session, page, ws = make_session("https://a.example/", capture_delay=0.25)
        slow = asyncio.ensure_future(session.send_page(reason="interaction:click"))
        await asyncio.sleep(0.02)
        # The page moves, and the new page serializes fast.
        page.url = "https://b.example/"
        page.capture_delay = 0.02
        await session._on_navigation_signal(
            "https://b.example/", same_document=False, source="url_watch")
        await asyncio.sleep(0.12)
        await session._pipeline.drain(1.0)

        doc = ws.first("full_document")
        ok(doc is not None,
           "the new page's document was held back by the capture of the page "
           "the visitor had already left: %r" % (ws.types(),))
        ok(doc["url"] == "https://b.example/", "the first document is the wrong page")

        # ...and the slow capture of the old page must never overwrite it.
        await slow
        await asyncio.sleep(0.05)
        await session._pipeline.drain(1.0)
        docs = ws.frames("full_document")
        ok(len(docs) == 1,
           "the superseded capture still sent a document: %r"
           % ([d["url"] for d in docs],))
        ok(session.last_sent_url == "https://b.example/",
           "the session ended up labelled with the page the visitor left")
        await session.shutdown()

    run(main())


def test_rapid_navigations_end_on_the_newest_page_once():
    async def main():
        session, page, ws = make_session("https://a.example/", capture_delay=0.2)
        page.url = "https://b.example/"
        await session._on_navigation_signal(
            "https://b.example/", same_document=False, source="url_watch")
        await asyncio.sleep(0.02)          # B's capture is in flight and slow
        page.url = "https://c.example/"
        page.capture_delay = 0.02
        await session._on_navigation_signal(
            "https://c.example/", same_document=False, source="url_watch")
        await asyncio.sleep(0.35)
        await session._pipeline.drain(1.0)

        navs = [n["url"] for n in ws.frames("navigation")]
        ok(navs == ["https://b.example/", "https://c.example/"],
           "both navigations must be announced, in order: %r" % (navs,))
        docs = [d["url"] for d in ws.frames("full_document")]
        ok(docs == ["https://c.example/"],
           "rapid navigations must deliver the newest page exactly once, got %r"
           % (docs,))
        ok(session.last_sent_url == "https://c.example/",
           "the mirror is labelled with a page that is no longer current: %r"
           % (session.last_sent_url,))
        await session.shutdown()

    run(main())


def test_same_document_route_change_does_not_rebuild_the_page():
    async def main():
        session, page, ws = make_session("https://a.example/", delta_ready=True)
        await asyncio.sleep(0)   # let the constructor's install task settle
        frame = await session._on_navigation_signal(
            "https://a.example/inbox", same_document=True,
            reason="url_change", source="history:pushState")
        ok(frame is not None, "the route change was not announced")
        await session._pipeline.drain(1.0)
        await asyncio.sleep(0.1)
        await session._pipeline.drain(1.0)

        nav = ws.first("navigation")
        ok(nav["same_document"] is True and nav["doc_expected"] is False,
           "a route change must say the document is not changing: %r" % (nav,))
        ok(ws.frames("full_document") == [],
           "an SPA route change rebuilt the whole mirrored page: %r" % (ws.types(),))
        ok(session._delta_active is True,
           "the patch channel was shut for a same-document navigation - the "
           "mirror would freeze for the length of a capture it did not need")
        await session.shutdown()

    run(main())


def test_document_signal_announces_then_captures():
    async def main():
        session, page, ws = make_session("https://a.example/", capture_delay=0.01)
        session._last_doc_token = "old-token"
        await session._on_document_signal("new-token")
        await asyncio.sleep(0.1)
        await session._pipeline.drain(1.0)
        ok(ws.types()[0] == "navigation",
           "the document commit was not announced first: %r" % (ws.types(),))
        nav = ws.first("navigation")
        ok(nav["doc_expected"] is True, "a new document must be expected: %r" % (nav,))
        ok(nav.get("doc") == "new-token",
           "the announcement does not identify the document: %r" % (nav,))
        ok(ws.first("full_document") is not None, "the document never followed")
        await session.shutdown()

    run(main())


def test_url_poller_announces_without_waiting_for_the_document():
    async def main():
        session, page, ws = make_session("https://a.example/", capture_delay=0.2)
        watcher = asyncio.ensure_future(session.watch_url(interval_s=0.01))
        try:
            await asyncio.sleep(0.03)
            page.url = "https://b.example/"
            await asyncio.sleep(0.05)
            await session._pipeline.drain(1.0)
            ok(ws.first("navigation") is not None,
               "the URL poller saw the change but told nobody")
            ok(ws.frames("full_document") == [],
               "the poller waited for a 200 ms capture before announcing")
            await asyncio.sleep(0.22)
            await session._pipeline.drain(1.0)
            doc = ws.first("full_document")
            ok(doc is not None and doc["url"] == "https://b.example/",
               "the document never followed the announcement: %r" % (ws.types(),))
        finally:
            watcher.cancel()
            try:
                await watcher
            except (asyncio.CancelledError, Exception):
                pass
        await session.shutdown()

    run(main())


def test_poller_does_not_re_announce_a_route_change_the_hooks_reported():
    async def main():
        session, page, ws = make_session("https://a.example/", delta_ready=True)
        await asyncio.sleep(0)
        route = "https://a.example/inbox"
        page.url = route
        session._note_history_nav(route, True)
        await session._on_navigation_signal(
            route, same_document=True, reason="url_change", source="history:pushState")
        await session._pipeline.drain(1.0)

        watcher = asyncio.ensure_future(session.watch_url(interval_s=0.01))
        try:
            await asyncio.sleep(0.06)
            await session._pipeline.drain(1.0)
        finally:
            watcher.cancel()
            try:
                await watcher
            except (asyncio.CancelledError, Exception):
                pass
        anns = ws.frames("navigation")
        ok(len(anns) <= 1,
           "the poller announced the same route change a second time: %r"
           % ([a["url"] for a in anns],))
        if anns:
            ok(anns[0]["same_document"] is True,
               "the route change was reclassified as a new document: %r" % (anns[0],))
        ok(ws.frames("full_document") == [],
           "the poller rebuilt the page for a route change the hooks reported")
        await session.shutdown()

    run(main())


def test_transition_monitor_defers_to_the_capture_pipeline():
    """session.py must not race the capture session's announcements."""
    import types
    try:
        import session as session_mod
    except Exception as exc:  # environment without the heavy deps
        print('  (skipped: session.py not importable: %s)' % (exc,))
        return

    class FakeWS:
        def __init__(self):
            self.sent = []
            self.closed = False

        async def send_json(self, payload):
            self.sent.append(payload)

    class FakeCap:
        def __init__(self, announce):
            self.announce = announce
            self.calls = []

        @property
        def nav_announcements(self):
            return self.announce

        def publish_navigation(self, url, **kw):
            self.calls.append(url)
            return {'nav_id': len(self.calls)} if self.announce else None

    class Stub:
        def __init__(self, announce):
            self.is_active = True
            self.page = types.SimpleNamespace(
                url='https://a.example/', is_closed=lambda: False)
            self.current_url = 'https://a.example/'
            self.transition_cooldown = 0.0
            self.last_transition_time = 0.0
            self.websocket = FakeWS()
            self.dom_capture = FakeCap(announce)
            self.notified = []

        def get_active_page(self):
            return self.page

        async def _send_navigation_notification(self, url):
            self.notified.append(url)

    async def scenario(announce):
        stub = Stub(announce)
        task = asyncio.ensure_future(
            session_mod.NeoStreamingSession._page_transition_monitor(stub))
        await asyncio.sleep(0.05)
        stub.page.url = 'https://b.example/'
        await asyncio.sleep(0.65)
        stub.is_active = False
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except Exception:
            task.cancel()
        return stub

    live = run(scenario(True))
    ok(live.dom_capture.calls == ['https://b.example/'],
       'the transition monitor did not hand the change to the capture session: %r'
       % (live.dom_capture.calls,))
    ok(live.websocket.sent == [],
       'session.py still sent its own page_transition alongside the capture '
       'pipeline announcement: %r' % (live.websocket.sent,))
    ok('https://b.example/' in live.notified,
       'the Telegram navigation notification was dropped by the delegation')

    legacy = run(scenario(False))
    ok(bool(legacy.websocket.sent)
       and legacy.websocket.sent[0].get('type') == 'page_transition',
       'with announcements off the legacy page_transition frame must still be '
       'sent: %r' % (legacy.websocket.sent,))


# ---------------------------------------------------------------------------
# 3. patches: delivered during captures, dropped when superseded
# ---------------------------------------------------------------------------
def test_patch_is_relayed_while_a_full_capture_is_in_flight():
    async def main():
        session, page, ws = make_session("https://a.example/", capture_delay=0.2,
                                         delta_ready=True)
        relay = await install_delta(session, page)
        session._last_doc_token = "tok"
        task = asyncio.ensure_future(session.send_page(reason="interaction:click"))
        await asyncio.sleep(0.03)
        await relay('[["a","3","class","hot"]]', "tok")
        await session._pipeline.drain(1.0)

        patch = ws.first("dom_patch")
        ok(patch is not None,
           "the patch was swallowed because a capture was running: %r" % (ws.types(),))
        ok(patch["ops"] == [["a", "3", "class", "hot"]], "the ops were mangled: %r" % (patch,))
        ok(isinstance(patch.get("gen"), int) and isinstance(patch.get("nav_id"), int),
           "the patch carries no version: %r" % (patch,))
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        await session.shutdown()

    run(main())


def test_patch_from_a_superseded_document_is_dropped():
    async def main():
        session, page, ws = make_session("https://a.example/", delta_ready=True)
        relay = await install_delta(session, page)
        session._last_doc_token = "new-document"
        await relay('[["a","3","class","hot"]]', "old-document")
        await session._pipeline.drain(1.0)
        ok(ws.frames("dom_patch") == [],
           "a patch from a document the client is not showing was relayed: %r"
           % (ws.types(),))

        # ...and the matching one still goes through.
        await relay('[["a","3","class","hot"]]', "new-document")
        await session._pipeline.drain(1.0)
        ok(len(ws.frames("dom_patch")) == 1, "the valid patch was dropped too")
        await session.shutdown()

    run(main())


def test_history_nav_control_is_relayed_immediately():
    async def main():
        session, page, ws = make_session("https://a.example/", delta_ready=True)
        relay = await install_delta(session, page)
        await relay('[["nav","https://a.example/x#y",true,"history:pushState"]]', "tok")
        await session._pipeline.drain(1.0)

        nav = ws.first("navigation")
        ok(nav is not None, "a history route change produced no announcement")
        ok(nav["url"] == "https://a.example/x#y", "the route went to the wrong URL: %r" % (nav,))
        ok(nav["same_document"] is True, "pushState must be same-document: %r" % (nav,))
        ok("https://a.example/x#y" in session._history_nav_at,
           "the URL poller has no way to know this change was already classified")
        await session.shutdown()

    run(main())


# ---------------------------------------------------------------------------
# 4. an early document, then convergence
# ---------------------------------------------------------------------------
def test_partial_navigation_document_is_followed_by_a_settled_one():
    async def main():
        session, page, ws = make_session("https://a.example/", capture_delay=0.0)
        page.ready = False                  # the parser is still running
        page.url = "https://b.example/"
        await session._on_navigation_signal(
            "https://b.example/", same_document=False, source="url_watch")
        # First capture: parser still running -> shipped early, marked partial.
        await asyncio.sleep(0.05)
        await session._pipeline.drain(1.0)
        first = ws.first("full_document")
        ok(first is not None, "the early document was never sent")
        ok(first.get("partial") is True,
           "the client cannot tell an early document from a settled one: %r" % (first,))

        # The parser finishes; the settled follow-up converges the mirror.
        page.ready = True
        page.checksum = "2:2:2"
        await asyncio.sleep(0.15)
        await session._pipeline.drain(1.0)
        docs = ws.frames("full_document")
        ok(len(docs) >= 2,
           "no settled follow-up followed the early document: %r" % (ws.types(),))
        ok(docs[-1].get("partial") is not True,
           "the follow-up was also marked partial: %r" % (docs[-1],))
        ok(docs[-1]["nav_id"] == first["nav_id"],
           "the follow-up belongs to a different navigation - it could overwrite "
           "a newer page")
        await session.shutdown()

    run(main())


def test_a_navigation_that_cannot_produce_a_document_says_so():
    async def main():
        session, page, ws = make_session("https://a.example/")

        async def broken_capture(settle=None):
            session._last_capture_partial = False
            return None

        session.capture_page = broken_capture
        page.url = "https://b.example/"
        await session._on_navigation_signal(
            "https://b.example/", same_document=False, source="url_watch")
        await asyncio.sleep(0.02)
        await session._pipeline.drain(1.0)
        ok(not any(f["type"] == "navigation" and f.get("phase") == "failed"
                   for f in ws.frames("navigation")),
           "one failure is not a verdict")

        await session.send_page(reason="navigation")
        await session._pipeline.drain(1.0)
        failed = [f for f in ws.frames("navigation") if f.get("phase") == "failed"]
        ok(failed, "the client was left waiting for a document that will never "
                   "arrive: %r" % (ws.types(),))
        ok(failed[0]["nav_id"] == session.nav_id,
           "the failure names a different navigation: %r" % (failed[0],))
        await session.shutdown()

    run(main())


# ---------------------------------------------------------------------------
# 5. the explicit path (operator-driven goto) announces before it navigates
# ---------------------------------------------------------------------------
def test_explicit_navigation_announces_before_goto():
    async def main():
        session, page, ws = make_session("https://a.example/")
        seen = {}

        def on_goto(url):
            seen["frames_at_goto"] = list(ws.types())

        page.on_goto = on_goto
        page.url = "https://b.example/"
        await session.handle_navigation("https://b.example/")
        await session._pipeline.drain(1.0)
        ok(page.goto_calls == 1, "the source browser was never navigated")
        ok(seen.get("frames_at_goto") == ["navigation"],
           "goto ran before the client was told where it was going: %r"
           % (seen.get("frames_at_goto"),))
        ok(session._delta_active in (True, False), "the patch gate ended up unset")
        await session.shutdown()

    run(main())


# ---------------------------------------------------------------------------
# 6. the plumbing in the module itself
# ---------------------------------------------------------------------------
def test_settle_levels():
    ok(dom_capture._settle_for_reason("navigation") == "nav",
       "a navigation must use the bounded settle, not the 5 s readyState wait")
    ok(dom_capture._settle_for_reason("url_change") == "nav",
       "a URL change is a navigation")
    ok(dom_capture._settle_for_reason("coalesce:navigation") == "nav",
       "a coalesced navigation must keep the navigation settle")
    ok(dom_capture._settle_for_reason("interaction:click") == "none",
       "an interaction recapture of a live page must not wait")
    ok(dom_capture._settle_for_reason("nav_settle") == "none",
       "the settled follow-up runs after the parser finished - it must not wait again")
    ok(dom_capture.NAV_READY_MS <= 1000,
       "the navigation ready-state bound is not bounded in any useful way")


def test_observer_carries_the_navigation_signals():
    js = dom_capture._DELTA_OBSERVER_JS
    for token in ("__shifixDocToken", "pushState", "replaceState", "popstate",
                  "hashchange", "__shifixNavHooked", "'nav'"):
        ok(token in js, "the in-page observer does not emit %r" % token)
    ok("sameDocument" in js or "same_document" in js or ", true," in js,
       "the history hooks do not classify the change as same-document")

    call = dom_capture._FAST_CAPTURE_CALL_JS
    ok("out.doc" in call,
       "the full capture does not report which document it was taken from, so "
       "the client cannot tell a superseded patch from a broken one")


def test_history_hooks_can_be_turned_off():
    # The script is built once at import; the capability flag must be readable
    # by whoever rebuilds it (and must exist as a knob).
    ok(hasattr(dom_capture, "NAV_HISTORY_HOOKS"),
       "there is no kill switch for the in-page history hooks")
    ok(hasattr(dom_capture, "NAV_IMMEDIATE") and hasattr(dom_capture, "NAV_SETTLE_FOLLOWUP"),
       "the navigation pipeline has no rollback flags")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("navigation pipeline: %d checks passed" % PASSED)
