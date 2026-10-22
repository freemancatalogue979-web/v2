"""
One-command startup for Fixiis / Neo Browser Stream.

The only command an operator should need is::

    python main.py

This module runs before main.py imports anything heavy and makes that true:

  * chdir to the code directory, so relative data paths (``lpv/``,
    ``profiles/``, ``logs/``) land next to the code no matter which folder
    the command was typed in;
  * re-exec into ``venv/`` or ``.venv/`` when one exists and we are not
    already running inside it;
  * create a virtualenv on first run when there is none and packages are
    missing (interactive sessions, or ``--setup``);
  * install whatever is missing from ``requirements.txt``;
  * create ``.env`` from ``.env.example`` on first run (never overwrite);
  * create the runtime directories the server writes to;
  * print the URLs to open, and optionally open the admin page.

Everything is skipped when it is already correct, so a systemd start costs a
few ``find_spec`` calls and nothing else. No network access happens unless a
package really is missing and installation is allowed.

Usage
-----
    python main.py                 # set things up if needed, then run
    python main.py --check         # diagnose the environment and exit
    python main.py --set-password  # choose the admin password (writes .env)
    python main.py --reset-password # go back to the built-in admin/admin
    python main.py --fix-env       # repair a .env left by an old template
    python main.py --ignore-env    # ignore .env completely, use config.py only
    python main.py --setup         # force venv creation + pip install
    python main.py --no-install    # never install; print instructions
    python main.py --network       # listen on this machine's network (0.0.0.0)
    python main.py --local         # listen on 127.0.0.1 only
    python main.py --port 9000     # override PORT for this run
    python main.py --host 198.51.100.10   # bind one specific address
    python main.py --open          # open the admin page when it is up

Where settings come from
------------------------
``config.py`` is the source of truth and works on its own: no ``.env`` is
created or needed for HOST, PORT, ``auto_update`` and the rest. A ``.env`` is
only read when *you* made one (deploy/setup.sh does), and it merely overrides
config.py. ``--ignore-env`` runs from config.py alone without touching it.

Auto update (restart when files change) is one line in config.py::

    auto_update = True      # default False


Environment overrides
---------------------
    FIXIIS_NO_SETUP=1     same as --no-install
    FIXIIS_AUTO_SETUP=1   same as --setup        (for non-interactive first run)
    FIXIIS_NO_VENV=1      never re-exec into or create a virtualenv
    FIXIIS_IGNORE_ENV=1   same as --ignore-env
    FIXIIS_BANNER=1       print the startup banner even without a TTY
    FIXIIS_BOOTSTRAPPED=1 set internally when re-execing; do not set by hand
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import socket
import subprocess
import sys
import sysconfig
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, TextIO, Tuple

ROOT = Path(__file__).resolve().parent
#: Where the operator actually ran the command, recorded before any chdir.
START_CWD = Path.cwd()
MAIN = ROOT / "main.py"
REQUIREMENTS = ROOT / "requirements.txt"
ENV_FILE = ROOT / ".env"
ENV_TEMPLATE = ROOT / ".env.example"

MIN_PYTHON = (3, 10)

# requirements.txt package name -> module you can import to prove it is there.
# Packages whose import name differs from the distribution name are listed
# explicitly; anything not listed falls back to the lower-cased name.
IMPORTS: Dict[str, str] = {
    "fastapi": "fastapi",
    "uvicorn": "uvicorn",
    "slowapi": "slowapi",
    "bcrypt": "bcrypt",
    "pyjwt": "jwt",
    "httpx": "httpx",
    "playwright": "playwright",
    "playwright-stealth": "playwright_stealth",
    "seleniumbase": "seleniumbase",
    "psutil": "psutil",
    "aiortc": "aiortc",
    "opencv-python-headless": "cv2",
    "numpy": "numpy",
    "asyncio-dgram": "asyncio_dgram",
    "brotli": "brotli",
}

# Without these main.py cannot even be imported, so they decide whether the
# startup path may continue without an install.
CRITICAL = ("fastapi", "uvicorn", "httpx", "bcrypt", "pyjwt", "slowapi", "psutil")

# Writable directories the server expects next to the code.
RUNTIME_DIRS = ("profiles", "logs", "lpv", "data", "cache", "mobile-shots")

VENV_DIR_NAMES = ("venv", ".venv", "env")

ENV_STARTED = "FIXIIS_BOOTSTRAPPED"

HELP = __doc__ or ""


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------
def _say(msg: str = "", stream: Optional[TextIO] = None) -> None:
    """Print one setup line, flushed, so it appears before uvicorn's banner."""
    print(msg, file=stream or sys.stdout, flush=True)


def _venv_python(venv_dir: Path) -> Path:
    """Interpreter inside ``venv_dir`` on this platform."""
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def find_venv(root: Path = ROOT) -> Optional[Path]:
    """Return ``(venv_dir, python)``'s venv dir for an existing virtualenv."""
    for name in VENV_DIR_NAMES:
        cand = root / name
        if _venv_python(cand).exists():
            return cand
    return None


def running_in(venv_dir: Path) -> bool:
    """True when the current interpreter already belongs to ``venv_dir``.

    Compares ``sys.prefix`` rather than ``sys.executable``: a virtualenv's
    ``bin/python`` is usually a symlink to the system interpreter, so the
    resolved paths are identical and an executable comparison would wrongly
    conclude we are already inside it.
    """
    try:
        return Path(sys.prefix).resolve() == Path(venv_dir).resolve()
    except OSError:
        return False


def find_spec_safe(module: str) -> bool:
    """``importlib.util.find_spec`` that treats any failure as missing."""
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError, AttributeError, TypeError):
        return False


