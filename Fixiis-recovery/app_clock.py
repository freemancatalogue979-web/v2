"""The server's clock, and the timezone an operator chose for it.

Every timestamp this server writes is *its* local time: the Telegram alerts,
the audit trail, the log lines, the session metadata. On a box in UTC that is
the wrong clock for the person reading the alerts, and until now the only way
to change it was to change the container's timezone and restart.

Settings -> Clock -> Timezone fixes that, live:

    app_clock.apply("Africa/Lagos")     # now; no restart
    app_clock.stamp()                   # 2026-10-26 23:41:07

Three kinds of value are accepted, because operators describe their clock in
three different ways:

    "Africa/Lagos"     an IANA zone — the right answer, it knows about DST
    "+01:00" / "GMT-5" a fixed offset that never shifts
    ""                 "server default" — leave the process alone

Applying does two things, so *everything* follows and not just our own
formatter:

  * the process environment is given ``TZ`` and ``time.tzset()`` is called, so
    ``time.strftime``, ``time.localtime`` and the logging formatter move too
    (that is what keeps a log line and a Telegram alert on the same minute);
  * a ``zoneinfo`` (or fixed-offset) tzinfo is kept here, so
    :func:`stamp` is correct even on a platform without ``tzset``.

Nothing here reads ``server_settings`` — the settings module calls
:func:`apply` — so there is no import cycle and the helpers stay testable.
"""

from __future__ import annotations

import os
import re
import time
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any, Dict, List, Optional

try:  # Python 3.9+
    from zoneinfo import ZoneInfo, available_timezones
except Exception:  # pragma: no cover - very old interpreter
    ZoneInfo = None  # type: ignore[assignment]
    available_timezones = None  # type: ignore[assignment]

STAMP_FORMAT = "%Y-%m-%d %H:%M:%S"

# How an operator says "just use whatever the machine has".
_AUTO_WORDS = {"", "auto", "server", "server default", "default", "local", "system"}

# "+01:00", "-0500", "GMT-5", "UTC+2"
_OFFSET_RE = re.compile(r"^(?:UTC|GMT)?\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?$", re.IGNORECASE)

_MAX_OFFSET = timedelta(hours=14)

