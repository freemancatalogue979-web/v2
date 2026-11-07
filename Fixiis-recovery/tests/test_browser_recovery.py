#!/usr/bin/env python3
"""Browser-crash self-healing: python3 tests/test_browser_recovery.py

Pins the dead-browser detection + recovery wiring in session.py / api.py:
when the remote browser's CDP channel drops ("CDP connection closed" etc.),
the session must relaunch the SAME persistent profile (cookies survive) and
rebind capture, instead of logging capture failures forever.
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PASSED = 0


def ok(cond, label):
    global PASSED
    assert cond, label
    PASSED += 1


session_src = open(os.path.join(ROOT, "session.py"), encoding="utf-8").read()
api_src = open(os.path.join(ROOT, "api.py"), encoding="utf-8").read()

# --- dead-browser markers cover the logged failure shapes --------------------
m = re.search(r"_DEAD_BROWSER_MARKERS = \((.*?)\)", session_src, re.S)
ok(m is not None, "marker tuple exists")
markers = m.group(1).lower()
for needle in ("connection closed", "target closed", "target crashed",
               "browser closed", "browser crashed"):
    ok(needle in markers, f"marker covers {needle!r}")

# the exact error from the field log must be classified as dead-browser
field_err = "evaluate: CDP connection closed (during Runtime.evaluate); outerHTML=failed"
# evaluate the marker check the same way the code does
marker_list = re.findall(r'"([^"]+)"', m.group(1))
ok(any(mk in field_err.lower() for mk in marker_list),
   "field error 'CDP connection closed' is classified as dead-browser")

# --- recovery relaunches the persistent profile -------------------------------
ok("async def _recover_browser(self)" in session_src, "_recover_browser exists")
ok("await self.browser_manager.create_browser(" in session_src,
   "recovery relaunches via browser_manager.create_browser")
ok("self.user_id" in session_src.split("async def _recover_browser")[1][:4500],
   "recovery passes user_id -> same persistent user-data dir (cookies survive the crash)")
ok("self._sync_dom_capture()" in session_src.split("async def _recover_browser")[1][:4500],
   "recovery rebinds the DOM capture to the new page")
ok("self._setup_page_listeners()" in session_src.split("async def _recover_browser")[1][:4500],
   "recovery re-installs tab/popup listeners")
ok("page.goto(url" in session_src.split("async def _recover_browser")[1][:4500],
   "recovery re-navigates to the current URL")
ok("_last_browser_recovery" in session_src and "< 15" in session_src,
   "recovery is rate-limited (one attempt per 15s, no relaunch storm)")

# --- capture path triggers recovery on dead-browser errors --------------------
cap = session_src[session_src.index("async def capture_remote_page"):]
ok("if self._dead_browser_err(exc):" in cap and "_recover_browser()" in cap,
   "capture_remote_page recovers on raised dead-browser errors")
ok('_LAST_FAST_CAPTURE_ERROR' in cap and "_browser_alive()" in cap,
   "capture_remote_page detects swallowed dead-browser capture failures and retries after recovery")

# --- URL watcher no longer spins on a closed page ------------------------------
ok("asyncio.create_task(self._maybe_recover())" in session_src,
   "URL watcher schedules recovery when the active page is closed")

# --- proxy sync + proxy cookie reads also trigger recovery ---------------------
ok("sync_remote_url" in session_src and "_maybe_recover()" in session_src,
   "proxy_sync (sync_remote_url) schedules recovery on dead browser")
ok("session._dead_browser_err(exc)" in api_src and "_maybe_recover()" in api_src,
   "/m/ cookie read schedules recovery when the context is gone")

# --- api.py / session.py still parse -------------------------------------------
import ast
ast.parse(session_src)
ast.parse(api_src)
ok(True, "session.py + api.py parse")

# --- DOM capture stands down while the proxy mirror owns the live view ----------
dom_src = open(os.path.join(ROOT, "dom_capture.py"), encoding="utf-8").read()
ok("def _dom_capture_suppressed" in session_src and "is_capture_enabled()" in session_src,
   "session gates capture on proxy mode")
cap_body = session_src[session_src.index("async def capture_remote_page"):]
ok('reason != "resync_request" and not force' in cap_body,
   "proactive captures skipped in proxy mode; client resync (fallback) still allowed")
nav_body = session_src[session_src.index("async def handle_navigation"):]
ok("_dom_capture_suppressed()" in nav_body and "wait_until='commit'" in nav_body,
   "navigate in proxy mode = light goto only (no capture); watcher sends page_transition for the /m/ redirect")
ok("browser/CDP lost, recovery in progress" in dom_src and "logger.warning" in dom_src,
   "dead-browser capture failure logged as warning (recovery owns it), not ERROR spam")

print("browser recovery: %d checks passed" % PASSED)
