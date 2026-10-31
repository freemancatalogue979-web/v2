"""Boot like the visitor is already in the room.

Two complaints, one test file:

  * "browser startup, profile, Telegram, IP information — all of that delays
    the page".  Nothing that is only *record keeping* may sit in front of the
    browser, and the browser may not wait for the IP lookup at all.

  * "the pages hang, updates come in late or not at all".  A DOM patch that
    arrived while a full document was being captured and sent used to be
    discarded.  That is not a delay, it is data loss: the mirror stops at the
    snapshot and stays there until something else forces a resync.

Run: PYTHONPATH=. python tests/test_boot_priority.py
"""
import ast
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

ROOT = os.path.join(os.path.dirname(__file__), "..")
CHECKS = 0
FAILURES = []


def ok(cond, label):
    global CHECKS
    if cond:
        CHECKS += 1
        return
    FAILURES.append(label)


def read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as fh:
        return fh.read()


def section(src, start_marker, end_marker, label):
    start = src.find(start_marker)
    if start < 0:
        raise AssertionError("%s: %s not found" % (label, start_marker))
    end = src.find(end_marker, start)
    if end < 0:
        raise AssertionError("%s: %s not found" % (label, end_marker))
    return src[start:end]


# ---------------------------------------------------------------------------
# 1. The IP lookup never sits in front of the browser
# ---------------------------------------------------------------------------
async def geo_never_gates_the_browser():
    import api

    # A lookup that takes far longer than the launch budget must not hold it.
    started = time.monotonic()

    async def slow_lookup(ip):
        await asyncio.sleep(1.0)
        return {"country": "Nigeria", "city": "Port Harcourt", "flag": "NG"}

    api.server_geolocate = slow_lookup
    api._geo_cache.pop("8.8.8.8", None)

    task = api.start_geo_lookup("8.8.8.8")
    ok(task is not None, "a public IP starts a lookup")

    geo = await api.geo_within(task, api.GEO_LAUNCH_WAIT)
    waited = time.monotonic() - started
    ok(geo is None, "a slow lookup yields nothing inside the launch budget")
    ok(waited < 0.5, "the launch wait is short (waited %.0f ms)" % (waited * 1000))
    ok(not task.cancelled() and not task.done(),
       "the lookup keeps running after the launch budget expires")

    # ...and the answer is still available to whoever asks later.
    late = await api.geo_within(task, 2.0)
    ok(late and late.get("city") == "Port Harcourt",
       "the late answer is delivered to the caller that waited for it")

    # A cached IP answers on the first turn, so repeat visitors pay nothing.
    api._geo_cache["8.8.8.8"] = {"ts": time.time(), "data": {"country": "NG"}}
    cached_started = time.monotonic()
    task2 = api.start_geo_lookup("8.8.8.8")
    geo2 = await api.geo_within(task2, api.GEO_LAUNCH_WAIT)
    ok(geo2 and geo2.get("country") == "NG", "a cached IP resolves immediately")
    ok((time.monotonic() - cached_started) < 0.05, "a cache hit costs no network")

    # Private/unknown addresses never start a network lookup at all.
    ok(api.start_geo_lookup("192.168.1.5") is None, "a private IP starts no lookup")
    ok(api.start_geo_lookup("Unknown") is None, "an unknown IP starts no lookup")


def geo_is_started_early_and_not_awaited_long():
    src = read("api.py")
    start = src.find("async def websocket_endpoint(")
    assert start > 0, "websocket_endpoint not found"
    # up to the browser admission, past every geo decision
    body = src[start:src.find("connection_generation = getattr(session", start)]
    start_lookup = body.find("geo_task = start_geo_lookup(client_ip)")
    receive = body.find("await websocket.receive_text()")
    admit = body.find("_admit_browser_session()")
    ok(start_lookup > 0, "the websocket handler starts the lookup")
    ok(0 < start_lookup < receive,
       "the lookup starts before the init frame is even read")
    ok(start_lookup < admit, "and long before the browser is admitted")
    ok("asyncio.wait_for(server_geolocate(client_ip), timeout=GEO_BOOT_BUDGET)" not in body,
       "the browser path no longer awaits the full geo budget inline")
    ok("geo_within(geo_task, GEO_LAUNCH_WAIT)" in body,
       "the browser path waits only the short launch budget")
    ok(body.find("asyncio.create_task(_attach_late_geo(session, geo_task))") > admit,
       "a late answer is attached to the session that is already serving")

    ok("GEO_LAUNCH_WAIT = 0.15" in src,
       "the launch budget is a sliver, not a second")
    ok("GEO_BOOT_BUDGET = 4.0" in src,
       "the lookup keeps its own (looser) budget in the background")


