"""The PCM cast really renders at the scale the operator's pane can show.

String checks live in test_stream_scale.py; this drives the manager with a fake
CDP session and asserts the numbers that reach Chrome, because "crisp" is not a
property of a comment:

  * the surface is emulated at pane_css_width * dpr / page_css_width,
  * the screencast is asked for page_css_width * that scale (Chrome never
    upscales a frame, so asking for less is asking for a soft picture),
  * input is converted with the scale the frames were encoded at,
  * frames are broadcast latest-wins through one pump, at a bounded rate.
"""
import asyncio
import base64
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CHECKS = [0]


def ok(cond, label):
    CHECKS[0] += 1
    assert cond, label


class FakeCdp:
    def __init__(self):
        self.sent = []
        self.handlers = {}

    async def send(self, method, params=None):
        self.sent.append((method, dict(params or {})))
        return {}

    def on(self, event, cb):
        self.handlers[event] = cb

    async def detach(self):
        self.sent.append(("detach", {}))

    def params(self, method):
        return [p for m, p in self.sent if m == method]


class FakeContext:
    def __init__(self, cdp):
        self._cdp = cdp
        self.calls = 0

    async def new_cdp_session(self, page):
        self.calls += 1
        return self._cdp


class FakePage:
    def __init__(self, cdp, url="https://example.com/"):
        self.url = url
        self.viewport_size = {"width": 1280, "height": 800}
        self.context = FakeContext(cdp)

    def is_closed(self):
        return False


class FakeWs:
    def __init__(self):
        self.frames = []

    async def send_bytes(self, raw):
        self.frames.append(raw)


async def main():
    from pcm_manager import PCMManager

    cdp = FakeCdp()
    page = FakePage(cdp)
    m = PCMManager()
    m._page = page
    m._cdp = cdp
    m._mode = "desktop"
    m._current_url = page.url

    # ------------------------------------------------------------------ 1
    # 1100 CSS px of pane on a 2x screen, over a 1280 CSS px page.
    m._op_dpr = 2.0
    m._view_css = (1100, 700)
    await m._start_screencast_locked()

    metrics = cdp.params("Emulation.setDeviceMetricsOverride")
    ok(len(metrics) == 1, "the cast asserts the surface scale itself")
    ok(metrics[0]["width"] == 1280 and metrics[0]["height"] == 800,
       "the layout viewport is never resized (only the surface is scaled)")
    ok(metrics[0]["deviceScaleFactor"] == 1.719,
       "surface = pane 1100 x dpr 2 / page 1280 = 1.719, got %s"
       % metrics[0]["deviceScaleFactor"])
    ok(metrics[0]["mobile"] is False, "and it is still a desktop surface")

    start = cdp.params("Page.startScreencast")
    ok(len(start) == 1, "one screencast, started once")
    ok(start[0]["maxWidth"] == 2200,
       "frame budget = 1280 x 1.719 = 2200, got %s" % start[0]["maxWidth"])
    ok(start[0]["maxHeight"] == 1375, "and 800 x 1.719 = 1375 tall")
    ok(start[0]["format"] == "png", "PNG: the panel shows it 1:1, no JPEG ringing")
    ok(m.capture_scale() == 1.719, "the cast remembers the scale it encoded at")
    ok(m._screencast_running is True, "and reports itself as running")

    # ------------------------------------------------------------------ 2
    # A one-pixel-per-screen-pixel frame is what the pane will show.
    ok(abs(2200 / 2.0 - 1100) < 0.5,
       "2200 frame px on a 2x screen is 1100 CSS px: exactly the pane")

    # ------------------------------------------------------------------ 3
    # Input uses the CAST scale, not the screen's DPR (2.0 would be wrong by
    # 16% and every click would drift).
    seen = {}

    class RecPage:
        class mouse:
            @staticmethod
            async def move(x, y):
                seen["move"] = (x, y)

            @staticmethod
            async def down(button="left"):
                seen["down"] = button

            @staticmethod
            async def up(button="left"):
                seen["up"] = button

        @staticmethod
        def is_closed():
            return False

        @staticmethod
        async def wheel(dx, dy):
            seen["wheel"] = (dx, dy)

    m._page = RecPage()
    await m.handle_input({"type": "mousemove", "x": 2200, "y": 1375, "frame": True})
    ok(seen.get("move") == (1280, 800),
       "frame (2200, 1375) is CSS (1280, 800), got %s" % (seen.get("move"),))

    # ------------------------------------------------------------------ 4
    # Frames: latest-wins, one pump, bounded rate.
    m._page = page
    ws = FakeWs()
    m._subs = {ws}
    handler = cdp.handlers.get("Page.screencastFrame")
    ok(callable(handler), "the frame handler is wired to the CDP session")

    def frame(n):
        return {"sessionId": "s%d" % n, "data": base64.b64encode(b"\x89PNG" + bytes([n])).decode()}

    for i in range(5):
        handler(frame(i))
    ok(m._cast_pump is not None and not m._cast_pump.done(),
       "the first frame starts exactly one pump")
    pump = m._cast_pump
    handler(frame(9))
    ok(m._cast_pump is pump, "further frames reuse it instead of spawning tasks")
    ok(m._cast_latest == b"\x89PNG\x09", "the newest frame wins the slot (5 was dropped)")
    for _ in range(40):
        await asyncio.sleep(0.01)
        if ws.frames:
            break
    ok(ws.frames == [b"\x89PNG\x09"],
       "the pump sends the newest frame once, not every queued one (got %d)"
       % len(ws.frames))

    # ------------------------------------------------------------------ 5
    # Teardown leaves nothing running.
    await m._stop_screencast_locked()
    ok(m._cast_pump is None, "the pump is dropped with the cast")
    ok(m._cast_latest is None, "and its frame slot cleared")
    ok(m._cast_scale == 0.0, "the capture scale resets for the next cast")
    ok(m._screencast_running is False, "the cast reports stopped")


_run = asyncio.new_event_loop()
try:
    _run.run_until_complete(main())
finally:
    for task in asyncio.all_tasks(_run):
        task.cancel()
    try:
        _run.run_until_complete(asyncio.gather(*asyncio.all_tasks(_run), return_exceptions=True))
    except BaseException:
        pass
    _run.close()

print("pcm cast: %d checks passed" % CHECKS[0])
