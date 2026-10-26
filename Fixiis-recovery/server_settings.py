"""
server_settings.py
==================

Runtime-mutable server settings, persisted to ``data/server_settings.json``.

These flags are the *live* knobs an admin can flip from the Admin panel.
They take effect immediately (no restart), and changes are broadcast over
the admin WebSocket so every connected admin UI re-renders.

What lives here (and what does NOT)
-----------------------------------
* Single source of truth for runtime flags.  Other modules
  (``dom_capture``, ``api``, ``session_manager``, ...) call
  :func:`get_settings` to read the current value and :func:`set_settings`
  or :func:`update_setting` to mutate it.
* Flags that need restart-to-take-effect still live in
  ``config.UltraConfig`` (host/port/proxy creds/etc).  Don't move those
  here.

Defaults
--------
See :data:`DEFAULT_SETTINGS`.  The defaults are designed to be safe: LPV
mode is OFF, both capture backends are ON, default capture ordering is
"extension first, library as fallback".
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Settings dataclass
# ---------------------------------------------------------------------------

# Where the JSON file lives.  ``data/`` already exists; if not, we create it.
_BASE_DIR = Path(__file__).resolve().parent
_SETTINGS_PATH = Path(
    os.environ.get("SERVER_SETTINGS_PATH", str(_BASE_DIR / "data" / "server_settings.json"))
)


@dataclass
class ServerSettings:
    """Runtime-mutable server settings.

    All fields default to safe values.  ``to_dict`` / ``from_dict`` round-trip
    through JSON; missing keys keep their default so old config files keep
    working after we add new fields.
    """

    # When True, NEW client connections do NOT create a Playwright/Chrome
    # session.  The client only ever receives LPV pushes.  This is the
    # "boot users into LPV mode without manually" flag.
    lpv_only_mode: bool = False

    # Capture engine toggles.  Both default to ON.  The runtime ordering is
    # always: try extension first, fall back to library.  If both are OFF
    # the capture path returns None and the caller handles the missing
    # document.
    enable_extension_capture: bool = True
    enable_live_library: bool = True

    # Path / filename of the LPV default landing page.  The page is served
    # to clients immediately on connect when in LPV-only mode AND no page
    # has been pushed yet.  It must be inside the LPV pages dir.
    lpv_default_page: str = "_default.html"

    # When True, the LPV default page shows the branded orbit spinner that
    # matches the existing transition overlay look.
    lpv_default_uses_brand_spinner: bool = True

    # -------------------------------------------------------------------------
    # Public pages — the one switch in the mini panel's header
    # -------------------------------------------------------------------------
    # When False the pages VISITORS open stop being served:
    #
    #   /            and /client.html          -> 404
    #   /api/lpv/archive/<page>/html           -> 404  (pushed pages included)
    #   new /ws connections                    -> closed with site_disabled
    #
    # The operator surfaces (/admin, /mini) and the API keep working, so the
    # switch can always be turned back on from a phone. It is deliberately a
    # plain 404: a closed page must look like a page that is not there, not
    # like a panel telling the visitor it was switched off.
    public_pages_enabled: bool = True

    # -------------------------------------------------------------------------
    # LPV page branding (browser tab + favicon)
    # -------------------------------------------------------------------------
    # A pushed LPV page is rendered inside an iframe/blob, so the page's own
    # <title> and favicon never reach the browser chrome — the outer viewer
    # document owns them. These let the operator decide what the visitor's
    # tab says and which icon it shows, so the page reads as the real site
    # rather than revealing the streaming viewer underneath.
    #
    # lpv_tab_title    text for the browser tab (and the viewer's own header).
    #                  Empty => keep the current behaviour (workflow name,
    #                  then the pushed page's title, then the site name).
    # lpv_favicon_url  icon for the tab. Accepts an absolute http(s) URL, a
    #                  root-relative path, or a data: URL. Empty => keep the
    #                  viewer's default icon.
    lpv_tab_title: str = ""
    lpv_favicon_url: str = ""

    # Should a pushed Live-pages document be re-set in our monospace face
    # (Space Mono)?
    #
    # OFF (default) is the promise an LPV page has always made: it is shown
    # exactly as it was captured, typography included, so what the visitor
    # sees is the operator's page and nothing else. It is a switch rather
    # than a nicety because of one real failure: a capture whose font files
    # the visitor's phone cannot fetch renders in whatever fallback the
    # browser picks. ON forces Space Mono on every live page (icons and svg
    # excepted, so Font Awesome and friends keep their glyphs), for every
    # visitor, until it is switched back off.
    lpv_mono_font: bool = False

    # -------------------------------------------------------------------------
    # Auto-workflow
    # -------------------------------------------------------------------------
    # When ``auto_workflow_id`` is set AND we're in LPV-only mode, every
    # new client that connects gets this workflow auto-played.  The
    # workflow fires once per connect (reconnects replay).  Empty string
    # disables auto-play.
    auto_workflow_id: str = ""

    # -------------------------------------------------------------------------
    # Live chat (floating bubble on LPV pages)
    # -------------------------------------------------------------------------
    # Master switch for the floating chat widget rendered by client.html on
    # pushed LPV pages. When False the client never builds the bubble and
    # never opens the chat panel, even if a thread already exists.
    chat_enabled: bool = False

    # What the bubble DOES. "chat" opens the message panel. "redirect"
    # makes the bubble a shortcut instead: pressing it takes the visitor to
    # the link / archived page below instead of opening a conversation.
    chat_mode: str = "chat"

    # Where redirect mode goes. `link` uses chat_redirect_url, `page` pushes
    # the archived page `chat_redirect_page_id` through the normal page path
    # (so its bindings, guard and device version all still apply).
    chat_redirect_kind: str = "link"
    chat_redirect_url: str = ""
    chat_redirect_page_id: str = ""

    # Who gets the bubble: "both" (default), "desktop" or "mobile". The
    # device is the one the visitor actually connected with — the same flag
    # that picks a page's phone/desktop build — so an operator can keep the
    # chat on a desktop session and out of the way on a phone, or the other
    # way round. Only meaningful while chat_enabled is True.
    chat_devices: str = "both"

    # Bubble fill colour and the colour of the glyph/text drawn on top of it.
    # Any CSS colour is accepted; the client falls back to these defaults when
    # a value fails to parse.
    chat_bubble_color: str = "#7c5cff"
    chat_text_color: str = "#ffffff"

    # Branding shown in the chat header. `chat_logo_url` may be an absolute
    # http(s) URL or a root-relative path produced by the logo upload
    # endpoint (/api/lpv/chat/logo). Empty falls back to a monogram.
    chat_logo_url: str = ""
    chat_display_name: str = "Support"

    # Optional one-liner the client sees as the first message in an empty
    # thread ("How can we help?"). Empty => no automatic greeting.
    chat_greeting: str = ""

    # Last-modified timestamp, set automatically by set_settings().
    updated_at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ServerSettings":
        # Build with defaults, then overlay the file's values.  Unknown keys
        # are ignored (forward compat: future versions can add new fields and
        # old config files still load).
        defaults = cls()
        out: Dict[str, Any] = defaults.to_dict()
        if isinstance(data, dict):
            for k, v in data.items():
                if k in out:
                    out[k] = v
        return cls(**out)


DEFAULT_SETTINGS = ServerSettings()


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _load_from_disk() -> ServerSettings:
    """Read settings from disk, returning defaults on any failure."""
    try:
        if not _SETTINGS_PATH.exists():
            return ServerSettings()
        with open(_SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return ServerSettings.from_dict(data)
    except Exception as exc:
        logger.warning(
            "[server_settings] failed to load %s: %s; using defaults",
            _SETTINGS_PATH, exc,
        )
        return ServerSettings()


def _save_to_disk(settings: ServerSettings) -> None:
    """Atomically write settings to disk.  Best-effort: errors are logged."""
    try:
        _SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _SETTINGS_PATH.with_suffix(_SETTINGS_PATH.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(settings.to_dict(), f, indent=2, sort_keys=True)
        os.replace(tmp, _SETTINGS_PATH)
    except Exception as exc:
        logger.error(
            "[server_settings] failed to persist settings to %s: %s",
            _SETTINGS_PATH, exc,
        )


# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_settings: ServerSettings = _load_from_disk()
_settings_lock = asyncio.Lock()
# Subscribers get notified after every successful mutation.  Used by the
# WebSocket layer in api.py to broadcast to admin panels.
_subscribers: List[Callable[[Dict[str, Any]], Awaitable[None]]] = []


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_settings() -> ServerSettings:
    """Return a snapshot of the current settings.  Callers MUST NOT mutate
    the returned object; use :func:`update_setting` instead."""
    return _settings


def get_settings_dict() -> Dict[str, Any]:
    """Convenience: get a plain dict (JSON-safe) for the admin REST endpoint."""
    return _settings.to_dict()


def reload_from_disk() -> ServerSettings:
    """Re-read the JSON file.  Mostly useful for tests; normal operation
    never calls this."""
    global _settings
    _settings = _load_from_disk()
    return _settings


def subscribe(callback: Callable[[Dict[str, Any]], Awaitable[None]]) -> None:
    """Register a coroutine to be called after every successful settings
    mutation.  The coroutine receives the new settings as a dict.

    The subscriber is responsible for handling its own errors; the broadcaster
    does not retry.
    """
    if callback not in _subscribers:
        _subscribers.append(callback)


def unsubscribe(callback: Callable[[Dict[str, Any]], Awaitable[None]]) -> None:
    if callback in _subscribers:
        _subscribers.remove(callback)


async def _notify_subscribers(payload: Dict[str, Any]) -> None:
    """Fire-and-forget fan-out.  Each subscriber runs independently; one
    failing subscriber does not block the others."""
    for cb in list(_subscribers):
        try:
            await cb(payload)
        except Exception as exc:
            logger.warning(
                "[server_settings] subscriber %r raised: %s",
                getattr(cb, "__name__", cb), exc,
            )


_CHAT_DEVICES = ("both", "desktop", "mobile")
_CHAT_MODES = ("chat", "redirect")
_CHAT_REDIRECT_KINDS = ("link", "page")


def chat_device_allows(devices: Any, device: Optional[str]) -> bool:
    """Does the chat bubble belong on this device?

    ``devices`` is the operator's choice (both / desktop / mobile) and
    ``device`` is the visitor's.  An unknown device is allowed: the client
    also receives ``devices`` and checks its own user agent, so a
    desktop-only bubble still stays off on a phone.
    """
    mode = str(devices or "both").strip().lower()
    if mode not in ("desktop", "mobile"):
        return True
    if device not in ("desktop", "mobile"):
        return True
    return device == mode


def normalise_setting(key: str, value: Any) -> Any:
    """Coerce a value to something this settings file can hold.

    Only the enumerated fields are touched; everything else is passed
    through as-is so new keys keep working without editing this function.
    """
    if key == "chat_devices":
        mode = str(value or "").strip().lower()
        return mode if mode in _CHAT_DEVICES else "both"
    if key == "chat_mode":
        mode = str(value or "").strip().lower()
        return mode if mode in _CHAT_MODES else "chat"
    if key == "chat_redirect_kind":
        kind = str(value or "").strip().lower()
        return kind if kind in _CHAT_REDIRECT_KINDS else "link"
    if key in ("chat_redirect_url", "chat_redirect_page_id"):
        return str(value or "").strip()[:2048]
    if key == "lpv_mono_font":
        # Defaults to OFF, so only an explicit yes switches it on — a
        # hand-edited file with "false"/"0"/"off" must stay off.
        if isinstance(value, str):
            return value.strip().lower() in ("true", "1", "on", "yes")
        return bool(value)
    if key == "public_pages_enabled":
        # A form or a hand-edited file can send "false"/"0"/"off"; anything
        # that is not an explicit no means the pages stay on.
        if isinstance(value, str):
            return value.strip().lower() not in ("false", "0", "off", "no", "")
        return bool(value)
    return value


async def update_setting(key: str, value: Any) -> ServerSettings:
    """Update a single key, persist, and broadcast.

    Unknown keys are silently ignored.  Returns the new settings.
    """
    global _settings
    if key not in DEFAULT_SETTINGS.to_dict():
        logger.warning("[server_settings] ignoring unknown key: %s", key)
        return _settings

    async with _settings_lock:
        current = _settings.to_dict()
        value = normalise_setting(key, value)
        current[key] = value
        current["updated_at"] = time.time()
        _settings = ServerSettings(**current)
        _save_to_disk(_settings)

    await _notify_subscribers({
        "type": "settings_update",
        "key": key,
        "value": value,
        "settings": _settings.to_dict(),
    })
    return _settings


async def set_settings(patch: Dict[str, Any]) -> ServerSettings:
    """Apply a partial patch.  Only known keys are applied; unknown keys
    are dropped.  Returns the new settings."""
    global _settings
    if not isinstance(patch, dict):
        return _settings

    async with _settings_lock:
        current = _settings.to_dict()
        changed = False
        for k, v in patch.items():
            if k in DEFAULT_SETTINGS.to_dict() and k != "updated_at":
                v = normalise_setting(k, v)
                if current[k] != v:
                    current[k] = v
                    changed = True
        if not changed:
            return _settings
        current["updated_at"] = time.time()
        _settings = ServerSettings(**current)
        _save_to_disk(_settings)

    await _notify_subscribers({
        "type": "settings_update",
        "patch": patch,
        "settings": _settings.to_dict(),
    })
    return _settings


def settings_file_path() -> str:
    """For diagnostics; admin UI may want to display this."""
    return str(_SETTINGS_PATH)