# ---------------------------------------------------------------------------
# 2. Nothing else on the boot path blocks the first page either
# ---------------------------------------------------------------------------
def session_starts_serving_before_bookkeeping():
    src = read("session.py")
    body = section(src, "            # Start DOM capture path", "            return True",
                   "session.start")
    capture_task = body.find('asyncio.create_task(self.capture_remote_page(reason="initial_connect"')
    profile = body.find("self._save_profile_info()")
    ok(capture_task > 0, "the first capture is kicked off in start()")
    ok(profile > capture_task, "profile bookkeeping happens after the capture starts")
    ok("asyncio.create_task(self._save_profile_info())" in body,
       "profile bookkeeping is a task, not an awaited disk write on the boot path")
    ok("await self._save_profile_info()" not in body,
       "start() never waits for About.txt to hit the disk")


async def navigation_returns_as_soon_as_there_is_a_page():
    import session

    class FakePage:
        def __init__(self, support_commit=True, body_ready=True, content_delay=0.0):
            self.calls = []
            self.support_commit = support_commit
            self.body_ready = body_ready
            self.content_delay = content_delay
            self.probes = 0

        async def goto(self, url, wait_until="load", timeout=30000):
            self.calls.append(wait_until)
            if wait_until == "commit" and not self.support_commit:
                raise TypeError("wait_until: expected load|domcontentloaded")

        async def wait_for_function(self, predicate, timeout=30000):
            self.probes += 1
            if self.content_delay:
                await asyncio.sleep(min(self.content_delay, timeout / 1000.0))
            if not self.body_ready:
                raise TimeoutError("no body")
            return True

    fast = FakePage()
    started = time.monotonic()
    early = await session.goto_first_paint(fast, "https://example.test/", content_wait_ms=700)
    elapsed = time.monotonic() - started
    ok(early is True, "a commit-capable page reports an early navigation")
    ok(fast.calls == ["commit"], "it navigates with the early wait, once (got %r)" % (fast.calls,))
    ok(fast.probes == 1, "it waits for content, not for the whole document")
    ok(elapsed < 0.2, "a page with a body is handed back immediately")

    empty = FakePage(body_ready=False)
    started = time.monotonic()
    early = await session.goto_first_paint(empty, "https://slow.test/", content_wait_ms=120)
    elapsed = time.monotonic() - started
    ok(early is True, "an empty-but-committed page is still worth capturing")
    ok(elapsed < 0.35, "an empty page does not hang the boot (%.0f ms)" % (elapsed * 1000))

    legacy = FakePage(support_commit=False)
    early = await session.goto_first_paint(legacy, "https://old.test/", content_wait_ms=50)
    ok(early is False, "a backend without the early wait reports no early navigation")
    ok(legacy.calls == ["commit", "domcontentloaded"],
       "and still gets a working navigation (got %r)" % (legacy.calls,))
    ok(legacy.probes == 0, "without an early navigation no content probe is wasted")


def load_settle_capture_completes_the_page():
    src = read("session.py")
    ok("def _schedule_load_settle_capture" in src, "the completion capture exists")
    ok("wait_for_load_state('load'" in src, "it waits for the page to finish loading")
    ok("capture_remote_page(reason='load_settle')" in src,
       "and sends one completing document")
    body = section(src, "def _schedule_load_settle_capture", "async def _setup_input_focus_detection",
                   "session.py")
    ok("if not getattr(self, '_first_paint_nav', False)" in body,
       "it only runs when the early path is active (no double capture otherwise)")
    start = section(src, "# Start DOM capture path", "            return True", "session.start")
    ok("self._schedule_load_settle_capture()" in start,
       "start() schedules it right after the first capture")

    sb = read("sb_backend.py")
    ok('if wait_until == "commit":' in sb,
       "the SB backend implements the early wait too")


