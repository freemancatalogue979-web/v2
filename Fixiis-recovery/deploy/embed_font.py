#!/usr/bin/env python3
"""Put the embedded Roboto Slab face into the HTML surfaces.

The font is shipped *inside* each page (see `fonts.py` for why), which means
the same 45 KB of base64 has to live in two files. Rather than paste it by
hand — where one copy would eventually go stale — this script owns it:

    python3 deploy/embed_font.py          # rewrite the blocks
    python3 deploy/embed_font.py --check  # exit 1 if any copy drifted

The source of truth is `assets/fonts/roboto-slab-latin-wght-normal.woff2`.
Run it after replacing that file; `tests/test_slab_font.cjs` fails the build
if you forget, and `deploy/make-standalone.sh` re-runs it before copying, so a
stale standalone folder cannot be produced either.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import fonts  # noqa: E402

MARKER_ID = "shf-font-face"

# Where a page does not carry the block yet, it goes immediately before the
# page's first real stylesheet — the face must be known before any rule that
# names the family is used (CSS does not care about order for @font-face, but
# a reader does: font first, then the typography that uses it).
# Admin.html is deliberately NOT here: every text in it is set in Corptic DEMO,
# embedded in its own <style id="shf-corptic-face"> block
# (asset: assets/fonts/corptic-demo-regular.woff2).
# See tests/test_slab_font.cjs section 2b, which asserts that.
TARGETS = [
    ("mini-admin.html", "<style>\n/* ============================================================"),
    ("client.html", "    <style>\n        /* ===== Global font:"),
]

BEGIN = "    <!-- The product font, embedded: see fonts.py. Generated from\n" \
        "         assets/fonts/roboto-slab-latin-wght-normal.woff2 by\n" \
        "         deploy/embed_font.py - never hand-edit this block. -->\n"
BLOCK_OPEN = '    <style id="%s">\n' % MARKER_ID
BLOCK_CLOSE = "\n    </style>"

EXISTING = re.compile(
    r"[ \t]*<!-- The product font, embedded.*?-->\n?[ \t]*<style id=\"%s\">\n(.*?)\n[ \t]*</style>"
    % re.escape(MARKER_ID),
    re.S,
)


def block_for(css: str) -> str:
    return BEGIN + BLOCK_OPEN + css + BLOCK_CLOSE


def rewrite(path: Path, anchor: str, css: str, check: bool) -> bool:
    """Return True when the file already matched (or was rewritten)."""
    text = path.read_text(encoding="utf-8")
    wanted = block_for(css)
    match = EXISTING.search(text)
    if match:
        if match.group(0) == wanted:
            return True
        if check:
            print("DRIFT %s: embedded face differs from assets/fonts" % path.name)
            return False
        text = text[: match.start()] + wanted + text[match.end() :]
    else:
        if check:
            print("DRIFT %s: no embedded face block" % path.name)
            return False
        if anchor not in text:
            raise SystemExit(
                "%s: no font block and the insertion anchor is gone.\n"
                "  expected: %r\n"
                "Update TARGETS in deploy/embed_font.py." % (path.name, anchor[:60])
            )
        text = text.replace(anchor, wanted + "\n" + anchor, 1)
    path.write_text(text, encoding="utf-8")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="verify instead of rewriting (exit 1 on drift)")
    args = parser.parse_args()

    css = fonts.font_face_css()
    if not css:
        print("assets/fonts/roboto-slab-latin-wght-normal.woff2 is missing")
        return 1

    ok = True
    for name, anchor in TARGETS:
        path = ROOT / name
        if not path.exists():
            print("missing %s" % name)
            ok = False
            continue
        if not rewrite(path, anchor, css, args.check):
            ok = False
    if ok and not args.check:
        print("embedded Roboto Slab (%d bytes of base64) in %d pages"
              % (len(fonts.font_data_uri()), len(TARGETS)))
    elif ok:
        print("all %d pages carry the current font" % len(TARGETS))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
