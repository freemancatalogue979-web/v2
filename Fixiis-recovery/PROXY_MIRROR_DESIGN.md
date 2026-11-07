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

## What exists now (P0, behind `MIRROR_PROXY=1`)

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

Kill switch / rollback: `MIRROR_PROXY=0` (default) → route 404s, no
`proxy_prefix` advertised, client behaves exactly as before. The DOM-capture
pipeline (fast serializer + deltas + snapshot hosts) remains fully intact as
the fallback and for archives (PCM/LPV/SingleFile/MHTML).

## Known limits of P0 / next phases

- **P1 — dynamic request routing**: a service worker registered on our origin
  inside the client iframe will route `fetch`/XHR/ws to `/m/…` so page JS needs
  no rewriting at all (today only the initial document is rewritten; inline
  event handlers that build absolute URLs still work because of the base).
- **P2 — full session fidelity**: localStorage/sessionStorage of the persistent
  context are not yet shared into the proxied page (cookies are); sync via CDP
  or serve an init script that replays them.
- **P3 — WebSocket proxying** (`wss://` frame relay) for chat/ticker pages;
  media Range streaming.
- **P4 — handoff**: remote browser does login/setup through the proxy, then the
  client continues the session; capture mirror demoted to fallback/archive.
- Sites with meta-CSS we must keep, HSTS preload, or certificate pinning will
  fail through the proxy → automatic per-page fallback to the capture mirror.

## Tests

- `tests/test_proxy_mirror.py` — 34 checks on the pure rewriter/cookies.
- `tests/test_proxy_mirror_client.cjs` — 11 checks on the client wiring.