def parse_requirements(path: Path = REQUIREMENTS) -> List[str]:
    """Requirement names from a requirements file, comments and extras gone.

    ``uvicorn[standard]==0.27.1`` becomes ``uvicorn``; ``-r other.txt``,
    ``--index-url ...`` and blank lines are dropped.
    """
    names: List[str] = []
    try:
        raw = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return names
    for line in raw:
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        name = line
        for sep in ("==", ">=", "<=", "~=", "!=", ">", "<", "[", ";", "@", " "):
            name = name.split(sep, 1)[0]
        name = name.strip()
        if name and name not in names:
            names.append(name)
    return names


def import_name(package: str) -> str:
    return IMPORTS.get(package.lower(), package.lower())


def missing_packages(
    required: Optional[Iterable[str]] = None,
    *,
    finder=find_spec_safe,
) -> List[str]:
    """Packages from ``requirements.txt`` whose module cannot be imported."""
    names = list(required) if required is not None else parse_requirements()
    return [p for p in names if not finder(import_name(p))]


# --------------------------------------------------------------------------
# .env and runtime directories
# --------------------------------------------------------------------------
#: Values that only ever appear in examples. A `.env` carrying these is
#: broken: the app builds absolute URLs from SERVER_DOMAIN, so a domain
#: nobody owns turns every LPV push / share link into "Failed to fetch".
PLACEHOLDER_DOMAINS = {
    "panel.example.com", "example.com", "www.example.com",
    "example.org", "example.net",
}

#: The hash api.py falls back to when the value is empty (password "admin").
DEFAULT_ADMIN_HASH = "$2b$12$K9Xm2pQ4rT7vN8wL3jH6e.nSuc39Ma1gWXoznLSkhTeaL9F1CfDne"

ENV_HEADER = """\
# Fixiis / Neo Browser Stream - created automatically by main.py.
# This file is safe to edit; it is never overwritten once it exists.
#
# HOST=0.0.0.0    -> this machine AND every device on its network
# HOST=127.0.0.1  -> this machine only (for nginx / tunnel setups)
# Leave SERVER_DOMAIN unset unless you really own that domain: when it is
# set, share links and LPV page URLs are built from it instead of the
# address the client actually connected to.
"""


def _random_secret(bytes_: int = 48) -> str:
    """A URL-safe random secret, stdlib only (no pip needed)."""
    import secrets

    return secrets.token_urlsafe(bytes_)


def generate_env_text(port: int = 8446) -> str:
    """A working .env: no placeholders, no blank secrets."""
    return "\n".join([
        ENV_HEADER,
        "ADMIN_USERNAME=admin",
        "ADMIN_PASSWORD_HASH=",          # filled by ensure_admin_password()
        f"JWT_SECRET_KEY={_random_secret()}",
        "HOST=0.0.0.0",
        f"PORT={port}",
        "BROWSER_BACKEND=sb",
        "",
    ])


def ensure_env_file(root: Path = ROOT, port: int = 8446) -> bool:
    """Create ``.env`` on first run. Returns True when it was created.

    Deliberately does *not* copy ``.env.example``: that template contains
    documentation placeholders (``SERVER_DOMAIN=panel.example.com``,
    ``USE_HTTPS=true``, blank secrets) which, if copied verbatim, produce a
    server whose links point at a domain nobody owns and whose login can
    never succeed. Only real values are written here.
    """
    target = root / ".env"
    if target.exists():
        return False
    try:
        target.write_text(generate_env_text(port), encoding="utf-8")
        try:
            target.chmod(0o600)
        except OSError:
            pass
    except OSError:
        return False
    return True


def set_admin_password(
    root: Path = ROOT,
    stream: Optional[TextIO] = None,
    password: Optional[str] = None,
) -> bool:
    """Store a bcrypt hash for the admin login. Writes ``.env`` on purpose.

    Only the secret lives in ``.env``; everything else comes from config.py.
    Interactive sessions are asked for a password (or offered a random one);
    non-interactive callers pass ``password`` or get a generated one.
    """
    try:
        import bcrypt
    except Exception:
        return False

    out = stream or sys.stdout
    if password is None:
        interactive = _interactive()
        if interactive:
            import getpass

            print("  Set the admin password for user 'admin'.", file=out)
            print("  Press Enter to generate a random one.", file=out, flush=True)
            try:
                typed = getpass.getpass("  Password: ")
            except (EOFError, KeyboardInterrupt):
                typed = ""
            if typed:
                confirm = getpass.getpass("  Repeat: ")
                if confirm != typed:
                    print("  passwords did not match - nothing changed", file=out)
                    return False
                if len(typed) < 8:
                    print("  use at least 8 characters - nothing changed", file=out)
                    return False
                password = typed
        if password is None:
            import secrets

            password = secrets.token_urlsafe(12)

    try:
        hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt(12)).decode()
    except Exception:
        return False

    env_path = root / ".env"
    lines = _env_lines(env_path) if env_path.exists() else []
    if not lines:
        lines = [ENV_HEADER.rstrip("\n"), "", "ADMIN_USERNAME=admin"]

    replaced = False
    result: List[str] = []
    for line in lines:
        key = _key_of(line)
        if key == "ADMIN_PASSWORD_HASH":
            result.append(f"ADMIN_PASSWORD_HASH={hashed}")
            replaced = True
        else:
            result.append(line)
    if not replaced:
        result.append(f"ADMIN_PASSWORD_HASH={hashed}")

    if not any(_key_of(line) == "JWT_SECRET_KEY" and _value_of(line) for line in result):
        result = [
            f"JWT_SECRET_KEY={_random_secret()}" if _key_of(line) == "JWT_SECRET_KEY" else line
            for line in result
        ]
        if not any(_key_of(line) == "JWT_SECRET_KEY" for line in result):
            result.append(f"JWT_SECRET_KEY={_random_secret()}")

    try:
        env_path.write_text("\n".join(result).rstrip("\n") + "\n", encoding="utf-8")
        env_path.chmod(0o600)
    except OSError:
        return False

    print(f"  admin user:     admin", file=out)
    print(f"  admin password: {password}", file=out)
    print(f"  stored as a bcrypt hash in {env_path}", file=out)
    print("  (this is the only time the password is shown - save it)", file=out, flush=True)
    return True


