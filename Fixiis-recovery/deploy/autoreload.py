#!/usr/bin/env python3
"""Restart the backend automatically when its source files change.

Why this exists: the service is a long-running systemd process, so copying a
new ``api.py`` (or ``client.html``, or ``.env``) onto the box does nothing
until someone runs ``systemctl restart fixiis``. This watcher closes that
gap — upload the file by any means (scp, SFTP, git, rsync, a panel editor)
and the app restarts itself a couple of seconds later.

It is deliberately conservative:

* only source/config files are watched (``.py``, ``.html``, ``.env``); the
  runtime directories (``lpv/``, ``logs/``, ``data/``, ``profiles/`` …) are
  ignored, so database writes can never trigger a restart loop;
* changes are debounced, so a 20-file upload restarts once, not 20 times;
* every changed ``.py`` is syntax-checked first — a broken upload logs an
  error and leaves the *working* process running instead of taking the
  service down;
* the restart command is configurable, so it works with systemd, supervisord
  or a bare ``nohup`` process.

Typical use (systemd):

    python deploy/autoreload.py --dir /opt/fixiis --unit fixiis

Manual use:

    python deploy/autoreload.py --dir . --restart-cmd "pkill -f 'python main.py'"

Options:
    --dir PATH            directory to watch (default: repo root)
    --unit NAME           systemd unit to restart (default: fixiis)
    --restart-cmd CMD     full restart command (overrides --unit)
    --interval SECONDS    scan interval (default 2)
    --debounce SECONDS    quiet period before restarting (default 3)
    --min-interval SEC    never restart more often than this (default 10)
    --ext .py,.html       extra/default extensions to watch
    --ignore NAME         extra directory or file name to ignore (repeatable)
    --health-url URL      poll this after a restart and log the result
    --health-timeout SEC  how long to wait for it (default 60)
    --state-file PATH     remember the last snapshot across runs (for --once)
    --max-restarts N      restart budget per window, 0 = unlimited (default 8)
    --budget-window SECS  length of that window (default 600)
    --dry-run             log what would happen, never restart
    --once                scan once, act, exit (for tests/CI/cron)
    --quiet               only log restarts and errors
"""

from __future__ import annotations

import argparse
import hashlib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

# ---------------------------------------------------------------------------
# What counts as a source file
# ---------------------------------------------------------------------------

# Extensions that mean "the running process must reload to see this".
DEFAULT_EXTS: Tuple[str, ...] = (".py", ".html")

# Exact file names worth watching even though they have no extension
# (or have one we don't generally care about).
WATCHED_NAMES: Set[str] = {".env", "requirements.txt", "mobile_devices.json"}

# Never descend into these: they are either runtime state (which the app
# writes to while running — watching them would restart forever), huge
# dependency trees, or things that never affect the running server.
IGNORED_DIRS: Set[str] = {
    ".git", ".hg", ".svn",
    "venv", ".venv", "env", ".env.d",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "node_modules", ".npm", ".cache",
    "lpv", "logs", "log", "cache", "data", "profiles", "mobile-shots",
    "standalone", "dist", "build", "target", "coverage", ".next", ".nuxt",
    "tests", "deploy",          # changing these must not restart the app
    ".idea", ".vscode", ".arena",
}

# Files whose *content* is state, not code, no matter where they live.
IGNORED_SUFFIXES: Set[str] = {
    ".pyc", ".pyo", ".pyd", ".so", ".dylib", ".dll",
    ".log", ".sqlite3", ".sqlite3-wal", ".sqlite3-shm", ".db",
    ".zip", ".tar", ".gz", ".png", ".jpg", ".jpeg", ".webp", ".gif",
    ".ico", ".woff", ".woff2", ".ttf", ".mp4", ".webm", ".pdf",
}

IGNORED_FILES: Set[str] = {
    "lpv.sqlite3", "persistent_links.json", ".DS_Store", "Thumbs.db",
    ".env.example",  # a template, not live configuration
}


