#!/usr/bin/env bash
# ============================================================================
# Why is the panel not connecting? Run this and read the top to bottom.
#
#   sudo ./deploy/doctor.sh          # full report
#   ./deploy/doctor.sh --watch       # follow both logs (Ctrl+C to stop)
#
# Read-only: it changes nothing, restarts nothing, prints no secrets.
# ============================================================================
set -uo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/fixiis}"
UNIT="${UNIT:-fixiis}"
RELOADER="${RELOADER:-fixiis-reloader}"
WATCH=0
[[ "${1:-}" == "--watch" ]] && WATCH=1

bold()  { printf '\033[1m%s\033[0m\n' "$*"; }
head2() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
ok()    { printf '  [ok]   %s\n' "$*"; }
bad()   { printf '  [!!]   %s\n' "$*"; }
meh()   { printf '  [??]   %s\n' "$*"; }

command -v systemctl >/dev/null || { bad "systemctl not found - this script is for the systemd deploy"; exit 1; }

if [[ $WATCH -eq 1 ]]; then
    bold "Following $RELOADER and $UNIT (Ctrl+C to stop)"
    exec journalctl -u "$RELOADER" -u "$UNIT" -f -n 20 --no-pager
fi

bold "Fixiis doctor - $(date)"
printf '  install dir: %s\n' "$INSTALL_DIR"
printf '  host: %s\n' "$(hostname)"

# ---------------------------------------------------------------- services
head2 "Services"
for unit in "$UNIT" "$RELOADER"; do
    state="$(systemctl is-active "$unit" 2>/dev/null || true)"
    enabled="$(systemctl is-enabled "$unit" 2>/dev/null || true)"
    if [[ "$state" == "active" ]]; then
        ok "$unit: active ($enabled)"
    else
        bad "$unit: ${state:-missing} ($enabled)"
    fi
done

# ---------------------------------------------------------------- restarts
head2 "Restart activity (is it flapping?)"
show_restarts() {
    local unit="$1" count started active_since
    count="$(systemctl show -p NRestarts --value "$unit" 2>/dev/null || echo '?')"
    started="$(systemctl show -p ExecMainStartTimestamp --value "$unit" 2>/dev/null || echo '?')"
    echo "  $unit: restarts=${count:-0}  started=${started:-?}"
    if [[ "${count:-0}" =~ ^[0-9]+$ ]] && [[ "$count" -ge 5 ]]; then
        bad "$unit has restarted $count times - something is restarting it repeatedly"
    fi
}
show_restarts "$UNIT"
show_restarts "$RELOADER"

recent_restarts="$(journalctl -u "$UNIT" --since '10 min ago' --no-pager 2>/dev/null | grep -c 'Started\|Starting' || true)"
if [[ "${recent_restarts:-0}" =~ ^[0-9]+$ ]] && [[ "$recent_restarts" -ge 4 ]]; then
    bad "$UNIT started $recent_restarts times in the last 10 minutes - that is a flap"
else
    ok "$UNIT started ${recent_restarts:-0} time(s) in the last 10 minutes"
fi

# ---------------------------------------------------------------- config
head2 "Configuration (.env)"
ENV_FILE="$INSTALL_DIR/.env"
if [[ -f "$ENV_FILE" ]]; then
    ok ".env present"
    for key in HOST PORT SERVER_DOMAIN PUBLIC_IP USE_HTTPS; do
        line="$(grep -E "^${key}=" "$ENV_FILE" 2>/dev/null | tail -1 || true)"
        if [[ -z "$line" ]]; then
            meh "$key: not set (built-in default)"
            continue
        fi
        value="${line#*=}"
        case "$key:$value" in
            SERVER_DOMAIN:panel.example.com|SERVER_DOMAIN:*example.com|SERVER_DOMAIN:*example.org)
                bad "$key=$value - template placeholder! share/LPV links point at a domain you do not own (run: python main.py --fix-env)" ;;
            HOST:127.0.0.1|HOST:localhost)
                meh "$key=$value - only this machine can connect (fine behind nginx/tunnel)" ;;
            PORT:|ADMIN_PASSWORD_HASH:)
                bad "$key is empty - remove the line or set a value" ;;
            *)
                ok "$key=$value" ;;
        esac
    done
    if grep -qE '^ADMIN_PASSWORD_HASH=.+' "$ENV_FILE"; then
        ok "admin password hash: set"
    else
        bad "admin password hash: empty - login uses the built-in default (run: python main.py --fix-env)"
    fi
    if grep -qE '^JWT_SECRET_KEY=.+' "$ENV_FILE"; then
        ok "JWT secret: set"
    else
        meh "JWT secret: not set - the built-in placeholder is used"
    fi
else
    bad ".env missing in $INSTALL_DIR - create it with: cd $INSTALL_DIR && python main.py --fix-env"
fi

# ---------------------------------------------------------------- port
head2 "Listening"
port="$(grep -E '^PORT=' "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2 | tr -d '[:space:]')"
port="${port:-8446}"
if command -v ss >/dev/null; then
    listeners="$(ss -ltnp 2>/dev/null | grep -E "[:.]${port}[[:space:]]" || true)"
    if [[ -n "$listeners" ]]; then
        echo "$listeners" | sed 's/^/  /'
        if echo "$listeners" | grep -q '127.0.0.1:'"$port"; then
            meh "bound to 127.0.0.1 only - other devices cannot connect (HOST=0.0.0.0 to change)"
        else
            ok "listening on port $port for other machines"
        fi
    else
        bad "nothing is listening on port $port - the app is not serving"
    fi
else
    meh "ss not available; skipping port check"
fi

if command -v curl >/dev/null; then
    health="$(curl -fsS --max-time 5 "http://127.0.0.1:${port}/health" 2>/dev/null || true)"
    if [[ -n "$health" ]]; then
        ok "GET /health -> $(printf '%s' "$health" | head -c 300)"
    else
        bad "GET http://127.0.0.1:${port}/health failed - the app is up but not answering"
    fi
fi

# ---------------------------------------------------------------- logs
head2 "Last 15 lines of $UNIT"
journalctl -u "$UNIT" -n 15 --no-pager 2>/dev/null | sed 's/^/  /' || meh "no journal for $UNIT"

head2 "Last 15 lines of $RELOADER (the auto-restart watcher)"
journalctl -u "$RELOADER" -n 15 --no-pager 2>/dev/null | sed 's/^/  /' || meh "no journal for $RELOADER"

# ---------------------------------------------------------------- verdict
head2 "What to do next"
printf '  If %s is flapping:  sudo systemctl stop %s\n' "$RELOADER" "$RELOADER"
printf '                       (the app keeps running; restarts become manual again)\n'
printf '  Stop the auto-restart for good:   sudo systemctl disable --now %s\n' "$RELOADER"
printf '  Repair a broken .env:             cd %s && sudo -u fixiis python3 main.py --fix-env\n' "$INSTALL_DIR"
printf '  Full environment report:          cd %s && sudo -u fixiis python3 main.py --check\n' "$INSTALL_DIR"
printf '  Follow the logs:                  %s --watch\n' "$0"
echo
