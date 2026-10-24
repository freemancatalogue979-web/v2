#!/usr/bin/env bash
# ============================================================================
# Produce standalone copies of the front-ends pointed at YOUR server.
#
# The panels in this repo talk to whatever origin serves them by default.
# This bakes your backend URL into a copy so you can:
#   - double-click it and run it straight off your disk (file://), or
#   - drop it on any static host (Netlify, Pages, S3, a shared drive)
# ...and it will still find your server.
#
#   ./deploy/make-standalone.sh http://YOUR-SERVER:8446
#   ./deploy/make-standalone.sh https://panel.example.com ./my-panels
#
# Output goes to ./standalone/ (or the directory you pass) and is
# gitignored, so your server address never ends up in a commit.
#
# Prefer not to generate files at all? Just append a query string:
#   file:///path/to/mini-admin.html?server=http://YOUR-SERVER:8446
# ============================================================================
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVER="${1:-}"
OUTDIR="${2:-$here/standalone}"

if [[ -z "$SERVER" ]]; then
    echo "usage: $0 <server-url|--none> [output-dir]" >&2
    echo "  e.g. $0 http://YOUR-SERVER:8446" >&2
    echo "       $0 --none          # no baked address: the copy asks / uses ?server=" >&2
    exit 1
fi

if [[ "$SERVER" == "--none" || "$SERVER" == "-" ]]; then
    # No baked address. The copy resolves its backend from ?server=, the
    # value saved in the panel, or the origin that served it - so a copy
    # committed to a repository carries nobody's address.
    SERVER=""
