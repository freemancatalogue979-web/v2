#!/usr/bin/env python3
"""Proxy-mirror URL rewriting: python3 tests/test_proxy_mirror.py

Pins the pure helpers in proxy_mirror.py — the same-origin /m/ redirect that
lets the client drive the persistent remote browser directly through the
proxy instead of waiting on the DOM-capture mirror.
"""
import os
import re as _re
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import unquote

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import proxy_mirror as pm  # noqa: E402

PASSED = 0


def ok(cond, label):
    global PASSED
    assert cond, label
    PASSED += 1


TARGET = "https://mail.example.com/mail/u/0/#inbox"

# --- single-ref rewriting ---------------------------------------------------
ok(pm.rewrite_ref("https://cdn.example.com/a.js", TARGET) == "/m/https://cdn.example.com/a.js",
   "absolute cross-origin ref -> /m/")
ok(pm.rewrite_ref("http://x.example.com/a", TARGET) == "/m/http://x.example.com/a",
   "http absolute -> /m/")
ok(pm.rewrite_ref("//cdn.example.com/b.css", TARGET) == "/m/https://cdn.example.com/b.css",
   "protocol-relative -> /m/https:")
ok(pm.rewrite_ref("/images/cleardot.gif", TARGET) == "/m/https://mail.example.com/images/cleardot.gif",
   "root-absolute -> target origin through /m/")
ok(pm.rewrite_ref("images/cleardot.gif", TARGET) is None,
   "relative ref left alone (the injected <base> routes it)")
for skip in ("data:image/png;base64,AAA", "javascript:void(0)", "#frag", "mailto:a@b.c", "blob:x"):
    ok(pm.rewrite_ref(skip, TARGET) is None, f"{skip.split(':')[0]} untouched")

# --- html rewriting ----------------------------------------------------------
html = (
    '<html><head><meta http-equiv="Content-Security-Policy" content="default-src *">'
    '<link rel="stylesheet" href="/css/site.css">'
    '<link rel="icon" href="https://cdn.example.com/favicon.ico">'
    "<style>.a{background:url(/img/a.png)}.b{background:url('https://cdn.example.com/b.png')}</style>"
    "</head><body>"
    '<img src="images/cleardot.gif">'
    '<img srcset="/img/1.png 1x, https://cdn.example.com/2.png 2x">'
    '<form action="/login" method="post"><input></form>'
    '<a href="page2">next</a>'
    '<video poster="/img/poster.jpg"></video>'
    '<div style="background:url(/img/inline.png)">x</div>'
    "</body></html>"
)
out = pm.rewrite_html_for_proxy(html, TARGET)
ok('<base href="' + pm.proxied_path(TARGET) + '">' in out, "base injected pointing at the proxied page")
ok('http-equiv="Content-Security-Policy"' not in out.lower().replace(" ", "") or "content-security-policy" not in out.lower(),
   "meta CSP stripped")
ok('href="/m/https://mail.example.com/css/site.css"' in out, "root-absolute link rewritten")
ok('href="/m/https://cdn.example.com/favicon.ico"' in out, "absolute link rewritten")
ok('src="images/cleardot.gif"' in out, "relative img untouched (base handles it)")
ok("/m/https://mail.example.com/img/1.png 1x" in out and "/m/https://cdn.example.com/2.png 2x" in out,
   "srcset tokens rewritten individually")
ok('action="/m/https://mail.example.com/login"' in out, "form action rewritten")
ok('href="page2"' in out, "relative link untouched")
ok('poster="/m/https://mail.example.com/img/poster.jpg"' in out, "poster rewritten")
ok("url(/m/https://mail.example.com/img/a.png)" in out, "style-block url() rewritten")
ok("url('/m/https://cdn.example.com/b.png')" in out, "absolute url() in style rewritten")
ok("url(/m/https://mail.example.com/img/inline.png)" in out, "inline style attr rewritten")
ok("serviceWorker.register" in out, "SW-neuter script injected")

# base replacement when the page has its own <base>
out2 = pm.rewrite_html_for_proxy('<html><head><base href="https://other.example/"></head><body></body></html>', TARGET)
ok('href="https://other.example/"' not in out2 and '<base href="' + pm.proxied_path(TARGET) + '">' in out2,
   "existing <base> replaced with the proxied base")

# --- css ---------------------------------------------------------------------
css = ".x{background:url(/a.png)} @import 'https://cdn.example.com/c.css'; .y{background:url(data:image/png;base64,AA)}"
outc = pm.rewrite_css_for_proxy(css, TARGET)
ok("url(/m/https://mail.example.com/a.png)" in outc, "css root-absolute rewritten")
ok("'/m/https://cdn.example.com/c.css'" in outc, "css @import rewritten")
ok("url(data:image/png;base64,AA)" in outc, "css data: untouched")

# --- location + cookies ------------------------------------------------------
ok(pm.rewrite_location("/mail/u/1/", TARGET) == "/m/https://mail.example.com/mail/u/1/",
   "relative Location rewritten against target origin")