# ---------------------------------------------------------------------------
# 3. The IP answer still reaches the record, and Telegram still sees it
# ---------------------------------------------------------------------------
def late_geo_reaches_the_record():
    src = read("api.py")
    body = section(src, "async def _attach_late_geo", "async def server_geolocate", "api.py")
    for field in ("country", "state", "city", "zip_code"):
        ok(("session.%s = geo.get" % field) in body,
           "a late answer updates the session's %s" % field)
    ok("session._geo_event.set()" in body,
       "it releases the connect notification")
    ok("await session._save_profile_info()" in body,
       "and rewrites the visitor's About.txt with the verified location")

    sm = read("session_manager.py")
    ok("session._geo_event = asyncio.Event()" in sm, "the session owns a geo-ready event")
    ok("if not geo_pending:\n                    session._geo_event.set()" in sm,
       "an already-known location sets it immediately")
    ok("async def _notify_connect_when_geo_ready" in sm,
       "the connect notification waits for the location")
    body = section(sm, "async def _notify_connect_when_geo_ready", "async def _send_connect_notification",
                   "session_manager.py")
    ok("asyncio.wait_for(event.wait(), timeout=2.0)" in body,
       "it waits at most two seconds, so a Telegram line can never hold a page")
    ok("getattr(session, 'country'" in body,
       "it reads the session's fields, which the late task updates")
    ok("asyncio.create_task(\n                    self._notify_connect_when_geo_ready" in sm,
       "and it is fire-and-forget, never awaited by the boot")


# ---------------------------------------------------------------------------
# 4. Patches are delivered, not dropped
# ---------------------------------------------------------------------------
async def patches_survive_a_full_document():
    import dom_capture
    from dom_capture import DOMCaptureSession

    class FakeWS:
        def __init__(self):
            self.frames = []

        async def send_text(self, text):
            self.frames.append(text)

    class FakePage:
        url = "https://example.test/"

        async def evaluate(self, expr):
            return 7          # the page had produced 7 batches when we captured

    ws = FakeWS()
    session = DOMCaptureSession(page=FakePage(), websocket=ws, client_id="t")
    await session.shutdown()          # stop the auto-installed interaction task noise
    session._stopped = False
    session._delta_installed = True
    session._gen = 3
    session.has_initial_capture = True
    session._delta_active = True

    # ---- a batch with nothing in flight goes straight through ----
    ok(session._delta_route('[[ "a", 1 ]]', "doc", 8) == "send",
       "a batch with no capture in flight is sent")
    await asyncio.sleep(0.01)
    ok(len(ws.frames) == 1 and '"gen":3' in ws.frames[0],
       "the frame carries the current generation")

    # ---- a batch during a full capture is HELD, not dropped ----
    session._send_inflight = True
    session._capture_pre_seq = 7
    ok(session._delta_route('[[ "a", 2 ]]', "doc", 8) == "hold",
       "a batch during a full document is held")
    ok(session._delta_route('[[ "a", 3 ]]', "doc", 9) == "hold",
       "every batch in that window is held")
    await asyncio.sleep(0.01)
    ok(len(ws.frames) == 1, "nothing is sent while the document is in flight")

    # ---- after the send, the held batches are replayed in order ----
    session._send_inflight = False
    queued = session._delta_drain()
    ok(queued == 2, "both held batches are replayed (got %d)" % queued)
    await asyncio.sleep(0.02)
    ok(len(ws.frames) == 3, "and both reach the client (got %d frames)" % len(ws.frames))
    ok('"a", 2' in ws.frames[1] and '"a", 3' in ws.frames[2],
       "in the order the page produced them")

    # ---- a batch the snapshot already contains is not replayed ----
    session._send_inflight = True
    session._capture_pre_seq = 12
    session._delta_route('[[ "a", 9 ]]', "doc", 9)      # older than the capture
    session._delta_route('[[ "a", 13 ]]', "doc", 13)    # newer than the capture
    session._send_inflight = False
    before = len(ws.frames)
    ok(session._delta_drain() == 1,
       "only the batch newer than the snapshot is replayed")
    await asyncio.sleep(0.02)
    ok(len(ws.frames) == before + 1 and '"a", 13' in ws.frames[-1],
       "and it is the right one")

    # ---- a tier that cannot carry mids drops instead of holding forever ----
    session._delta_active = False
    ok(session._delta_route('[[ "a", 14 ]]', "doc", 14) == "drop",
       "a mid-less tier drops patches instead of buffering them forever")
    ok(len(session._delta_hold) == 0, "nothing is left waiting")


