"""The product font: Roboto Slab — embedded, never fetched.

Every surface the operator or a visitor looks at (the client shell, the LIVE
mirror, Mini Admin) is set in **Roboto Slab**; Admin.html is set entirely in an
embedded Corptic DEMO with an embedded Montserrat digit subset for the numbers
(black & white themes, rounded shapes only).
One decision per surface, no third-party host, no render-blocking stylesheet.

Why the glyphs travel *inside* the document
-------------------------------------------
A mirrored page is rendered into an iframe whose document comes from a `blob:`
URL, so it has an **opaque origin**. A `@font-face` pointing at a URL — ours or
Google's — is then a cross-origin font request, and the CSS Fonts spec makes
font loads CORS-checked. The page would come back with its own typography
silently dropped (or, worse, blank boxes) while images and CSS loaded fine.
A `data:` URI sidesteps origin rules entirely, and is also what makes the
standalone copies work from `file://` and behind a filter.

The payload below is the **variable** Latin build (weights 100-900 in one
34 KB file), which is smaller than the 400 + 700 pair it replaces and covers
every weight the panels actually use (400, 500, 600, 650, 700, 800) by
interpolation instead of snapping to the nearest static face.

`assets/fonts/roboto-slab-latin-wght-normal.woff2` is the source of truth.
`deploy/embed_font.py` copies `font_face_css()` into the HTML files, and
`tests/test_slab_font.cjs` fails if any copy drifts from it. Apache-2.0, see
`assets/fonts/LICENSE-RobotoSlab.txt`.
"""

from __future__ import annotations

import base64
from functools import lru_cache
from pathlib import Path

FONT_FILE = (
    Path(__file__).resolve().parent
    / "assets"
    / "fonts"
    / "roboto-slab-latin-wght-normal.woff2"
)

# NB: unquoted family names on purpose. A quoted family inside a JS string or
# an inline-style attribute has silently killed these pages before (the quote
# ended the string early — see tests/test_inline_scripts_parse.cjs). A family
# name made of identifiers does not need quotes and cannot collide.
FONT_FAMILY = "Roboto Slab"

# Slab serif with slab-flavoured fallbacks: if the embedded face is ever
# refused (a mirrored site's own `font-src` CSP, an exotic engine), the text
# keeps its serif texture instead of flipping to a sans face mid-page.
FONT_STACK = "Roboto Slab, Georgia, 'Times New Roman', serif"

# Where a monospace face is meant, it stays monospace — Roboto Slab is only
# the last resort before the generic keyword.
MONO_STACK = "ui-monospace, SFMono-Regular, Menlo, Consolas, Roboto Slab, monospace"


@lru_cache(maxsize=1)
def font_data_uri() -> str:
    """The whole font as a `data:` URI, or "" when the asset is missing.

    Missing asset is not fatal: the stack falls back to Georgia and the product
    still works. A crash at import time would take the server down instead.
    """
    try:
        raw = FONT_FILE.read_bytes()
    except OSError:
        return ""
    return "data:font/woff2;base64," + base64.b64encode(raw).decode("ascii")


@lru_cache(maxsize=1)
def font_face_css() -> str:
    """One `@font-face` rule describing the embedded variable font."""
    uri = font_data_uri()
    if not uri:
        return ""
    return (
        "@font-face{font-family:Roboto Slab;font-style:normal;"
        "font-weight:100 900;font-display:swap;"
        "src:url(" + uri + ") format('woff2');}"
    )
