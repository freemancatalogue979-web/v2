#!/usr/bin/env python3
"""A crisp live view: the surface renders at CSS x DPR.

"PCM manager, access manager and the stream display don't have a crisp display
because of DPR. Resolution x DPR to get the resolution we need."

Chrome never upscales a screencast frame, so a frame rendered at 1x and then
scaled to fill an operator panel is soft no matter what the panel does. The
page LAYOUT stays in CSS pixels (geometry, breakpoints and click coordinates
are unaffected) and the surface rasterises at `CSS x stream_dpr`.

What this pins:

  * one setting decides the scale (config.stream_dpr, env STREAM_DPR), clamped;
  * every launch path hands that value to the browser — no `1.0` left behind;
  * every screencast asks for CSS x DPR pixels, and the counters in the meta
    message still describe the CSS viewport;
  * operator input arrives in FRAME pixels and is converted back to CSS before
    it reaches page.mouse() — a 2x frame mapped as 1x would click at half
    position;
  * the diagnostic that asserts devicePixelRatio expects the same value.

Run: PYTHONPATH=. python tests/test_stream_scale.py
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CHECKS = 0


def ok(cond, label):
    global CHECKS
    assert cond, label
    CHECKS += 1


def reader(name):
    return (ROOT / name).read_text(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# 1. One setting, one clamp
# ---------------------------------------------------------------------------
import config as cfg_mod  # noqa: E402

ok(0.0 < float(cfg_mod.CONFIG.stream_dpr) <= 3.0,
   "the render scale is a real number in range (got %r)" % cfg_mod.CONFIG.stream_dpr)
ok(cfg_mod.stream_dpr() >= 1.0, "it never goes below 1 (no downscaling)")

saved = os.environ.get("STREAM_DPR")
try:
    from dataclasses import fields
    fresh = cfg_mod.UltraConfig()
    ok(abs(float(fresh.stream_dpr) - 2.0) < 1e-9,
       "default is 2.0 (a 2x raster shown at CSS size is sharp)")

    os.environ["STREAM_DPR"] = "1.5"
    ok(abs(float(cfg_mod.UltraConfig().stream_dpr) - 1.5) < 1e-9,
       "STREAM_DPR overrides it")

    os.environ["STREAM_DPR"] = "9"
    ok(float(cfg_mod.UltraConfig().stream_dpr) <= 3.0,
       "an absurd value is clamped instead of blowing up frame size")

    os.environ["STREAM_DPR"] = "not-a-number"
    ok(float(cfg_mod.UltraConfig().stream_dpr) == 2.0,
       "an unparseable value falls back to the default")
finally:
    if saved is None:
        os.environ.pop("STREAM_DPR", None)
    else:
        os.environ["STREAM_DPR"] = saved

# frame -> CSS conversion (operator canvas -> page.mouse)
ok(abs(cfg_mod.frame_to_css(200, 2) - 100.0) < 1e-9, "a 2x frame maps to CSS")
ok(abs(cfg_mod.frame_to_css(200, 1) - 200.0) < 1e-9, "a 1x frame is unchanged")
ok(cfg_mod.frame_to_css("nonsense", 2) == 0.0, "junk becomes 0, never NaN")
ok(abs(cfg_mod.frame_to_css(90, 0) - 90.0) < 1e-9,
   "a zero scale cannot divide by zero")

# ---------------------------------------------------------------------------
# 2. Every browser launch uses it
# ---------------------------------------------------------------------------
bm = reader("browser_manager.py")
ok("from config import stream_dpr as _stream_dpr" in bm,
   "the browser manager reads the shared scale")
ok("device_scale_factor = 1.0" not in bm,
   "no launch path still pins device_scale_factor to 1.0")
ok("'deviceScaleFactor': 1" not in bm and '"deviceScaleFactor": 1' not in bm,
   "no CDP metrics override still pins the scale to 1")
ok(bm.count("device_scale_factor = _stream_dpr()") >= 3,
   "all three launch paths (direct Chrome, persistent context, simple) use it "
   "(found %d)" % bm.count("device_scale_factor = _stream_dpr()"))
ok(bm.count("'device_scale_factor': device_scale_factor") >= 2,
   "both Playwright context builders pass the scale they were given")
ok("device_scale_factor=device_scale_factor" in bm,
   "and the direct-Chrome launcher receives it too")

# ---------------------------------------------------------------------------
# 3. Every screencast asks for CSS x DPR
# ---------------------------------------------------------------------------
pcm = reader("pcm_manager.py")
ok("from config import stream_dpr, clamp_dpr, frame_to_css" in pcm,
   "PCM uses the shared scale, its clamp and converter")
ok("pixelRatio\": _dpr" in pcm or "pixelRatio': _dpr" in pcm,
   "the PCM browser is launched at the render scale")
ok("def render_scale(self) -> float:" in pcm,
   "the PCM session works out the scale its own pane can show")
ok("pane_w * dpr / css_w" in pcm,
   "pane CSS px x screen DPR, spread over the page's CSS width")
ok("return clamp_dpr(round(pane_w * dpr / css_w, 3), dpr)" in pcm,
   "clamped to the range we render at, screen DPR as the fallback")
ok('"deviceScaleFactor": _scale' in pcm,
   "the surface is RENDERED at that scale (Chrome never upscales a frame)")
ok("w = int(round(int(mcw) * _scale))" in pcm and "h = int(round(int(mch) * _scale))" in pcm,
   "the frame budget is the CSS viewport x that scale")
ok("if max(w, h) > 3200:" in pcm,
   "with a ceiling, so the PNG payload cannot outgrow the socket")
ok("self._cast_scale = _scale" in pcm,
   "the running cast remembers the scale it encoded at")
ok("_dpr = self.capture_scale()" in pcm,
   "input converts with the CAST's scale, not the screen's")
ok("self._cast_latest = raw" in pcm and "self._cast_pump = asyncio.create_task(_pump())" in pcm,
   "frames are broadcast latest-wins through one pump")
ok("await asyncio.sleep(0.008)" in pcm and "1.0 / 30.0" in pcm,
   "paced at ~30fps so a 2x-3x picture cannot turn into lag")
ok("def _stop_frame_pump(self):" in pcm,
   "and the pump is torn down with the cast (no frames from a dead page)")
ok("self._stop_frame_pump()" in pcm.split("async def _stop_screencast_locked")[1][:200],
   "starting with the stop path")
ok("_cast_content = (int(mcw), int(mch), mquality)" in pcm,
   "the content rect stays in CSS pixels")
ok("return int(round(frame_to_css(number, _dpr)))" in pcm,
   "PCM input is converted from frame pixels to CSS")

acc = reader("access_manager.py")
ok("from config import stream_dpr, frame_to_css" in acc,
   "Access uses the shared scale and converter")
ok('"deviceScaleFactor": _dpr' in acc, "the Access page renders at the scale")
ok("_frame_w = int(round(self._cast_width * _dpr))" in acc
   and "_frame_h = int(round(self._cast_height * _dpr))" in acc,
   "the Access screencast asks for CSS x DPR")
ok("point(\"x\"), point(\"y\")" in acc and 'point("x"), point("y")' in acc,
   "Access pointer payloads are converted to CSS")
ok("frame_to_css(value, getattr(self, \"_cast_dpr\", None))" in acc
   or "frame_to_css(value, getattr(self, '_cast_dpr', None))" in acc,
   "using the scale that stream is actually rendering at")

api_src = reader("api.py")
ok("from config import stream_dpr" in api_src, "the admin stream uses the scale")
ok("int(round(max(640, min(int(content_w), 1920)) * _dpr))" in api_src,
   "the Display screencast budget is CSS x DPR too")
ok("'css_width': content_w" in api_src and "'dpr': _dpr" in api_src,
   "the meta message still reports the CSS viewport (and the scale)")

sess = reader("session.py")
ok("from config import frame_to_css as _frame_to_css" in sess
   or "frame_to_css" in sess,
   "the streamed session converts operator payloads")
ok("_px(input_data.get('x', 0))" in sess, "x is converted")
ok("_px(input_data.get('y', 0))" in sess, "y is converted")
ok("_frame_space = bool(input_data.get('frame'))" in sess,
   "only frame-space payloads are converted (visitor input is already CSS)")

# ---------------------------------------------------------------------------
# 4. The operator canvas says which space it measured
# ---------------------------------------------------------------------------
admin = reader("Admin.html")
ok(admin.count("payload.frame = true") == 3,
   "all three operator canvases (Display, PCM, Access) mark frame pixels "
   "(found %d)" % admin.count("payload.frame = true"))

# ---------------------------------------------------------------------------
# 5. The diagnostic agrees with the browser
# ---------------------------------------------------------------------------
diag = reader("diagnostic_runner.py")
ok("config.stream_dpr" in diag,
   "the dpr check is tied to the setting, not a hardcoded 1.0")
scorecard = diag.split("SCORECARD = [")[1].split("]")[0]
dpr_line = [l for l in scorecard.splitlines() if l.strip().startswith('("dpr"')][0]
ok("_STREAM_DPR" in dpr_line, "the expected dpr is the render scale")
ok("1.0" not in dpr_line, "and no longer the old logical-pixel 1.0")

# ---------------------------------------------------------------------------
# 6. Nothing else claims to pin the scale at 1x
# ---------------------------------------------------------------------------
for name in ("browser_manager.py", "session.py", "api.py", "access_manager.py",
             "pcm_manager.py"):
    text = reader(name)
    ok(not re.search(r"logical[- ]pixel policy[^\n]*1\.0", text, re.IGNORECASE),
       "%s has no stale logical-pixel-policy comment" % name)

# ---------------------------------------------------------------------------
# 7. The scale is the DPR of the screen the OPERATOR is using
# ---------------------------------------------------------------------------
# "The PCM manager stream is not crisp: it should be the resolution x the DPR
# of the person using the PCM." A server-side guess is wrong in both
# directions: too low and the frame is soft on a retina panel, too high and it
# is bandwidth for pixels that panel cannot show. So the panel sends its own
# `window.devicePixelRatio` and the session renders at that.

clamp = cfg_mod.clamp_dpr
ok(clamp(2) == 2.0, "a whole-number DPR passes through")
ok(clamp(1.25) == 1.25, "a fractional DPR (125% zoom) is kept")
ok(clamp(0.5) == 1.0, "below 1 is raised to 1 (never downscale)")
ok(clamp(4) == 3.0, "above 3 is capped")
ok(clamp(None) == cfg_mod.stream_dpr(), "junk falls back to the server default")
ok(clamp("nonsense") == cfg_mod.stream_dpr(), "a non-number falls back too")
ok(clamp(float("nan")) == cfg_mod.stream_dpr(), "NaN falls back, never poisons the scale")
ok(clamp(99) == 3.0, "an absurd value cannot blow up the frame budget")

ok("def operator_dpr(self)" in pcm, "the PCM session asks its own operator's screen")
ok("self._op_dpr: Optional[float] = None" in pcm,
   "the scale starts unknown (a watchdog-started session still renders)")
ok("return clamp_dpr(self._op_dpr, stream_dpr())" in pcm,
   "and is clamped when it is set")
ok(pcm.count("self.operator_dpr()") >= 4,
   "every PCM scale use goes through it (launch, metrics, budget, input) "
   "(found %d)" % pcm.count("self.operator_dpr()"))
ok("async def set_view_scale(self, raw: Any, width: Any = None," in pcm,
   "the session takes the operator's scale at runtime")
ok('"deviceScaleFactor": scale' in pcm,
   "and re-asserts the emulated scale on the running page")
rescale = pcm.split("async def set_view_scale")[1].split("async def handle_input")[0]
ok("await self._stop_screencast_locked()" in rescale
   and "await self._start_screencast_locked()" in rescale,
   "the cast is restarted so the NEXT frame is already at the new scale")
ok('"type": "view_scale", "dpr": dpr' in pcm,
   "every subscriber is told which scale the frames are in")

ok('mtype in ("view_scale", "set_dpr")' in api_src,
   "the /ws/pcm socket accepts it")
ok('dpr = await _pcm.set_view_scale(' in api_src
   and 'data.get("dpr"), data.get("width"), data.get("height"))' in api_src,
   "and hands it to the session with the pane size")
ok('"scale": _pcm.capture_scale()' in api_src,
   "the socket echoes the scale the frames are encoded at")

ok("function pcmSendViewScale()" in admin, "the panel sends its screen's scale")
ok("window.devicePixelRatio" in admin.split("function pcmScreenDpr")[1][:220],
   "using the operator's real devicePixelRatio")
ok("Math.min(3, Math.max(1, d))" in admin, "clamped to the range we render at")
ok("pcmSendViewScale();" in admin.split("pcmWs.onopen")[1][:900],
   "on connect, before the first frame is judged")
ok("window.addEventListener('resize', ()=>{ pcmSendViewScale();" in admin,
   "and again when the screen's scale changes (zoom, another monitor)")
ok('"frame_scale": _pcm.render_scale()' in api_src,
   "and /api/pcm/status shows the scale the live view is rendering at")
ok("function pcmPaneSize()" in admin and "width: pane.width, height: pane.height" in admin,
   "the panel also reports the box the frames are shown in")
ok("function pcmPaneResized()" in admin and "pcmPaneSentWidth" in admin,
   "a pane resize renegotiates the scale (debounced, width-gated)")
ok("const k = Math.min(1, availW / w, availH / h);" in admin,
   "and the stage is only ever shrunk to fit, never stretched")
ok("let w = fw / dpr, h = fh / dpr;" in admin,
   "the frame is shown at one frame pixel per SCREEN pixel (÷ devicePixelRatio)")

# ---------------------------------------------------------------------------
# 8. A running session really rescales (not just a source-level claim)
# ---------------------------------------------------------------------------
import asyncio  # noqa: E402
from pcm_manager import PCMManager  # noqa: E402


class _FakeCdp:
    def __init__(self):
        self.sent = []

    async def send(self, method, params=None):
        self.sent.append((method, params or {}))
        return {}


class _FakePage:
    def is_closed(self):
        return False


def _metrics(cdp):
    return [p for m, p in cdp.sent if m == "Emulation.setDeviceMetricsOverride"]


_run = asyncio.new_event_loop()
try:
    session = PCMManager()
    ok(session.operator_dpr() == cfg_mod.stream_dpr(),
       "before the panel says anything, the server default renders")
    session._page = _FakePage()
    session._cdp = _FakeCdp()
    session._mode = "desktop"
    ok(_run.run_until_complete(session.set_view_scale(2)) == 2.0,
       "a 2x screen asks for 2x")
    got = _metrics(session._cdp)
    ok(len(got) == 1, "and the running page is re-emulated exactly once")
    ok(got[0]["deviceScaleFactor"] == 2.0, "at the operator's scale")
    ok(got[0]["width"] == 1280 and got[0]["height"] == 800,
       "with the layout viewport unchanged (only the surface is scaled)")
    ok(_run.run_until_complete(session.set_view_scale(1.5)) == 1.5,
       "125% zoom keeps its fraction")
    ok(session.operator_dpr() == 1.5, "and the session remembers it")
    ok(_run.run_until_complete(session.set_view_scale("nope")) == cfg_mod.stream_dpr(),
       "junk falls back to the server default instead of pinning 0")
    session._mode = "mobile"
    _run.run_until_complete(session.set_view_scale(9))
    last = _metrics(session._cdp)[-1]
    ok(last["deviceScaleFactor"] == 3.0, "an absurd DPR is capped at 3")
    ok(last["width"] == 500 and last["height"] == 687,
       "and a mobile session is rescaled at the mobile viewport")
    ok(last["mobile"] is True, "still emulating a phone")
    session._mode = "desktop"
    session._cast_content = (1280, 800, 85)
    ok(_run.run_until_complete(session.set_view_scale(2, 900, 500)) == 2.0,
       "the DPR is still reported as the DPR")
    ok(session.render_scale() == 1.406,
       "a 900px pane on a 2x screen renders 1.406x (900 x 2 / 1280), got %s"
       % session.render_scale())
    ok(_metrics(session._cdp)[-1]["deviceScaleFactor"] == 1.406,
       "and the surface is re-emulated at that scale")
    ok(_run.run_until_complete(session.set_view_scale(2, 3200, 2000)) == 2.0,
       "the DPR is unchanged by a wide pane")
    ok(session.render_scale() == 3.0,
       "a huge pane is capped at 3x instead of an unbounded frame")
    ok(_run.run_until_complete(session.set_view_scale(2, 0, 0)) == 2.0,
       "a zero-width pane is still just a DPR report")
    ok(session.render_scale() == 3.0,
       "a zero-width (hidden tab) report keeps the last real pane")
    ok(session.capture_scale() == 2.0,
       "with no cast running, input falls back to the screen's DPR")
finally:
    # set_view_scale arms the stall watchdog; stop it before the loop closes
    # so the test leaves nothing pending behind.
    _watch = getattr(session, "_watch_task", None)
    if _watch is not None:
        _watch.cancel()
        try:
            _run.run_until_complete(_watch)
        except BaseException:
            pass
    _run.close()

print("stream scale: %d checks passed" % CHECKS)