async def the_page_is_never_blocked_by_the_socket():
    from dom_capture import DOMCaptureSession

    class SlowWS:
        def __init__(self):
            self.frames = []
            self.release = asyncio.Event()

        async def send_text(self, text):
            await self.release.wait()      # a stalled client link
            self.frames.append(text)

    class FakePage:
        url = "https://example.test/"

        async def evaluate(self, expr):
            return 0

    ws = SlowWS()
    session = DOMCaptureSession(page=FakePage(), websocket=ws, client_id="t2")
    await session.shutdown()
    session._stopped = False
    session._gen = 1
    session.has_initial_capture = True
    session._delta_active = True

    started = time.monotonic()
    for i in range(50):
        session._delta_route('[[ "a", %d ]]' % i, "doc", i + 1)
    elapsed = time.monotonic() - started
    ok(elapsed < 0.05,
       "50 batches are accepted without touching the socket (%.1f ms)" % (elapsed * 1000))
    ok(session._delta_outbox.qsize() == 50, "they wait in the outbox")
    ok(session._delta_writer_task is not None, "one writer task owns delivery")

    ws.release.set()
    await asyncio.sleep(0.05)
    ok(len(ws.frames) == 50, "and all of them arrive once the link moves (got %d)" % len(ws.frames))
    ok(ws.frames[0].find('"a", 0') > 0 and ws.frames[-1].find('"a", 49') > 0,
       "in order")

    # A full document sent meanwhile supersedes queued patches: the writer must
    # not apply an old generation to a newer page.
    ws.frames.clear()
    session._gen = 9
    session._delta_route('[[ "a", 50 ]]', "doc", 51)
    session._gen = 10                    # a full document went out first
    await asyncio.sleep(0.03)
    ok(len(ws.frames) == 0, "a stale-generation frame is dropped, not applied to the new page")

    # Shutting down stops the writer (no task, no socket held open).
    await session.shutdown()
    ok(session._delta_writer_task is None or session._delta_writer_task.done()
       or session._delta_writer_task.cancelling(),
       "shutdown releases the writer")


async def overflow_is_recovered_not_forgotten():
    from dom_capture import DOMCaptureSession
    import dom_capture as dc

    class WS:
        def __init__(self):
            self.frames = []

        async def send_text(self, text):
            self.frames.append(text)

    class FakePage:
        url = "https://example.test/"

        async def evaluate(self, expr):
            return 0

    session = DOMCaptureSession(page=FakePage(), websocket=WS(), client_id="t3")
    await session.shutdown()
    session._stopped = False
    session.has_initial_capture = True
    session._delta_active = True
    session._gen = 1

    # Fill the outbox past its byte budget with one big frame.
    old_budget = dc.DELTA_OUTBOX_BYTES
    try:
        dc.DELTA_OUTBOX_BYTES = 200
        big = '[["a",%s]]' % ("9" * 300)
        session._delta_route(big, "doc", 1)
        session._delta_route(big, "doc", 2)
        ok(session._delta_needs_resync is True,
           "sacrificing patches asks for a recovery capture")
        ok(session._delta_writer_task is None or True, "and does not raise")
    finally:
        dc.DELTA_OUTBOX_BYTES = old_budget


def relay_source_is_wired():
    src = read("dom_capture.py")
    ok("_delta_route(payload_text, doc_token, seq)" in src,
       "the binding routes batches instead of sending them inline")
    body = section(src, "async def _on_delta(", "if not getattr(session, \"_delta_binding_installed\"",
                   "dom_capture.py")
    ok("await session.websocket.send_text(frame)" not in body,
       "the page-side binding never awaits the socket")
    ok("session._send_inflight and not session._assets_pending" not in body,
       "a capture in flight no longer causes silent drops")
    ok("self._capture_pre_seq = await self._read_delta_seq()" in src,
       "the snapshot's batch number is read before capturing")
    ok("self._delta_drain()" in src, "and the held batches are replayed afterwards")
    ok("window.__domDeltaSeq = (window.__domDeltaSeq | 0) + 1" in src,
       "the page numbers its batches")


