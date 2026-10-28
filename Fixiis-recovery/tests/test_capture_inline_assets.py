#!/usr/bin/env python3
"""Refs the mirror can only reach through CSS or SVG indirection.

The asset pipeline used to see a URL only when it was an attribute of
``<img>``, ``<script>`` or ``<link>``. Everything a page references *through
CSS or through SVG* was left pointing at the site, and each kind fails in its
own silent way from the mirror's origin (a ``blob:`` document inherits ours):

* ``@font-face { src: url(/f/x.woff2) }`` in the page's own ``<style>`` —
  fonts are fetched in CORS mode, the site does not send
  ``Access-Control-Allow-Origin`` for its own fonts, so the request is blocked
  and the whole page renders in the fallback face. No error anywhere.
* ``style="background-image:url(/hero.jpg)"`` — re-fetched from the site on
  every mirror load; hotlink protection (Referer: ours) blanks it.
* ``<use href="/sprite.svg#icon">`` — browsers refuse cross-origin ``<use>``,
  so every icon in a sprite disappears.
* ``<video poster>`` — same as a background image.

These tests run the real ``_rewrite_assets_to_cache`` against a stubbed httpx
and assert both halves: the refs are COLLECTED (one request each) and the
markup they live in is REPLACED.

    PYTHONPATH=. python3 tests/test_capture_inline_assets.py
"""
import asyncio
import sys
import types

sys.path.insert(0, "/home/user/v2/Fixiis-recovery")

PASSED = 0


def ok(cond, label):
    global PASSED
    assert cond, label
    PASSED += 1


PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64
FONT = b"wOF2" + b"0" * 64
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><symbol id="i"/></svg>'
CSS = b".x{background:url(/img/x.png)}"


class FakeResponse:
    def __init__(self, url, content, ctype):
        self.url = url
        self.status_code = 200 if content else 404
        self.content = content
        self.headers = {"content-type": ctype}


class FakeClient:
    """Serves `routes`; records every URL it was asked for."""

    routes = {}
    requests = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, timeout=None):
        type(self).requests.append(url)
        body, ctype = type(self).routes.get(url, (b"", "text/plain"))
        return FakeResponse(url, body, ctype)


def install_fake_httpx():
    mod = types.ModuleType("httpx")
    mod.AsyncClient = FakeClient
    sys.modules["httpx"] = mod


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


install_fake_httpx()

import dom_capture  # noqa: E402

BASE = "https://site.example/page"

ROUTES = {
    "https://site.example/img/x.png": (PNG, "image/png"),
    "https://site.example/f/x.woff2": (FONT, "font/woff2"),
    "https://site.example/f/y.ttf": (FONT, "font/ttf"),
    "https://site.example/sprite.svg": (SVG, "image/svg+xml"),
    "https://site.example/css/site.css": (CSS, "text/css"),
}


def fresh_manager():
    """The URL memo is process-wide on purpose (fetch-once). A test that wants
    to see a fetch has to start from an empty one, or the second test silently
    reads the first test's cache."""
    dom_capture._GLOBAL_ASSET_MANAGER = dom_capture.AssetManager()


def rewrite(html, routes=None):
    fresh_manager()
    FakeClient.routes = dict(ROUTES if routes is None else routes)
    FakeClient.requests = []
    out, meta = run(dom_capture._rewrite_assets_to_cache(html, BASE))
    return out, meta, list(FakeClient.requests)


# ---------------------------------------------------------------------------
# 1. The font this whole change exists for
# ---------------------------------------------------------------------------
def test_inline_font_face_is_embedded():
    html = ('<html><head><style>@font-face{font-family:X;'
            'src:url(/f/x.woff2) format("woff2")}</style></head><body>hi</body></html>')
    out, _meta, req = rewrite(html)
    ok("https://site.example/f/x.woff2" in req, "the inline @font-face was not fetched")
    ok("url(data:font/woff2;base64," in out, "the inline font was not embedded: %r" % out)
    ok("/f/x.woff2" not in out, "the site URL survived — the browser would CORS-block it")
    ok('format("woff2")' in out, "the format() hint must survive")


def test_fonts_on_a_second_face_in_the_same_block_all_resolve():
    html = ('<style>@font-face{font-family:A;src:url(/f/x.woff2)}'
            '@font-face{font-family:B;src:url(/f/y.ttf)}</style>')
    out, _meta, req = rewrite(html)
    ok(len([u for u in req if "/f/" in u]) == 2, "both faces should be fetched: %r" % req)
    ok(out.count("data:font/") == 2, "both faces should be embedded: %r" % out)


