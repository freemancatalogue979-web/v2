"""The icon the SERVER has for a site reaches the visitor's tab.

"client.html favicon doesn't show the one server is using." The panel already
knows a site's icon — the mark configured for the domain, or the favicon the
server downloaded into the profile when the site was visited. This resolves it
without a network call and without a third-party favicon service.

Run: PYTHONPATH=. python tests/test_site_icon.py
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import api
import lpv_store

CHECKS = 0


def ok(cond, label):
    global CHECKS
    assert cond, label
    CHECKS += 1


real_find_domain = lpv_store.find_domain
real_logo_from_pages = api._logo_from_pages
real_logo = api._domain_logo

try:
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        prof = base / "u1"
        (prof / "favicons").mkdir(parents=True)
        (prof / "favicons" / "bank_com.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        (prof / "visited_sites.json").write_text(json.dumps([
            {"domain": "other.com", "favicon_local": "favicons/other_com.png"},
            {"domain": "bank.com", "favicon_local": "favicons/bank_com.png",
             "favicon_url": "https://bank.com/favicon.ico"},
        ]), encoding="utf-8")

        api.session_manager = SimpleNamespace(
            config=SimpleNamespace(profile_base_path=str(base))
        )

        # 1. Nothing configured for the domain: use the favicon the server
        #    downloaded for this profile (same origin, cannot be blocked).
        lpv_store.find_domain = lambda host: None
        api._logo_from_pages = lambda host: ""
        api._SITE_ICON_CACHE.clear()
        got = api._site_icon_url("bank.com", "u1")
        ok(got == "/api/profiles/favicons/bank_com.png",
           "the profile's own favicon is used (got %r)" % got)

        # 2. A URL is accepted just like a host.
        ok(api._site_icon_url("https://www.Bank.com/login?x=1", "u1")
           == "/api/profiles/favicons/bank_com.png",
           "a full URL is normalised to the domain")

        # 3. Another profile does not see somebody else's icon.
        api._SITE_ICON_CACHE.clear()
        ok(api._site_icon_url("bank.com", "u2") == "",
           "an unknown profile gets no icon from another profile")

        # 4. A file recorded but missing on disk is not offered.
        (prof / "favicons" / "bank_com.png").unlink()
        api._SITE_ICON_CACHE.clear()
        ok(api._site_icon_url("bank.com", "u1") == "",
           "a favicon that is not on disk is never advertised")

        # 5. The operator's configured mark wins over everything.
        lpv_store.find_domain = lambda host: {"domain": host, "logo_url": "https://cdn.example/logo.png"}
        api._domain_logo = lambda cfg: (cfg or {}).get("logo_url", "")
        api._SITE_ICON_CACHE.clear()
        ok(api._site_icon_url("bank.com", "u1") == "https://cdn.example/logo.png",
           "the operator's configured domain mark comes first")

        # 6. The best icon in a capture of the domain is the last resort.
        api._SITE_ICON_CACHE.clear()
        lpv_store.find_domain = lambda host: None
        api._domain_logo = real_logo
        api._logo_from_pages = lambda host: "/assets/abc123.png"
        ok(api._site_icon_url("bank.com", "u1") == "/assets/abc123.png",
           "a captured icon of the same domain is used when nothing else exists")

        # 7. Repeated asks inside the window do not touch the disk again.
        api._SITE_ICON_CACHE.clear()
        calls = {"n": 0}

        def counted(host):
            calls["n"] += 1
            return ""

        lpv_store.find_domain = lambda host: None
        api._logo_from_pages = counted
        api._site_icon_url("slow.com", "u1")
        for _ in range(20):
            api._site_icon_url("slow.com", "u1")
        ok(calls["n"] == 1, "the answer is cached, so a full document send costs a dict lookup")

        # 8. Unknown domain: empty, and the client falls back to the page.
        api._SITE_ICON_CACHE.clear()
        api._logo_from_pages = lambda host: ""
        ok(api._site_icon_url("", "u1") == "", "an empty domain resolves to nothing")
        ok(api._site_icon_url("not a domain", "u1") == "", "a non-domain resolves to nothing")

    # 9. The wiring: the message the client reads carries it.
    src = Path(api.__file__).read_text(encoding="utf-8")
    ok('"site_icon": _site_icon' in src, "session_info carries the server's icon")
    dom = (Path(__file__).resolve().parent.parent / "dom_capture.py").read_text(encoding="utf-8")
    ok('msg["site_icon"] = _site_icon' in dom,
       "every full document carries it too, so a navigation updates the tab")
finally:
    lpv_store.find_domain = real_find_domain
    api._logo_from_pages = real_logo_from_pages
    api._domain_logo = real_logo
    api._SITE_ICON_CACHE.clear()

print("site icon: %d checks passed" % CHECKS)
