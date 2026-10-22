#!/usr/bin/env bash
# ============================================================================
# Auto-restart watcher: turn it on, off, or check it.
#
#   sudo ./deploy/autoreload.sh off     # stop restarts-on-change (app keeps running)
#   sudo ./deploy/autoreload.sh on      # uploads restart the app again
#   sudo ./deploy/autoreload.sh status  # what is it doing right now?
#   sudo ./deploy/autoreload.sh uninstall   # remove the unit entirely
#
# "off" is the safe, reversible action: it stops the watcher and prevents it
# from starting on boot, but leaves fixiis.service running and untouched.
# Manual restarts work exactly as before:
#
#   sudo systemctl restart fixiis
# ============================================================================
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/fixiis}"
UNIT="fixiis-reloader"
TARGET="/etc/systemd/system/${UNIT}.service"
ACTION="${1:-status}"

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '\033[1;33m    warning: %s\033[0m\n' "$*" >&2; }

need_root() {
    if [[ $EUID -ne 0 ]]; then
        echo "run as root: sudo $0 $ACTION" >&2
        exit 1
    fi
}

unit_installed() { [[ -f "$TARGET" ]]; }

app_state() { systemctl is-active fixiis 2>/dev/null || true; }
watch_state() { systemctl is-active "$UNIT" 2>/dev/null || true; }

install_unit() {
    local source="$INSTALL_DIR/deploy/fixiis-reloader.service"
    [[ -f "$source" ]] || { echo "missing $source" >&2; exit 1; }
    local port
    port="$(sed -n 's/^PORT=//p' "$INSTALL_DIR/.env" 2>/dev/null | tail -1)"
    port="${port:-8446}"
    sed "s|/opt/fixiis|$INSTALL_DIR|g; s|127.0.0.1:8446|127.0.0.1:${port}|" \
        "$source" > "$TARGET"
    systemctl daemon-reload
}

case "$ACTION" in
    off|disable|stop)
        need_root
        if ! unit_installed; then
            info "watcher is not installed - nothing to turn off"
        else
            say "Turning the auto-restart watcher OFF"
            systemctl disable --now "$UNIT" >/dev/null 2>&1 || true
            info "watcher: $(watch_state)"
            if [[ "$(app_state)" == "active" ]]; then
                info "app: still running (fixiis.service untouched)"
            else
                warn "app is not running; start it with: sudo systemctl start fixiis"
            fi
            info "uploads no longer restart anything - deploy with:"
            info "  sudo systemctl restart fixiis"
        fi
        # Keep /health honest about it.
        if [[ -f "$INSTALL_DIR/.env" ]]; then
            sed -i 's|^FIXIIS_AUTORELOAD=.*|FIXIIS_AUTORELOAD=0|' "$INSTALL_DIR/.env" 2>/dev/null || true
        fi
        ;;

    on|enable|start)
        need_root
        say "Turning the auto-restart watcher ON"
        install_unit
        systemctl enable --now "$UNIT"
        sleep 1
        if [[ "$(watch_state)" == "active" ]]; then
            info "watcher: active - changing .py/.html files restarts the app"
            info "logs: journalctl -u $UNIT -f"
            if [[ -f "$INSTALL_DIR/.env" ]] && ! grep -q '^FIXIIS_AUTORELOAD=' "$INSTALL_DIR/.env"; then
                printf '\nFIXIIS_AUTORELOAD=1\n' >> "$INSTALL_DIR/.env"
            fi
        else
            warn "watcher did not start: journalctl -u $UNIT -n 30"
            exit 1
        fi
        ;;

    status)
        say "Auto-restart watcher status"
        if ! unit_installed; then
            info "installed:  no"
        else
            info "installed:  yes ($TARGET)"
            info "enabled:    $(systemctl is-enabled "$UNIT" 2>/dev/null || echo no)"
            info "running:    $(watch_state)"
            local restarts
            restarts="$(systemctl show -p NRestarts --value "$UNIT" 2>/dev/null || true)"
            [[ -n "$restarts" ]] && info "restarts:   $restarts"
            info ""
            info "last log lines:"
            journalctl -u "$UNIT" -n 8 --no-pager 2>/dev/null | sed 's/^/      /' || true
        fi
        app="$(app_state)"
        info ""
        info "app (fixiis): ${app:-not installed / not running}"
        if [[ -z "$(watch_state)" || "$(watch_state)" != "active" ]]; then
            info "restarts are MANUAL: sudo systemctl restart fixiis"
        fi
        ;;

    uninstall|remove)
        need_root
        say "Removing the auto-restart watcher"
        systemctl disable --now "$UNIT" >/dev/null 2>&1 || true
        rm -f "$TARGET"
        systemctl daemon-reload
        info "unit removed; fixiis.service is unchanged and still $(app_state)"
        ;;

    -h|--help|help)
        sed -n '2,20p' "$0"
        ;;

    *)
        echo "unknown action: $ACTION (use on|off|status|uninstall)" >&2
        exit 1
        ;;
esac