def test_font_url_with_a_query_string_keeps_its_shape():
    routes = dict(ROUTES)
    routes["https://site.example/f/x.woff2?v=3"] = (FONT, "font/woff2")
    html = '<style>@font-face{src:url("/f/x.woff2?v=3")}</style>'
    out, _meta, req = rewrite(html, routes)
    ok("https://site.example/f/x.woff2?v=3" in req, "query string lost: %r" % req)
    ok("data:font/woff2" in out, "not embedded: %r" % out)


# ---------------------------------------------------------------------------
# 2. Backgrounds: style attribute + <style> block
# ---------------------------------------------------------------------------
def test_style_attribute_background_is_rewritten():
    html = '<div style="background-image:url(/img/x.png)">hero</div>'
    out, _meta, req = rewrite(html)
    ok("https://site.example/img/x.png" in req, "inline style bg was not fetched")
    ok("url(data:image/png" in out or "url(/assets/" in out, "inline style bg not rewritten: %r" % out)
    ok("/img/x.png" not in out, "the site URL survived in a style attribute")
    ok('style="background-image:url(' in out, "the attribute shape changed: %r" % out)


def test_single_quoted_style_attribute_and_multiple_urls():
    html = "<div style='background:url(/img/x.png) no-repeat, url(/img/x.png)'>x</div>"
    out, _meta, _req = rewrite(html)
    ok(out.count("url(data:image/png") == 2, "both url()s in one attribute: %r" % out)
    ok("/img/x.png" not in out, "a site URL survived")


def test_style_block_background_is_rewritten():
    html = "<style>.hero{background-image:url('/img/x.png')}</style><div class=hero></div>"
    out, _meta, _req = rewrite(html)
    ok("/img/x.png" not in out, "the <style> background survived: %r" % out)
    ok("data:image/png" in out or "/assets/" in out, "not rewritten: %r" % out)


# ---------------------------------------------------------------------------
# 3. SVG: <use> sprites and <image>
# ---------------------------------------------------------------------------
def test_sprite_is_served_not_inlined_and_keeps_its_fragment():
    html = '<svg><use href="/sprite.svg#icon"></use></svg>'
    out, _meta, req = rewrite(html)
    ok("https://site.example/sprite.svg" in " ".join(req), "the sprite was not fetched: %r" % req)
    ok("/assets/" in out, "a sprite must be served from our origin: %r" % out)
    ok("#icon" in out, "the #fragment identifies the symbol and must survive: %r" % out)
    ok("data:image/svg" not in out,
       "<use> with a data: URI is unreliable — a served file is required")


def test_xlink_href_is_handled_too():
    html = ('<svg><use xlink:href="/sprite.svg#a"></use>'
            '<use xlink:href="/sprite.svg#b"></use></svg>')
    out, _meta, _req = rewrite(html)
    ok(out.count("/assets/") >= 2, "both xlink uses: %r" % out)
    ok("#a" in out and "#b" in out, "fragments lost: %r" % out)


def test_a_local_fragment_is_left_alone():
    html = '<svg><use href="#local"></use></svg>'
    out, _meta, req = rewrite(html)
    ok(req == [], "a same-document reference must not be fetched: %r" % req)
    ok('<use href="#local">' in out, "a local reference was rewritten: %r" % out)


def test_svg_image_href_is_rewritten():
    html = '<svg><image href="/img/x.png" width="10" height="10"/></svg>'
    out, _meta, req = rewrite(html)
    ok("https://site.example/img/x.png" in req, "svg <image> not fetched")
    ok("/img/x.png" not in out, "svg <image> not rewritten: %r" % out)


# ---------------------------------------------------------------------------
# 4. Poster frames
# ---------------------------------------------------------------------------
def test_video_poster_is_rewritten():
    html = '<video poster="/img/x.png" src="/v.mp4"></video>'
    out, _meta, req = rewrite(html)
    ok("https://site.example/img/x.png" in req, "poster not fetched: %r" % req)
    ok('poster="data:image/png' in out or 'poster="/assets/' in out, out)
    ok('src="/v.mp4"' in out, "the video source itself is not our business and must not move")


# ---------------------------------------------------------------------------
# 5. Do no harm
# ---------------------------------------------------------------------------
def test_a_failed_fetch_leaves_the_authored_ref():
    html = '<style>.a{background:url(/img/missing.png)}</style>'
    out, _meta, req = rewrite(html, {})
    ok("https://site.example/img/missing.png" in req, "it should try")
    ok("url(/img/missing.png)" in out, "a 404 must leave the page as authored: %r" % out)


def test_javascript_text_is_never_rewritten():
    # The capture strips scripts afterwards; rewriting their string literals
    # first would corrupt what little remains (and lie in the log).
    html = ('<script>el.style.background="url(/img/x.png)";'
            'go("/sprite.svg#i")</script><div>x</div>')
    out, _meta, req = rewrite(html)
    ok(req == [], "refs inside a <script> must not be fetched: %r" % req)
    ok('url(/img/x.png)' in out, "script text was rewritten: %r" % out)


