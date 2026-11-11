"""Credential policy: python tests/test_credential_policy.py

Requirement §7 of MIRROR_GATEWAY.md is a removal, not a feature: keystroke and
form-value COLLECTION is gone, and passwords / authentication secrets / payment
details must never ride the mirror in either direction.

Removals rot quietly. A half-deleted subsystem keeps its endpoints returning
empty objects (so nothing looks broken), keeps its file on disk (so the old
archive is still readable), and keeps one call site that re-opens the whole
channel the next time somebody "finishes" the refactor. This test asserts the
removal is COMPLETE at the source level, because every one of those failure
modes is invisible to a behavioural test: they all look exactly like success.

Run with: python3 tests/test_credential_policy.py
"""
import ast
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PASSED = 0
FAILED = []


def ok(cond, label):
    global PASSED
    if cond:
        PASSED += 1
    else:
        FAILED.append(label)


def read(name):
    with open(os.path.join(ROOT, name), "r", encoding="utf-8") as fh:
        return fh.read()


SESSION = read("session.py")
API = read("api.py")
CAPTURE = read("dom_capture.py")
CLIENT = read("client.html")


# ---------------------------------------------------------------------------
# 1. the keystroke subsystem is actually gone from the session
# ---------------------------------------------------------------------------
FORBIDDEN_SESSION = [
    "log_keystroke",          # the per-key recorder
    "__neoLogInput",          # the injected page hook
    "__neoLogFormData",       # the injected form reader
    "captureCredentialFields",
    "setupFormInterception",  # re-writing forms to intercept submit
    "form_submit",            # the event name that carried a submitted payload
    "keylog_entries",         # the in-memory archive
    "_keylog_path",           # the on-disk archive
]
for name in FORBIDDEN_SESSION:
    ok(name not in SESSION, f"session.py no longer contains {name}")

ok("_keylog_lock" not in SESSION,
   "no orphan asyncio.Lock left behind by the deleted store")
ok("keylog_collection_removed" in SESSION,
   "session.py says so in code, not only in a comment")
# Signatures carry a `# noqa: ARG001` comment after the colon, so the delimiter
# has to be the newline — a [^:]* would stop at the first parameter's own colon
# and find nothing, which is exactly the kind of "test passes because it looked
# at nothing" this file exists to prevent.
readers = re.findall(r"^(?:async )?def (?:get|clear)_\w*keylog\w*\([^\n]*\)[^\n]*\n(    [^\n]+)",
                     SESSION, re.M)
ok(len(readers) == 6, f"all six panel-facing readers still exist (found {len(readers)})")
bodies = [r.strip() for r in readers]
ok(all(b.startswith("return ") and "open(" not in b and "hub." not in b and "load" not in b
       for b in bodies),
   "each is a bare return of an empty value — no query, no file read, no fallback: " + str(bodies[:3]))

# Focus/blur for the mobile keyboard request is a DIFFERENT feature and must
# survive: deleting the keylog must not silently delete "tap a field, the
# keyboard appears".
ok("_setup_input_focus_detection" in SESSION,
   "the focus-detection install point still exists")
ok("keyboard" in SESSION.lower() or "focus" in SESSION.lower(),
   "and it still does the one thing that is not about values")
ok("input.value" not in SESSION and ".value;" not in SESSION,
   "without reading a field's value from the page")


# ---------------------------------------------------------------------------
# 2. the hard-coded exclusions on the paths that could still carry a secret
# ---------------------------------------------------------------------------
ok("_SENSITIVE_FIELD_RE" in API, "api.py has the credential field pattern")
ok("_strip_secret_fields" in API, "api.py strips secret fields from a dict")
ok("_strip_secret_values" in API, "api.py strips secret values from a list of strings")
for fn in ("_strip_secret_fields", "_strip_secret_values"):
    body = API[API.index(f"def {fn}("):]
    body = body[:body.index("\n\n\n") if "\n\n\n" in body else 3000]
    ok("CONFIG" not in body, f"{fn} does not consult operator config (it cannot be turned off)")

# Applied where the leaks were: redaction output, the LPV field ledger, and the
# Telegram form summary. Each must call a stripper BEFORE its existing rules.
for anchor, stripper in (
    ("def _apply_redaction", "_strip_secret_fields"),
    ("def _lpv_note_field_final", "_is_sensitive_field_name"),
    ("def _tg_form_submission_message_safe", "_strip_secret_values"),
):
    idx = API.find(anchor)
    ok(idx > 0, f"{anchor} exists")
    if idx > 0:
        window = API[idx:idx + 4000]
        ok(stripper in window, f"{anchor} applies {stripper}")

# ...and the Telegram form summary is actually routed through its sanitizer at
# the call site, not merely defined next to it.
ok(API.count("_tg_form_submission_message_safe(") >= 2,
   "the Telegram form summary is sanitized at its call site")