def is_watched(path: Path, root: Path, exts: Sequence[str], extra_ignore: Iterable[str] = ()) -> bool:
    """True when a change to ``path`` should trigger a restart."""
    try:
        rel = path.relative_to(root)
    except ValueError:
        return False
    parts = rel.parts
    if not parts:
        return False
    ignored = IGNORED_DIRS | {name for name in extra_ignore}
    for part in parts[:-1]:
        if part in ignored:
            return False
    name = parts[-1]
    if name in ignored or name in IGNORED_FILES:
        return False
    suffix = path.suffix.lower()
    if suffix in IGNORED_SUFFIXES or name.endswith(".sqlite3-wal") or name.endswith(".sqlite3-shm"):
        return False
    if name in WATCHED_NAMES:
        return True
    return suffix in {e.lower() for e in exts}


def snapshot(root: Path, exts: Sequence[str], extra_ignore: Iterable[str] = ()) -> Dict[str, Tuple[int, int]]:
    """Map relative path -> (mtime_ns, size) for every watched file."""
    out: Dict[str, Tuple[int, int]] = {}
    ignored = IGNORED_DIRS | {name for name in extra_ignore}
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except (PermissionError, FileNotFoundError):
            continue
        for entry in entries:
            path = Path(entry.path)
            try:
                if entry.is_dir(follow_symlinks=False):
                    if entry.name in ignored:
                        continue
                    stack.append(path)
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                if not is_watched(path, root, exts, extra_ignore):
                    continue
                stat = entry.stat(follow_symlinks=False)
            except (OSError, PermissionError):
                continue
            rel = str(path.relative_to(root))
            out[rel] = (stat.st_mtime_ns, stat.st_size)
    return out


def load_state(path: Path) -> Dict[str, Tuple[int, int]]:
    """Read a snapshot persisted by a previous --once run. A missing or
    unreadable file simply means "no baseline yet"."""
    try:
        import json

        raw = json.loads(path.read_text(encoding="utf-8"))
        out: Dict[str, Tuple[int, int]] = {}
        for rel, value in (raw or {}).items():
            if isinstance(value, (list, tuple)) and len(value) == 2:
                out[str(rel)] = (int(value[0]), int(value[1]))
        return out
    except Exception:
        return {}


def save_state(path: Path, snap: Dict[str, Tuple[int, int]]) -> None:
    try:
        import json

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({k: [v[0], v[1]] for k, v in snap.items()}, sort_keys=True),
            encoding="utf-8",
        )
    except Exception as exc:
        log_error(f"could not write state file {path}: {exc}")


def diff_snapshots(
    old: Dict[str, Tuple[int, int]], new: Dict[str, Tuple[int, int]]
) -> Tuple[List[str], List[str], List[str]]:
    """(changed, added, removed) relative paths."""
    changed = sorted(k for k in new.keys() & old.keys() if new[k] != old[k])
    added = sorted(new.keys() - old.keys())
    removed = sorted(old.keys() - new.keys())
    return changed, added, removed


def syntax_errors(root: Path, rel_paths: Iterable[str]) -> List[Tuple[str, str]]:
    """Compile-check the Python files among ``rel_paths`` without importing
    them. Returns [(path, message)] — empty when everything parses."""
    problems: List[Tuple[str, str]] = []
    for rel in rel_paths:
        if not str(rel).lower().endswith(".py"):
            continue
        path = root / rel
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
            compile(source, str(path), "exec")
        except SyntaxError as exc:
            problems.append((rel, f"{exc.__class__.__name__}: {exc.msg} (line {exc.lineno})"))
        except FileNotFoundError:
            # Deleted between snapshot and check — treat as fine.
            continue
        except Exception as exc:  # unreadable, bad encoding, …
            problems.append((rel, f"{exc.__class__.__name__}: {exc}"))
    return problems


def should_restart(
    now: float,
    pending_since: Optional[float],
    last_restart: float,
    debounce: float,
    min_interval: float,
) -> bool:
    """True once the change set has been quiet for ``debounce`` seconds and
    we are not restarting more often than ``min_interval``."""
    if pending_since is None:
        return False
    if now - pending_since < debounce:
        return False
    if last_restart and now - last_restart < min_interval:
        return False
    return True


