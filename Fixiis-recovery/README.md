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
python main.py --host 10.0.0.5  # bind one specific address
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

    admin   http://192.168.0.14:8446/admin
    mini    http://192.168.0.14:8446/mini
    client  http://192.168.0.14:8446/client.html
    health  http://192.168.0.14:8446/health
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
| Login always says *Invalid credentials* | `ADMIN_PASSWORD_HASH=` blank or malformed | `python main.py --set-password` |
| Works on the server, refused from phone/laptop | bound to `127.0.0.1`, or firewall | `python main.py --network`, then open the firewall port |
| Port/bind address is not what `config.py` says | a `.env` overrides it | `python main.py --check` shows the source; `--ignore-env` to bypass |
| Everything disconnects over and over | the app is restarting repeatedly | `./deploy/doctor.sh`; set `auto_update = False` in `config.py`, restart once |
| `/health` answers but the UI is stale | a deploy landed while a session was live | reconnect; restarts drop live sessions by design |

### Where the reason is written down

* **Logs tab → Server log** (in the panel): the server's last 300 warnings and
  errors plus the effective Telegram state, LPV counts and build stamp. Same
  data over HTTP: `GET /api/diagnostics/server` (admin token).
* **The process log** now defaults to `WARNING` (it used to default to
  `ERROR`, which hid every "why did it stop" line). `LOG_LEVEL=INFO` or
  `DEBUG` in the environment for more.
* `GET /health` never fails because an optional subsystem is broken; it
  reports `build.revision`, uptime and auto-update state.

### The remembered backend

`Admin.html`, `mini-admin.html` and `client.html` all remember the backend in
`localStorage` under `lpv_server_endpoint`. An address typed without a scheme
(`192.168.1.5:8446`) now keeps the page's own scheme instead of being forced
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
node tests/test_backend_address.cjs  # "Connection error": backend address handling
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

Expect `{"success": true, "token": "..."}`. A `401` means the password
hash drifted from the default — re-bake it with bcrypt.

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
