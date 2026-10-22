#!/usr/bin/env bash
# ============================================================================
# Fixiis / Neo Browser Stream — one-shot VPS installer
#
# Target: Ubuntu 22.04 or 24.04, fresh box, run as root.
#
#   curl -fsSL https://raw.githubusercontent.com/freemancatalogue979-web/v2/arena/e2dc03bd-v2/Fixiis-recovery/deploy/setup.sh \
#     | sudo bash -s -- --domain panel.example.com --email you@example.com
#
# Or clone first and run locally (handier if you want to read the diff):
#   git clone -b arena/e2dc03bd-v2 https://github.com/freemancatalogue979-web/v2.git /opt/src
#   sudo /opt/src/Fixiis-recovery/deploy/setup.sh --domain panel.example.com --email you@example.com
#
# What it does:
#   1. system packages + Google Chrome
#   2. service user, install dir, venv, Python deps
#   3. Playwright browser binaries
#   4. .env from template
#   5. systemd unit
#   6. nginx reverse proxy + Let's Encrypt
#   7. firewall
#
# Idempotent: safe to re-run. Re-running skips anything already installed.
# ============================================================================
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/fixiis}"
SERVICE_USER="${SERVICE_USER:-fixiis}"
REPO_URL="${REPO_URL:-https://github.com/freemancatalogue979-web/v2.git}"
REPO_BRANCH="${REPO_BRANCH:-arena/e2dc03bd-v2}"
SRC_SUBDIR="Fixiis-recovery"

DOMAIN=""
EMAIL=""
SKIP_TLS=0
SKIP_NGINX=0
SKIP_AUTORELOAD=0

# ---------------------------------------------------------------- parse args
while [[ $# -gt 0 ]]; do
    case "$1" in
        --domain)   DOMAIN="${2:-}"; shift 2 ;;
        --email)    EMAIL="${2:-}";  shift 2 ;;
        --dir)      INSTALL_DIR="${2:-}"; shift 2 ;;
        --no-tls)   SKIP_TLS=1; shift ;;
        --no-nginx) SKIP_NGINX=1; shift ;;
        --no-autoreload) SKIP_AUTORELOAD=1; shift ;;
        -h|--help)  sed -n '2,30p' "$0"; exit 0 ;;
        *) echo "unknown option: $1 (try --help)" >&2; exit 1 ;;
    esac
done

if [[ $EUID -ne 0 ]]; then
    echo "run as root: sudo $0" >&2
    exit 1
fi

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '\033[1;33m    warning: %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[1;31merror: %s\033[0m\n' "$*" >&2; exit 1; }

[[ -r /etc/os-release ]] || die "cannot detect OS"
. /etc/os-release
[[ "${ID:-}" == "ubuntu" ]] || warn "this script targets Ubuntu; detected ${ID:-unknown}"

export DEBIAN_FRONTEND=noninteractive

# ------------------------------------------------------------ 1. system deps
say "Installing system packages"
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
    curl wget gnupg ca-certificates lsb-release software-properties-common \
    git unzip xz-utils \
    python3 python3-venv python3-dev python3-pip build-essential \
    fonts-liberation fonts-noto-color-emoji \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libpango-1.0-0 libcairo2 libasound2 libatspi2.0-0 \
    libx11-xcb1 libxcb-dri3-0 libxss1 libgconf-2-4 \
    xvfb nginx certbot python3-certbot-nginx ufw \
    jq bc

# ------------------------------------------------------------ 2. Google Chrome
if command -v google-chrome >/dev/null 2>&1; then
    info "Google Chrome already installed: $(google-chrome --version)"
else
    say "Installing Google Chrome"
    tmp="$(mktemp -d)"
    wget -q -O "$tmp/chrome.deb" https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
    apt-get install -y -qq "$tmp/chrome.deb" || apt-get -y -qq -f install
    rm -rf "$tmp"
    info "installed: $(google-chrome --version 2>/dev/null || echo 'check manually')"
fi

# ------------------------------------------------------------ 3. service user
say "Creating service user '$SERVICE_USER'"
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --home-dir "$INSTALL_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi

# ------------------------------------------------------------ 4. source code
say "Fetching source into $INSTALL_DIR"
if [[ -d "$INSTALL_DIR/.git" ]]; then
    info "existing checkout found — updating"
    git -C "$INSTALL_DIR" fetch --quiet origin "$REPO_BRANCH"
    git -C "$INSTALL_DIR" checkout --quiet "$REPO_BRANCH"
    git -C "$INSTALL_DIR" reset --quiet --hard "origin/$REPO_BRANCH"