_state: Dict[str, Any] = {"name": "", "tz": None}


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _offset_from_match(match: "re.Match[str]") -> Optional[str]:
    sign = 1 if match.group(1) == "+" else -1
    hours = int(match.group(2))
    minutes = int(match.group(3) or 0)
    if hours > 14 or minutes > 59:
        return None
    delta = timedelta(hours=hours, minutes=minutes) * sign
    if abs(delta) > _MAX_OFFSET:
        return None
    total = int(delta.total_seconds()) // 60
    sign_txt = "+" if total >= 0 else "-"
    total = abs(total)
    return "%s%02d:%02d" % (sign_txt, total // 60, total % 60)


def _zone_for(name: str) -> Optional[tzinfo]:
    """The tzinfo for a stored value (``""`` means "no override")."""
    key = normalise(name)
    if not key:
        return None
    offset = _OFFSET_RE.match(key)
    if offset:
        sign = 1 if offset.group(1) == "+" else -1
        return timezone(sign * timedelta(hours=int(offset.group(2)),
                                        minutes=int(offset.group(3) or 0)))
    if ZoneInfo is None:
        return None
    try:
        return ZoneInfo(key)
    except Exception:
        return None


def normalise(name: Any) -> str:
    """The value to store for what the operator typed (``""`` = server).

    Junk answers ``""`` rather than raising: this runs inside a settings save,
    where throwing would turn one bad string into a failed request. Use
    :func:`explain` when you want to tell somebody why their value was
    rejected.
    """
    raw = str(name or "").strip()
    if raw.lower() in _AUTO_WORDS:
        return ""
    offset = _OFFSET_RE.match(raw)
    if offset:
        return _offset_from_match(offset) or ""
    if ZoneInfo is not None:
        try:
            ZoneInfo(raw)
            return raw
        except Exception:
            # Case-insensitive second try ("africa/lagos").
            for candidate in known_zones():
                if candidate.lower() == raw.lower():
                    return candidate
    return ""


def explain(name: Any) -> Dict[str, Any]:
    """``{ok, stored, message}`` — for an API answer or a test."""
    raw = str(name or "").strip()
    if raw.lower() in _AUTO_WORDS:
        return {"ok": True, "stored": "", "message": "server default"}
    stored = normalise(raw)
    if stored:
        return {"ok": True, "stored": stored, "message": ""}
    return {
        "ok": False,
        "stored": "",
        "message": "'%s' is not a timezone I know — use an IANA name like "
                   "'Africa/Lagos' or an offset like '+01:00'" % (raw,),
    }


def is_valid(name: Any) -> bool:
    raw = str(name or "").strip()
    return raw.lower() in _AUTO_WORDS or bool(normalise(raw))


def known_zones() -> List[str]:
    """Every IANA zone this machine knows, sorted (empty when unavailable)."""
    try:
        if available_timezones is None:
            return []
        return sorted(available_timezones())
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------

def _posix_for_offset(key: str) -> str:
    """POSIX TZ string for a fixed offset.

    libc inverts the sign: ``TZ=UTC-5:30`` means *five and a half hours east*
    of UTC, i.e. ``+05:30``. Getting this backwards would leave the log lines
    an offset away from the alerts, which is exactly the bug this module
    exists to remove.
    """
    match = _OFFSET_RE.match(key)
    if not match:
        return key
    east = 1 if match.group(1) == "+" else -1
    hours = int(match.group(2))
    minutes = int(match.group(3) or 0)
    text = "%s%d" % ("-" if east > 0 else "", hours)
    if minutes:
        text += ":%02d" % minutes
    return "UTC" + text


def _tzset() -> None:
    try:
        time.tzset()
    except Exception:  # Windows: no tzset; stamp() still uses the tzinfo
        pass


def apply(name: Any = None) -> Dict[str, Any]:
    """Make ``name`` the server's timezone, now. Returns :func:`describe`."""
    key = normalise(name)
    zone = _zone_for(key)
    try:
        if key and zone is not None:
            os.environ["TZ"] = key if not _OFFSET_RE.match(key) else _posix_for_offset(key)
        else:
            # "" (or something unusable): hand the process back to the machine.
            os.environ.pop("TZ", None)
            key = "" if zone is None else key
    except Exception:
        pass
    _tzset()
    _state["name"] = key
    _state["tz"] = zone
    return describe()


def current_name() -> str:
    return str(_state.get("name") or "")


def current_tz() -> Optional[tzinfo]:
    return _state.get("tz")


def offset_now() -> timedelta:
    zone = current_tz()
    if zone is None:
        return datetime.now().astimezone().utcoffset() or timedelta(0)
    return datetime.now(zone).utcoffset() or timedelta(0)


def now() -> datetime:
    zone = current_tz()
    return datetime.now(zone) if zone is not None else datetime.now()


def stamp(when: Optional[float] = None, fmt: str = STAMP_FORMAT) -> str:
    """One timestamp, in the operator's timezone, to the second."""
    try:
        moment = datetime.fromtimestamp(float(when), current_tz()) if when else now()
    except Exception:
        moment = now()
    return moment.strftime(fmt)


def tz_abbreviation() -> str:
    try:
        zone = current_tz()
        if zone is not None:
            return datetime.now(zone).tzname() or ""
        return time.tzname[0]
    except Exception:
        return ""


def describe() -> Dict[str, Any]:
    """What the panel shows: the choice, the real offset, and the clock now."""
    zone = current_tz()
    name = current_name()
    try:
        offset = offset_now()
        minutes = int(offset.total_seconds()) // 60
        sign = "+" if minutes >= 0 else "-"
        minutes = abs(minutes)
        offset_text = "%s%02d:%02d" % (sign, minutes // 60, minutes % 60)
    except Exception:
        offset_text = "+00:00"
    return {
        "name": name,                                  # "" = the server's own
        "source": "admin" if name else "server",
        "command": os.environ.get("TZ", ""),           # what the process uses
        "offset": offset_text,
        "tzname": tz_abbreviation(),
        "now": stamp(),
        "format": STAMP_FORMAT,
    }