else
    # Normalise: bare host gets a scheme, trailing slashes are dropped.
    if [[ ! "$SERVER" =~ ^[a-zA-Z][a-zA-Z0-9+.-]*:// ]]; then
        echo "no scheme given — assuming http://" >&2
        SERVER="http://$SERVER"
    fi
    SERVER="${SERVER%/}"
fi

mkdir -p "$OUTDIR"

# Inject the address, then PROVE the copy is still the same page. A silently
# broken build (a lost <head>, a truncated panel) must never reach the host,
# so every output is checked before it is reported as ok.
emit() {
    local src="$1" dst="$2" label="$3" marker="$4"
    [[ -f "$src" ]] || { echo "  skip  $label (not found)"; return; }

    python3 - "$src" "$dst" "$SERVER" "$marker" <<'PYINJECT'
import re, sys
src, dst, server, marker = sys.argv[1:5]

with open(src, encoding='utf-8') as fh:
    html = fh.read()

tag = "<script>window.SERVER_ENDPOINT=%s;</script>" % repr(server).replace("'", '"')
m = re.search(r'<head[^>]*>', html, re.I)
if not m:
    sys.exit("no <head> in " + src)

out = html[:m.end()] + "\n" + tag + html[m.end():]

# 1. Exactly one address assignment, and it is the one just injected.
if len(re.findall(r'window\.SERVER_ENDPOINT\s*=', out)) != 1:
    sys.exit("injection count wrong in " + dst)
# 2. The rest of the page is byte-identical to the source.
if out[m.end() + len(tag) + 1:] != html[m.end():]:
    sys.exit("body changed while injecting into " + dst)
# 3. A marker that only exists in the real page — catches the wrong source.
if marker and marker not in out:
    sys.exit("marker %r missing from %s - is that the right source file?" % (marker, dst))

with open(dst, 'w', encoding='utf-8') as fh:
    fh.write(out)
PYINJECT
    printf '  ok    %-18s %8s  -> %s\n' "$label" "$(stat -c %s "$dst" | awk '{printf "%.0f KB", $1/1024}')" "$dst"
}

echo "Baking server: ${SERVER:-<none — resolved at runtime>}"
echo "Output:        $OUTDIR"
echo

# Each marker is a feature that must be in that panel. If a marker is gone the
# source moved, and the copy that was just produced is the wrong thing.
emit "$here/mini-admin.html" "$OUTDIR/mini-admin.html" "mini-admin.html" 'id="stChatMode"'
emit "$here/Admin.html"      "$OUTDIR/Admin.html"      "Admin.html"      'id="lpv-archive-list"'
emit "$here/client.html"     "$OUTDIR/client.html"     "client.html"     'id="loadingScreen"'

# A stamp, so "am I hosting the current build?" is answerable from the folder
# itself instead of by guessing at file sizes.
commit=""
if command -v git >/dev/null 2>&1; then
    commit="$(cd "$here" && git rev-parse --short HEAD 2>/dev/null || true)"
    if [[ -n "$commit" ]] && ! (cd "$here" && git diff --quiet 2>/dev/null); then
        commit="$commit (with uncommitted changes)"
    fi
fi
{
    echo "Standalone build"
    echo "  generated : $(date -u '+%Y-%m-%d %H:%M UTC')"
    echo "  server    : ${SERVER:-<none - resolved at runtime>}"
    echo "  commit    : ${commit:-unknown}"
    echo
    for f in mini-admin.html Admin.html client.html; do
        if [[ -f "$OUTDIR/$f" ]]; then
            printf '  %-16s %8s bytes  sha1 %s\n' "$f" \
                "$(stat -c %s "$OUTDIR/$f")" "$(sha1sum "$OUTDIR/$f" | cut -c1-12)"
        fi
    done
} > "$OUTDIR/BUILD.txt"
printf '  ok    %-18s %8s  -> %s\n' "BUILD.txt" \
    "$(( ($(stat -c %s "$OUTDIR/BUILD.txt") + 512) / 1024 )) KB" "$OUTDIR/BUILD.txt"

# A dropdown of the folder: opening the hosted directory shows a menu instead
# of a 403. No scripts, no fonts, no network - it must work on any host.
cat > "$OUTDIR/index.html" <<EOF
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Panels</title>
<style>
  :root { color-scheme: dark; }
  body { margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
         background: #0c0e14; color: #e8ecf7;
         font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
  main { width: min(560px, 100%); padding: 24px; box-sizing: border-box; }
  h1 { font-size: 20px; margin: 0 0 4px; }
  p  { color: #9aa4bd; font-size: 13px; line-height: 1.5; margin: 0 0 18px; }
  a  { display: block; padding: 14px 16px; margin-bottom: 10px; border-radius: 12px;
       border: 1px solid rgba(255,255,255,0.12); background: #151926; color: #e8ecf7;
       text-decoration: none; font-size: 15px; font-weight: 600; }
  a small { display: block; margin-top: 4px; font-size: 12px; font-weight: 400; color: #9aa4bd; }
  a:hover, a:focus { border-color: rgba(74,144,217,0.6); background: #1a2032; }
  footer { margin-top: 16px; font-size: 11px; color: #6b7590; line-height: 1.6; }
  code { font-family: ui-monospace, Menlo, monospace; color: #c9d4ee; }
</style>
</head>
<body>
<main>
  <h1>Panels</h1>
  <p>Open one below. If this host is not the server, the panel asks for its
     address once and remembers it.</p>
  <a href="Admin.html">Admin panel<small>Pages, workflows, click actions, chat, settings</small></a>
  <a href="mini-admin.html">Mini panel<small>The phone layout: clients, pages, flows, Telegram</small></a>
  <a href="client.html">Client page<small>What a visitor opens</small></a>
  <footer>
    Build: $(date -u '+%Y-%m-%d %H:%M UTC') · ${commit:-unknown}<br>
    Point a panel at a server with <code>?server=http://YOUR-SERVER:8446</code>
  </footer>
</main>
</body>
</html>
EOF
printf '  ok    %-18s %8s  -> %s\n' "index.html" \
    "$(( ($(stat -c %s "$OUTDIR/index.html") + 512) / 1024 )) KB" "$OUTDIR/index.html"

cat > "$OUTDIR/README.txt" <<EOF
Standalone panels - generated by deploy/make-standalone.sh
Build: $(date -u '+%Y-%m-%d %H:%M UTC')   Commit: ${commit:-unknown}

Server: ${SERVER:-<none - resolved at runtime>}
$(if [[ -z "$SERVER" ]]; then cat <<'NOTE'
No address is baked into these files, so they are safe to publish and reuse
anywhere. They find the backend in this order:

  1. ?server=http://YOUR-SERVER:8446  on the address you opened
  2. the server address this browser used last time (remembered)
  3. the origin that served the file - so if you upload these to the server
     itself (or to a domain that points at it), nothing has to be set at all

If you host them somewhere else, open one once with ?server=... or type the
address into the "Server:" field on the login screen; it is remembered.
NOTE
fi)

WHAT IS HERE

  index.html        a small menu linking to the panels below
  Admin.html        the full panel (pages, workflows, chat, settings)
  mini-admin.html   the compact panel (phone-friendly, four jobs)
  client.html       the page visitors open - serve it yourself only if you
                    are not letting the server inject it
  BUILD.txt         when this was generated, from which commit, with sizes

HOW TO HOST

  Upload the whole folder anywhere that serves static files. Nothing needs
  PHP, Node or a database - these are plain HTML files that talk to your
  backend over HTTP + WebSocket.

  Same server (recommended): put the folder next to the app and open
  /standalone/index.html. The panels pick up the server automatically.

  A different host: open .../Admin.html?server=http://YOUR-SERVER:8446 once.
  Every panel on that host remembers it afterwards.

LINKS YOU GENERATE

  Both panels offer three Link base options:
    Default server URL      the backend's own client.html address
    Custom client.html URL  the address where you hosted client.html
    This page's origin      this panel's origin + /client.html
  The choice is remembered in this browser and shared by panels on the same
  origin. It does not change the backend API/WebSocket address. Generated
  links carry ?server=... so a visitor who opens one from the other host
  still reaches this backend. On file://, page-origin mode falls back to the
  server URL.

LOGGING IN

  Same username and password as the server's admin login.

IF A PANEL WILL NOT CONNECT

  1. Is the server reachable?   curl ${SERVER:-http://YOUR-SERVER:8446}/api/settings
  2. Opening these from an HTTPS page (Netlify, Pages, ...)? The browser
     BLOCKS an http:// backend as mixed content. Either put HTTPS on the
     server, or open these files from disk / from the server itself.
  3. Wrong address remembered? Add ?server=<new-url>, or clear the "Server:"
     field on the login screen.

UPDATING THESE FILES

  Regenerate the folder from the repository - never edit the copies:

    ./deploy/make-standalone.sh ${SERVER:---none}
$(if [[ -n "$SERVER" ]]; then echo "    ./deploy/make-standalone.sh --none   # a copy with no address baked in"; else echo "    ./deploy/make-standalone.sh http://YOUR-SERVER:8446   # bake an address"; fi)

  Then re-upload. Compare BUILD.txt to know whether the host is current.

  If your address changes (a tunnel URL is often temporary), regenerate
  with the new one and re-upload - do not hand-edit the copies.
EOF

printf '  ok    %-18s %8s  -> %s\n' "README.txt" \
    "$(( ($(stat -c %s "$OUTDIR/README.txt") + 512) / 1024 )) KB" "$OUTDIR/README.txt"

echo
echo "Done. Upload the folder, or open $OUTDIR/index.html to start."

# Mixed content is the most common failure; warn when it applies.
if [[ "$SERVER" == http://* ]]; then
    echo
    echo "  warning: backend is plain HTTP."
    echo "           Fine from file:// or another http:// page, but a page"
    echo "           served over HTTPS will be blocked by the browser."
fi
