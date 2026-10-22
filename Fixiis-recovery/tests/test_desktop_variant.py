#!/usr/bin/env python3
"""Device versions: one workflow, a phone build and a desktop build.

Run: python3 tests/test_desktop_variant.py

A page can declare who it is built for ('mobile' / 'desktop' / 'any') and be
paired with the build for the other audience ('variant_page_id'). A workflow
still references ONE page; the server resolves which HTML to deliver from the
visitor's device (is_mobile in the client's boot payload). Existing pages stay
unpaired, so every old workflow behaves exactly as before.

These checks cover the store (migration, pairing, resolution) plus the wiring
in api.py / Admin.html that makes the resolution reach the client.
"""
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

checks = 0


def ok(cond, label):
    global checks
    if not cond:
        raise AssertionError(label)
    checks += 1


def eq(actual, expected, label):
    ok(actual == expected, f"{label} (expected {expected!r}, got {actual!r})")


TMP = Path(tempfile.mkdtemp(prefix="lpv-device-"))
DB_PATH = TMP / "lpv.sqlite3"
PAGES_DIR = TMP / "pages"
PAGES_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# 1) migration: an OLD database (no device columns) gains them in place.
# ---------------------------------------------------------------------------
conn = sqlite3.connect(DB_PATH)
conn.executescript(
    """
    CREATE TABLE lpv_pages (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        description TEXT DEFAULT '',
        tags TEXT DEFAULT '[]',
        file_path TEXT NOT NULL,
        size_bytes INTEGER DEFAULT 0,
        version INTEGER DEFAULT 1,
        created_at REAL,
        updated_at REAL,
        bindings TEXT DEFAULT '[]',
        guard TEXT DEFAULT '{}'
    );
    INSERT INTO lpv_pages (id, name, file_path, version, created_at, updated_at)
    VALUES ('legacy1', 'legacy.html', '%s', 1, 0, 0);
    """
    % (PAGES_DIR / "legacy1.html")
)
conn.commit()
conn.close()

import lpv_store  # noqa: E402  (after the temp DB exists)

lpv_store.LPV_DB_PATH = DB_PATH
lpv_store.LPV_PAGES_DIR = PAGES_DIR
lpv_store.init_db()

with lpv_store.get_conn() as _conn:
    cols = {r[1] for r in _conn.execute("PRAGMA table_info(lpv_pages)").fetchall()}
ok("device" in cols, "migration adds lpv_pages.device")
ok("variant_page_id" in cols, "migration adds lpv_pages.variant_page_id")

# Idempotent: running it again must not raise.
lpv_store.init_db()
lpv_store.init_db()
ok(True, "init_db is re-runnable")

legacy = lpv_store.get_page("legacy1")
eq(legacy["device"], "any", "an old page defaults to 'any'")
eq(legacy["variant_page_id"], "", "an old page has no pair")

# ---------------------------------------------------------------------------
# 2) defaults + normalisation
# ---------------------------------------------------------------------------
eq(lpv_store.normalise_device(None), "any", "missing device -> any")
eq(lpv_store.normalise_device("MOBILE"), "mobile", "device is case-insensitive")
eq(lpv_store.normalise_device("tablet"), "any", "unknown device -> any (never raises)")

# ---------------------------------------------------------------------------
# 3) pairing
# ---------------------------------------------------------------------------
phone = lpv_store.save_page("phone-signin", "<html><body>phone build</body></html>")
desktop = lpv_store.save_page("desktop-signin", "<html><body>desktop build</body></html>")
eq(phone["device"], "any", "a fresh page starts unpaired")
eq(phone["variant_page_id"], "", "a fresh page has no variant")

phone = lpv_store.set_page_device(phone["id"], "mobile", desktop["id"])
eq(phone["device"], "mobile", "the page records the audience it was built for")
eq(phone["variant_page_id"], desktop["id"], "the page records its counterpart")

desk = lpv_store.get_page(desktop["id"])
eq(desk["device"], "desktop", "the counterpart takes the complementary role")
eq(desk["variant_page_id"], phone["id"], "the pairing is symmetric")

# Saving the same page again (re-upload) must not clobber the pairing.
again = lpv_store.save_page("phone-signin", "<html><body>phone build v2</body></html>")
eq(again["device"], "mobile", "re-upload keeps the device")
eq(again["variant_page_id"], desktop["id"], "re-upload keeps the pair")
eq(again["version"], 2, "re-upload still versions the page")

# ---------------------------------------------------------------------------
# 4) resolution — the whole feature in one table
# ---------------------------------------------------------------------------
def resolve(page_id, device):
    page, swapped = lpv_store.resolve_page_for_device(page_id, device)
    return (page or {}).get("name"), swapped


eq(resolve(phone["id"], "mobile")[0], "phone-signin.html",
   "a phone visitor gets the phone build")
eq(resolve(phone["id"], "desktop"), ("desktop-signin.html", phone["id"]),
   "a desktop visitor gets the desktop build (from either member)")
eq(resolve(desktop["id"], "mobile"), ("phone-signin.html", desktop["id"]),
   "referencing the desktop build still serves phones the phone build")