def run_restart(command: str, dry_run: bool = False) -> Tuple[bool, str]:
    if dry_run:
        return True, "(dry-run) would run: " + command
    try:
        proc = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=120
        )
    except subprocess.TimeoutExpired:
        return False, f"timed out after 120s: {command}"
    except Exception as exc:  # command not found, permission denied, …
        return False, f"{exc.__class__.__name__}: {exc}"
    output = (proc.stdout or "").strip()
    if proc.returncode != 0:
        err = (proc.stderr or "").strip() or output
        return False, f"exit {proc.returncode}: {err[:500]}"
    return True, output


def wait_for_health(url: str, timeout: float) -> Tuple[bool, str]:
    """Poll ``url`` until it answers 2xx (urllib only, no deps)."""
    import urllib.error
    import urllib.request

    deadline = time.time() + timeout
    last = "no attempt"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                if 200 <= getattr(resp, "status", 200) < 300:
                    return True, f"healthy ({getattr(resp, 'status', 200)})"
                last = f"HTTP {getattr(resp, 'status', '?')}"
        except urllib.error.HTTPError as exc:
            last = f"HTTP {exc.code}"
        except Exception as exc:
            last = f"{exc.__class__.__name__}: {exc}"
        time.sleep(2)
    return False, f"not healthy after {timeout:g}s ({last})"


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def log(message: str, quiet: bool = False) -> None:
    if quiet:
        return
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[autoreload {stamp}] {message}", flush=True)


def log_error(message: str) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[autoreload {stamp}] ERROR {message}", flush=True, file=sys.stderr)