def clear_admin_password(root: Path = ROOT, stream: Optional[TextIO] = None) -> bool:
    """Go back to the built-in login: user ``admin``, password ``admin``.

    Removes ``ADMIN_PASSWORD_HASH`` from ``.env`` so nothing overrides the
    built-in hash. Deliberately blunt: when the generated password is lost,
    "type a new one" is a worse answer than "get me back in".

    The password is weak on purpose - it is the documented default. Callers
    are told to set a real one afterwards.
    """
    out = stream or sys.stdout
    env_path = root / ".env"
    removed = False
    kept: List[str] = []
    try:
        lines = _env_lines(env_path) if env_path.exists() else []
    except OSError:
        lines = []
    for line in lines:
        if _key_of(line) == "ADMIN_PASSWORD_HASH":
            removed = True
            continue
        kept.append(line)

    if removed:
        try:
            text = "\n".join(kept).rstrip("\n")
            env_path.write_text((text + "\n") if text else "", encoding="utf-8")
            env_path.chmod(0o600)
        except OSError as exc:
            print(f"  could not write {env_path}: {exc}", file=out)
            return False

    print(f"  admin user:     admin", file=out)
    print(f"  admin password: admin   (built-in default)", file=out)
    if removed:
        print(f"  {env_path} no longer overrides it", file=out)
    elif env_path.exists():
        print(f"  {env_path} had no admin hash - nothing to remove", file=out)
    else:
        print(f"  no .env - the built-in default was already in use", file=out)

    # A hash in the real environment (systemd Environment=, a shell export)
    # still wins over the file we just cleaned.
    if (os.environ.get("ADMIN_PASSWORD_HASH") or "").strip():
        print(
            "  WARNING: ADMIN_PASSWORD_HASH is also set in the environment\n"
            "           (systemd unit or shell export) - remove it there too,\n"
            "           or it keeps overriding the default.",
            file=out,
        )
    print("", file=out)
    print("  Anyone who can reach the panel can now log in as admin/admin.", file=out)
    print("  Set a real one when you are back in: python main.py --set-password", file=out)
    return True


def _env_lines(path: Path) -> List[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def _key_of(line: str) -> Optional[str]:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    return stripped.split("=", 1)[0].strip()


def _value_of(line: str) -> str:
    return line.split("=", 1)[1].strip().strip('"').strip("'") if "=" in line else ""


def is_placeholder_domain(value: str) -> bool:
    host = (value or "").strip().lower()
    for prefix in ("http://", "https://"):
        if host.startswith(prefix):
            host = host[len(prefix):]
    host = host.split("/", 1)[0]
    return host in PLACEHOLDER_DOMAINS or host.endswith((".example.com", ".example.org"))


def repair_env_file(root: Path = ROOT, path: Optional[Path] = None) -> List[str]:
    """Fix a `.env` that was created from the old template. Idempotent.

    Returns a list of human-readable descriptions of what was changed
    (empty when the file was already fine). Only known-broken values are
    touched - a real domain, a real password hash and every other setting
    are preserved exactly as written.
    """
    env_path = path or (root / ".env")
    if not env_path.exists():
        return []

    original = _env_lines(env_path)
    if not original:
        return []

    changes: List[str] = []
    out: List[str] = []
    seen: Dict[str, str] = {}
    placeholder_domain = False
    host_disabled = False

    # A loopback HOST is only correct when something on this box proxies to
    # the app (nginx, or the built-in Cloudflare tunnel). Otherwise it is the
    # reason phones and other PCs get "connection refused" while the server
    # looks perfectly healthy locally.
    tunnelled = (
        seen.get("CLOUDFLARE_TUNNEL", "").lower() in ("1", "true", "yes", "on")
        or bool(seen.get("TUNNEL_HOSTNAME", ""))
        or any(_key_of(line) == "CLOUDFLARE_TUNNEL" and _value_of(line).lower() in
               ("1", "true", "yes", "on") for line in original)
        or any(_key_of(line) == "TUNNEL_HOSTNAME" and _value_of(line) for line in original)
    )

    for line in original:
        key = _key_of(line)
        if key is None:
            out.append(line)
            continue
        value = _value_of(line)
        seen[key] = value

        if key == "SERVER_DOMAIN" and value and is_placeholder_domain(value):
            placeholder_domain = True
            out.append("# (removed by main.py) template placeholder, not a real domain:")
            out.append(f"# {line.strip()}")
            changes.append(f"disabled placeholder SERVER_DOMAIN={value}")
            continue

        if key == "HOST" and value and is_loopback(value) and not tunnelled:
            host_disabled = True
            out.append("# main.py changed this from " + value + ": loopback kept phones")
            out.append("# and other PCs out. Put it back if nginx or a tunnel proxies")
            out.append("# to the app on this machine.")
            out.append("HOST=0.0.0.0")
            changes.append(f"HOST={value} -> 0.0.0.0 (this machine AND its network)")
            continue

        if key in ("PUBLIC_IP", "HOST", "TUNNEL_HOSTNAME", "CHROME_PATH",
                   "PROFILE_BASE_PATH", "DEFAULT_URL") and not value:
            out.append(f"# (removed by main.py) empty value overrides the default: {key}")
            changes.append(f"dropped empty {key}=")
            continue

        if key == "ADMIN_PASSWORD_HASH" and not value:
            out.append("# (set by main.py) ADMIN_PASSWORD_HASH is empty; the built-in")
            out.append("# default applies until you generate your own hash.")
            out.append("# ADMIN_PASSWORD_HASH=")
            changes.append("removed empty ADMIN_PASSWORD_HASH (default hash now applies)")
            continue

        out.append(line)

    if placeholder_domain and "USE_HTTPS" in seen and seen["USE_HTTPS"].lower() == "true":
        changes.append("set USE_HTTPS=false (it only made sense with that domain)")

    needs_https_fix = placeholder_domain and seen.get("USE_HTTPS", "").lower() == "true"
    if needs_https_fix:
        out = [
            "USE_HTTPS=false" if _key_of(line) == "USE_HTTPS" else line
            for line in out
        ]

    if not seen.get("JWT_SECRET_KEY"):
        if "JWT_SECRET_KEY" in seen:
            out = [
                f"JWT_SECRET_KEY={_random_secret()}" if _key_of(line) == "JWT_SECRET_KEY" else line
                for line in out
            ]
        else:
            out.append(f"JWT_SECRET_KEY={_random_secret()}")
        changes.append("generated a random JWT_SECRET_KEY")

    if not seen.get("HOST") and not host_disabled and "HOST" not in seen:
        out.append("HOST=0.0.0.0")
        changes.append("added HOST=0.0.0.0 (reachable from the network)")

    if not changes:
        return []

    text = "\n".join(out).rstrip("\n") + "\n"
    try:
        env_path.write_text(text, encoding="utf-8")
    except OSError:
        return []
    return changes


def ensure_admin_password(root: Path = ROOT, stream: Optional[TextIO] = None) -> Optional[str]:
    """Set a random admin password on first run and return it once.

    Needs bcrypt, so it is called after dependency installation. When the
    hash is already set (setup.sh, or a previous run) nothing happens and
    ``None`` is returned. Also made idempotent by the fact that the file is
    only rewritten when the value really changes.
    """
    env_path = root / ".env"
    values = env_file_values(root)
    if values.get("ADMIN_PASSWORD_HASH"):
        return None
    try:
        import bcrypt
    except Exception:
        return None

    import secrets

    password = secrets.token_urlsafe(9)
    try:
        hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt(12)).decode()
    except Exception:
        return None

    lines = _env_lines(env_path)
    if not lines:
        return None
    replaced = False
    out: List[str] = []
    for line in lines:
        key = _key_of(line)
        if key == "ADMIN_PASSWORD_HASH" and not _value_of(line):
            out.append(f"ADMIN_PASSWORD_HASH={hashed}")
            replaced = True
        elif key == "ADMIN_PASSWORD_HASH":
            out.append(line)          # already has a hash - keep it
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"ADMIN_PASSWORD_HASH={hashed}")
    try:
        env_path.write_text("\n".join(out).rstrip("\n") + "\n", encoding="utf-8")
        env_path.chmod(0o600)
    except OSError:
        return None

    if stream is not None:
        print(f"    admin user: admin", file=stream)
        print(f"    admin password: {password}", file=stream)
        print("    (written to .env as a bcrypt hash; this is the only time it is shown)",
              file=stream, flush=True)
    return password


