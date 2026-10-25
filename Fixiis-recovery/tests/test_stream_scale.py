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
ok("from config import stream_dpr, frame_to_css" in pcm,
   "PCM uses the shared scale and converter")
ok("pixelRatio\": _dpr" in pcm or "pixelRatio': _dpr" in pcm,
   "the PCM browser is launched at the render scale")
ok("'deviceScaleFactor': stream_dpr()" in pcm,
   "and any metrics re-apply keeps it (no flip back to 1x mid-stream)")
ok("int(round(css_w * _dpr))" in pcm and "int(round(css_h * _dpr))" in pcm,
   "the frame budget is the CSS viewport x DPR")
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

print("stream scale: %d checks passed" % CHECKS)