ok(pm.rewrite_location("https://other.example/x", TARGET) == "/m/https://other.example/x",
   "absolute Location rewritten")
ok(pm.rewrite_location("", TARGET) == "", "empty Location untouched")

hdr = pm.cookie_header_from_records([
    {"name": "SID", "value": "abc"}, {"name": "pref", "value": "x=1"},
    {"name": "SID", "value": "dup"}, {"name": "", "value": "z"},
])
ok(hdr == "SID=abc; pref=x=1", f"cookie header dedupes names: {hdr!r}")
recs = pm.set_cookie_records(["SID=xyz; Domain=example.com; Path=/; HttpOnly", "bad", "n=v; Secure"], TARGET)
ok(recs == [{"name": "SID", "value": "xyz", "url": TARGET}, {"name": "n", "value": "v", "url": TARGET}],
   f"Set-Cookie parsed to context records (domain dropped): {recs!r}")

ok(pm.proxied_page_url(TARGET, "s1") == pm.proxied_path(TARGET) + "?sid=s1", "proxied url carries sid")
ok(pm.is_proxy_enabled() is True, "proxy enabled by default (MIRROR_PROXY=0 is the kill switch)")
for _v in ("0", "false", "no", "off"):
    os.environ["MIRROR_PROXY"] = _v
    ok(pm.is_proxy_enabled() is False, "MIRROR_PROXY=%s disables" % _v)
os.environ.pop("MIRROR_PROXY", None)


# --- target encoding: a target's own ?query&#fragment must survive ----------
# Regression: iframe src "/m/https://.../identifier?flowName=..." used to lose
# its query (the ? became the proxy request's query), so upstream got the bare
# path and answered 400 "malformed".
QT = "https://accounts.google.com/signin/identifier?flowName=GlifWebSignIn&flowEntry=ServiceLogin&dsh=S237#frag"
pp = pm.proxied_page_url(QT, "s1")
ok(pp == "/m/https://accounts.google.com/signin/identifier%3FflowName%3DGlifWebSignIn%26flowEntry%3DServiceLogin%26dsh%3DS237%23frag?sid=s1",
   "target query/fragment percent-encoded into the /m/ path, sid stays a real query param")
ok("%2F" not in pp, "slashes stay raw so injected <base> relative resolution keeps working segment-by-segment")
ok(unquote(pp[len("/m/"):].split("?sid=")[0]) == QT, "proxied path decodes back to the exact target (ASGI unquotes the path before routing)")
ok(pm.rewrite_ref("https://x.example.com/p?a=1&b=2", TARGET) == "/m/https://x.example.com/p%3Fa%3D1%26b%3D2",
   "rewritten refs with query strings are encoded too")
ok(pm.rewrite_location("/cb?code=abc&state=xyz", QT) == "/m/https://accounts.google.com/cb%3Fcode%3Dabc%26state%3Dxyz",
   "redirect Location carrying a query is encoded")

# --- <script> bodies must never be rewritten ---------------------------------
# Regression: attr regexes used to run over inline JS (strings like
# src="https://..." inside scripts) and corrupt page code.
js = 'var a = \'src="https://evil.example.com/x.png";\'; try { f("action=/y") } catch(e) { g() }'
html2 = ("<html><head></head><body><script>" + js + "</script>"
         '<img src="https://cdn.example.com/real.png"></body></html>')
out3 = pm.rewrite_html_for_proxy(html2, TARGET)
ok(js in out3, "inline <script> body is byte-identical after rewriting")
ok("/m/https://evil.example.com" not in out3, "no /m/ rewrite leaked into script strings")
ok('src="/m/https://cdn.example.com/real.png"' in out3, "real attrs outside scripts are still rewritten")

# --- injected SW-neuter script must be VALID js ------------------------------
# Regression: the inject used to be "try{...}}" with no catch -> client console
# "Uncaught SyntaxError: Missing catch or finally after try".
bodies = _re.findall(r"<script>(.*?)</script>", out3, _re.DOTALL)
sw = [b for b in bodies if "serviceWorker" in b]
ok(len(sw) == 1, "exactly one SW-neuter script injected")
if sw and shutil.which("node"):
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(sw[0])
        tmp = f.name
    r = subprocess.run(["node", "--check", tmp], capture_output=True)
    ok(r.returncode == 0, "injected SW-neuter script passes node --check: " + r.stderr.decode()[:200])

# --- api.py route wiring pins -------------------------------------------------
api_src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "api.py"), encoding="utf-8").read()
ok(_re.search(r"RedirectResponse\(\s*rewrite_location\([^)]*\),\s*status_code=[^,]+,\s*headers=rheaders", api_src) is not None,
   "redirect responses strip COOP/CSP/XFO headers (else the browser warns on our http origin)")
ok('not p.lower().startswith("sid=")' in api_src,
   "route re-attaches the /m/ request query string (minus sid) to the upstream URL")

print("proxy mirror: %d checks passed" % PASSED)