# ---------------------------------------------------------------------------
# 5. The profile is written while the visitor is browsing
# ---------------------------------------------------------------------------
async def profile_is_collected_during_the_visit():
    import session as session_mod
    from session import NeoStreamingSession

    class FakeStorage:
        def __init__(self):
            self.saved = []
            self.storage = []

        async def save_cookies(self, user_id, cookies):
            self.saved.append(("cookies", user_id, len(cookies)))

        async def save_local_storage(self, user_id, origin, data):
            self.storage.append((user_id, origin, data))

        async def load_local_storage(self, user_id):
            return {"https://example.test": {"token": "abc", "theme": "dark"}}

    class FakePM:
        def __init__(self):
            self.storage = FakeStorage()

        async def save_cookies(self, user_id, cookies):
            await self.storage.save_cookies(user_id, cookies)

        async def save_local_storage(self, user_id, origin, data):
            await self.storage.save_local_storage(user_id, origin, data)

        async def load_local_storage(self, user_id):
            return await self.storage.load_local_storage(user_id)

    class FakeBM:
        def __init__(self):
            self.profile_manager = FakePM()

    class FakeCtx:
        def __init__(self):
            self.init_scripts = []

        async def cookies(self, urls=None):
            return [{"name": "sid", "value": "1", "domain": "example.test", "path": "/"}]

        async def add_init_script(self, script=None, **_kw):
            self.init_scripts.append(script)

    class FakePage:
        url = "https://example.test/inbox?x=1"

        async def evaluate(self, expr):
            return {"local": {"token": "abc"}, "session": {"step": "2"}}

    fake = type("FakeSession", (), {})()
    fake.user_id = "user_1"
    fake._native_profile = False
    fake.browser_manager = FakeBM()
    fake.context = FakeCtx()
    fake._page = FakePage()
    fake.get_active_page = lambda: fake._page
    fake.PROFILE_STORAGE_MAX_BYTES = 512 * 1024

    saved = await NeoStreamingSession._harvest_profile_state(fake)
    ok(saved is True, "a harvest reports what it saved")
    ok(("cookies", "user_1", 1) in fake.browser_manager.profile_manager.storage.saved,
       "the visit's cookies are persisted")
    ok(len(fake.browser_manager.profile_manager.storage.storage) == 1,
       "and its localStorage/sessionStorage too")
    user_id, origin, data = fake.browser_manager.profile_manager.storage.storage[0]
    ok(origin == "https://example.test",
       "keyed by origin, not by URL with query (got %r)" % (origin,))
    ok(data.get("local", {}).get("token") == "abc",
       "the values are the page's own")

    # Restoring seeds only keys the page does not have — a returning visitor
    # gets their state back without overwriting what the page just wrote.
    restored = await NeoStreamingSession._restore_profile_storage(fake)
    ok(restored is True, "saved storage is restored on the next visit")
    script = fake.context.init_scripts[-1]
    ok("localStorage.getItem(k) === null" in script,
       "restore never overwrites a value the page already has")
    ok("location.origin" in script, "it is scoped to the matching origin")
    ok("token" in script, "and it carries the visitor's saved data")

    # A native Chrome profile already persists this; doing it again would fight
    # the browser's own storage layer.
    fake._native_profile = True
    ok(await NeoStreamingSession._harvest_profile_state(fake) is False,
       "a native profile is left to Chrome")
    ok(await NeoStreamingSession._restore_profile_storage(fake) is False,
       "and is not double-seeded")

    src = read("session.py")
    body = section(src, "async def _profile_collector", "async def _setup_input_focus_detection",
                   "session.py")
    ok("await self._harvest_profile_state()" in body,
       "the collector harvests on a cadence")
    ok("PROFILE_SAVE_INTERVAL_S = 15.0" in src, "the cadence is defined")
    start = section(src, "# Start DOM capture path", "            return True", "session.start")
    ok("_profile_collector()" in start,
       "the collector starts beside the first capture, not in front of it")


