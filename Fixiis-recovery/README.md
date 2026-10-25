# Neo Browser Stream

> **READ THIS FIRST — LEGAL & ETHICAL NOTICE**

This project is a remote browser-streaming platform. It is intended **only for
legitimate, authorized use**, including:

- **Support and troubleshooting** of computers you own or are explicitly
  authorized to administer.
- **Remote assistance** for family, friends, or end users who have given
  informed consent and asked for help.
- **Internal IT operations** within an organization, on systems and networks
  you are authorized to manage.
- **Security research, education, and penetration testing** that is
  performed under a written scope of work against systems you own or have
  written permission to test.
- **Personal use** of the operator's own devices and accounts.

### You must NOT use this project to

- Access computers, accounts, or data you do not own or have explicit
  permission to control.
- Bypass authentication, encryption, access controls, or any other
  security mechanism without documented authorization.
- Conduct surveillance, stalking, harassment, or unauthorized monitoring
  of any person.
- Infringe copyright, scrape content in violation of a site's terms of
  service, or otherwise break the law.

In most jurisdictions, using software like this against systems you do not
own or have permission to control is a **criminal offense** (e.g. the U.S.
Computer Fraud and Abuse Act, the EU Directive 2013/40/EU, the U.K. Computer
Misuse Act, and similar laws worldwide). The maintainers of this project do
not condone illegal use and accept no liability for misuse.

**By installing, running, or modifying this software you confirm that you
understand and accept the above.**

---

## What is this?

**Neo Browser Stream** is a FastAPI service that streams a remote browser
session to an admin over WebSocket. The codebase contains three components:

| File         | Role                                                                 |
|--------------|----------------------------------------------------------------------|
| `api.py`     | FastAPI server, WebSocket endpoints, admin auth, file/session APIs.  |
| `client.html`| Page served to the *remote* browser; captures stream + telemetry.    |
| `Admin.html` | Admin console UI: live viewer, session manager, file browser, etc.   |

The admin can watch a single live client stream at a time, navigate the
remote browser, send URLs / refresh commands, and browse downloaded files
and profiles from the server.

> **Note.** The upstream version of this project shipped a Google reCAPTCHA
> step on the admin login flow. The current build disables that step
> (`verify_recaptcha` is a no-op) and the default credentials are
> `admin` / `admin`. Change the password before exposing the server to
> any untrusted network.

---

## Requirements

- **Python** 3.10+ (uses `Optional`, `match`, and modern type hints)
- **pip**
- A modern browser on the operator's machine (Chrome / Edge / Firefox)
- Outbound HTTPS for outbound links the streamed browser is asked to load

### Python dependencies

The server imports, among others:

```
fastapi
uvicorn[standard]
websockets
httpx
bcrypt
slowapi
python-multipart
```

Install them with:

```bash
pip install -r requirements.txt
```

> If you don't have a `requirements.txt`, the minimum command is:
> `pip install fastapi uvicorn websockets httpx bcrypt slowapi python-multipart`

---

## Install

Nothing to install by hand. Get the files and run `main.py`:

```bash
git clone <your-fork-or-repo-url> neo-browser-stream
cd neo-browser-stream/Fixiis-recovery
python main.py
```

