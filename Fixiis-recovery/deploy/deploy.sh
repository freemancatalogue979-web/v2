#!/usr/bin/env bash
# ============================================================================
# Pull the configured branch onto this server and hand off to the watcher.
#
#   sudo ./deploy/deploy.sh                 # pull + let the watcher restart
#   sudo ./deploy/deploy.sh --restart-now   # pull + restart immediately
#   sudo ./deploy/deploy.sh --branch main
#
# The reloader (deploy/autoreload.py, installed by deploy/setup.sh) notices
# the changed files within a couple of seconds and restarts fixiis.service,
# so you never stop/start anything by hand. `--restart-now` just takes the
# waiting out of the equation.
#
# Hook it up however you like:
#   - run it by hand after a push,
#   - cron:  */5 * * * * root /opt/fixiis/deploy/deploy.sh --quiet
#   - a GitHub webhook that shells out to it,
#   - or a bare-repo post-receive hook (see the end of this file).
# ============================================================================
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/fixiis}"
BRANCH="${BRANCH:-}"
SUBDIR="Fixiis-recovery"
RESTART_NOW=0
QUIET=0

say()  { [[ $QUIET -eq 1 ]] || printf '\033[1;36m==> %s\033[0m\n' "$*"; }
info() { [[ $QUIET -eq 1 ]] || printf '    %s\n' "$*"; }
warn() { printf '\033[1;33m    warning: %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[1;31merror: %s\033[0m\n' "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --branch)      BRANCH="${2:-}"; shift 2 ;;
        --dir)         INSTALL_DIR="${2:-}"; shift 2 ;;
        --restart-now) RESTART_NOW=1; shift ;;
        --quiet)       QUIET=1; shift ;;
        -h|--help)     sed -n '2,22p' "$0"; exit 0 ;;
        *) die "unknown option: $1 (try --help)" ;;
    esac
done

cd "$INSTALL_DIR" || die "$INSTALL_DIR does not exist"
[[ -d .git ]] || die "$INSTALL_DIR is not a git checkout — copy the files instead (the watcher still restarts)"

if [[ -z "$BRANCH" ]]; then
    BRANCH="$(git rev-parse --abbrev-ref HEAD)"
fi

say "Fetching origin/$BRANCH"
git fetch --prune --quiet origin "$BRANCH"
BEFORE="$(git rev-parse HEAD)"
AFTER="$(git rev-parse "origin/$BRANCH")"

if [[ "$BEFORE" == "$AFTER" ]]; then
    info "already up to date ($(git rev-parse --short HEAD))"
    exit 0
fi

# Files live at the repo root of the subdirectory, so pull the subdir out
# rather than checking out the whole repository into the install directory.
if [[ -d "$SUBDIR" && "$(git rev-parse --show-toplevel)" == "$INSTALL_DIR" ]]; then
    info "checking out $SUBDIR/ into the install directory"
    tmp="$(mktemp -d)"
    git archive "origin/$BRANCH" "$SUBDIR" | tar -x -C "$tmp"
    # Keep server-owned state and config out of the checkout.
    (cd "$tmp/$SUBDIR" && find . -maxdepth 1 -type f \
        ! -name '.env*' ! -name '*.json' -print0 | xargs -0 -I{} cp -f {} "$INSTALL_DIR"/)
else
    git reset --quiet --hard "origin/$BRANCH"
fi

info "$(git rev-parse --short "$BEFORE") -> $(git rev-parse --short "$AFTER")"

# Python deps change rarely; install them before the restart so the new code
# finds what it imports.
if git diff --name-only "$BEFORE" "$AFTER" 2>/dev/null | grep -q 'requirements.txt'; then
    say "requirements.txt changed — installing"
    if [[ -x "$INSTALL_DIR/venv/bin/pip" ]]; then
        "$INSTALL_DIR/venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt" || \
            warn "pip install failed — check the log above"
    else
        warn "no venv found; install dependencies manually"
    fi
fi

if [[ $RESTART_NOW -eq 1 ]]; then
    say "Restarting fixiis.service"
    systemctl restart fixiis || die "restart failed"
    sleep 3
    systemctl is-active --quiet fixiis && info "service is running" || \
        warn "service not active — check: journalctl -u fixiis -n 50"
else
    info "the reloader will restart the service within a few seconds"
    info "watch it with: journalctl -u fixiis-reloader -f"
fi

# ----------------------------------------------------------------------------
# Bare-repo post-receive hook (if you push straight to this box):
#
#   git init --bare /srv/fixiis.git
#   cat > /srv/fixiis.git/hooks/post-receive <<'HOOK'
#   #!/bin/sh
#   GIT_WORK_TREE=/opt/fixiis git checkout -f main
#   HOOK
#   chmod +x /srv/fixiis.git/hooks/post-receive
#
# Checking out is enough — the watcher sees the new files and restarts.
# ----------------------------------------------------------------------------