eq(resolve(desktop["id"], "desktop")[0], "desktop-signin.html",
   "referencing the desktop build serves desktops it")
eq(resolve(phone["id"], "any")[0], "phone-signin.html",
   "an unknown device is served the referenced page")
eq(resolve(phone["id"], "")[0], "phone-signin.html",
   "a missing device signal is served the referenced page")

unpaired = lpv_store.save_page("shared", "<html><body>one version</body></html>")
eq(resolve(unpaired["id"], "mobile"), ("shared.html", ""),
   "an unpaired page is served unchanged to phones")
eq(resolve(unpaired["id"], "desktop"), ("shared.html", ""),
   "an unpaired page is served unchanged to desktops")

eq(lpv_store.resolve_page_for_device("does-not-exist", "mobile"), (None, ""),
   "an unknown page id resolves to nothing")

# ---------------------------------------------------------------------------
# 5) guards: no self-pairs, no loops, deleted counterpart falls back
# ---------------------------------------------------------------------------
try:
    lpv_store.set_page_device(phone["id"], "mobile", phone["id"])
    ok(False, "a page cannot be its own variant")
except ValueError:
    ok(True, "a page cannot be its own variant")

try:
    lpv_store.set_page_device(phone["id"], "mobile", "ghost-page")
    ok(False, "a missing variant page is rejected")
except ValueError:
    ok(True, "a missing variant page is rejected")

# A -> B -> A must not bounce; resolution follows exactly one hop.
lpv_store.set_page_device(desktop["id"], "desktop", phone["id"])
page, _ = lpv_store.resolve_page_for_device(phone["id"], "desktop")
eq(page["id"], desktop["id"], "resolution follows exactly one hop (no ping-pong)")

# An explicit role on the counterpart is never overwritten by a later pairing.
third = lpv_store.save_page("third", "<html>third</html>")
lpv_store.set_page_device(third["id"], "desktop", "")
lpv_store.set_page_device(third["id"], "desktop", phone["id"])
eq(lpv_store.get_page(phone["id"])["device"], "mobile",
   "pairing a second page to the phone build does not change the phone build's role")

# Deleting the counterpart degrades to the page the workflow references.
vanishing = lpv_store.save_page("vanishing", "<html>v</html>")
lpv_store.set_page_device(unpaired["id"], "mobile", vanishing["id"])
lpv_store.delete_page(vanishing["id"])
page, swapped = lpv_store.resolve_page_for_device(unpaired["id"], "desktop")
eq((page or {}).get("id"), unpaired["id"],
   "a deleted counterpart falls back to the referenced page")
eq(swapped, "", "the fallback is not reported as a swap")

# 'any' unpairs.
cleared = lpv_store.set_page_device(unpaired["id"], "any", "")
eq((cleared["device"], cleared["variant_page_id"]), ("any", ""),
   "setting a page back to 'any' removes the pair")

# ---------------------------------------------------------------------------
# 6) wiring: api.py resolves at every push site, Admin.html can configure it
# ---------------------------------------------------------------------------
api_src = (ROOT / "api.py").read_text(encoding="utf-8")
admin_src = (ROOT / "Admin.html").read_text(encoding="utf-8")
client_src = (ROOT / "client.html").read_text(encoding="utf-8")

ok('@app.get("/api/lpv/pages/{page_id}/device")' in api_src,
   "GET /api/lpv/pages/<id>/device exists")
ok('@app.put("/api/lpv/pages/{page_id}/device")' in api_src,
   "PUT /api/lpv/pages/<id>/device exists")
ok("async def _client_device(" in api_src, "api resolves a client's device")
ok("_remember_client_device(client_id, init_data)" in api_src,
   "the device is captured when an LPV client connects")
ok(api_src.count("await _page_for_client(") >= 2,
   "both push paths (workflow step, manual push) resolve the variant")

# The workflow page step must use the resolved id, not the step's raw id.
step_src = api_src[api_src.index('if kind == "page":'):]
step_src = step_src[:step_src.index('elif kind == "goto":')]
ok("_page_for_client(" in step_src, "the workflow page step resolves the page")
# The URL may also carry the push-time binding token (?b=…), so match the
# call on the resolved id rather than an exact argument list.
ok('_lpv_page_url(page_id' in step_src,
   "the workflow step pushes the RESOLVED page url")
ok("_page_guard(page_id, page)" in step_src,
   "the click guard follows the resolved page")

# /html keeps serving a concrete page id (resolution happens before the push).
ok('@app.get("/api/lpv/archive/{page_id}/html")' in api_src,
   "the html route still serves a concrete page id")

# client.html needs no changes: the server decides, the client just fetches.
ok("is_mobile" in client_src, "client.html still sends is_mobile")
ok("resolve_page_for_device" not in client_src,
   "resolution stays server-side (client.html is unchanged)")

ok('lpvDeviceOpen' in admin_src, "Admin.html can open the device picker")
ok("/device'" in admin_src, "Admin.html saves to the device endpoint")
ok("serve a different page to desktop" in admin_src,
   "Admin.html labels the control for operators")

print("device-version checks passed")
print(f"{checks} checks passed")
