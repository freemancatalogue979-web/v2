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

On the first start `main.py` creates `.venv/`, installs `requirements.txt`
into it, copies `.env.example` to `.env` and makes the runtime folders
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
| `.env` missing | copies `.env.example` to `.env` |
| packages already there | nothing — start is instant |

Useful flags:

```bash
python main.py --check          # report python, packages, .env, bind, port, Chrome
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

| Variable               | Default                | Purpose                                          |
|------------------------|------------------------|--------------------------------------------------|
| `HOST`                 | `0.0.0.0`              | Bind address. `127.0.0.1` = this machine only     |
| `PORT`                 | `8446`                 | HTTP port                                         |
| `ADMIN_USERNAME`       | `admin`                | Admin login username                              |
| `ADMIN_PASSWORD_HASH`  | bcrypt hash of `admin` | bcrypt `$2b$…` hash; `main.py` generates one on first run |
| `JWT_SECRET_KEY`       | random on first run    | Signs admin session tokens. Rotate if it leaks    |
| `SERVER_DOMAIN`        | _unset_                | Only set it to a domain that really resolves here |
| `PUBLIC_IP`            | _unset_                | Same rule; used in absolute links/reports         |
| `USE_HTTPS`            | `false`                | `true` only when TLS terminates in front of the app |

**Empty means "not configured".** A line like `ADMIN_PASSWORD_HASH=` does not
blank out the default, and a template placeholder (`SERVER_DOMAIN=panel.example.com`)
is ignored — previously those two turned into "login impossible" and
"links point at a domain nobody owns". `python main.py --fix-env` repairs a
`.env` that was written by an older template.

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
| Panel opens, LPV push fails with *Failed to fetch* | `SERVER_DOMAIN` set to a domain that does not resolve here | `python main.py --fix-env`, restart |
| Login always says *Invalid credentials* | `ADMIN_PASSWORD_HASH=` blank or malformed | `python main.py --fix-env` |
| Works on the server, refused from phone/laptop | bound to `127.0.0.1`, or firewall | `--network`, then open the firewall port |
| Everything disconnects over and over | the service is restarting repeatedly | `./deploy/doctor.sh`; `sudo systemctl stop fixiis-reloader` to pause auto-restarts |
| `/health` answers but the UI is stale | a deploy landed while a session was live | reconnect; restarts drop live sessions by design |

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
python3 tests/test_autoreload.py     # deploy watcher: what restarts the app
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