Settings live in **`config.py`** — `host = "0.0.0.0"`, `port = 8446` — and
that is all a run needs. Nothing is written to a `.env` for you: no placeholders,
no generated secrets, no surprises. On the first start `main.py` creates
`.venv/`, installs `requirements.txt` into it and makes the runtime folders
(`logs/`, `profiles/`, `lpv/`, …). Later starts reuse all of it and skip
straight to serving. Full details: [Run](#run) below.

The server stores sessions, files, and profiles on disk next to the code, so
that folder must be writable by the user running it.

---

## Run

One command, from any directory:

```bash
python main.py
```

`main.py` bootstraps itself through `bootstrap.py`:

| Situation | What happens |
|---|---|
| started from another folder | switches to the code directory first |
| `venv/` or `.venv/` exists | re-execs into it (no activation step) |
| no venv, packages missing | creates `.venv/`, installs requirements, restarts itself |
| `.env` missing | nothing to do — `config.py` is used as-is |
| `.env` present but broken | placeholders/blank secrets are repaired, once |
| packages already there | nothing — start is instant |

Settings and their precedence:

```
environment variable  >  .env (if you made one)  >  config.py  >  built-in
```

`--check` prints which of those each value came from, so there is never a
guess about why the port or bind address is what it is.

Useful flags:

```bash
python main.py --check          # report python, packages, settings source, port, Chrome
python main.py --set-password   # choose the admin password (writes only that to .env)
python main.py --ignore-env     # ignore .env entirely - run from config.py
python main.py --fix-env        # repair a .env left by an old template
python main.py --setup          # force venv creation + pip install
python main.py --network        # listen on the whole network (0.0.0.0)
python main.py --local          # listen on 127.0.0.1 only
python main.py --port 9000      # override PORT for this run
python main.py --host 198.51.100.10  # bind one specific address
python main.py --open           # open the admin page once it is up
python main.py --help           # all options
```

`--check` is the first thing to run when a start fails; it names the exact
missing package, unwritable folder or busy port.

### Auto update: restart when files change

One line in `config.py`, off by default:

```python
    auto_update: bool = False      # True -> restart when a source file changes
```

With `True`, uploading a file or running `git pull` on the server is enough —
the app closes its sessions, re-execs itself and comes back a few seconds
later with the new code. Nothing to install: no systemd unit, no scripts.

It is off by default because a restart closes live sessions; leave it off if
you would rather choose the moment (`sudo systemctl restart fixiis`).
Runtime folders (`lpv/`, `logs/`, `profiles/` …) never trigger it, changes are
debounced, there are at most 8 restarts per 10 minutes, and a file that does
not compile is reported instead of being loaded.

```bash
curl -s localhost:8446/health | jq .build.auto_update   # true / false
python main.py --check                                  # says which file it came from
```

### Changing the port, the host or any other setting

Edit `config.py` and restart. That file is the source of truth:

```python
    host: str = "0.0.0.0"   # every interface: this machine and its network
    port: int = 8446
```

No `.env` is required. If one exists (created by you or by
`deploy/setup.sh`), it only *overrides* `config.py` — and if it contains a
template placeholder, a loopback `HOST` while nothing proxies to the app, or
a blank secret, it gets repaired once at startup and the change is printed.

### Reaching it from a phone or another PC

The server binds `HOST` from `.env`, and the shipped `.env.example` says
`HOST=0.0.0.0` - "listen on every interface", so this machine *and* every
device on the same network can open it. `main.py` prints the address to use:

```
  Fixiis / Neo Browser Stream

    admin   http://198.51.100.10:8446/admin
    mini    http://198.51.100.10:8446/mini
    client  http://198.51.100.10:8446/client.html
    health  http://198.51.100.10:8446/health
    local   http://127.0.0.1:8446/admin   (this machine only)
```

If the banner shows `http://127.0.0.1` and a `! listening on 127.0.0.1`
warning instead, the server is local-only. Either:

```bash
python main.py --host 0.0.0.0                     # this run only
# or make it permanent:
#   HOST=0.0.0.0  in .env, then restart
python main.py --check                            # shows the bind address
```

Still refused from another device? The port is blocked by the firewall:

```bash
sudo ufw allow 8446/tcp                    # Linux (ufw)
sudo firewall-cmd --add-port=8446/tcp --permanent && sudo firewall-cmd --reload  # firewalld
# Windows: allow python.exe on the Private network when the prompt appears
```

Then check that both devices are on the same network/VPN. `HOST=127.0.0.1`
is the right setting only when something else on the same box (nginx, a
Cloudflare tunnel) proxies to it. Never expose a public interface without
auth + TLS in front of it.

Open:

- `http://127.0.0.1:8446/client.html` — the *client* page (loads in the
  remote browser)
- `http://127.0.0.1:8446/admin`     — the admin console
- `http://127.0.0.1:8446/health`    — JSON health check

Log in to the admin console with `admin` / `admin` and change the password
immediately.

### Environment variables

Nothing below has to be set: these are `.env` keys, and every one of them
has a working default in `config.py`. Set one only when you need to
override it.

| Variable               | Default (config.py)    | Purpose                                          |
|------------------------|------------------------|--------------------------------------------------|
| `HOST`                 | `0.0.0.0`              | Bind address. `127.0.0.1` = this machine only     |
| `PORT`                 | `8446`                 | HTTP port                                         |
| `ADMIN_USERNAME`       | `admin`                | Admin login username                              |
| `ADMIN_PASSWORD_HASH`  | bcrypt hash of `admin` | Set yours with `python main.py --set-password`    |
| `JWT_SECRET_KEY`       | built-in placeholder   | Signs admin tokens; `--set-password` writes one   |
| `SERVER_DOMAIN`        | _unset_                | Only set it to a domain that really resolves here |
| `PUBLIC_IP`            | _unset_                | Same rule; used in absolute links/reports         |
| `USE_HTTPS`            | `false`                | `true` only when TLS terminates in front of the app |

**Empty means "not configured".** A line like `ADMIN_PASSWORD_HASH=` does not
blank out the default, and a template placeholder (`SERVER_DOMAIN=panel.example.com`)
is ignored — previously those two turned into "login impossible" and
"links point at a domain nobody owns". `python main.py --fix-env` repairs a
`.env` that was written by an older template.

---

## The CDP cast screens are desktop-only

The panels themselves work on a phone — `Admin.html` has drawers and bottom
tabs, and `mini-admin.html` is the one-hand panel. What does **not** work on a
phone is the **screen**: the CDP cast of a server-side browser (the Display
tab's live view, PCM, and Access). It is driven with a mouse and a keyboard, it
decodes at desktop size, and a mis-tap drives somebody's real browser. So each
cast area — not the page — is what refuses to run there.

| In a cast area (`[data-pc-cast]`) on a phone | What happens |
|---|---|
| the area is shown | it goes blank and says **"View on PC only"**: everything inside it (`> *:not(.fixiis-cast-note)`) is hidden, the area is marked `fixiis-cast-blocked`, and `<html>` carries `data-pc-only-screens="1"` |
| a client card is selected | the operator still gets the client's **info and timeline** — the `subscribe` message that starts a screencast is never sent, so the server is not asked to encode a stream nobody can watch |
| the PCM screen | `/ws/pcm` is never opened — the PCM screen *is* the cast |
| the Access screen | `/ws/access` is never opened, and the status line says why |
| the check | a phone user agent, or `(pointer: coarse)` with `min(screen.width, screen.height) < 700`; an iPad reporting a desktop UA is refused unless the device also has `(any-pointer: fine)` — a touch-screen laptop still drives the screens |
| everything else | untouched: no session socket is disabled, no `fetch` is replaced, the lists, logs, links, chat and file tools all work on the phone |
| never a redirect | the address stays put |

`mini-admin.html` has no cast screen, so it is **not** gated at all — it is the
phone panel. `client.html`, the visitor's page, is never gated either: visitors
are on phones. `tests/test_pc_only_gate.cjs` (70 checks) runs the shipped gate
bytes against a fake DOM for a phone, an iPhone, an iPad, four desktop sizes and
a touch laptop, and checks that the three cast entry points stop at the flag
while the page itself keeps its sockets.

## Device versions: one workflow, phone + desktop

Some offers need a different layout on a phone and on a desktop. You do **not**
need to duplicate the workflow: archive both HTML files, then in the page
archive press the **⌨** button on a card and say who the page is built for.

| Setting | What the visitor gets |
|---|---|
| `Everyone · one version` (default) | the page, on every device |
| `📱 Phones` + desktop page | phones get this page, desktops get the other one |
| `⌨ Desktops` + phone page | desktops get this page, phones get the other one |

The pairing is saved on both pages, so a workflow step can point at either
one — the server looks at the device the visitor connected with (`is_mobile`
in the client's boot payload) and delivers the matching HTML. Nothing else
changes: `page_url`, the click guard and the version history all follow the
page that is actually served, `Push` resolves per target client, and pages
that were never paired keep behaving exactly as before.

Set it up over HTTP (`GET`/`PUT /api/lpv/pages/<page_id>/device`, body
`{"device": "mobile", "variant_page_id": "<other page id>"}`) if you prefer
scripting it.

---

## Page fields: type a value, then push

A page can carry slots — an amount, a reference, a sign-in word, a name. Save
the slot once, and **every push asks you what this visitor should see**. There
is no "one universal rewrite": two people get the same page with different
words in it.

1. Page archive → **⌨ Bind** on the page card.
2. Add a row: what to **find** (`Sign`, `NGN 0.00`, `.amount` …), how to match
   it (`text` by default), and the **Value** you want pre-filled in the push
   dialog. The optional **Name** is just a shorter label for that field.
3. **Preview** shows how many places matched before anything is saved.
4. Push the page: the panel asks for every binding, and the visitor's copy of
   the page says what you typed. Leave a field empty and that spot keeps the
   page's own text.

| Matcher | What it means | Example |
|---|---|---|
| `text` (default) | every exact occurrence of a string | `NGN 0.00` |
| `element` | the content of elements matching a CSS selector | `.amount`, `#card .total`, `[data-role=amount]` |
| `regex` | every match of a regular expression | `NGN [\d,]+\.\d\d` |

What you should know:

* **The stored page is never edited.** Substitution happens on the bytes a
  visitor receives for that one push, so re-pushing the same page without a
  value shows the original HTML again.
* **A value you leave empty means "leave the page alone"** — the saved default
  is used, and if there is no default the original text stays.
* **Values are per push.** They are carried in the pushed URL (`?b=<token>`),
  expire after six hours, and only ever apply to the page they were typed for.
* **A binding is labelled by what it binds.** No name needed: bind `Sign` and
  the push dialog asks for "Sign". Two bindings with the same name (or the same
  text) share one answer.
* **Serving without a push** (a page opened directly, a workflow step with no
  values) still uses each binding's saved Value, so pages keep working.
* Both panels ask: the mini panel shows the fields above **Send page**, the
  full panel shows them in the push dialog. Cancel pushes nothing.
* HTML in values is escaped, so `<b>1</b>` renders as text rather than markup.
* Over HTTP: `GET`/`PUT /api/lpv/pages/<page_id>/bindings`, and
  `POST /api/lpv/pages/<page_id>/bindings/preview` with
  `{"rules": [...], "bindings": {"amount": "NGN 5.00"}}`.
  Pushing with values = `{"type":"lpv_push_page", "page_id": …, "bindings": {...}}`;
  a workflow run accepts the same `bindings` object and offers each value to the
  page steps that declare that name.

---

## Editing an archive (re-uploading its HTML)

A page is not frozen once it is in the archive. On its card press **✎** (or
open **⋯ → Edit HTML**):

* the editor starts from the **stored** HTML, not the rendered visitor copy,
  so saving cannot bake bindings into the page;
* **Choose file…** loads a local `.html` into the editor, or paste straight in;
* **Save new version** keeps the page's id, name, bindings, click guard,
  click actions and device pairing — only the file changes;
* the outgoing HTML goes into **Version history**, so an edit is reversible
  from the same card.

Over HTTP: `GET /api/lpv/archive/<id>/source` (raw, admin only) and
`PUT /api/lpv/archive/<id>/html` with `{"html": "..."}`.

---

## Click actions: "if they click this, take them there"

Actions belong to the **page**, not to a workflow: they travel with the HTML
wherever that page is pushed. On the page card (Archive tab) press **⚡**
(or **⋯ → Actions**):

| Field | Meaning |
|---|---|
| **🎯 Pick** | opens the page and lets you click the real button — the rule is filled in for you |
| **Control** | or type it, using the same rules as the click guard: `text:Sign in`, `.btn-next`, `#submit`, `a[href*="/go"]`, `button[type=submit]` |
| **go to a page** | show another archived page (bindings, guard and its phone/desktop version all still apply) |
| **open a link** | send the visitor to an external address — `google.com` is accepted and read as `https://google.com` |

Picking prefers a stable identifier (`#checkout`, `name:next`), then the
visitor's own words (`text:Sign in`), so the rule stays readable in the panel
afterwards.

**The click cannot be swallowed.** The mapping is checked before the click
guard and before the page's own handlers, so it fires:

* on a **locked** page — the mapping is the permission, and a mapped control
  is drawn as allowed rather than as a dead button;
* when the page's own `href` or `onclick` says something else — the mapped
  target is the one used, because the click is captured and the default
  action is cancelled;
* when the click lands on the icon or text *inside* the button.

A **link** target travels with the page, so the visitor is moved immediately —
no round trip to wait on. A **page** target still comes from the server (that
is where the HTML, its bindings and its device build are); the click shows the
spinner at once, and if that page was deleted in the meantime the visitor is
told instead of being left with a button that does nothing. Every click is
recorded in the activity log as `page_action`; a miss is recorded as
`page_action_skipped` with the reason (`stale_page`, `no_actions`,
`index_out_of_range`, `rule_not_found`, `target_missing`).

Saving actions updates visitors who are already on that page, and actions
saved on one build of a **device pair** also apply when a visitor is served
the paired build.

**Workflows no longer offer an `+ Action` step.** Old saved flows that already
have one keep running and the step is shown read-only, with a reminder to move
it onto the page.

Over HTTP: `GET`/`PUT /api/lpv/pages/<id>/actions`.

---

## Turning the visitor pages off (the one switch in the mini panel)

The mini panel has a single row under its title — **Visitor pages** — with a
switch. It is the site's power button for the pages *visitors* open:

| Switch | What a visitor gets |
|---|---|
| **ON** (default) | the page, as normal |
| **OFF** | `404 Not Found` — `/`, `/client.html` and every pushed page (`/api/lpv/archive/<id>/html`) |

Turning it off is immediate and needs no restart. Everything the operator
needs keeps working, so a switch flipped from a phone can always be flipped
back from the same phone:

| Still served while pages are OFF |
|---|
| `/admin`, `/mini` — the panels |
| `/api/*` — including the login and the settings endpoint behind the switch |

Details that matter in practice:

* **It looks like an ordinary 404** — the page says "This page could not be
  found" and nothing else. A closed site never tells a visitor that somebody
  is operating it.
* **It is not cached** (`Cache-Control: no-store`), so turning it back on is
  seen on the next load.
* **A visitor already on a page keeps what is on screen** until they reload;
  the reload gets the 404, and a tab that reconnects is sent
  `{"type": "site_disabled"}` and closed instead of retrying forever.
* **Pushed pages stop too**, so a running workflow cannot quietly deliver the
  next step to somebody whose site is closed.
* **Standalone copies** (`standalone/client.html` hosted elsewhere) still
  load, because they are plain files — but their pages come from the API, so
  what they render is 404 until the switch is back on.
* **It fails open**: if the settings file cannot be read, the site stays up.
* The same setting is `public_pages_enabled` over HTTP —
  `POST /api/settings {"public_pages_enabled": false}`.

---

## Live chat bubble: on, off, and which device

Settings → **Live chat bubble** (or mini panel → Pages → Advanced):

| Control | What it does |
|---|---|
| **Show chat on live pages** | OFF removes the bubble for every visitor, on every page. ON brings it back. |
| **Show it on** | `Both` (default), `Desktop only`, `Mobile only` |
| **Bubble does** | `Open the chat` (default), or `Go to a page or link` |
| **Goes to** | `A link` or `An archived page` |
| **Link** / **Archived page** | The URL box, or a list of your archives (`name (vN)`) |

**Two modes.** In *chat* mode the bubble opens the conversation panel. In
*redirect* mode the bubble is a shortcut: pressing it shows an archived page
(through the normal push path, so bindings and guard still apply) or sends the
visitor to a link — no conversation, no thread.

The target is picked the same way in all three places it can be set —
**Settings → Live chat bubble**, **Live pages → Chat → Bubble settings**, and
the mini panel under **Pages → Advanced** — and the list of pages is fetched
fresh each time it opens, so an archive uploaded a minute ago is already there.
The rows follow **Goes to**, not **Bubble does**: you can choose the page or
paste the link while the bubble is still opening the chat, then flip the mode
when you are ready. A page that was deleted after being chosen stays listed as
`(missing page) …` instead of disappearing silently, so you can see what to fix.
The switch itself is never a prerequisite — a chosen target is saved with the
rest of the settings and used the moment the bubble is set to redirect.

The device is the same one that picks a page's phone/desktop build — the
device the visitor actually connected with, not the window size. Both
directions work: a desktop-only bubble stays off on a phone, and a
mobile-only bubble stays off on a desktop.

Changes apply **immediately**, including to visitors already sitting on a
page: the server re-sends the config per visitor when you save, and the client
refuses to build the bubble on the wrong device even if it is told about a
switch mid-session. While chat is off, anything a stale widget tried to send
is ignored.

Over HTTP: `POST /api/settings` with `{"chat_enabled": true, "chat_devices":
"desktop"}` (also `mobile`, `both`), plus `chat_mode` (`chat`|`redirect`),
`chat_redirect_kind` (`link`|`page`), `chat_redirect_url` and
`chat_redirect_page_id`. A bad device value is stored as `both`
rather than wedging the setting.

---

## Fast start: why the visitor's page appears quickly

A visitor opens a link on a phone, often on a mobile network. Four things used
to sit between them and the page, and all four are fixed:

| What was happening | Why it hurt | Now |
|---|---|---|
| `client.html` (~550 KB) and every archived page were served **uncompressed** | On a 3G connection that is a minute of waiting before anything moves | Gzip on every text response: `client.html` is **113 KB**, a 230 KB page is ~25 KB |
| The Google Fonts stylesheet was render-blocking | The browser refuses to paint until a third-party host answers — slow or filtered networks stalled the whole page | Fetched **after** first paint (`preload` → `onload`), with the system font stack covering the gap |
| `init()` awaited a `POST /api/client/session/disconnect` before opening the WebSocket | A mobile round trip (or a hung POST) delayed the socket, and with it the page | The handoff gets a **1.2 s budget**; the socket opens either way, and a late handoff can no longer strand the visitor on "Session replaced" |
| The server awaited geolocation (3 providers × 4 s) before the boot message, then pushed the landing page **again** | Up to 12 s before the visitor was even told where their page was, then the page was downloaded twice | Geolocation gets **0.6 s** (cached per IP for 6 h), the boot message goes out **before** bookkeeping is written, and the landing page is pushed **once** |

Repeat visits cost almost nothing: both the client page and the panels send an
`ETag`, so a reload is a `304`, not another download. `/admin` and `/mini` get
the same treatment (the admin panel is ~1 MB of HTML).

If a page ever feels slow, the numbers are recorded. In the browser console
the boot logs its own timeline:

```
[boot] websocket open @210ms
[boot] first page painted @640ms
```

and the same first-page figure travels with the page-load event, so it lands in
the activity log as `ms_since_boot`.

A pushed page is fetched with a per-address budget for the answer (8 s) and no
budget on the body: an unreachable backend address can no longer hang the page
for the browser's own TCP timeout, and a slow phone can still finish
downloading a big page instead of having it cancelled halfway.

---

## The custom loading page is in the page, not fetched afterwards

A workflow can carry its own **custom loading page** (the spinner you upload in
the workflow editor, or one of your archived pages). It used to be fetched
*after* the WebSocket boot, so a visitor opening the link saw the built-in
black ring first and your spinner a round trip later.

Now the server **embeds that page in the `client.html` it hands out**. The
document arrives with your spinner already in it, so the browser paints it
while it is still parsing — nothing else can appear first.

| What you configured | What is in the served page |
|---|---|
| Uploaded HTML | your HTML, in an iframe with `srcdoc` |
| An archived page | the same page you would see, `data-lpv-url` pointing at `/api/lpv/archive/<id>/html` |
| Nothing (built-in spinner) | nothing added — the page is byte-for-byte what it was, `ETag` unchanged |
| LPV-only mode with a landing page | the landing page, unless it is the built-in `_default.html` |

How it behaves:

* **It is only a stand-in.** The moment the live runtime has the same page up
  (`_showRedirect`) or the first pushed page is painted (`showPage`), the
  embedded copy fades out over 0.18 s and is removed. Only ever one on screen.
* **The built-in black loader is retired while it is up**, and only then —
  the rule is scoped with `:has()`, so if the copy is gone the normal loader
  still works instead of leaving a white screen behind nothing.
* **Clicks in it still count.** Pressing something inside the spinner sends the
  same `lpv_spinner` event a `redirect` workflow step waits for, so a flow that
  advances on "visitor clicked the spinner" keeps working during the hand-over.
* **Your HTML is escaped and sanitised** on the way in — quotes, `<script>` or
  `</iframe>` in somebody's spinner cannot break out of the document — and the
  copy is rebuilt when you change it (the cache key includes the page's hash).
* A visit with **no** custom loading page is untouched: no markup, no extra
  stylesheet, the normal `ETag`.
* The console hook `window.__LPV_DROP_BOOT_LOADING__()` removes the embedded
  copy by hand if you ever need to see what is underneath.

Tests: `python3 tests/test_boot_loading_embed.py`.

---

## Domains: a site's own loading screen

**Domains** tab (next to *Live pages*). One row per site — the loading screen
that site's visitors see, and the provider's logo they recognise. Configure a
domain once and every page captured from it uses it.

| Field | What it does |
|---|---|
| **Domain** | the site. A full link is fine — `https://www.chase.com/login` is stored as `chase.com` — and its subdomains match too (`login.chase.com` finds `chase.com`) |
| **Name shown under the logo** | e.g. `Chase` |
| **Provider logo** | the mark drawn on the loading screen. Leave it empty and the site's own **icon** is used: the best one a capture of that site carries (apple-touch-icon first — 180px stays sharp), else `https://<domain>/favicon.ico`. A 16px favicon is the floor, not the goal: paste the provider's real logo URL for a sharp mark. "Use this page" fills it in from a capture |
| **Loading screen** | `Logo + progress bar` (built in), `My own HTML file` (upload or paste, up to 1 MB), or `An archived page` |
| **Use this loading screen** | the on/off switch; off behaves as if the domain were not configured |

How it reaches the visitor:

* **It is inside the page they receive.** The server resolves the site this
  visit lands on — the first page step of the workflow, or the `?url=` the
  visitor was given, or the LPV landing page — finds that site's configuration,
  and embeds the resolved loading screen in `client.html` (same mechanism as a
  workflow's custom loading page). The browser paints it while parsing, so
  there is no blank moment and no fetch.
* **A plain client link shows it too — for the site it names.** "Send the
  visitor the client link and push a page by hand" carries `?url=`, and the
  host in that URL is the site whose screen applies. A visit to a site with no
  configuration gets the **built-in branded ring**, which is the normal loading
  screen — not another domain's custom HTML. ("There is only one domain
  configured" used to be treated as an answer, so the one configured screen was
  shown to every visitor; that is exactly the leak this fixes.)
* **Resolution order:** the workflow's own custom loading page → the site's
  domain configuration (from the landing page) → the site the visit's `?url=`
  names → the LPV landing page (LPV-only mode) → the built-in ring. The most
  specific choice wins, and a domain's screen is only ever used for that
  domain.
* **A page is never branded as a site it merely links to.** The page's own host
  is read from `<base href>`, then `rel=canonical` / `og:url`, then the most
  mentioned URL — and a configuration only applies when it belongs to the SAME
  site as that host. A page that embeds one asset from another provider is not
  that provider.
* **A capture's first URL is not the site.** Google's markup opens with a
  `gstatic.com` asset; banks open with their CDN. Believing the first URL made
  the site's configuration unreachable *and* sent the logo to the favicon
  fallback. So the host is read from `<base href>`, then `rel=canonical` /
  `og:url`, then every absolute URL by how often it appears — and every host the
  page mentions is tried against the configured domains, so an asset host in
  front of the real one is harmless.
* **The provider's logo replaces the workflow's "custom branding".** A visitor
  on a bank page sees the bank's mark, not a colour picked once for the whole
  flow — including in the branded ring and in each later push, which carries
  `spinner_url` so a multi-step flow keeps one identity.
* **A domain that has a loading screen never sees ours.** The runtime adopts the
  embedded copy the moment LPV starts (`LPV.redirectUrl` is set from it), and
  every path that puts a custom loading page on screen retires the hard-coded
  branded loader and the built-in ring. There is no moment where our own spinner
  appears instead of, or on top of, the domain's.
* **A streamed visitor gets their mirror too.** The loading page is embedded in
  every visit, and a *streamed* visitor never goes through `LPV.showPage`, so
  none of the Live-pages hand-over ran for them: the site's spinner stayed on
  top of the mirror (and swallowed their clicks) for good. Every way a streamed
  page reaches the screen — a snapshot, a DOM patch, a body replace, a repeated
  frame — now retires it, and `lpvHoldLoadingScreen()` refuses to raise a loader
  over a stream that is already visible.
* **A page on screen is never covered again.** Two invariants, both enforced in
  the runtime: a loading screen may only be visible while a transition is in
  flight (or before the first page), and a spinner that outlives its purpose is
  taken down by a watchdog (15 s). This closes the cases where a loader used to
  stay in front of the page for good — a branding/boot message repeated after a
  reconnect, a click whose target never arrived, a loading page that finished
  downloading late. `window.__LPV_LOADERS_STATE__()` in the console says which
  loader is still on screen, if one ever is.
* **It is only a loading screen.** The moment a mirrored page commits, the
  loading page is gone for good: the embedded copy is removed, the runtime's own
  loading-page frame is hidden, and — this is the part that used to break — a
  loading page whose download *finishes after the page* never paints itself back
  over it. The runtime stamps each loading page with the moment it was asked
  for and compares it before revealing anything, so a slow spinner download
  can't leave the visitor staring at a spinner with the page behind it.
* **One spinner, not two.** The runtime checks the copy already embedded in the
  document it is running in before loading anything: same page → it is adopted,
  nothing is fetched, nothing is drawn on top. Reading `window.__LPV_BOOT_LOADING_STATE__()`
  in the console tells you which page it is and whether it is on screen.
* **The file picker is a normal button.** `Choose HTML file` opens the hidden
  file input and the chosen name (with its size) is shown next to it — no native
  file control in the middle of the panel.
* **A missing icon never leaves a broken box**: the mark walks
  `apple-touch-icon.png` → `favicon.ico` → a letter tile, one step per failed
  load. The same chain runs in the Domains list, so every configured row shows
  that site's own icon — your logo, else the icon a capture carries, else
  `https://<domain>/favicon.ico`, else the first letter — instead of a column of
  grey squares, and the form previews the typed domain's favicon before you
  save. Each row also has a **Preview** button that opens the exact document
  served at `/api/lpv/domains/<domain>/spinner.html`.
* **A finished loading screen gets out of the way** as soon as the real page
  paints (see *Fast start* above).

Picking a page with **Use this page** reads the domain *and* the logo out of the
capture itself (`GET /api/lpv/pages/<id>/logo`) — no external lookup, no
guessing — so it works on a machine with no internet.

Over HTTP:

| Call | Purpose |
|---|---|
| `GET /api/lpv/domains` | every configuration, without the (large) HTML, plus `resolved_logo` and `spinner_url` |
| `POST /api/lpv/domains` | `{domain, label, logo_url, spinner_html, spinner_page_id, enabled}` — omit a field to keep it |
| `DELETE /api/lpv/domains/<domain>` | remove one |
| `GET /api/lpv/domains/<domain>/spinner.html` | the resolved loading screen (404 when the domain is off) |
| `GET /api/lpv/pages/<id>/logo` | the page's domain + logo candidates, best first |

Tests: `python3 tests/test_domain_spinner.py`, `node tests/test_domain_tab_ui.cjs`.

---

## Stream and Live pages are two different visitors

`client.html` serves both, and they must not be dressed as each other.

| Visit | What they get |
|---|---|
| **Stream** — `/client.html?auth=…&url=…`, a real browser streamed to them | the plain client page. No workflow name, no workflow colour/logo, no workflow loading page. The tab keeps the **streamed site's own title** (`metadata.title`, else the site's name), because that is what they are looking at |
| **Live pages** — `/client.html?workflow=…` (or a link made by the panel), and every visitor while **Live pages only** is on | the workflow's branding: its name as the tab title, its colour and logo, its custom loading page |

Why it matters: the auto-workflow ("Auto-play for new clients") is a Live-pages
feature — it fires only in Live pages only mode, and there is no browser to
watch there. It used to brand every visitor, including the streaming ones, and
a branded client stops following the stream's title — so an operator watching a
streamed site saw a Live-pages workflow name instead. Now the server only hands
the workflow to an LPV visit (`?workflow=`, an LPV link, or Live pages only
mode) and the client lets the stream keep its own title unless an LPV page is
actually on screen.

The site's **Domains** loading screen is not workflow branding: it still applies
to both kinds of visit, because it belongs to the website, not to a flow.

---

### Crisp at every scale (the stream is rendered at 2× by default)

The streamed view used to be pinned to a device scale factor of `1.0`: a
1920×1080 viewport became a 1920×1080 surface, so on a laptop or phone whose
screen runs at 2× or 3× every glyph, border and avatar was upscaled by the
browser and looked soft. The surface is now rendered at **viewport × DPR** and
scaled down for display, which is what makes it crisp.

| Where | What happens |
|---|---|
| `CONFIG.stream_dpr` (default **2.0**, clamped 1.0–3.0, env `STREAM_DPR`) | one number, read everywhere — no path may pick its own |
| the browser launch | `--force-device-scale-factor=<DPR>` on the direct-Chrome path, `device_scale_factor` on the Playwright persistent-context path, and the same in the SeleniumBase backend (`launch_for_pcm`, `launch_for_access`) |
| the screencast | frames are requested at `css_width × DPR` by `css_height × DPR`, and the start message carries `css_width`, `css_height`, `dpr` so the panel knows the frame space |
| the panel | the canvas is displayed at CSS size (`payload.frame = true` for the operator canvases); the admin screen-cast requests the same scaled surface |
| input | a click or point is divided by the DPR on the way back in (`pcm_manager._pt`, `access_manager.point`, `session._px`), so a click at (400, 300) still lands at (400, 300) |

Wheel, keys and scroll sync are never converted — they are not coordinates.
`config.frame_to_css(v, dpr)` is the one conversion (`v / dpr`, junk → `0.0`,
`dpr = 0` → unchanged). Set `STREAM_DPR=1` to trade sharpness for bandwidth.

A forced scale factor and the emulated metrics have to agree: patching only the
CDP override (`Emulation.setDeviceMetricsOverride`) inside a browser launched at
1× still renders a 1× surface, which is why both are set from the same value —
and why `tests/test_stream_scale.py` fails if any launch path, the screencast
budget or an operator canvas regresses to a pinned `1`.

### The icon API (why the tab can trust it)

`GET /api/site/icon?domain=<host>` (optionally `&url=<page>`) answers with the
icon **of that domain**, as bytes, from our own origin:

| The request | What the server does |
|---|---|
| `domain=bank.example` | normalises it to the registrable host; anything that is not a public hostname — `localhost`, an IP literal, a bare label, a path, a port — is refused with `400`, so the endpoint can never be pointed at the sandbox itself |
| fetch | `/apple-touch-icon.png`, then `/favicon.ico`; if both fail, the site's HTML is read and its declared `rel*="icon"` links are ranked (`apple-touch-icon` first, declared `sizes`, `svg` before a bitmap) |
| verify | the body must **decoded as an image** (PNG/JPEG/GIF/ICO/SVG/WebP) or it is not an icon — a soft-404 HTML page is discarded instead of served |
| last resort | the server's own copy for the site (the operator's domain mark, or the favicon already in the profile) |
| answer | `image/*` + `Cache-Control: public, max-age=86400` + `X-Icon-Source: domain\|declared\|stored`; `404` when the domain has no icon at all, so the tab falls through to its floor |
| cost | cached per host for 6 hours, 4 s fetch timeout, 512 KB cap — an icon is a nicety on a loading path, never something a visitor waits on |
| load | **one fetch per host at a time**: ten visitors opening the same site share the first fetch instead of starting ten, and a host with no icon is remembered (`_SITE_ICON_MISS`, 5 min) instead of being asked again on every page load |
| TLS | certificates are verified; if this machine cannot verify them (a TLS-intercepting proxy with its CA not installed in Python's store) the fetch retries **once** unverified and remembers it, because an icon is a public picture and a machine-wide 404 is the worse outcome |

The tab asks for it two ways: first paint (`updateFavicon`, before any capture
has arrived) and after a commit (`mirrorIconCandidates`, after the page's own
declared icons and the server's stored icon). The client sends its domain and
gets a picture; the resolution happens on the server, where the request is not
cross-site and cannot be hotlink-blocked.

## The tab is the page (streamed visits)

The browser tab is part of what a visitor is looking at, so on a streamed visit
it has to be the **site's** tab: the site's title and the site's own icon. Not a
hostname guess from a favicon service, and not the title from five minutes ago.

| Where the tab identity comes from | Why |
|---|---|
| the mirrored document itself — its `<title>` and its `<link rel="icon">` | the capture already shipped those exact bytes (a `data:` URI, or `/assets/<hash>` on our own origin), so the tab shows what the page shows |
| the site's **own** icon ranking: `icon` / `shortcut icon` over `apple-touch-icon`, declared `sizes` over none, `svg` over a bitmap, later declarations win ties | the same choice the browser would have made |
| **the icon the server already has for that site** | the same picture the panel shows for the visit: the mark configured for the domain (Domains tab), else the favicon the server downloaded into the profile, else the best icon a capture of that domain carries. It is served from our own origin, so a hotlink block or a 404 cannot lose it |
| **the domain's own icon, through our API** (`/api/site/icon?domain=…`) | the icon of the site the page is **in**, fetched from that domain and served from our own origin: `/apple-touch-icon.png`, then `/favicon.ico`, then whatever the site's HTML declares, best first, and only then the copy the server keeps. The tab cannot make that request itself — it is cross-site, some hosts refuse the referrer, and a site that answers `200` with an HTML error page decodes as no icon at all. A third-party service answers with its own globe for a site it does not know, so this endpoint replaces it |
| the origin's `/favicon.ico` when the page declares nothing | a page with no icon still has an icon |
| the server's own `/favicon.svg` | the floor above the inline SVG: an app icon rather than a generic globe (asked for as `.svg` because Chrome discards an SVG served under an `.ico` name) |
| an **inline SVG data URI** as the last candidate | a tab with no icon reads as a broken tab, so the chain cannot come up empty: no request, no 404, no hotlink block |
| — and when the site changes either one, the change is followed | "Inbox" → "Inbox (3)", an unread-badge icon, a login flow renaming the tab |

There is deliberately **no third-party favicon service** in the chain — not even
for the first paint. It was an external request on every page load, usually
blocked, and when it answered it answered with someone else's idea of the site's
mark (a generic globe for any site it did not know). `/api/site/icon` replaces
it: the domain the page is in answers for its own icon, and `updateFavicon()` —
the old hostname guess that ended in that service — now runs the same verified
chain instead.


**A bad icon URL can no longer blank the tab.** The chain probes each candidate
with an `Image()` and takes the first one that actually loads — 1.2 s per
candidate, and a 1.5 s deadline for the chain as a whole, after which the inline
default is already up. A candidate that arrives later still replaces it, so the
visitor sees the site's icon as soon as it is known and a real icon rather than
a hole until then. Two details are the reason this used to fail outright:

* **The base URL.** The mirrored document lives at `blob:https://…`, so its
  `baseURI` **is** that blob URL. Resolving a relative icon against it produced
  `blob:https://…/favicon.ico`, which loads nothing. The chain resolves against
  the capture's own `<base href>`, then a clean `baseURI`, then the page URL the
  mirror was rendered from (`state._mirrorUrl`) — never a `blob:`/`about:`/`data:`
  base.
* **The `<link>` element.** Chrome caches a favicon per element, so assigning
  `href` on the link that is already the tab icon can leave the old picture up
  forever ("the favicon never updates"). The mirror appends a **fresh**
  `<link id="shf-mirror-favicon" rel="icon">` and removes every other
  `link[rel*=icon]`, which is what makes a swap visible immediately. A newer
  page supersedes an older probe chain, so a slow icon from the previous page
  can never win over the new page's first candidate.

**How a change travels.** A full capture carries both inside the document, so
the tab updates when the page paints. Between captures, a rename or an icon swap
has no DOM target the mirror can absorb (the `<title>` text and the
`<link rel="icon">` href are not page content), so the capture page reports both
as a control op:

```
["meta", "Inbox (3)", "https://bank.example/badge.png"]
```

It rides at the head of the next delta batch, the relay forwards it untouched,
and `client.html` applies it to the **outer** tab — it never needs the mirror
document, a mid map or a generation match, so it can never be the reason a
resync is requested. The icon in that op is verified by loading it first: a
broken icon URL keeps the previous icon, because a blank tab is worse than a
stale one.

**Branding still wins — on LPV visits only.** A workflow palette, an LPV tab
favicon or an LPV visit with a workflow name owns the tab, and nothing above
ticks over the top of it. The same guard means the old hostname-guess
`updateFavicon()` can no longer replace an icon taken from the capture.

That cuts both ways, and it used to leak: saving the tab title/favicon in
Settings pushed the new values to **every** connected client, so an operator
renaming a Live page also renamed the tab of a visitor watching a streamed
browser. Now:

* the served document states which kind of visit it is
  (`<script id="lpv-visit-flag">window.__LPV_VISIT__ = true|false</script>`, in
  both the plain and the branded copy), and `applyLpvTabBranding()` is a no-op
  on a streamed visit;
* `_push_lpv_branding_to_clients()` only fans out to LPV clients, never to a
  session that is streaming a browser;
* the operator's icon is applied as a **fresh `<link rel="icon">`** (the old
  ones are removed) — Chrome caches a favicon per element, so editing `href` in
  place is why an operator's tab icon could fail to appear until a reload;
* `mirrorTabIsBranded()` looks at **every** icon link, not just the first: the
  mirror appends its own link, so "the first link" is not necessarily the one
  the operator branded.

With a page on screen, this answers "what does the tab think it is?":

```js
__MIRROR_TAB_STATE__()
// { title: "Inbox (3)", appliedTitle: "Inbox (3)",
//   icon: "…/assets/<hash>.png", appliedIcon: "…", branded: false }
```

Two more diagnostics live in the same console — what the tab chose from, and a
forced re-sync onto the page that is on screen:

```js
__mirrorSyncTabFrom__()                 // re-read the live page, apply, report
__mirrorIconCandidates__(frame.contentDocument)
// [ "…/assets/<hash>.svg", "https://panel.example/api/profiles/favicons/bank_com.png",
//   "https://panel.example/api/site/icon?domain=bank.example",
//   "https://bank.example/favicon.ico",
//   "https://panel.example/favicon.svg",
//   "data:image/svg+xml;utf8,…" ]
```

It also runs end to end: the tests boot the real `client.html` in a DOM
(`tests/` covers the block in a `vm`; a manual jsdom boot of the served page
shows the same), so the chain, the fresh `<link>` and the never-blank floor are
verified against the page that actually ships.

The page's own boot icon had the same disease in a different place: it was an
**unencoded** SVG data URI (`href="data:image/svg+xml,<svg …>"`), which is at the
mercy of URL parsing — and a favicon whose URL fails to parse is a blank tab
before the mirror has said anything at all. It is now the same percent-encoded
globe, so a visit shows a deliberate mark from the first paint and the site's
own icon replaces it as soon as it is known.

Tests: `node tests/test_tab_identity.cjs` (137 checks — the tab block, the icon
chain, the visit-flag classifier and the capture-page observer all run for
real), and `PYTHONPATH=. python tests/test_site_icon.py` +
`tests/test_site_icon_api.py` for the server-side resolution and the
`/api/site/icon` endpoint (73 checks: only a public host is ever fetched, only
an image is ever served, the answer is cached, and a machine that cannot verify
certificates still gets icons).

---

## The font: Space Grotesk everywhere

`client.html` and both panels are set in **Space Grotesk**, so a mirrored page
is too — including the page pushed by an LPV link. The captured document gets
two things injected into its `<head>`:

| Injected | What it does |
|---|---|
| `<link rel="stylesheet">` for Space Grotesk | the page can actually render the font; the shell already fetched the same file, so it comes from cache |
| `<style id="__shifix_mirror_font__">` | `html`, `body` and every element in the page get the family with `!important`, so the site's own `font-family` cannot win |

Details:

* **Icons keep their own font.** Font Awesome, Material Icons, Glyphicons, MDI
  and anything with `icon` in its class are excluded, plus `svg` — their glyphs
  are characters in *their* family, so replacing it would turn every icon into a
  letter. (The exclusions are simple `:not()` conditions only; a complex one
  like `:not(svg *)` is silently ignored by some engines.)
* **The override is the last rule in `<head>`**, so a page stylesheet injected
  later cannot win back its own font on a tie.
* **`?keep_fonts=1`** on the client URL (`/client.html?workflow=…&keep_fonts=1`)
  leaves the page with the site's real typography, for checking a capture
  against the original.
* The font is still fetched **after the first paint** in the shell (preload →
  `onload`), so a slow or filtered network never delays the page; the system
  stack (`-apple-system`, Segoe UI, Roboto) covers the gap.

Tests: `node tests/test_mirror_font.cjs`.

---

## Scrolling a mirrored page

A captured page arrives as HTML + CSS with its scripts removed, so anything the
original page moved with **script** cannot move here. The most common shape is
a full-height shell (`height:100%` + `overflow:hidden`) wrapping several screens
of content, with a scroll handler that is no longer there: the page is all
there, but nothing scrolls. It reads as "the client is blocking my scroll".

`client.html` puts the page back under your finger without changing how it
looks:

| What it finds | What it does |
|---|---|
| A page that already scrolls | **Nothing** to its layout. Ordinary captures keep their exact rendering |
| A full-height wrapper that clips more than it shows | makes **that wrapper** scroll (wheel, trackpad, scrollbar, finger) |
| A fixed shell that hugs the top and spans the width, holding most of the page's text | lets it scroll — but only after everything else failed, so a page that can move is never restyled |
| A fixed modal / cookie bar / sticky header (`role=dialog`, `aria-modal`, centred, narrow, or carrying little of the page) | left alone — making one of those scroll would be a new bug |
| Still nothing, yet the body is taller than the frame | lets `html`/`body` take their natural height and scroll the document |
| **Nothing moved despite all of the above** | nothing is applied by hand — the page is left to the browser, and only its CSS is corrected: see *Scrolling is the browser's* below |

Details that matter:

* **It retries once you stop trying.** A wheel or a finger drag on a page that
  still refuses to move schedules another check 350 ms after the gesture ends
  (and only if the page still cannot move) — never during the scroll, where a
  scan would compete with the browser for frames. That also covers a page whose
  layout only becomes tall after its images load.
* **The page reset stylesheet is the last rule in `<head>`.** It used to be
  injected first, so an author `html,body { overflow:hidden !important }` with
  the same specificity silently outranked it.
* **Both surfaces are covered:** the live mirror (`renderPageSnapshot`,
  `body_replace`) and an LPV page pushed from the panel.
* iOS momentum scrolling (`-webkit-overflow-scrolling: touch`) is set on the
  frames and on whatever it rescues — without it a finger drag does not move a
  nested scroller on an iPhone.
* A rescued element is tagged `data-scroll-rescued="1"`, so it is visible in
  devtools when a page misbehaves.

### Scrolling is the browser's — nothing is intercepted

The rescue above is **CSS only**: it lets the browser scroll a box that the
capture had left clipped. It never moves anything itself.

That is a rule, not an implementation detail. An earlier version applied the
gesture by hand when native scrolling "did not move" (read `scrollTop`, add the
wheel delta, write it back). It made a page scroll — and it felt wrong: no
acceleration, no momentum, no rubber-banding, stutter on every tick, and a fight
whenever the browser and the client disagreed about which box should move. A
mirrored page has to scroll exactly like the real one, so that layer is gone:

* no `wheel`, `touch` or key handler cancels the browser's default — every one
  of them is `passive`;
* nothing is scrolled by hand; the only `scrollTop`/`scrollTo` writes left are
  **scroll-position restores** after a patch or a body swap, and they are
  skipped while the visitor is scrolling;
* a rescued box chains its scroll to the page around it (no
  `overscroll-behavior` clamp), so reaching the end of an inner panel is what
  it is on the real site;
* the retry for a page that still refuses to move runs **350 ms after the
  gesture ends**, and only if the page still cannot move. A rescue pass is up to
  600 `getComputedStyle` reads — running that inside a scroll is how a page ends
  up dropping frames;
* `-webkit-overflow-scrolling: touch` stays on the frames and on whatever is
  rescued, because without it a finger drag has no momentum on an iPhone.

With a mirrored page on screen, this answers "is scrolling wired up here?":

```js
__MIRROR_SCROLL_STATE__()
// { frame: true, intercepted: false, rescued: 2, scrollable: true,
//   height: 812, contentHeight: 4310 }
```

`scrollable: false` with `contentHeight` barely above `height` means the page
itself is short — nothing to scroll, and nothing wrong.

### Chrome on a phone: the three things that made it "not scroll"

The same mirror scrolled on some phones and not in Chrome on Android. None of
these were about our gesture handling — they are all things a page tells the
browser, and the browser is entitled to believe.

* **`touch-action`.** A captured page (or a third-party consent/chat widget on
  it) setting `touch-action: none` on `html`/`body` is honoured by Chrome, and
  then the page cannot be dragged at all; other engines ignore it. Every injected
  reset (`__shifix_stream_reset__`, the LPV `viewport-guard`, and the inline
  rescue) now sets `touch-action: auto !important` on `html`/`body`, and
  `touch-action: pan-y` on anything it rescues or unlocks. `overscroll-behavior`
  is likewise un-clamped on `html`/`body` (a clamp there stops the gesture from
  chaining out of an inner scroller), while a **rescued box keeps** chaining like
  a normal element.
* **The viewport height.** `height: 100%` on a fixed, `overflow: hidden` shell
  resolves against the **largest** viewport in Chrome on Android (URL bar
  hidden), so the bottom of the mirror sits behind the browser UI and nothing the
  page does can reach it. The client shell, `#lpv-root` and `#lpv-page` now use
  `100dvh` under `@supports (height: 100dvh)`, with `100%` as the fallback, so
  the frame is never taller than what the visitor can see.
* **The app shell.** A page can be laid out as one clipping, full-viewport frame
  around the whole site, with nothing that matches the earlier rescue rules. A
  final rung walks to the tallest content in the document and up its ancestor
  chain, and lets every clipping box between them scroll
  (`overflow-y:auto !important`, `touch-action:pan-y`, tagged
  `data-scroll-unlocked="1"`). Dialogs are skipped, boxes that hide less than a
  line are left alone, at most a dozen ancestors are walked, and it is still CSS
  only — the browser keeps doing the scrolling.

`__MIRROR_SCROLL_STATE__()` reports the diagnosis as well as the state, so a
phone can be checked without guesswork:

```js
__MIRROR_SCROLL_STATE__()
// { frame: true, intercepted: false, rescued: 1, scrollable: true,
//   height: 812, contentHeight: 4310,
//   touchAction: "auto", overscroll: "auto", hitElement: "div#app" }
```

`hitElement` is what is actually under the middle of the screen: if it is an
invisible overlay, that is the answer — the CSS is fine and the page put
something in the way.

### The chat panel scrolls, and stays where the reader put it

The mirrored page's chat had its own version of the same bug: the message list
was a `flex: 1` child of a column-flex panel **without** `min-height: 0`, so it
grew to its content height instead of shrinking and scrolling, and the panel's
`overflow: hidden` clipped it. Scrolling the list therefore did nothing. The
list is now `flex:1; min-height:0; overflow-y:auto` with `overscroll-behavior:
contain` (its own scroll does not drag the page behind it), `touch-action: pan-y`
and iOS momentum; `scroll-behavior` is `auto`, because a smooth-scroll animation
animating against a finger is exactly what "unscrollable" feels like.

The second half of the same complaint: every incoming message called
`scrollTop = scrollHeight`, so reading back through history yanked the reader to
the bottom. `atBottom()` measures where the reader is **before** the list grows,
and a message only auto-scrolls when they were already at the bottom (their own
message always lands in view; opening the panel always jumps to the newest).

Tests: `node tests/test_mirror_scroll.cjs` (121 checks).

---

## Logging: quiet by default, and never on a hot path

An app is not killed by logging, it is killed by logging on a **per-event**
path. A warning attached to every pointer move, every keystroke or every frame
costs string formatting, a handler lock, a write and a flush — hundreds of times
a second — and buries the one line that actually explains a problem.

The rules now:

* **The shipped client page is silent.** `client.html` writes nothing to the
  console unless it is asked to: append `?debug=1`, or set
  `localStorage.shf_debug = '1'` and reload. `dlog()` is the only informational
  channel, and the build line / boot timings go through the same switch. A
  warning that can fire repeatedly (a failed patch batch, an unknown op, a
  missing region) goes through `warnRare()`: once per 15 s per key.
* **The server logs at WARNING** (`LOG_LEVEL=INFO`/`DEBUG` for a trace). Nothing
  informational is printed while things work.
* **Per-event failures are throttled, not repeated.** `log_utils.throttled()`
  writes the first occurrence, then at most one per 30 s **with a count of what
  was swallowed**, so a rate is still visible:

  ```
  [INPUT][drop] session=ab12 inputs_enabled=False (+412 more in the last 30s)
  ```

  It is applied to the paths that used to flood: an input event arriving while
  inputs are off (one line per mouse move), rejected keys, clicks that were not
  dispatched, SeleniumBase CDP/W3C input-dispatch failures (one per pointer
  move), and per-frame screenshot warnings in the WebRTC streamer.
* **Logging can never break a session.** `throttled()` swallows a handler
  exception and returns `False`.

Tests: `PYTHONPATH=. python tests/test_logging_quiet.py` (41 checks — the
throttle's window and count, the client's debug gate, and every flooded call
site).

---

## What the capture does — and what it used to lose

A capture is a photograph of a live page: the Playwright page runs the site's
scripts, then one in-page pass serializes the **DOM as it is now** (not the HTML
that was downloaded) into a document with no scripts. Everything visual has to
be carried across in that pass, and the things that quietly did not get carried
are exactly what "some content is missing" looks like.

### Accuracy: what is now carried across

| The page does this | Why it used to vanish | Now |
|---|---|---|
| draws an icon / bullet / quote / badge with `content:` on `::before` / `::after` | generated content is not part of the DOM, so serializing elements drops it | the stylesheets are asked which selectors *could* have generated content, and those elements carry it as an in-flow `<span data-shf-pseudo="before|after">` — no `getComputedStyle` per node, so the cost stays proportional to the generated-content rules, not to the page |
| …but only on `:hover` / `:focus` | materializing a hover style would show a permanent ghost of it | skipped on purpose (pseudo-class selectors are not resolved) |
| …positioned over the element (`position:absolute`) | re-emitting it in flow would move the glyph | skipped on purpose |
| switches a control off in script (`el.disabled = true`) | `disabled`/`readOnly` are properties and never reflect back to the attribute, so the mirror came out clickable | shipped as attributes, in full captures and in delta patches |
| shows a responsive image via `<picture>` / `srcset` | the `src` we resolved was outranked by the `<source>` list at render time | the source list is dropped and `src` is the candidate the page's own viewport actually displayed (`currentSrc`) |
| lazy-loads images (`loading="lazy"`) | native lazy loading defers them again in the mirror's frame | forced eager — the capture is a photograph of a loaded page |

### Speed: where the wall clock went

The rewrite phase (turn every external ref into an inline copy or an
`/assets/...` path) is the slow half of a capture after the DOM pass. It used to
process **stylesheets one at a time**, and each one opened its own HTTP client
and awaited its inner `url()`s: a page with a dozen stylesheets paid a dozen
sequential handshakes, all inside the same time budget, and fonts/background
images are the last thing a browser announces.

Now there is one client and one bounded pool for the whole phase, so every
stylesheet's inner refs (sprites, fonts, nested `@import`) are fetched together:

* `DOM_CAPTURE_ASSET_CONCURRENCY` (default **10**) caps concurrent downloads.
* The global budget (`DOM_CAPTURE_ASSET_TOTAL_TIMEOUT_S`, default 10 s) still
  holds: whatever finished is kept, the rest keeps its authored URL — the same
  degradation as any other failed asset, never an error.
* `dom_capture._LAST_REWRITE_STATS` records `refs`, `memo_hits`, `fetched`,
  `css_sheets`, `embedded`, `cached` and `ms` for the pass, so "why was this
  page slow" has an answer in the capture log instead of a guess.

Tests: `node tests/capture_serializer_harness.cjs` (21 checks — the serializer
runs in a `vm` against a hand-built DOM) and
`python tests/test_capture_pipeline.py` (20 checks — real rewrite against a
stubbed HTTP layer that records peak concurrency).

### Still known to be missing (next, not done)

Honest list, in the order the visual damage shows up:

| Gap | Why it is hard | Shape of the fix |
|---|---|---|
| A **cross-origin iframe** (map, payment widget, embedded video) is dropped entirely | the mirror is served from another origin, so `X-Frame-Options` / `frame-ancestors` refuse to embed it | screenshot the frame's box during capture (Playwright element screenshot, clipped to the box) and ship it as an `<img>` placeholder — an image cannot be refused, and a visible, correct-looking box beats a hole |
| **Icon fonts** that were still loading at capture time | the glyph is drawn by a font file that arrives after the snapshot | wait for `document.fonts.ready` before serializing (one await; bounded), or inline the font in CSS — the rewrite already embeds fonts under `CSS_EMBED_MAX_BYTES` |
| A **closed shadow root** renders as a comment | closed roots are unreadable by design | CDP `DOM.getDocument(pierce:true)` / `DOMSnapshot.captureSnapshot` reads them out-of-page; needs a CDP backend, so it is a `capture_page` path change, not a serializer tweak |
| **Canvas without a 2D context** (WebGL) | `toDataURL()` returns a blank canvas | `page.screenshot(clip=element box)` for WebGL canvases only |
| A **scroll position other than the top** | the mirror opens at 0,0 | capture `scrollX/scrollY` per scroller and restore after the reset stylesheet lands |

---

## Troubleshooting "it connects but nothing works"

Run these first, in this order:

```bash
python main.py --check            # bind address, .env, packages, port, Chrome
python main.py --fix-env          # repair placeholder/blank .env values
./deploy/doctor.sh                # on the server: services, restarts, port, logs
```

| Symptom | Likely cause | Fix |
|---|---|---|
| **Login shows "Connection error — <host>"** | the panel is talking to the wrong backend (remembered address from another machine, old LAN IP, `https://` saved for a plain-http server) | **the login screen shows `Server: <host>` under the Sign In button — click it and set the right address (empty = the server that served the page)**; the panel also falls back to that server by itself after two failed attempts |
| Workflows never move, Telegram stays silent, no clients appear | usually the same cause as above: the panel is not connected, so no client is registered and nothing is pushed | fix `Server:`, then re-push |
| Telegram notifications stop | server cannot reach `api.telegram.org`, or the saved Admin config has no token/chat id | **Logs tab → Server log** shows the exact error (`Request error: TLS/SSL...`, `401 Unauthorized`, `chat not found`); `config.py` values are used until an Admin save overrides them |
| Panel opens, LPV push fails with *Failed to fetch* | `SERVER_DOMAIN` set to a domain that does not resolve here | `python main.py --fix-env`, restart |
| Login says *Invalid credentials* / *Wrong password for 'admin'* | the password is not the one the server has — `deploy/setup.sh` writes a **random** one and prints it only once (`/root/fixiis-credentials.txt`); a blank or malformed hash also rejects every password | `python main.py --set-password` on the server, then restart. The panel now says *which* it is, and after two rejections it offers this command. `python main.py --check` prints the user and where the password lives |
| Login says *Too many attempts* | 5 sign-ins a minute per address | wait a minute; the server was reachable all along |
| Works on the server, refused from phone/laptop | bound to `127.0.0.1`, or firewall | `python main.py --network`, then open the firewall port |
| Port/bind address is not what `config.py` says | a `.env` overrides it | `python main.py --check` shows the source; `--ignore-env` to bypass |
| Everything disconnects over and over | the app is restarting repeatedly | `./deploy/doctor.sh`; set `auto_update = False` in `config.py`, restart once |
| `/health` answers but the UI is stale | a deploy landed while a session was live | reconnect; restarts drop live sessions by design |

### If you cannot log in

`Invalid credentials` is never a connection problem — a connection problem says
*Connection error* or *Cannot reach …*. It means the server did not accept the
password, and there are only three ways that happens. The panel tells them
apart now:

| Panel says | Meaning | What to run on the server |
|---|---|---|
| *Wrong password for 'admin'* | the account exists, the password is not the stored one (the usual case: `setup.sh` generated a random one) | `python main.py --set-password` |
| *Unknown user* | the username is not `admin` (or `ADMIN_USERNAME` was changed) | `python main.py --check` shows the real user |
| *The server's admin password is misconfigured* | the stored hash is unusable, so **no** password can work | `python main.py --set-password` |

`python main.py --set-password` asks for a new password (Enter = generate one),
stores a bcrypt hash in `.env`, and prints it once. Restart afterwards:
`python main.py`.

**Want the plain `admin` / `admin` back?** Then stop overriding it:

```bash
python main.py --reset-password     # removes ADMIN_PASSWORD_HASH from .env
python main.py                      # restart
```

That is the built-in login, so it is deliberately weak — anyone who can reach
the panel can use it. `--check` and the startup banner both say when it is in
use. Set a real password again with `--set-password` once you are back in
(the default is too short for `--set-password` to store, by design).

### Where the reason is written down

* **Logs tab → Server log** (in the panel): the server's last 300 warnings and
  errors plus the effective Telegram state, LPV counts and build stamp. Same
  data over HTTP: `GET /api/diagnostics/server` (admin token).
* **The process log** now defaults to `WARNING` (it used to default to
  `ERROR`, which hid every "why did it stop" line). `LOG_LEVEL=INFO` or
  `DEBUG` in the environment for more.
* `GET /health` never fails because an optional subsystem is broken; it
  reports `build.revision`, uptime and auto-update state.

### No server address is baked into the code

The only place an address may live is **your** settings: `config.py` (or
`PUBLIC_IP` / `SERVER_DOMAIN` in the environment, or the `Server:` field on
the login screen, which is stored in the browser). Both config values ship
blank, and blank means "no address": page URLs and links are server-relative
and each client resolves them against the backend it actually reached. That is
what makes one install work on localhost, on a LAN address, behind nginx and
behind a tunnel without editing anything.

`tests/test_no_hardcoded_addresses.cjs` enforces it: the build fails if a
private or public IP literal appears in `api.py`, `main.py`, `bootstrap.py`,
`config.py`, the three panels or `deploy/`. Only loopback/wildcard, the RFC
5737 documentation ranges (`192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24`)
and the connectivity probes (`8.8.8.8`, `1.1.1.1`) are allowed.

`deploy/make-standalone.sh <url>` is the deliberate exception: it produces
copies with **your** address baked in, for double-click use off a disk or a
static host. That output is git-ignored, so the address never enters a commit.
`./deploy/make-standalone.sh --none` produces copies with no address at all
(they ask once, `?server=…`, or use the origin that served them) — that is what
the committed `standalone/client.html` is.

### The standalone folder (hosting the panels yourself)

```bash
./deploy/make-standalone.sh --none            # safe to publish anywhere
./deploy/make-standalone.sh http://YOUR-SERVER:8446   # bake your address in
./deploy/make-standalone.sh https://panel.example.com ./my-panels
```

It writes into `./standalone/`, one folder you can upload as-is. The folder is
entirely generated and git-ignored: a baked copy carries your address, so it
must never end up in a commit.

| File | What it is |
|---|---|
| `index.html` | a tiny menu that links to the three pages below (no scripts, no fonts) |
| `Admin.html` | the full panel |
| `mini-admin.html` | the phone panel |
| `client.html` | the page visitors open, if you serve it yourself |
| `README.txt` | what each file is, how to point it at the server, how to update |
| `BUILD.txt` | generated-at, commit, sizes and sha1 of each copy |

**Every copy is verified before it is written.** The generator re-checks that
the page kept its body byte-for-byte, that exactly one address line was added,
and that a feature marker still exists (`#stChatMode`, `#lpv-archive-list`,
`#loadingScreen`); a copy that fails any of them aborts the build instead of
landing in the folder you upload.

Where a copy finds the backend, in order: `?server=http://host:8446` on the
address you opened → the address baked into the copy by this script → the
address this browser used last time → the origin that served the file. So a
copy generated with your address works from any host (or from `file://`), a
`--none` copy needs a `?server=` hint or a matching origin, and panels served
by the server itself need nothing configured at all.

**Updating** is the same command again (never edit the copies), then re-upload
and compare `BUILD.txt`. `python3 tests/test_standalone.py` fails if the
committed `standalone/client.html` has drifted from `client.html`, if any copy
gained a real server address, or if the two-line change became a re-write.

### The remembered backend

`Admin.html`, `mini-admin.html` and `client.html` all remember the backend in
`localStorage` under `lpv_server_endpoint`. An address typed without a scheme
(`192.0.2.10:8446`) now keeps the page's own scheme instead of being forced
to `https://`, and if it is stale the panel and the viewer retry against the
origin that served them. Clear it from the login screen (`Server:` → empty) or
in the browser devtools if you ever need to start over.

---

## Test

There is no formal test suite in this repo yet. The minimum things to
verify before considering a change "done":

### 1. Static checks

```bash
python -m py_compile api.py main.py bootstrap.py
```

Dependency-free front-end regression checks (Node 18+):

```bash
node tests/test_link_base.cjs        # link-base rewriting in both panels
node tests/test_client_lpv_urls.cjs  # standalone client LPV page resolution
node tests/test_page_guard.cjs       # per-page click guard (interaction allowlist)
node tests/test_mobile_cards.cjs     # archive cards stay compact on phones
node tests/test_visitor_location.cjs # a visitor's place is a page NAME, never an /api/ path
node tests/test_site_switch.cjs     # the visitor-pages ON/OFF switch (needs jsdom)
node tests/test_backend_address.cjs  # "Connection error": backend address handling
node tests/test_no_hardcoded_addresses.cjs  # no server address baked into code
node tests/test_login_diagnostics.cjs     # a rejected sign-in says why
node tests/test_mini_simple.cjs           # the phone panel stays four jobs
node tests/test_bind_push.cjs             # page fields: saved once, asked at push
node tests/test_chat_switch.cjs           # chat on/off + which device sees the bubble
node tests/test_chat_bubble_ui.cjs        # bubble target pickers (needs jsdom; skipped otherwise)
node tests/test_boot_speed.cjs            # startup: compression, ETag, budgets, no font stall
node tests/test_page_actions.cjs          # archive edit, click actions, retired action step
node tests/test_mirror_scroll.cjs         # a mirrored page can still be scrolled
node tests/test_mirror_font.cjs           # Space Grotesk across a mirrored page
node tests/test_tab_identity.cjs          # the tab is the page: title + icon, live
node tests/test_pc_only_gate.cjs          # the CDP cast screens refuse a phone
node tests/test_domain_tab_ui.cjs         # the Domains tab (uploaded spinner + provider logo)
node tests/test_loading_handover.cjs      # the loading screen gets out of the way (real page, jsdom)
node tests/test_page_actions_ui.cjs       # the action editor, picker + chat modes (needs jsdom)
python3 tests/test_page_actions.py     # replace HTML + action rules + action step
python3 tests/test_click_action_resolution.py  # which rule a click runs (drift, device pairs;
                                      # needs fastapi, skipped without it)
python3 tests/test_chat.py             # the chat switch's decision table
python3 tests/test_standalone.py       # the folder you host: copies + BUILD.txt
python3 tests/test_public_pages.py     # pages OFF means 404 for visitors (needs fastapi)
node tests/test_bind_ui.cjs               # the push dialogs (needs jsdom; skipped otherwise)
python3 tests/test_page_bindings.py    # text / element / regex rewriting engine
python3 tests/test_desktop_variant.py  # device versions (phone + desktop page pair)
python3 tests/test_autoupdate.py     # auto_update: what restarts the app
python3 tests/test_bootstrap.py     # python main.py one-command startup
python3 tests/test_boot_loading_embed.py  # the custom spinner is in the served page
python3 tests/test_domain_spinner.py   # per-domain loading screens + logo (needs fastapi)
python3 tests/test_site_icon.py        # the icon the server has for a site reaches the tab
python3 tests/test_site_icon_api.py    # /api/site/icon: the domain answers for its icon
python3 tests/test_stream_scale.py     # viewport × DPR on every launch path (needs config)
python3 tests/test_logging_quiet.py    # quiet client, throttled per-event warnings
```

### 2. Server boots

```bash
python main.py --no-install &
sleep 3
curl -fsS http://127.0.0.1:8446/health
```

Expect a JSON response with `{"status": "ok", ...}`.

### 3. Admin login works

```bash
curl -fsS -X POST http://127.0.0.1:8446/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"admin"}'
```

Expect `{"success": true, "token": "..."}`.

A `401` is not a connection problem, and the body now says which one it is:

```json
{"success": false, "reason": "bad_password",
 "message": "Wrong password for 'admin'", "hint": "", "attempts": 1}
```

`reason` is `bad_password`, `unknown_user` or `hash_unusable`; from the second
rejection `hint` carries the fix (`python main.py --set-password`). A `429`
means five sign-ins a minute were used up. See
[If you cannot log in](#if-you-cannot-log-in).

### A password must not travel in the clear

`http://<public-ip>:8446/admin` puts the admin password, the JWT and every
captured page on the wire unencrypted. On a LAN that is a choice; over the
internet it is not. Put TLS in front of the app (nginx —
`deploy/nginx.conf` — a Cloudflare tunnel, or `use_https = True` behind a proxy
that terminates TLS) and open the panel over `https://`. The panel follows the
page scheme for its backend address, so it keeps working after the switch.

### 4. End-to-end (manual)

1. Start the server.
2. Open `http://127.0.0.1:8446/client.html` in **one** browser window.
3. Open `http://127.0.0.1:8446/admin`, log in, in **another** window.
4. The admin console should show the client session, allow you to view
   the live stream, and let you push a URL / refresh.

If the live stream doesn't show, check the browser devtools console on
both sides for WebSocket errors against `/ws/manager` or `/ws/...`.

---

## Working on the project

### Code map

- **`api.py`** is the entry point. It's a single-file FastAPI app with
  ~5,000 lines. Key sections, in order:
  - imports + config (top)
  - lifespan / app setup
  - WebSocket manager(s) (admin subscriptions, single-stream enforcement)
  - session, profile, file APIs
  - `verify_recaptcha` (currently a no-op — see Legal note above)
  - `/api/auth/login` (JWT issuance)
  - archive / LPV (live-pushed-viewer) routes
  - `if __name__ == "__main__"` runner at the bottom

- **`client.html`** is the page loaded in the *remote* browser. It
  handles WebSocket telemetry, frame streaming, and the in-iframe
  `resizeRecaptchaLogos` visual fixup for pushed pages that embed
  Google widgets.

- **`Admin.html`** is the operator console. Big single-file SPA — search
  by function name (e.g. `connect`, `handleStream`, `loadProfiles`)
  rather than line number.

- **`mini-admin.html`** (`/mini`) is the one-hand, on-a-phone panel. It has
  exactly four jobs, one per tab, and is deliberately smaller than the full
  panel:

  | Tab | Job |
  |---|---|
  | **Clients** | who is on your pages now; tap a card to send a page or run a flow |
  | **Pages** | pick a saved page → pick a visitor → it appears |
  | **Flows** | pick a flow → pick a visitor → it runs in order |
  | **Telegram** | is the bot working, and what should it tell you |

  Each client card says where the person is in the operator's language: the
  name of the page you sent them ("Page One", "Page Two"), or the site's
  hostname for a remote browser. The push URL behind it
  (`/api/lpv/archive/<id>/html`) is never printed — it reads like the visitor
  is "on the API" and hides which page they are actually sitting on.

  "Past visitors" sits one tap inside Clients (it is history, not a fifth
  job). The chat *console* is not part of this panel — it lives in the full
  console — but the chat on/off switch and its device choice sit in Pages →
  Advanced, so silencing the bubble never needs a desktop. Anything advanced
  stays folded inside a `<details>` so the first screen of each tab is one
  decision. `tests/test_mini_simple.cjs` enforces that shape.

### Conventions

- **Single file per surface** — please keep `api.py` and the two HTML
  files self-contained. Don't introduce a build step unless you also
  add a CI path that runs the resulting bundle.
- **Match the existing style** — async/await, `logger = logging.getLogger(__name__)`,
  type hints where the surrounding code uses them, FastAPI `Depends`
  for auth.
- **No secrets in source.** Use env vars; never commit a real
  `JWT_SECRET_KEY` or `ADMIN_PASSWORD_HASH`.
- **No new external services without documenting them** in this README.

### Before you open a PR

1. `python -m py_compile api.py`
2. Boot the server and run the manual smoke test (login + live stream).
3. If you touched the WebSocket protocol, confirm both `client.html` and
  `Admin.html` still connect (`/ws/manager` or whichever route you
  changed).
4. If you changed auth, regenerate `ADMIN_PASSWORD_HASH` and update
  this README.

### Reporting security issues

Please don't file public issues for vulnerabilities. Contact the
maintainers privately (see your fork's `SECURITY.md` if present).

---

## License

See `LICENSE` if present in your checkout. If there is no license file,
**all rights are reserved by the original author** — treat the code as
"source-available, no granted rights" until you add a license. Adding a
license without the author's permission is itself a license violation.
