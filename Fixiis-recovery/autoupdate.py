"""
Auto update: restart the app when its own source files change.

Turned on with one line in ``config.py``::

    auto_update = True      # default: False (off)

Nothing else to install, no systemd unit, no scripts. ``main.py`` starts a
background thread that watches this folder; when a source file changes
(an upload, ``git pull``, an editor save) the app shuts itself down and
re-executes the same command, so the new code is live a few seconds later.

It is deliberately conservative:

* only source/config files are watched (``.py``, ``.html``, ``.env``,
  ``requirements.txt``); runtime folders (``lpv/``, ``logs/``, ``data/``,
  ``profiles/`` …) are ignored, so the app's own writes can never cause a
  restart loop;
* changes are debounced (3 s of quiet), so a 20-file deploy restarts once;
* at most 8 restarts per 10 minutes, then it stops and logs why;
* every changed ``.py`` is compiled first: a broken upload logs an error and
  the *working* process keeps running.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

# Extensions that mean "the running process must reload to see this".
DEFAULT_EXTS: Tuple[str, ...] = (".py", ".html")

# Exact file names worth watching even though they have no useful extension.
WATCHED_NAMES: Set[str] = {".env", "requirements.txt", "mobile_devices.json"}

# Never descend into these: runtime state the app writes while running (so
# watching them would restart forever), dependency trees, or things that can
# never affect the running server.
IGNORED_DIRS: Set[str] = {
    ".git", ".hg", ".svn",
    "venv", ".venv", "env", ".env.d",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "node_modules", ".npm", ".cache",
    "lpv", "logs", "log", "cache", "data", "profiles", "mobile-shots",
    "standalone", "dist", "build", "target", "coverage", ".next", ".nuxt",
    "tests", "deploy",
    ".idea", ".vscode", ".arena",
}

# Files whose content is state, not code, wherever they live.
IGNORED_SUFFIXES: Set[str] = {
    ".pyc", ".pyo", ".pyd", ".so", ".dylib", ".dll",
    ".log", ".sqlite3", ".sqlite3-wal", ".sqlite3-shm", ".db",
    ".zip", ".tar", ".gz", ".png", ".jpg", ".jpeg", ".webp", ".gif",
    ".ico", ".woff", ".woff2", ".ttf", ".mp4", ".webm", ".pdf",
}

IGNORED_FILES: Set[str] = {
    "lpv.sqlite3", "persistent_links.json", ".DS_Store", "Thumbs.db",
    ".env.example",
}

Snapshot = Dict[str, Tuple[int, int]]


# ---------------------------------------------------------------------------
# What counts as a source file
# ---------------------------------------------------------------------------
def is_watched(
    path: Path,
    root: Path,
    exts: Sequence[str] = DEFAULT_EXTS,
    extra_ignore: Iterable[str] = (),
) -> bool:
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
    if suffix in IGNORED_SUFFIXES or name.endswith((".sqlite3-wal", ".sqlite3-shm")):
        return False
    if name in WATCHED_NAMES:
        return True
    return suffix in {e.lower() for e in exts}


def snapshot(
    root: Path,
    exts: Sequence[str] = DEFAULT_EXTS,
    extra_ignore: Iterable[str] = (),
) -> Snapshot:
    """Map relative path -> (mtime_ns, size) for every watched file."""
    out: Snapshot = {}
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
            out[str(path.relative_to(root))] = (stat.st_mtime_ns, stat.st_size)
    return out


def diff_snapshots(old: Snapshot, new: Snapshot) -> Tuple[List[str], List[str], List[str]]:
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
            continue                      # deleted mid-check: nothing to reload
        except Exception as exc:
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


# ---------------------------------------------------------------------------
# The background watcher used by main.py
# ---------------------------------------------------------------------------
class AutoUpdater(threading.Thread):
    """Watch ``root`` and call ``on_change(files)`` when sources change.

    ``on_change`` runs on this thread; main.py schedules the actual restart on
    its own event loop, so nothing here touches the server directly.
    """

    def __init__(
        self,
        root: Path,
        on_change: Callable[[List[str]], None],
        *,
        interval: float = 2.0,
        debounce: float = 3.0,
        min_interval: float = 10.0,
        budget: int = 8,
        budget_window: float = 600.0,
        exts: Sequence[str] = DEFAULT_EXTS,
        extra_ignore: Iterable[str] = (),
        log: Optional[Callable[[str], None]] = None,
    ) -> None:
        super().__init__(name="auto-update", daemon=True)
        self.root = Path(root)
        self.on_change = on_change
        self.interval = max(0.2, interval)
        self.debounce = max(0.0, debounce)
        self.min_interval = max(0.0, min_interval)
        self.budget = budget
        self.budget_window = budget_window
        self.exts = tuple(exts)
        self.extra_ignore = tuple(extra_ignore)
        self._log = log or (lambda msg: print(f"[auto-update] {msg}", flush=True))

        self._stopped = threading.Event()
        self._pending: Set[str] = set()
        self._pending_since: Optional[float] = None
        self._last_restart = 0.0
        self._restarts: List[float] = []
        self._budget_warned = False

    # -- lifecycle ------------------------------------------------------
    def stop(self) -> None:
        self._stopped.set()

    def run(self) -> None:  # noqa: D401 - thread entry point
        previous = snapshot(self.root, self.exts, self.extra_ignore)
        self._log(f"watching {self.root} ({len(previous)} files)")
        while not self._stopped.is_set():
            self._stopped.wait(self.interval)
            if self._stopped.is_set():
                break
            try:
                self._tick(previous)
            except Exception as exc:              # never kill the watcher
                self._log(f"error during scan: {exc.__class__.__name__}: {exc}")
            previous = getattr(self, "_last_snapshot", previous)

    def _tick(self, previous: Snapshot) -> None:
        now = time.time()
        current = snapshot(self.root, self.exts, self.extra_ignore)
        self._last_snapshot = current

        changed, added, removed = diff_snapshots(previous, current)
        if changed or added or removed:
            touched = changed + added + removed
            self._pending.update(touched)
            self._pending_since = now
            self._log("change detected: " + ", ".join(touched[:6])
                      + (" …" if len(touched) > 6 else ""))

        if not should_restart(now, self._pending_since, self._last_restart,
                              self.debounce, self.min_interval):
            return

        if self.budget:
            window_start = now - self.budget_window
            self._restarts = [t for t in self._restarts if t >= window_start]
            if len(self._restarts) >= self.budget:
                if not self._budget_warned:
                    self._budget_warned = True
                    self._log(
                        f"restart budget reached ({self.budget} in "
                        f"{int(self.budget_window)}s) - not restarting. "
                        "Something keeps changing files."
                    )
                self._pending.clear()
                self._pending_since = None
                return

        touched = sorted(self._pending)
        problems = syntax_errors(self.root, touched)
        if problems:
            for rel, message in problems:
                self._log(f"ERROR {rel} does not compile - NOT restarting: {message}")
            self._log("fix the file and save it again; the app is still running")
            self._pending.clear()
            self._pending_since = None
            return

        self._pending.clear()
        self._pending_since = None
        self._last_restart = time.time()
        self._restarts.append(self._last_restart)
        self._budget_warned = False
        self._log(f"restarting after {len(touched)} changed file(s)")
        try:
            self.on_change(touched)
        except Exception as exc:
            self._log(f"restart request failed: {exc.__class__.__name__}: {exc}")


def start(
    root: Path,
    on_change: Callable[[List[str]], None],
    **kwargs,
) -> AutoUpdater:
    """Convenience: build, start and return an :class:`AutoUpdater`."""
    updater = AutoUpdater(root, on_change, **kwargs)
    updater.start()
    return updater


def main(argv: Optional[Sequence[str]] = None) -> int:
    """``python autoupdate.py`` - watch and print, for a quick manual test."""
    root = Path(__file__).resolve().parent
    updater = AutoUpdater(root, lambda files: print("would restart:", files, flush=True))
    updater.start()
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        updater.stop()
        return 0


if __name__ == "__main__":
    sys.exit(main())