def test_comments_are_never_rewritten():
    html = '<!-- <div style="background:url(/img/x.png)"> --><p>x</p>'
    out, _meta, req = rewrite(html)
    ok(req == [], "refs inside a comment must not be fetched: %r" % req)
    ok("/img/x.png" in out, "a comment was rewritten: %r" % out)


def test_existing_data_and_asset_refs_are_untouched():
    html = ('<div style="background:url(data:image/png;base64,AAAA)"></div>'
            '<div style="background:url(/assets/abc.png)"></div>')
    out, _meta, req = rewrite(html)
    ok(req == [], "data:/assets refs must not be fetched: %r" % req)
    ok(out.count("url(data:image/png;base64,AAAA)") == 1, "data URI mangled: %r" % out)
    ok("url(/assets/abc.png)" in out, "/assets ref mangled: %r" % out)


def test_running_twice_changes_nothing():
    # Recaptures re-run the whole pipeline on freshly captured HTML, but the
    # delta path can hand back markup that already went through once.
    html = ('<style>@font-face{src:url(/f/x.woff2)}</style>'
            '<div style="background:url(/img/x.png)"></div>'
            '<svg><use href="/sprite.svg#i"></use></svg>')
    once, _m1, _r1 = rewrite(html)
    twice, _m2, req2 = rewrite(once)
    ok(twice.count("data:font/woff2") == 1, "the font was embedded twice: %r" % twice)
    ok("site.example/f/x.woff2" not in " ".join(req2),
       "a second pass re-fetched an embedded asset: %r" % req2)


def test_the_link_stylesheet_path_still_works():
    html = '<link rel="stylesheet" href="/css/site.css"><p>x</p>'
    out, _meta, req = rewrite(html)
    ok("https://site.example/css/site.css" in req, "stylesheet not fetched")
    ok("<style" in out, "small stylesheet should still inline: %r" % out)


def test_plain_html_is_returned_unchanged():
    html = '<html><body><p>nothing to fetch</p></body></html>'
    out, meta, req = rewrite(html)
    ok(out == html, "markup with no refs was altered")
    ok(meta == [] and req == [], "nothing should have been fetched")


# ---------------------------------------------------------------------------
# 6. The stats the capture log reads
# ---------------------------------------------------------------------------
def test_document_refs_are_counted():
    html = ('<style>@font-face{src:url(/f/x.woff2)}</style>'
            '<div style="background:url(/img/x.png)"></div>')
    rewrite(html)
    stats = dom_capture._LAST_REWRITE_STATS
    ok(stats.get("doc_refs", 0) >= 2, "doc_refs should count the indirection refs: %r" % stats)
    ok("ms" in stats, "timing must still be recorded")


# ---------------------------------------------------------------------------
# 5b. The new refs must not starve the ones that were already handled
# ---------------------------------------------------------------------------
def test_document_refs_are_capped_so_img_tags_keep_their_budget():
    ok(dom_capture.DOC_REF_MAX > 0, "the cap must allow the normal case")
    ok(dom_capture.DOC_REF_MAX < dom_capture.ASSET_MAX_PER_PAGE,
       "the cap has to leave room for <img>/<link>/<script>")
    # 300 inline backgrounds before a single <img>: the <img> must still be
    # collected, or this change would be a regression on image-heavy pages.
    html = "".join('<div style="background:url(/a/%d.png)"></div>' % i for i in range(300))
    html += '<img src="/img/x.png">'
    out, _meta, req = rewrite(html, {"https://site.example/img/x.png": (PNG, "image/png")})
    ok("https://site.example/img/x.png" in req,
       "the <img> was starved by inline backgrounds: %d requests" % len(req))
    ok(len(req) <= dom_capture.DOC_REF_MAX + 1, "more refs than the cap allows: %d" % len(req))
    ok('src="data:image/png' in out or 'src="/assets/' in out, "the <img> was not rewritten")


# ---------------------------------------------------------------------------
# 6a. Offset maths of the splice (one scan, then replace exact ranges)
# ---------------------------------------------------------------------------
def test_refs_after_a_script_or_a_comment_are_still_found():
    html = ('<script>"url(/img/x.png)"</script>'
            '<div style="background:url(/img/x.png)"></div>'
            '<!-- url(/img/x.png) -->'
            '<svg><use href="/img/x.png#i"></use></svg>')
    out, _meta, req = rewrite(html)
    # the style attribute AND the <use> — the two real refs, found either side
    # of the protected regions (this is where an off-by-one in the offsets
    # between chunks would show up as a silently unrewritten ref).
    ok('style="background:url(/img/x.png)"' not in out, "the attr after a script: %r" % out)
    ok('<use href="/img/x.png#i">' not in out, "the <use> after a comment: %r" % out)
    ok('url(/img/x.png)' in out, "the commented-out ref must be left alone: %r" % out)