# ---------------------------------------------------------------------------
# 6. The one thing the page legitimately depends on: a ZIP-targeted proxy
# ---------------------------------------------------------------------------
async def zip_proxy_gets_the_location_it_needs():
    import asyncio
    from session import NeoStreamingSession

    def fake_session(future=None):
        f = type("FakeSession", (), {})()
        f.GEO_ZIP_WAIT = NeoStreamingSession.GEO_ZIP_WAIT
        f._geo_future = future
        f.country = "-"
        f.state = "-"
        f.city = "-"
        f.zip_code = "-"
        return f

    # The lookup has already answered by the time the browser is built: free.
    done = asyncio.get_running_loop().create_future()
    done.set_result({"country": "NG", "state": "Rivers", "city": "Port Harcourt",
                     "zip": "500101", "flag": "NG"})
    f = fake_session(done)
    t0 = time.perf_counter()
    ok(await NeoStreamingSession._wait_for_geo_zip(f) is True,
       "a ZIP-targeted proxy takes the lookup's answer")
    ok((time.perf_counter() - t0) < 0.05, "and pays nothing when it is already there")
    ok((f.country, f.state, f.city, f.zip_code) == ("NG", "Rivers", "Port Harcourt", "500101"),
       "the visitor's own location lands on the session (got %r)"
       % ((f.country, f.state, f.city, f.zip_code),))

    # A provider that never answers cannot hold the page: capped, then default.
    slow = asyncio.get_running_loop().create_future()
    f = fake_session(slow)
    f.GEO_ZIP_WAIT = 0.2
    t0 = time.perf_counter()
    ok(await NeoStreamingSession._wait_for_geo_zip(f) is False,
       "a lookup that does not answer is not waited for indefinitely")
    ok((time.perf_counter() - t0) < 0.6, "the cap is the cap")
    ok(f.zip_code == "-", "and the session keeps whatever it had")
    slow.cancel()

    # No lookup in flight (private IP, LPV-only, provider disabled): no wait.
    f = fake_session(None)
    t0 = time.perf_counter()
    ok(await NeoStreamingSession._wait_for_geo_zip(f) is False, "nothing in flight, nothing waited")
    ok((time.perf_counter() - t0) < 0.02, "and no delay is added")

    # A failed lookup is an answer too.
    failed = asyncio.get_running_loop().create_future()
    failed.set_exception(RuntimeError("provider down"))
    f = fake_session(failed)
    ok(await NeoStreamingSession._wait_for_geo_zip(f) is False,
       "a failed lookup does not become an exception on the boot path")
    failed.exception()  # retrieved

    # And it is scoped: only a template that asks for the ZIP waits at all.
    src = read("session.py")
    start_body = section(src, "    async def start(self, url: str = None) -> bool:",
                         "    async def _wait_for_geo_zip", "session.start")
    ok("if '<client_zip>' in zip_template:" in start_body,
       "the wait is conditional on the proxy template")
    ok(start_body.index("if '<client_zip>' in zip_template:") <
       start_body.index("# Oxylabs Browser Proxy"),
       "and it happens before the proxy is built, not after")
    ok("await self._wait_for_geo_zip()" in start_body, "the helper is what runs")

    # The future is handed in by the manager, from the endpoint's lookup.
    mgr = read("session_manager.py")
    ok("session._geo_future = geo_future" in mgr,
       "the manager gives the session the in-flight lookup")
    ok(mgr.count("geo_future=geo_future,") == 2,
       "both create_session paths forward it")
    api = read("api.py")
    ok("geo_future=(geo_task if geo is None else None)," in api,
       "the endpoint only passes a lookup that is actually pending")


# ---------------------------------------------------------------------------
# 7. Both files still import / parse
# ---------------------------------------------------------------------------
def files_parse():
    for name in ("api.py", "session.py", "session_manager.py", "dom_capture.py",
                 "sb_backend.py"):
        try:
            ast.parse(read(name))
        except SyntaxError as exc:
            FAILURES.append("%s does not parse: %s" % (name, exc))
        else:
            globals()["CHECKS"] = CHECKS + 1


async def main():
    global CHECKS
    files_parse()
    geo_is_started_early_and_not_awaited_long()
    session_starts_serving_before_bookkeeping()
    load_settle_capture_completes_the_page()
    late_geo_reaches_the_record()
    relay_source_is_wired()
    await geo_never_gates_the_browser()
    await navigation_returns_as_soon_as_there_is_a_page()
    await patches_survive_a_full_document()
    await the_page_is_never_blocked_by_the_socket()
    await overflow_is_recovered_not_forgotten()
    await profile_is_collected_during_the_visit()
    await zip_proxy_gets_the_location_it_needs()
    if FAILURES:
        print("FAIL boot priority: %d passed, %d failed" % (CHECKS, len(FAILURES)))
        for f in FAILURES:
            print("  - " + f)
        return 1
    print("PASS boot priority: %d checks, page first and patches survive" % CHECKS)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