elif [[ -d "/opt/src/$SRC_SUBDIR" ]]; then
    info "copying from /opt/src/$SRC_SUBDIR"
    mkdir -p "$INSTALL_DIR"
    cp -a "/opt/src/$SRC_SUBDIR/." "$INSTALL_DIR/"
else
    git clone --quiet --branch "$REPO_BRANCH" --depth 1 "$REPO_URL" "$INSTALL_DIR.tmp"
    rm -rf "$INSTALL_DIR"
    mv "$INSTALL_DIR.tmp/$SRC_SUBDIR" "$INSTALL_DIR"
    rm -rf "$INSTALL_DIR.tmp"
fi

# The repo may or may not be inside a subdir depending on how it was fetched.
if [[ ! -f "$INSTALL_DIR/main.py" && -f "$INSTALL_DIR/$SRC_SUBDIR/main.py" ]]; then
    mv "$INSTALL_DIR/$SRC_SUBDIR" "$INSTALL_DIR.nested"
    rm -rf "$INSTALL_DIR"
    mv "$INSTALL_DIR.nested" "$INSTALL_DIR"
fi
[[ -f "$INSTALL_DIR/main.py" ]] || die "main.py not found in $INSTALL_DIR"

# ------------------------------------------------------------ 5. venv + deps
say "Setting up Python environment"
if [[ ! -d "$INSTALL_DIR/venv" ]]; then
    python3 -m venv "$INSTALL_DIR/venv"
fi
"$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade pip setuptools wheel
"$INSTALL_DIR/venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"

say "Installing Playwright browser binaries"
"$INSTALL_DIR/venv/bin/playwright" install chromium
"$INSTALL_DIR/venv/bin/playwright" install-deps chromium || warn "playwright install-deps had issues (usually harmless when Chrome is already present)"

# ------------------------------------------------------------ 6. .env
say "Configuring .env"
if [[ ! -f "$INSTALL_DIR/.env" ]]; then
    cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"

    # Generate an admin password hash for a random password and print it once.
    genpass="$("$INSTALL_DIR/venv/bin/python" -c "import secrets;print(secrets.token_urlsafe(16))")"
    genhash="$("$INSTALL_DIR/venv/bin/python" - <<'PY'
import bcrypt, sys
pw = sys.stdin.read().strip()
print(bcrypt.hashpw(pw.encode(), bcrypt.gensalt(12)).decode())
PY
    <<< "$genpass")"

    sed -i "s|^ADMIN_PASSWORD_HASH=.*|ADMIN_PASSWORD_HASH=$genhash|" "$INSTALL_DIR/.env"
    [[ -n "$DOMAIN" ]] && sed -i "s|^SERVER_DOMAIN=.*|SERVER_DOMAIN=$DOMAIN|" "$INSTALL_DIR/.env"

    PUBLIC_IP="$(curl -fsS --max-time 5 https://api.ipify.org || true)"
    [[ -n "$PUBLIC_IP" ]] && sed -i "s|^PUBLIC_IP=.*|PUBLIC_IP=$PUBLIC_IP|" "$INSTALL_DIR/.env"

    {
        echo "--------------------------------------------------------------"
        echo "  PANEL LOGIN"
        echo "  username: admin"
        echo "  password: $genpass"
        echo ""
        echo "  Save this now — it is not shown again."
        echo "--------------------------------------------------------------"
    } | tee /root/fixiis-credentials.txt
else
    info ".env already exists — leaving it alone"
fi

# Heal placeholder values left behind by an older template
# (SERVER_DOMAIN=panel.example.com, USE_HTTPS=true, blank secrets) and make
# sure the admin password is a real random one. Idempotent.
if [[ -f "$INSTALL_DIR/bootstrap.py" ]]; then
    ( cd "$INSTALL_DIR" && "$INSTALL_DIR/venv/bin/python" -c \
        'import bootstrap; [print("    .env:", c) for c in bootstrap.repair_env_file()]; bootstrap.ensure_admin_password()' \
    ) 2>/dev/null || true
fi

chmod 600 "$INSTALL_DIR/.env"
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"

# ------------------------------------------------------------ 7. systemd
say "Installing systemd unit"
if [[ -f "$INSTALL_DIR/deploy/fixiis.service" ]]; then
    sed "s|/opt/fixiis|$INSTALL_DIR|g; s|^User=.*|User=$SERVICE_USER|; s|^Group=.*|Group=$SERVICE_USER|" \
        "$INSTALL_DIR/deploy/fixiis.service" > /etc/systemd/system/fixiis.service
else
    warn "deploy/fixiis.service missing — skipping unit install"
