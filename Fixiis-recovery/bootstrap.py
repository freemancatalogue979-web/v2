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
    python main.py --setup         # force venv creation + pip install
    python main.py --no-install    # never install; print instructions
    python main.py --network       # listen on this machine's network (0.0.0.0)
    python main.py --local         # listen on 127.0.0.1 only
    python main.py --port 9000     # override PORT for this run
    python main.py --host 192.168.0.14   # bind one specific address
    python main.py --open          # open the admin page when it is up

Environment overrides
---------------------
    FIXIIS_NO_SETUP=1     same as --no-install
    FIXIIS_AUTO_SETUP=1   same as --setup        (for non-interactive first run)
    FIXIIS_NO_VENV=1      never re-exec into or create a virtualenv
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
MINIMAL_ENV = """\
# Fixiis / Neo Browser Stream - created automatically by main.py.
# Edit freely; this file is never overwritten once it exists.
ADMIN_USERNAME=admin
HOST=0.0.0.0
PORT=8446
BROWSER_BACKEND=sb
"""


def ensure_env_file(root: Path = ROOT) -> bool:
    """Create ``.env`` on first run. Returns True when it was created.

    The file is only written when absent, so a restart never rewrites it
    (the deploy watcher would otherwise restart the app forever).
    """
    target = root / ".env"
    if target.exists():
        return False
    template = root / ".env.example"
    try:
        if template.exists():
            target.write_bytes(template.read_bytes())
        else:
            target.write_text(MINIMAL_ENV, encoding="utf-8")
        try:
            target.chmod(0o600)
        except OSError:
            pass
    except OSError:
        return False
    return True


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

    env_exists = (ROOT / ".env").exists()
    line(
        ".env",
        "present" if env_exists else "missing (created on next start)",
        True,
        "",
    )

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

    host = host_from_env()
    if is_loopback(host):
        line(
            "bind address",
            f"{host} - this machine only",
            warn=True,
            hint=(
                "other devices (phones, laptops, other PCs) cannot connect. "
                "Use: python main.py --host 0.0.0.0, or set HOST=0.0.0.0 in .env"
            ),
        )
    else:
        where = "all interfaces" if host in ("", "0.0.0.0", "::") else host
        machine = lan_ip()
        reach = f" -> http://{machine}:{port}/admin" if machine else ""
        line("bind address", f"{where} (network reachable){reach}")

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
def banner(
    host: str,
    port: int,
    tunnel: str = "",
    stream: Optional[TextIO] = None,
    machine_ip: Optional[str] = None,
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

    index = 0
    while index < len(args):
        arg = args[index]
        if arg in ("-h", "--help"):
            _say(HELP.strip() or "python main.py")
            raise SystemExit(0)
        if arg == "--check":
            check = True
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

    # 3. First-run files: .env and the data directories. Done before anything
    #    that can fail, so a half-finished install still leaves a config to
    #    edit and somewhere for the app to write.
    if ensure_env_file():
        startup.created_env = True
        _say("  created .env from the template - edit it to set your passwords")
    ensure_dirs()

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
    if missing and not no_install:
        if allow_setup:
            _say(f"  {len(missing)} package(s) missing: {', '.join(missing)}")
            if install_requirements(sys.executable, ROOT):
                startup.installed = True
                still = missing_packages()
                if still:
                    _say(f"  still missing after install: {', '.join(still)}")
                    _exit(_install_hint(still), 2)
            else:
                _exit(_install_hint(missing), 2)
        else:
            _exit(_install_hint(missing), 2)
    elif missing:
        _exit(_install_hint(missing), 2)

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
