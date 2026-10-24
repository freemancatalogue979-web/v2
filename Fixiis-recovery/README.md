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