fi
# Auto-restart watcher: uploads (scp/SFTP/git/editor) to the backend files
# bounce fixiis.service automatically, so deploys never need a manual
# stop/start. Opt out with --no-autoreload.
if [[ $SKIP_AUTORELOAD -eq 0 && -f "$INSTALL_DIR/deploy/fixiis-reloader.service" ]]; then
    say "Installing auto-restart watcher"
    # Follow PORT from .env for the post-restart health check.
    APP_PORT="$(sed -n 's/^PORT=//p' "$INSTALL_DIR/.env" 2>/dev/null | tail -1)"
    APP_PORT="${APP_PORT:-8446}"
    sed "s|/opt/fixiis|$INSTALL_DIR|g; s|127.0.0.1:8446|127.0.0.1:${APP_PORT}|" \
        "$INSTALL_DIR/deploy/fixiis-reloader.service" > /etc/systemd/system/fixiis-reloader.service
    # Tell the app (and therefore /health) that the watcher is in charge.
    if ! grep -q '^FIXIIS_AUTORELOAD=' "$INSTALL_DIR/.env" 2>/dev/null; then
        printf '\n# Set by deploy/setup.sh: deploy/autoreload.py restarts the app\nFIXIIS_AUTORELOAD=1\n' \
            >> "$INSTALL_DIR/.env"
    fi
    info "watcher: journalctl -u fixiis-reloader -f"
elif [[ $SKIP_AUTORELOAD -eq 1 ]]; then
    info "auto-restart disabled by --no-autoreload"
fi

systemctl daemon-reload
systemctl enable fixiis.service

# ------------------------------------------------------------ 8. nginx + TLS
if [[ $SKIP_NGINX -eq 0 ]]; then
    say "Configuring nginx"
    if [[ -f "$INSTALL_DIR/deploy/nginx.conf" && -n "$DOMAIN" ]]; then
        sed "s|panel\.example\.com|$DOMAIN|g" "$INSTALL_DIR/deploy/nginx.conf" \
            > /etc/nginx/sites-available/fixiis
        ln -sf /etc/nginx/sites-available/fixiis /etc/nginx/sites-enabled/fixiis
        rm -f /etc/nginx/sites-enabled/default
        nginx -t && systemctl reload nginx
    else
        warn "skipping nginx vhost (need deploy/nginx.conf and --domain)"
    fi

    if [[ $SKIP_TLS -eq 0 && -n "$DOMAIN" && -n "$EMAIL" ]]; then
        say "Requesting Let's Encrypt certificate for $DOMAIN"
        certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos -m "$EMAIL" --redirect || \
            warn "certbot failed — check DNS points at this box, then re-run certbot manually"
        systemctl enable certbot.timer
    elif [[ -n "$DOMAIN" ]]; then
        warn "no --email given, skipped TLS. Run: certbot --nginx -d $DOMAIN"
    fi
fi

# ------------------------------------------------------------ 9. firewall
say "Configuring firewall"
ufw --force reset >/dev/null
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow OpenSSH >/dev/null
ufw allow 'Nginx Full' >/dev/null
# Only needed if you want to reach :8446 directly (debugging, or no nginx).
# ufw allow 8446/tcp
ufw --force enable >/dev/null
info "firewall: SSH + HTTP/HTTPS open, everything else closed"

# ------------------------------------------------------------ 10. start
say "Starting Fixiis"
systemctl restart fixiis.service
sleep 4

if systemctl is-active --quiet fixiis.service; then
    info "service is running"
else
    warn "service did not come up cleanly — recent log:"
    journalctl -u fixiis.service -n 30 --no-pager || true
fi

if [[ $SKIP_AUTORELOAD -eq 0 && -f /etc/systemd/system/fixiis-reloader.service ]]; then
    systemctl enable --now fixiis-reloader.service
    sleep 1
    if systemctl is-active --quiet fixiis-reloader.service; then
        info "auto-restart watcher is running — uploads now restart the app by themselves"
    else
        warn "watcher did not start — check: journalctl -u fixiis-reloader -n 30"
    fi
fi

say "Done"
if [[ -n "$DOMAIN" ]]; then
    info "Panel:      https://$DOMAIN/admin   (full)"
    info "Mini panel: https://$DOMAIN/mini    (compact)"
else
    info "Panel:      http://$(hostname -I | awk '{print $1}'):8446/admin"
    info "Mini panel: http://$(hostname -I | awk '{print $1}'):8446/mini"
fi
info ""
info "Useful commands:"
info "  systemctl status fixiis     journalctl -u fixiis -f"
info "  journalctl -u fixiis-reloader -f    (auto-restart watcher log)"
info "  deploy a change: copy files over, or run deploy/deploy.sh"
info "  health check:    sudo -u fixiis python3 main.py --check"
info "  manual start:    sudo -u fixiis python3 main.py   (sets itself up)"
info "  cat /root/fixiis-credentials.txt"