def ensure_dirs(root: Path = ROOT, names: Sequence[str] = RUNTIME_DIRS) -> List[str]:
    """Create the runtime directories. Returns the ones that were missing."""
    created: List[str] = []
    for name in names:
        path = root / name
        if path.is_dir():
            continue
        try:
            path.mkdir(parents=True, exist_ok=True)
            created.append(name)
        except OSError:
            pass
    return created


# --------------------------------------------------------------------------
# installation
# --------------------------------------------------------------------------
def pip_available(python: str) -> bool:
    try:
        return subprocess.run(
            [python, "-m", "pip", "--version"],
            capture_output=True,
        ).returncode == 0
    except OSError:
        return False


def create_venv(dest: Path, python: Optional[str] = None) -> bool:
    """Create a virtualenv at ``dest``. Returns True when it is usable."""
    if _venv_python(dest).exists():
        return True
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [python or sys.executable, "-m", "venv", str(dest)],
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return False
    return _venv_python(dest).exists()


def install_requirements(python: str, root: Path = ROOT, packages: Sequence[str] = ()) -> bool:
    """Install ``requirements.txt`` (or just ``packages``) into ``python``."""
    if not pip_available(python):
        try:
            subprocess.run([python, "-m", "ensurepip", "--upgrade"], capture_output=True)
        except OSError:
            return False
        if not pip_available(python):
            return False

    cmd = [python, "-m", "pip", "install", "--disable-pip-version-check"]
    if packages:
        cmd.extend(packages)
    elif (root / "requirements.txt").exists():
        cmd.extend(["-r", str(root / "requirements.txt")])
    else:
        return False

    _say("    installing dependencies (first run only - this can take a few minutes)...")
    try:
        proc = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True)
    except OSError as exc:
        _say(f"    pip could not run: {exc}")
        return False
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-8:]
        _say("    pip failed:")
        for line in tail:
            _say(f"      {line}")
        return False
    return True


# --------------------------------------------------------------------------
# environment diagnostics (--check)
# --------------------------------------------------------------------------
def playwright_browsers_dir() -> Path:
    override = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if override and override not in ("0", "1"):
        return Path(override)
    home = Path.home()
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", home / "AppData/Local")) / "ms-playwright"
    if sys.platform == "darwin":
        return home / "Library/Caches/ms-playwright"
    return home / ".cache/ms-playwright"


def playwright_chromium_installed() -> bool:
    directory = playwright_browsers_dir()
    try:
        return any(p.name.startswith("chromium") for p in directory.iterdir())
    except OSError:
        return False


def chrome_binary() -> Optional[str]:
    """Best-effort Chrome/Chromium path, matching config.py's auto-detection."""
    override = os.environ.get("CHROME_PATH")
    if override and Path(override).exists():
        return override
    candidates = (
        "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
        "microsoft-edge", "chrome",
    )
    for name in candidates:
        found = shutil.which(name)
        if found:
            return found
    mac = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    if mac.exists():
        return str(mac)
    return None