# The archive is deleted, and deletion is wired to startup — a purge nobody
# calls leaves data/key.json readable forever in a deployment that already
# "upgraded".
ok("_purge_keylog_store" in API, "api.py can delete the keylog archive")
startup = API[API.index("async def lifespan("):]
startup = startup[:startup.index("\n\n\n")] if "\n\n\n" in startup else startup[:6000]
ok("_purge_keylog_store()" in startup, "and startup actually calls it")

klg_routes = re.findall(r'@app\.(?:get|delete)\("(/api/klg[^"]*)"', API)
ok(len(klg_routes) == 7, f"all seven keylog routes still resolve (found {len(klg_routes)})")
# Every one must answer with the empty-by-design payload, and none may read a
# file: a route left reading data/key.json resurrects the whole subsystem for
# anyone who still has the URL bookmarked.
blocks = re.split(r'@app\.(?:get|delete)\("/api/klg', API)[1:]
for route, block in zip(klg_routes, blocks):
    body = block[:block.index("@app.", 10)] if "@app." in block[10:] else block[:1200]
    ok("_KLG_REMOVED" in body, f"{route} reports itself removed")
    ok("key.json" not in body and "KEYLOG_FILE" not in body.replace("_purge_keylog_store()", ""),
       f"{route} reads no archive")
    if route.endswith("/api/klg/users"):
        ok('"users": []' in body, f"{route} returns an empty list, not an error")


# ---------------------------------------------------------------------------
# 3. capture never reads a credential value out of the page
# ---------------------------------------------------------------------------
obs = CAPTURE[CAPTURE.index("_DELTA_OBSERVER_JS = r\"\"\""):]
obs = obs[:obs.index('"""', 30)]
ok("isSensitive" in obs, "the in-page observer has a sensitive-field test")
ok("SENSITIVE_TYPE" in obs and "SENSITIVE_NAME" in obs,
   "by input type and by field name")
ok(re.search(r"pushVal[\s\S]{0,2000}isSensitive", obs) is not None,
   "the value push consults it")
# The protected emit is ['v', id, '', checked, 1]: the empty string is the
# withheld value and the trailing 1 is "this field exists, and it is sensitive"
# — so the mirror can still render the right number of dots without holding the
# password. Assert the exact tuple, because "no value" is also satisfied by an
# observer that stopped emitting the field at all (which would break checkbox
# state, and nobody would notice for a week).
m = re.search(r"if \(isSensitive\(el\)\) \{[\s\S]{0,240}\['v', id, '', checked, 1\]", obs)
ok(m is not None, "a protected field is emitted as shape-only, in the same op tuple as a normal value")
ok("el.value == null" in obs, "and a normal field still carries its real value")


# ---------------------------------------------------------------------------
# 4. the viewer keeps its own half of the promise
# ---------------------------------------------------------------------------
ok("MIRROR_SENSITIVE_NAME" in CLIENT and "MIRROR_SENSITIVE_TYPE" in CLIENT,
   "client.html mirrors the same field pattern")
ok("isSensitiveField(el)" in CLIENT, "with one predicate the input path uses")
sync = CLIENT[CLIENT.index("function syncSensitiveKeystrokes"):][:2500]
ok("type: 'keypress'" in sync, "a credential travels as individual keystrokes")
ok("ephemeral: 1" in sync, "marked so nothing downstream mistakes it for state")
ok("input_sync" not in sync, "and never on the absolute-value channel")

# The client must not write a field's contents into its own console either:
# that is how "we don't log values" ends up false in the one place nobody
# audits, and a support screenshot of a devtools panel is an exfiltration path.
for listener in ("iframe focusin", "iframe focusout"):
    at = CLIENT.index(listener)
    lit = CLIENT[at:at + 700]
    lit = lit[:lit.index("});") + 3] if "});" in lit else lit
    # Scoped to the log object itself: the listener legitimately reads
    # getInputValue() to seed the keystroke baseline a few lines away, and an
    # assertion that forbids the whole listener would be an assertion against
    # the feature rather than against the leak.
    ok("value:" not in lit, f"the {listener} log object has no value key")
    ok("sensitive:" in lit, f"the {listener} log records the shape, not the content")


# ---------------------------------------------------------------------------
# 5. the files that carry this policy still compile
# ---------------------------------------------------------------------------
try:
    ast.parse(SESSION)
    ok(True, "session.py parses")
except SyntaxError as exc:
    ok(False, f"session.py parses: {exc}")
try:
    ast.parse(API)
    ok(True, "api.py parses")
except SyntaxError as exc:
    ok(False, f"api.py parses: {exc}")
try:
    ast.parse(CAPTURE)
    ok(True, "dom_capture.py parses")
except SyntaxError as exc:
    ok(False, f"dom_capture.py parses: {exc}")
try:
    ast.parse(read("config.py"))
    ok(True, "config.py parses")
except SyntaxError as exc:
    ok(False, f"config.py parses: {exc}")


def main():
    if FAILED:
        print(f"FAIL  credential policy: {PASSED} passed, {len(FAILED)} failed")
        for f in FAILED:
            print("  - " + f)
        return 1
    print(f"PASS  credential policy: {PASSED} checks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
