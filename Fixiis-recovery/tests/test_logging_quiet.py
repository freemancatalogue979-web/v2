"""Logging must not be the thing that costs the app.

Two rules are pinned here:

* a message on a per-event path (pointer move, keystroke, frame) is said once
  per window, not once per event — nobody reads 400 identical lines a second,
  and formatting + writing + flushing them is real work on the hot path;
* the shipped client page says nothing at all unless it is asked to
  (`?debug=1`), and the server logs at WARNING by default.

Run: PYTHONPATH=. python tests/test_logging_quiet.py
"""
from __future__ import annotations

import logging
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHECKS = 0


def ok(cond, label):
    global CHECKS
    assert cond, label
    CHECKS += 1


# ---------------------------------------------------------------------------
# 1. The throttle itself
# ---------------------------------------------------------------------------
import log_utils


class FakeLogger:
    def __init__(self):
        self.lines = []

    def log(self, level, message, *args, **kwargs):
        self.lines.append((level, message % args if args else message))


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


clock = Clock()
real_monotonic = log_utils.time.monotonic
log_utils.time.monotonic = clock
try:
    log_utils.reset()
    log = FakeLogger()

    first = log_utils.throttled(log, "k", "[INPUT] drop session=%s", "abc")
    ok(first is True, "the first occurrence is always written")
    ok(log.lines and log.lines[0][1] == "[INPUT] drop session=abc", "with its arguments filled in")
    ok(log.lines[0][0] == logging.WARNING, "at the level asked for (warning by default)")

    for _ in range(400):
        log_utils.throttled(log, "k", "[INPUT] drop session=%s", "abc")
    ok(len(log.lines) == 1, "400 identical events inside the window produce no more lines")
    ok(log_utils.suppressed("k") == 400, "but the count is kept")

    clock.now += 31
    again = log_utils.throttled(log, "k", "[INPUT] drop session=%s", "abc")
    ok(again is True, "after the window it is written once more")
    ok("+400 more" in log.lines[-1][1], "and says how many were swallowed (got %r)" % log.lines[-1][1])

    # Different keys do not share a window.
    log_utils.throttled(log, "other", "second key")
    ok(len(log.lines) == 3, "another key logs immediately")

    # A decorator-level failure cannot break a session.
    class Boom(FakeLogger):
        def log(self, *a, **k):
            raise RuntimeError("handler exploded")

    log_utils.reset()
    ok(log_utils.throttled(Boom(), "boom", "x") is False,
       "a logging failure is swallowed, never raised into the caller")
finally:
    log_utils.time.monotonic = real_monotonic
    log_utils.reset()


# ---------------------------------------------------------------------------
# 2. The places that used to flood are throttled
# ---------------------------------------------------------------------------
def source(name):
    return (ROOT / name).read_text(encoding="utf-8")


session = source("session.py")
ok("from log_utils import throttled" in session, "session.py imports the throttle")
ok('throttled(\n                logger, "input-drop-disabled-' in session,
   "an input event that lands while inputs are off is throttled")
ok('throttled(\n                logger, "input-drop-inactive-' in session,
   "the inactive-session drop is throttled")
ok('throttled(\n                    logger, "input-key-rejected-' in session,
   "a rejected key is throttled")
ok('throttled(\n                            logger, "input-click-miss-' in session,
   "a click that was not dispatched is throttled")
ok(source("pcm_manager.py").count("throttled(") >= 1, "pcm_manager input failures are throttled")
ok(source("sb_backend.py").count("throttled(") >= 4, "every SB input-dispatch failure is throttled")
ok(source("webrtc_stream.py").count("throttled(") >= 2, "per-frame capture warnings are throttled")

# Nothing on those paths still logs per event unconditionally.
for name, needle in [
    ("session.py", 'logger.warning("[INPUT][drop] session=%s inputs_enabled=False"'),
    ("session.py", 'logger.warning(\n                    "[INPUT][key-rejected]'),
    ("sb_backend.py", 'logger.warning("[SB][CDP-INPUT]'),
    ("sb_backend.py", 'logger.warning("[SB][W3C-INPUT] mouse'),
    ("pcm_manager.py", 'logger.warning(f"[PCM] input'),
]:
    ok(needle not in source(name), "%s: no un-throttled per-event line (%s)" % (name, needle[:28]))

# ---------------------------------------------------------------------------
# 3. The client page is silent unless asked
# ---------------------------------------------------------------------------
client = source("client.html")
ok("window.__SHF_DEBUG" in client, "the client has a debug switch")
ok("'shf_debug'" in client, "it can be turned on from localStorage too")

log_calls = [m.start() for m in re.finditer(r"console\.log\(", client)]
ok(len(log_calls) >= 1, "the page still has a couple of diagnostics")
for at in log_calls:
    window = client[max(0, at - 260):at]
    ok("__SHF_DEBUG" in window,
       "every console.log is behind the debug switch, never on by default")

ok("const dlog = function ()" in client, "there is one gated log helper")
ok("const warnRare = function (key)" in client, "and one rate-limited warning helper")

for gone in ("[INPUT] wheel capture listener attached",
             "[INPUT] keydown listener attached",
             "[INPUT] keyup listener attached",
             "[INPUT] streamContainer activity listeners attached",
             "[INPUT] Input handler initialization complete"):
    ok(gone not in client, "boot chatter is gone: " + gone)

for key in ("patch-ops-failed", "op-unknown-", "region-missing-", "body-replace-empty"):
    ok(key in client, "a per-patch warning uses warnRare (%s)" % key)
ok(client.count("warnRare(") >= 4, "warnRare is actually used, not just defined")
# 15 s window, so a failing patch loop cannot print per patch.
ok(re.search(r"const warnRare = function \(key\) \{[\s\S]{0,260}< 15000", client),
   "warnRare holds a warning to once per 15 s")

# ---------------------------------------------------------------------------
# 4. The server is quiet by default
# ---------------------------------------------------------------------------
main = source("main.py")
ok('os.environ.get("LOG_LEVEL") or "WARNING"' in main,
   "the server logs at WARNING unless LOG_LEVEL says otherwise")

print("logging: %d checks passed" % CHECKS)