def port_free(port: int, host: str = "") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def is_usable_password_hash(value: str) -> bool:
    """True when ``value`` is a bcrypt hash (or a legacy MD5 digest).

    Anything else - a placeholder, a truncated paste, a quoted value, a
    plain-text password typed by mistake - makes every login fail, and the
    panel can then only answer "Invalid credentials".
    """
    value = (value or "").strip()
    if value.startswith(("$2a$", "$2b$", "$2y$")):
        return len(value) == 60
    if len(value) == 32 and all(c in "0123456789abcdefABCDEF" for c in value):
        return True
    return False


def env_file_values(root: Path = ROOT) -> Dict[str, str]:
    """KEY=VALUE pairs from .env, without touching the process environment.

    Needed because ``--check`` runs before main.py loads .env, so the report
    has to read the file itself to show the settings that will really apply.
    """
    values: Dict[str, str] = {}
    try:
        lines = (root / ".env").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return values
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def env_value(key: str, default: str = "", root: Path = ROOT) -> str:
    """Effective value: real environment first, then .env, then the default."""
    if key in os.environ:
        return os.environ[key]
    return env_file_values(root).get(key, default)


CONFIG_SETTINGS = (
    "host", "port", "server_domain", "public_ip", "use_https",
    "cloudflare_tunnel", "tunnel_hostname", "auto_update",
)


def config_values() -> Dict[str, object]:
    """Values from config.py, the source of truth for every setting.

    Imported inside a guard: config.py reads the environment when imported,
    so this is only used by --check (which exits) and never during a real
    start, where main.py imports it at the right moment.
    """
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        import importlib

        module = importlib.import_module("config")
        conf = getattr(module, "CONFIG", None)
        if conf is None:
            return {}
        return {name: getattr(conf, name, None) for name in CONFIG_SETTINGS}
    except Exception:
        return {}


def env_is_ignored() -> bool:
    return os.environ.get("FIXIIS_IGNORE_ENV") == "1"


def effective(key: str, default: object = None, root: Path = ROOT) -> Tuple[object, str]:
    """Value **and where it came from**: env var, .env, config.py, default."""
    if key in os.environ:
        return os.environ[key], "environment"
    if not env_is_ignored():
        from_file = env_file_values(root).get(key)
        if from_file not in (None, ""):
            return from_file, ".env"
    value = config_values().get(key.lower(), None)
    if value not in (None, ""):
        return value, "config.py"
    return default, "default"


def port_from_env(default: int = 8446, root: Path = ROOT) -> int:
    """``PORT`` from the environment or .env, tolerating junk."""
    try:
        value = int(env_value("PORT", str(default), root) or default)
    except (TypeError, ValueError):
        return default
    return value if 1 <= value <= 65535 else default


def host_from_env(default: str = "0.0.0.0", root: Path = ROOT) -> str:
    """``HOST`` from the environment or .env, blank meaning "all interfaces"."""
    host = (env_value("HOST", default, root) or default).strip()
    return host or default


LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "0:0:0:0:0:0:0:1"}


def is_loopback(host: str) -> bool:
    """True when ``host`` can only be reached from this machine."""
    value = (host or "").strip().strip("[]").lower()
    return value in LOOPBACK_HOSTS or value.startswith("127.")


def usable_ip(address: str) -> bool:
    """False for loopback, link-local (169.254.x.x), 0.0.0.0 and garbage."""
    try:
        parts = [int(p) for p in address.split(".")]
    except ValueError:
        return False
    if len(parts) != 4 or any(not 0 <= p <= 255 for p in parts):
        return False
    if parts[0] in (0, 127):
        return False
    if parts[0] == 169 and parts[1] == 254:
        return False
    return True


def ip_rank(address: str) -> int:
    """Lower is better: RFC1918 LAN addresses first, then anything routable."""
    parts = [int(p) for p in address.split(".")]
    if parts[0] == 10:
        return 0
    if parts[0] == 192 and parts[1] == 168:
        return 0
    if parts[0] == 172 and 16 <= parts[1] <= 31:
        return 0
    if parts[0] == 100 and 64 <= parts[1] <= 127:      # carrier-grade NAT / VPN
        return 2
    return 1