def test_a_long_replacement_does_not_corrupt_a_later_one():
    # A data: URI is much longer than the URL it replaces. Splicing has to run
    # from the end backwards or every later offset lands inside the previous
    # replacement.
    routes = dict(ROUTES)
    routes["https://site.example/f/late.woff2"] = (FONT, "font/woff2")
    html = ('<div style="background:url(/img/x.png)"></div>'
            '<style>@font-face{src:url(/f/late.woff2)}</style>'
            '<div style="background:url(/img/x.png)"></div>')
    out, _meta, req = rewrite(html, routes)
    ok(out.count("data:image/png") == 2, "both backgrounds: %r" % out)
    ok("data:font/woff2" in out, "the later font: %r" % out)
    ok("/f/late.woff2" not in out and "/img/x.png" not in out, "a site ref survived: %r" % out)
    # the document must still be intact around the splices
    ok(out.count("<div") == 2, "the markup around a splice was damaged: %r" % out)


def test_the_same_ref_twice_is_rewritten_twice():
    html = ('<div style="background:url(/img/x.png)"></div>'
            '<div style="background:url(/img/x.png)"></div>')
    out, _meta, req = rewrite(html)
    ok(out.count("data:image/png") == 2, "a repeated ref was rewritten once: %r" % out)
    ok("https://site.example/img/x.png" in req, "not fetched at all")


def test_one_ref_in_css_and_as_an_attribute():
    html = ('<div style="background:url(/img/x.png)"><img src="/img/x.png"></div>')
    out, _meta, req = rewrite(html)
    ok("data:image/png" in out, out)
    ok("/img/x.png" not in out, "one of the two uses survived: %r" % out)


def test_overlapping_matches_are_applied_once():
    # A style attribute whose own value happens to look like CSS the collector
    # also matches as a block must not be rewritten twice (a data: URI inside
    # a data: URI would corrupt the attribute).
    html = '<div style="background:url(/img/x.png)">x</div>'
    out, _meta, _req = rewrite(html)
    ok(out.count("data:image/png") == 1, "applied more than once: %r" % out)


# ---------------------------------------------------------------------------
# 6b. The unchanged-skip must never be the reason a change is lost
# ---------------------------------------------------------------------------
def test_unchanged_skip_needs_something_else_carrying_the_change():
    may = dom_capture._may_skip_unchanged
    # delta observer installed, interaction recapture, page already known
    ok(may("click", "none", True, "12:900:4000") is True,
       "with the observer installed an unchanged click can be skipped")
    # NO observer: this capture is the only path to the mirror
    ok(may("click", "none", False, "12:900:4000") is False,
       "without the delta observer nothing else carries the change")
    # recovery reasons always capture
    for reason in ("navigation", "delta_overflow", "coalesce:navigation",
                   "coalesce:delta_overflow", "resync", "RESYNC"):
        ok(may(reason, "none", True, "12:900:4000") is False, reason)
    # first capture of a session has nothing to compare against
    ok(may("click", "none", True, None) is False, "no previous checksum -> capture")
    # settling captures are never skipped on a heuristic
    ok(may("click", "light", True, "12:900:4000") is False, "settle=light must capture")


def test_the_checksum_cannot_see_an_attribute_change():
    # This is the reason the guard above exists: the fingerprint is
    # elements : text-length : scroll-height, so a class swap is invisible.
    js = dom_capture._DOM_CHECKSUM_JS
    ok("getElementsByTagName" in js and "textContent" in js and "scrollHeight" in js,
       "the checksum shape changed — re-check what it can and cannot see")
    ok("className" not in js and "attributes" not in js,
       "if the checksum ever learns about attributes, the guard can be relaxed")


# ---------------------------------------------------------------------------
# 7. The client can actually load what we emitted
# ---------------------------------------------------------------------------
def test_the_client_absolutizes_assets_in_css_and_svg():
    client = open("/home/user/v2/Fixiis-recovery/client.html", encoding="utf-8", errors="replace").read()
    # /assets/<hash> is root-relative to OUR server; the page's <base> points
    # at the site, so an unrewritten ref would 404 there.
    ok("url\\((['\"]?)(\\/assets\\/[^)'\"]+)" in client,
       "client no longer absolutizes /assets inside CSS")
    ok("(href|src)=([\"'])(\\/assets\\/" in client,
       "client no longer absolutizes /assets in href/src (the <use> path)")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("capture inline assets: %d checks passed" % PASSED)