def _fingerprint(changed: Sequence[str], added: Sequence[str], removed: Sequence[str]) -> str:
    blob = "|".join(list(changed) + ["+"] + list(added) + ["-"] + list(removed))
    return hashlib.sha1(blob.encode("utf-8", "replace")).hexdigest()[:12]


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    default_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(
        description="Restart the Fixiis backend when its source files change.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--dir", default=str(default_root), help="directory to watch")
    parser.add_argument("--unit", default="fixiis", help="systemd unit to restart")
    parser.add_argument("--restart-cmd", default="", help="full restart command")
    parser.add_argument("--interval", type=float, default=2.0, help="scan interval (s)")
    parser.add_argument("--debounce", type=float, default=3.0, help="quiet period (s)")
    parser.add_argument("--min-interval", type=float, default=10.0, help="minimum gap between restarts (s)")
    parser.add_argument("--settle", type=float, default=3.0, help="pause after a restart (s)")
    parser.add_argument("--ext", default="", help="extra extensions, e.g. .js,.css")
    parser.add_argument("--ignore", action="append", default=[], help="extra ignored name")
    parser.add_argument("--health-url", default="", help="poll this URL after a restart")
    parser.add_argument("--health-timeout", type=float, default=60.0)
    parser.add_argument("--state-file", default="", help="persist the snapshot between --once runs")
    parser.add_argument("--max-restarts", type=int, default=8,
                        help="restart budget per window (0 = unlimited)")
    parser.add_argument("--budget-window", type=float, default=600.0,
                        help="seconds the restart budget is measured over")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--once", action="store_true", help="scan once and exit")
    parser.add_argument("--quiet", action="store_true")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    root = Path(args.dir).resolve()
    if not root.is_dir():
        log_error(f"--dir {root} is not a directory")
        return 2

    exts = list(DEFAULT_EXTS)
    if args.ext:
        exts += [e.strip() if e.strip().startswith(".") else "." + e.strip()
                 for e in args.ext.split(",") if e.strip()]
    command = args.restart_cmd or f"systemctl restart {args.unit}"

    log(f"watching {root} (ext {','.join(exts)})", args.quiet)
    log(f"restart: {command}" + ("  [dry-run]" if args.dry_run else ""), args.quiet)

    stopping = False

    def _stop(signum, _frame):  # noqa: ANN001
        nonlocal stopping
        stopping = True
        log(f"received signal {signum}, exiting", args.quiet)

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _stop)
        except (ValueError, OSError):
            pass

    state_path = Path(args.state_file).resolve() if args.state_file else None
    if state_path is not None:
        previous = load_state(state_path)
        if not previous:
            # No baseline yet: adopt the current tree so the first --once
            # run does not restart the service for no reason.
            previous = snapshot(root, exts, args.ignore)
            save_state(state_path, previous)
            if args.once:
                log(f"baseline recorded ({len(previous)} files) — nothing to do", args.quiet)
                return 0
    else:
        previous = snapshot(root, exts, args.ignore)
    log(f"{len(previous)} watched file(s) at start", args.quiet)

    pending: Set[str] = set()
    pending_since: Optional[float] = None
    last_restart = 0.0
    last_reported = ""
    restart_times: List[float] = []
    budget_warned = False

    while not stopping:
        now = time.time()
        current = snapshot(root, exts, args.ignore)
        changed, added, removed = diff_snapshots(previous, current)
        if changed or added or removed:
            touched = changed + added + removed
            pending.update(touched)
            pending_since = now
            title = ", ".join(touched[:6]) + (" …" if len(touched) > 6 else "")
            if not args.quiet:
                log(f"change detected: {title}")

        # Restart budget: a deploy that keeps touching files (an editor
        # saving on a timer, a sync client, a crash-looping watcher) must not
        # bounce the service every 10 seconds forever. That is exactly what
        # "everything keeps disconnecting" looks like from the outside.
        if args.max_restarts > 0:
            window_start = now - args.budget_window
            restart_times = [t for t in restart_times if t >= window_start]
        over_budget = args.max_restarts > 0 and len(restart_times) >= args.max_restarts

        if should_restart(now, pending_since, last_restart, args.debounce, args.min_interval):
            if over_budget:
                if not budget_warned:
                    budget_warned = True
                    log_error(
                        f"restart budget reached ({args.max_restarts} restarts in "
                        f"{int(args.budget_window)}s) — NOT restarting. The app is "
                        "left running as-is."
                    )
                    log_error(
                        "something keeps changing files (editor auto-save, sync "
                        "client, or a deploy loop). Fix that, raise --max-restarts, "
                        "or restart the watcher: systemctl restart fixiis-reloader"
                    )
                pending.clear()
                pending_since = None
                previous = current
                if state_path is not None:
                    save_state(state_path, current)
                if args.once:
                    return 1
                time.sleep(max(0.2, args.interval))
                continue

            touched = sorted(pending)
            problems = syntax_errors(root, touched)
            if problems:
                for rel, message in problems:
                    log_error(f"{rel} does not compile — NOT restarting: {message}")
                log_error("fix the file and save it again; the running process is untouched")
                pending.clear()
                pending_since = None
                previous = current
                if state_path is not None:
                    save_state(state_path, current)
                if args.once:
                    return 1
            else:
                log(f"restarting after {len(touched)} changed file(s)", args.quiet)
                ok, output = run_restart(command, args.dry_run)
                if ok:
                    last_restart = time.time()
                    restart_times.append(last_restart)
                    budget_warned = False
                    log("restart command succeeded" + (f": {output}" if output else ""), args.quiet)
                    if args.health_url:
                        healthy, detail = wait_for_health(args.health_url, args.health_timeout)
                        if healthy:
                            log(f"health check ok — {detail}", args.quiet)
                        else:
                            log_error(f"service did not come back: {detail}")
                else:
                    log_error(f"restart command failed: {output}")
                pending.clear()
                pending_since = None
                # Files may have changed while the restart was running.
                time.sleep(max(0.0, args.settle))
                previous = snapshot(root, exts, args.ignore)
                if state_path is not None:
                    save_state(state_path, previous)
                if args.once:
                    return 0 if ok else 1
                continue

        previous = current
        if state_path is not None:
            save_state(state_path, current)
        if args.once:
            if pending:
                log("--once: change seen but debounce window not elapsed; nothing restarted")
            return 0
        time.sleep(max(0.2, args.interval))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