def hostname_addresses() -> List[str]:
    """Every IPv4 address this host's own name resolves to."""
    found: List[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = info[4][0]
            if address not in found:
                found.append(address)
    except (OSError, UnicodeError):
        pass
    return found


def lan_ip() -> Optional[str]:
    """Address other devices on the network can use to reach this machine.

    Tries the routing table first (no packet is actually sent), then the
    hostname's own addresses, and ignores loopback and 169.254.x.x
    link-local addresses, which no other machine can use.
    """
    candidates: List[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(0.2)
            sock.connect(("8.8.8.8", 80))
            candidates.append(sock.getsockname()[0])
    except OSError:
        pass
    candidates.extend(hostname_addresses())

    usable: List[str] = []
    for address in candidates:
        if usable_ip(address) and address not in usable:
            usable.append(address)
    if not usable:
        return None
    usable.sort(key=ip_rank)
    return usable[0]


def diagnose(
    port: Optional[int] = None,
    stream: Optional[TextIO] = None,
    finder=find_spec_safe,
) -> int:
    """Print an environment report. Returns 0 when the server can start."""
    out = stream or sys.stdout
    ok = True

    def line(
        label: str,
        value: str,
        good: bool = True,
        hint: str = "",
        warn: bool = False,
    ) -> None:
        nonlocal ok
        if not good:
            ok = False
        mark = "warn" if warn else ("ok  " if good else "FAIL")
        print(f"  [{mark}] {label:<32} {value}", file=out)
        if hint and (warn or not good):
            print(f"         -> {hint}", file=out)

    print("Fixiis / Neo Browser Stream - environment check", file=out)
    print(f"  directory: {ROOT}", file=out)
    print(f"  typed in:  {START_CWD}", file=out)
    print("", file=out)

    version = ".".join(str(n) for n in sys.version_info[:3])
    line(
        "Python",
        f"{version} ({sys.executable})",
        sys.version_info >= MIN_PYTHON,
        f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ is required",
    )

    venv = find_venv()
    if venv and running_in(venv):
        line("virtualenv", f"active ({venv.name})")
    elif venv:
        # Not an error: prepare() re-execs into it on the next start.
        line("virtualenv", f"{venv.name} exists (used automatically)")
    else:
        line("virtualenv", "none (runs in system Python)", True)

    for package in parse_requirements():
        module = import_name(package)
        present = finder(module)
        critical = package.lower() in CRITICAL
        line(
            f"package {package}",
            module if present else "missing",
            present or not critical,
            "python main.py --setup" if not present else "",
        )

    env_exists = ENV_FILE.exists()
    values = env_file_values()
    line(
        ".env",
        ("present (overrides config.py)" if env_exists and not env_is_ignored()
         else "present but ignored (--ignore-env)" if env_exists
         else "not used - settings come from config.py"),
    )

    # (.env's own SERVER_DOMAIN / JWT / password are reported by the
    # effective-value rows further down, which also name where the value came
    # from. Printing them here as well produced two contradictory-looking
    # lines for the same setting.)

    # --check never modifies anything, so no repair pass here.

    dirs = [name for name in RUNTIME_DIRS if not (ROOT / name).is_dir()]
    line("runtime dirs", "all present" if not dirs else "created on start: " + ", ".join(dirs))

    try:
        probe = ROOT / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        line("writable directory", "yes")
    except OSError as exc:
        line("writable directory", str(exc), False,
             f"chown/chmod {ROOT} so this user can write there")

    if port is None:
        port = port_from_env()
    free = port_free(port)
    line("port", f"{port} {'free' if free else 'in use (will be reclaimed on start)'}")

    host, host_src = effective("HOST", "0.0.0.0")
    port_value, port_src = effective("PORT", 8446)
    if is_loopback(str(host)):
        line(
            "HOST (from %s)" % host_src,
            f"{host} - this machine only",
            warn=True,
            hint=(
                "other devices (phones, laptops, other PCs) cannot connect. "
                "Fix: set host = \"0.0.0.0\" in config.py, or run --network"
            ),
        )
    else:
        where = "all interfaces" if str(host) in ("", "0.0.0.0", "::") else str(host)
        machine = lan_ip()
        reach = f" -> http://{machine}:{port_value}/admin" if machine else ""
        line(f"HOST (from {host_src})", f"{where} (network reachable){reach}")

    line(f"PORT (from {port_src})", str(port_value))

    domain, domain_src = effective("SERVER_DOMAIN", "")
    if domain and is_placeholder_domain(str(domain)):
        line(
            "SERVER_DOMAIN",
            f"{domain} - template placeholder",
            warn=True,
            hint=(
                "links would point at a domain nobody owns ('Failed to fetch'). "
                "Fix: python main.py --fix-env, or --ignore-env to use config.py"
            ),
        )
    elif domain:
        line(f"SERVER_DOMAIN (from {domain_src})", str(domain))
    else:
        line("SERVER_DOMAIN", "unset - URLs use the address the client connected to")

    line("settings from", "config.py" + (" (.env ignored)" if env_is_ignored() else
                                         (" + .env overrides" if ENV_FILE.exists() else " only")))

    auto_update, auto_src = effective("AUTO_UPDATE", None)
    if auto_update is None:
        auto_update = config_values().get("auto_update", False)
        auto_src = "config.py"
    elif auto_src == "default":
        auto_src = "config.py"
    enabled = str(auto_update).strip().lower() in ("1", "true", "yes", "on")
    line(
        f"auto update (from {auto_src})",
        "ON - a file change restarts the app (closes live sessions)"
        if enabled else "off - restart yourself after changes",
    )

    # One row, stated plainly: which user, where the password comes from and
    # how to change (or drop) it. "Invalid credentials" with no explanation is
    # the most common lockout there is.
    login_text, login_warn = admin_login_summary()
    line(
        "admin login",
        login_text,
        warn=login_warn,
        hint="go back to admin/admin: python main.py --reset-password" if login_warn else "",
    )

    jwt_value, jwt_src = effective("JWT_SECRET_KEY", "")
    line(
        "JWT secret",
        f"set (from {jwt_src})" if jwt_value else "built-in placeholder",
        warn=not jwt_value,
        hint="" if jwt_value else "python main.py --fix-env (writes one)",
    )

    line("Chrome / Chromium", chrome_binary() or "not found", bool(chrome_binary()),
         "install Chrome, or set CHROME_PATH in .env")

    if playwright_chromium_installed():
        line("Playwright chromium", "installed")
    else:
        line("Playwright chromium", "not downloaded (only needed with BROWSER_BACKEND=playwright)",
             True, "python -m playwright install chromium")

    print("", file=out)
    print(
        "Ready: python main.py" if ok else "Fix the FAIL lines above, then: python main.py",
        file=out,
    )
    return 0 if ok else 1


# --------------------------------------------------------------------------
# startup banner
# --------------------------------------------------------------------------
def admin_login_summary(root: Path = ROOT) -> Tuple[str, bool]:
    """What the admin login accepts, and whether that is worth a warning.

    * no hash anywhere -> the built-in default ('admin'), warn: change it
    * a hash in .env   -> custom password, but say where it is, because the
                          only copy of the password is in the operator's
                          notes (/root/fixiis-credentials.txt from setup.sh)
    * unusable hash    -> warn loudly: no password can ever work
    """
    values = {} if env_is_ignored() else env_file_values(root)
    value = (values.get("ADMIN_PASSWORD_HASH") or
             os.environ.get("ADMIN_PASSWORD_HASH", "")).strip()
    user = (os.environ.get("ADMIN_USERNAME") or "admin").strip()
    if not value:
        return (f"user '{user}' - default password: admin  "
                f"(change: python main.py --set-password)", True)
    if not is_usable_password_hash(value):
        return (f"user '{user}' - stored hash is unusable, no password works  "
                f"(fix: python main.py --set-password)", True)
    where = "environment" if os.environ.get("ADMIN_PASSWORD_HASH") else ".env"
    return (f"user '{user}' - password set in {where}  "
            f"(forgotten? python main.py --set-password)", False)


def banner(
    host: str,
    port: int,
    tunnel: str = "",
    stream: Optional[TextIO] = None,
    machine_ip: Optional[str] = None,
    login: str = "",
) -> None:
    """Print the URLs an operator needs right after a successful start.

    ``host`` is the bind address (``0.0.0.0`` = every interface). The URLs
    are printed with the machine's own network address whenever the server
    is reachable from more than localhost, so a phone or another PC can use
    them as-is.
    """
    out = stream or sys.stdout
    local = f"http://127.0.0.1:{port}"
    loopback_only = is_loopback(host)
    machine = None if loopback_only else (machine_ip if machine_ip is not None else lan_ip())
    wildcard = (host or "").strip() in ("", "0.0.0.0", "::")
    if loopback_only:
        base = local
    elif not wildcard:
        base = f"http://{host}:{port}"          # bound to one specific address
    else:
        base = f"http://{machine}:{port}" if machine else local
    no_detected_ip = base == local and not loopback_only

    pages = (
        ("admin", "/admin"),
        ("mini", "/mini"),
        ("client", "/client.html"),
        ("health", "/health"),
    )

    lines = ["", "  Fixiis / Neo Browser Stream", ""]
    for name, path in pages:
        lines.append(f"    {name:<7} {base}{path}")
    if base != local:
        lines.append(f"    {'local':<7} {local}/admin   (this machine only)")
    if tunnel:
        lines.append(f"    {'tunnel':<7} {tunnel}")
    lines.append(f"    {'stop':<7} Ctrl+C")
    if login:
        lines.append(f"    {'login':<7} {login}")

    if no_detected_ip:
        lines += [
            "",
            "    note: bound to all interfaces, but this machine's network",
            "          address could not be detected. Use its own IPv4 address",
            "          (hostname -I on Linux, ipconfig on Windows) with the port",
            f"          above, e.g. http://<machine-ip>:{port}/admin",
        ]
    if loopback_only:
        lines += [
            "",
            "    ! listening on 127.0.0.1 - only this machine can open it.",
            "      phones, laptops and other PCs will get 'connection refused'.",
            "      fix:  python main.py --host 0.0.0.0",
            f"      or:   HOST=0.0.0.0 in {ROOT / '.env'}, then restart",
        ]
    for line in lines:
        print(line, file=out)
    print("", file=out, flush=True)


def open_when_ready(port: int, path: str = "/admin", timeout: float = 45.0) -> None:
    """Open ``path`` in a browser once the port accepts connections."""
    import threading
    import time
    import webbrowser

    url = f"http://127.0.0.1:{port}{path}"

    def _worker() -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1.0):
                    webbrowser.open(url)
                    return
            except OSError:
                time.sleep(0.4)

    threading.Thread(target=_worker, name="open-browser", daemon=True).start()


# --------------------------------------------------------------------------
# argument handling + the one entry point main.py calls
# --------------------------------------------------------------------------
@dataclass
class Startup:
    """What main.py needs to know after the setup pass."""

    host: str = ""
    port: int = 0
    show_banner: bool = False
    open_browser: bool = False
    interactive: bool = False
    created_env: bool = False
    env_repairs: List[str] = field(default_factory=list)
    admin_password: str = ""
    installed: bool = False
    args: List[str] = field(default_factory=list)


def _parse_port(value: str) -> int:
    try:
        port = int(value)
    except ValueError:
        _exit(f"--port must be a number, got {value!r}", 2)
    if not 1 <= port <= 65535:
        _exit(f"--port must be between 1 and 65535, got {port}", 2)
    return port


def _interactive() -> bool:
    try:
        return sys.stdin is not None and sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def _exit(message: str, code: int = 2) -> "None":
    print(message, file=sys.stderr, flush=True)
    raise SystemExit(code)


def prepare(argv: Optional[Sequence[str]] = None) -> Startup:
    """Do every one-off setup step, then return. Called at the top of main.py.

    May replace the current process with the virtualenv interpreter, and may
    exit (``--check``, ``--help``, or an unfixable environment).
    """
    args = list(sys.argv[1:] if argv is None else argv)
    startup = Startup(interactive=_interactive(), args=args)

    force_setup = False
    no_install = os.environ.get("FIXIIS_NO_SETUP") == "1"
    no_venv = os.environ.get("FIXIIS_NO_VENV") == "1"
    host: Optional[str] = None
    port: Optional[int] = None
    check = False
    fix_env = False
    set_password = False
    reset_password = False

    index = 0
    while index < len(args):
        arg = args[index]
        if arg in ("-h", "--help"):
            _say(HELP.strip() or "python main.py")
            raise SystemExit(0)
        if arg == "--check":
            check = True
        elif arg == "--fix-env":
            fix_env = True
        elif arg == "--set-password":
            set_password = True
        elif arg in ("--reset-password", "--default-password"):
            reset_password = True
        elif arg == "--ignore-env":
            os.environ["FIXIIS_IGNORE_ENV"] = "1"
        elif arg == "--setup":
            force_setup = True
        elif arg == "--no-install":
            no_install = True
        elif arg == "--open":
            startup.open_browser = True
        elif arg == "--network":
            host = "0.0.0.0"
        elif arg == "--local":
            host = "127.0.0.1"
        elif arg in ("--port", "--host"):
            if index + 1 >= len(args):
                _exit(f"{arg} needs a value", 2)
            value = args[index + 1]
            index += 1
            if arg == "--port":
                port = _parse_port(value)
            else:
                host = value
        elif arg.startswith("--port=") or arg.startswith("--host="):
            key, _, value = arg.partition("=")
            if key == "--port":
                port = _parse_port(value)
            else:
                host = value
        else:
            _exit(f"unknown option: {arg}\nRun 'python main.py --help' for the list.", 2)
        index += 1

    # 1. Work from the code directory, whatever folder the command came from.
    try:
        os.chdir(ROOT)
    except OSError:
        pass

    # 2. CLI overrides win over .env, and must be in os.environ *before*
    #    config.py is imported (which happens after this function returns).
    if port is not None:
        os.environ["PORT"] = str(port)
    if host is not None:
        os.environ["HOST"] = host

    if check:
        raise SystemExit(diagnose(port=port))

    if fix_env:
        changes = repair_env_file()
        if changes:
            _say("  .env repaired:")
            for change in changes:
                _say(f"    - {change}")
        else:
            _say("  .env already looks correct - nothing to fix")
        _say("")
        _say("Restart the app for the changes to apply: python main.py")
        raise SystemExit(0)

    if reset_password:
        if not clear_admin_password(stream=sys.stdout):
            _exit("Could not clear the stored admin password (check .env permissions)", 2)
        _say("")
        _say("Restart the app for it to apply: python main.py")
        raise SystemExit(0)

    if set_password:
        if not set_admin_password(stream=sys.stdout):
            _exit(
                "Could not set a password (bcrypt missing?). Run:\n"
                "  python main.py --setup      # installs dependencies first",
                2,
            )
        _say("")
        _say("Restart the app for the new password to apply: python main.py")
        raise SystemExit(0)

    # 3. Data directories only. No .env is created: config.py is the source
    #    of truth, and an auto-written .env (with template placeholders in it)
    #    is exactly what broke HOST/SERVER_DOMAIN/login for people before.
    #    A .env that the operator made is healed only when it is broken.
    ensure_dirs()
    if ENV_FILE.exists() and not env_is_ignored():
        for change in repair_env_file():
            startup.env_repairs.append(change)
            _say(f"  .env: {change}")

    if force_setup:
        os.environ.setdefault("FIXIIS_AUTO_SETUP", "1")
    auto_setup = force_setup or os.environ.get("FIXIIS_AUTO_SETUP") == "1"

    # Permission to change the machine: an interactive shell, --setup, or
    # FIXIIS_AUTO_SETUP=1. A systemd start has none of those, so a production
    # box never creates environments or runs pip behind the operator's back.
    allow_setup = startup.interactive or auto_setup

    # 4. Python version gate, before anything cryptic fails later.
    if sys.version_info < MIN_PYTHON:
        _exit(
            f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ is required, this is "
            f"{sys.version_info.major}.{sys.version_info.minor}.\n"
            "Install a newer Python and run the same command again.",
            2,
        )

    # 5. Re-exec into the project virtualenv, if there is one.
    venv = find_venv()
    already_setup = os.environ.get(ENV_STARTED) == "1"
    if venv and not running_in(venv) and not no_venv and not already_setup:
        os.environ[ENV_STARTED] = "1"
        python = _venv_python(venv)
        _say(f"  using {venv.name}/ - {python}")
        try:
            os.execv(str(python), [str(python), str(MAIN), *args])
        except OSError:
            os.environ.pop(ENV_STARTED, None)

    missing = missing_packages()

    # 6. No virtualenv and nothing installed: build one, then come back in.
    if missing and not venv and not no_venv and not already_setup and allow_setup:
        dest = ROOT / ".venv"
        _say("  first run: creating .venv ...")
        if create_venv(dest):
            os.environ[ENV_STARTED] = "1"
            python = _venv_python(dest)
            _say(f"  created .venv/ - {python}")
            try:
                os.execv(str(python), [str(python), str(MAIN), *args])
            except OSError:
                os.environ.pop(ENV_STARTED, None)
        else:
            _say("  could not create .venv (python3-venv missing?) - continuing with this Python")

    # 7. Install whatever is still missing, when we are allowed to.
    #    Only a *critical* package is allowed to stop the boot: a missing
    #    optional extra must never turn into a restart loop under systemd.
    if missing:
        installed_now = False
        critical_missing = [p for p in missing if p.lower() in CRITICAL]
        # pip only runs when the app cannot start without it, or when the
        # operator explicitly asked (--setup). A long pip run on every boot
        # is how a "restart" turns into minutes of downtime.
        if allow_setup and not no_install and (critical_missing or force_setup):
            _say(f"  {len(missing)} package(s) missing: {', '.join(missing)}")
            installed_now = install_requirements(sys.executable, ROOT)
            startup.installed = installed_now
            missing = missing_packages()
            if missing and not installed_now:
                _say("  could not install them (no network, or pip refused)")

        critical = [p for p in missing if p.lower() in CRITICAL]
        if critical:
            _exit(_install_hint(critical), 2)
        if missing:
            _say(f"  continuing without optional package(s): {', '.join(missing)}")

    startup.host = host_from_env()
    startup.port = port_from_env()
    startup.show_banner = (
        os.environ.get("FIXIIS_BANNER") == "1" or startup.interactive
    )
    return startup


def _install_hint(missing: Sequence[str]) -> str:
    python = sys.executable
    return (
        "Missing Python packages: " + ", ".join(missing) + "\n\n"
        "Install them with one of:\n"
        f"  {python} main.py --setup\n"
        f"  {python} -m pip install -r requirements.txt"
    )


if __name__ == "__main__":  # `python bootstrap.py` behaves like --check
    raise SystemExit(diagnose())
