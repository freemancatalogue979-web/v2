"""
Construct Store - Storage layer for Constructs, Archive Pages, and Workflow Steps.

Three tables, three jobs:

    construct_archive
        Archive of custom HTML pages an operator can attach to workflow steps.
        Stored on disk under config.CONSTRUCT_ARCHIVE_DIR; the DB row points
        at the file and keeps display metadata (name, description, tags,
        url_mapping, version).

    construct_workflows
        One row per Construct workflow. A workflow is a step-based program
        that connects custom HTML interfaces to remote browser actions.
        Tracks the active step, current state, and associated session.

    construct_steps
        One row per workflow step. Each step defines:
        - A browser action (navigate, click, input, wait, etc.)
        - An attached HTML page from the archive (shown to client)
        - Element mappings between custom HTML and real browser elements
        - Navigation actions, input definitions, and transition rules

    construct_sessions
        One row per active Construct runtime session. Tracks the current
        workflow step, client connection, remote browser session binding,
        and execution state.

    construct_audit
        Append-only event log: step transitions, action executions,
        client events, engine actions. Indexed by (session_id, timestamp).

We use SQLite (stdlib) with WAL journaling.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from config import CONSTRUCT_DB_PATH, CONSTRUCT_ARCHIVE_DIR


# ---------------------------------------------------------------------------
# Connection management
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_initialized = False


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(
        CONSTRUCT_DB_PATH,
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
    """Thread-safe connection context."""
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


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS construct_archive (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    filename TEXT NOT NULL,
    html TEXT NOT NULL,
    description TEXT DEFAULT '',
    tags TEXT DEFAULT '[]',
    url_mapping TEXT DEFAULT '[]',        -- JSON list of URL patterns this page maps to
    version INTEGER DEFAULT 1,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS construct_workflows (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    target_url TEXT DEFAULT '',           -- The real website URL this construct targets
    status TEXT DEFAULT 'draft',          -- draft, active, archived
    current_step_id TEXT DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS construct_steps (
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    step_order INTEGER NOT NULL,
    step_name TEXT NOT NULL,
    action_type TEXT NOT NULL,            -- navigate, click, input, wait, navigate_and_wait, custom
    browser_action TEXT DEFAULT '',       -- e.g. "navigate_to", "click_signin", "fill_form"
    target_selector TEXT DEFAULT '',      -- CSS selector on the real browser page
    attached_page_id TEXT DEFAULT NULL,   -- References construct_archive.id (NULL = no page)
    attached_page_name TEXT DEFAULT '',
    element_mappings TEXT DEFAULT '[]',   -- JSON list of {custom_id, browser_selector, action, field_type}
    input_values TEXT DEFAULT '{}',       -- JSON of default input values
    wait_conditions TEXT DEFAULT '[]',    -- JSON list of wait conditions
    transition_to TEXT DEFAULT '',        -- Next step id on success
    transition_on_failure TEXT DEFAULT '', -- Next step id on failure
    is_entry_point INTEGER DEFAULT 0,
    is_identical INTEGER DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    FOREIGN KEY (workflow_id) REFERENCES construct_workflows(id) ON DELETE CASCADE,
    FOREIGN KEY (attached_page_id) REFERENCES construct_archive(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS construct_sessions (
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    client_id TEXT DEFAULT '',
    remote_session_id TEXT DEFAULT '',    -- References the remote browser session
    current_step_id TEXT DEFAULT '',
    status TEXT DEFAULT 'initializing',   -- initializing, active, paused, completed, error
    step_history TEXT DEFAULT '[]',       -- JSON list of {step_id, entered_at, action, result}
    variables TEXT DEFAULT '{}',          -- JSON of runtime variables
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    FOREIGN KEY (workflow_id) REFERENCES construct_workflows(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS construct_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    step_id TEXT DEFAULT '',
    event_type TEXT NOT NULL,             -- step_entered, action_executed, client_event, engine_action, transition, error
    event_data TEXT DEFAULT '{}',         -- JSON
    timestamp REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_construct_steps_workflow ON construct_steps(workflow_id);
CREATE INDEX IF NOT EXISTS idx_construct_sessions_workflow ON construct_sessions(workflow_id);
CREATE INDEX IF NOT EXISTS idx_construct_audit_session ON construct_audit(session_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_construct_audit_type ON construct_audit(event_type);
"""


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(SCHEMA)
        # Migration: add is_identical column to existing construct_steps tables
        try:
            conn.execute("ALTER TABLE construct_steps ADD COLUMN is_identical INTEGER DEFAULT 0")
        except Exception:
            # Column already exists — ignore
            pass


# ---------------------------------------------------------------------------
# Archive pages
# ---------------------------------------------------------------------------

def save_archive_page(
    name: str,
    html: str,
    *,
    description: str = "",
    tags: Optional[List[str]] = None,
    url_mapping: Optional[List[str]] = None,
    page_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Insert or update an archive page. Returns the page row."""
    now = time.time()
    pid = page_id or str(uuid.uuid4())
    tags_json = json.dumps(tags or [])
    mapping_json = json.dumps(url_mapping or [])

    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM construct_archive WHERE id = ?", (pid,)
        ).fetchone()
        if existing:
            conn.execute(
                """UPDATE construct_archive
                   SET name=?, html=?, description=?, tags=?, url_mapping=?,
                       version=version+1, updated_at=?
                   WHERE id=?""",
                (name, html, description, tags_json, mapping_json, now, pid),
            )
        else:
            conn.execute(
                """INSERT INTO construct_archive
                   (id, name, filename, html, description, tags, url_mapping,
                    version, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (pid, name, f"{name}.html", html, description, tags_json,
                 mapping_json, 1, now, now),
            )
        row = conn.execute(
            "SELECT * FROM construct_archive WHERE id=?", (pid,)
        ).fetchone()
        return _row_to_dict(row)


def get_archive_page(page_id: str) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM construct_archive WHERE id=?", (page_id,)
        ).fetchone()
        return _row_to_dict(row) if row else None


def get_archive_page_by_name(name: str) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM construct_archive WHERE name=? ORDER BY updated_at DESC LIMIT 1",
            (name,),
        ).fetchone()
        return _row_to_dict(row) if row else None


def list_archive_pages(
    query: str = "",
    limit: int = 200,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    with get_conn() as conn:
        if query:
            like = f"%{query}%"
            rows = conn.execute(
                """SELECT * FROM construct_archive
                   WHERE name LIKE ? OR description LIKE ? OR tags LIKE ?
                   ORDER BY updated_at DESC LIMIT ? OFFSET ?""",
                (like, like, like, limit, offset),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM construct_archive ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [_row_to_dict(r) for r in rows]


def delete_archive_page(page_id: str) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM construct_archive WHERE id=?", (page_id,)
        )
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Workflows
# ---------------------------------------------------------------------------

def save_workflow(
    name: str,
    target_url: str = "",
    *,
    description: str = "",
    workflow_id: Optional[str] = None,
    status: str = "draft",
) -> Dict[str, Any]:
    now = time.time()
    wid = workflow_id or str(uuid.uuid4())

    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM construct_workflows WHERE id = ?", (wid,)
        ).fetchone()
        if existing:
            conn.execute(
                """UPDATE construct_workflows
                   SET name=?, target_url=?, description=?, status=?, updated_at=?
                   WHERE id=?""",
                (name, target_url, description, status, now, wid),
            )
        else:
            conn.execute(
                """INSERT INTO construct_workflows
                   (id, name, description, target_url, status, current_step_id,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (wid, name, description, target_url, status, "", now, now),
            )
        row = conn.execute(
            "SELECT * FROM construct_workflows WHERE id=?", (wid,)
        ).fetchone()
        return _row_to_dict(row)


def get_workflow(workflow_id: str) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM construct_workflows WHERE id=?", (workflow_id,)
        ).fetchone()
        return _row_to_dict(row) if row else None


def list_workflows(
    query: str = "",
    limit: int = 200,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    with get_conn() as conn:
        if query:
            like = f"%{query}%"
            rows = conn.execute(
                """SELECT * FROM construct_workflows
                   WHERE name LIKE ? OR description LIKE ? OR target_url LIKE ?
                   ORDER BY updated_at DESC LIMIT ? OFFSET ?""",
                (like, like, like, limit, offset),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM construct_workflows ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [_row_to_dict(r) for r in rows]


def delete_workflow(workflow_id: str) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM construct_workflows WHERE id=?", (workflow_id,)
        )
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

def save_step(
    workflow_id: str,
    step_name: str,
    action_type: str,
    *,
    step_id: Optional[str] = None,
    step_order: int = 0,
    browser_action: str = "",
    target_selector: str = "",
    attached_page_id: Optional[str] = None,
    attached_page_name: str = "",
    element_mappings: Optional[List[Dict[str, Any]]] = None,
    input_values: Optional[Dict[str, Any]] = None,
    wait_conditions: Optional[List[Dict[str, Any]]] = None,
    transition_to: str = "",
    transition_on_failure: str = "",
    is_entry_point: bool = False,
    is_identical: bool = False,
) -> Dict[str, Any]:
    now = time.time()
    sid = step_id or str(uuid.uuid4())
    mappings_json = json.dumps(element_mappings or [])
    input_json = json.dumps(input_values or {})
    wait_json = json.dumps(wait_conditions or [])

    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM construct_steps WHERE id = ?", (sid,)
        ).fetchone()
        if existing:
            conn.execute(
                """UPDATE construct_steps
                   SET step_name=?, action_type=?, step_order=?, browser_action=?,
                       target_selector=?, attached_page_id=?, attached_page_name=?,
                       element_mappings=?, input_values=?, wait_conditions=?,
                       transition_to=?, transition_on_failure=?,
                       is_entry_point=?, is_identical=?, updated_at=?
                   WHERE id=?""",
                (
                    step_name, action_type, step_order, browser_action,
                    target_selector, attached_page_id, attached_page_name,
                    mappings_json, input_json, wait_json,
                    transition_to, transition_on_failure,
                    1 if is_entry_point else 0,
                    1 if is_identical else 0, now, sid,
                ),
            )
        else:
            conn.execute(
                """INSERT INTO construct_steps
                   (id, workflow_id, step_order, step_name, action_type,
                    browser_action, target_selector, attached_page_id,
                    attached_page_name, element_mappings, input_values,
                    wait_conditions, transition_to, transition_on_failure,
                    is_entry_point, is_identical, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    sid, workflow_id, step_order, step_name, action_type,
                    browser_action, target_selector, attached_page_id,
                    attached_page_name, mappings_json, input_json, wait_json,
                    transition_to, transition_on_failure,
                    1 if is_entry_point else 0,
                    1 if is_identical else 0, now, now,
                ),
            )
        row = conn.execute(
            "SELECT * FROM construct_steps WHERE id=?", (sid,)
        ).fetchone()
        return _row_to_dict(row)


def get_step(step_id: str) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM construct_steps WHERE id=?", (step_id,)
        ).fetchone()
        return _row_to_dict(row) if row else None


def list_steps(workflow_id: str) -> List[Dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM construct_steps WHERE workflow_id=? ORDER BY step_order ASC, step_name ASC",
            (workflow_id,),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


def delete_step(step_id: str) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM construct_steps WHERE id=?", (step_id,))
        return cur.rowcount > 0


def move_step(workflow_id: str, step_id: str, direction: str) -> bool:
    """Move a step up or down by swapping its step_order with the adjacent step."""
    with get_conn() as conn:
        # Get all steps for this workflow ordered by step_order
        rows = conn.execute(
            """SELECT id, step_order FROM construct_steps
               WHERE workflow_id=? ORDER BY step_order ASC, step_name ASC""",
            (workflow_id,),
        ).fetchall()
        if len(rows) < 2:
            return False

        step_list = [dict(r) for r in rows]
        idx = next((i for i, s in enumerate(step_list) if s["id"] == step_id), None)
        if idx is None:
            return False

        if direction == "up" and idx > 0:
            swap_idx = idx - 1
        elif direction == "down" and idx < len(step_list) - 1:
            swap_idx = idx + 1
        else:
            return False

        # Swap step_order values
        order_a = step_list[idx]["step_order"]
        order_b = step_list[swap_idx]["step_order"]
        conn.execute(
            "UPDATE construct_steps SET step_order=? WHERE id=?",
            (order_b, step_list[idx]["id"]),
        )
        conn.execute(
            "UPDATE construct_steps SET step_order=? WHERE id=?",
            (order_a, step_list[swap_idx]["id"]),
        )
        # Reassign sequential step_order values so even steps that were
        # all created with order=0 get distinct positions (0,1,2,3...)
        for i, s in enumerate(step_list):
            conn.execute(
                "UPDATE construct_steps SET step_order=? WHERE id=?",
                (i, s["id"]),
            )
        return True


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

def save_session(
    session_id: str,
    workflow_id: str,
    *,
    client_id: str = "",
    remote_session_id: str = "",
    current_step_id: str = "",
    status: str = "initializing",
) -> Dict[str, Any]:
    now = time.time()
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM construct_sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if existing:
            conn.execute(
                """UPDATE construct_sessions
                   SET client_id=?, remote_session_id=?, current_step_id=?,
                       status=?, updated_at=?
                   WHERE id=?""",
                (client_id, remote_session_id, current_step_id, status, now, session_id),
            )
        else:
            conn.execute(
                """INSERT INTO construct_sessions
                   (id, workflow_id, client_id, remote_session_id, current_step_id,
                    status, step_history, variables, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (session_id, workflow_id, client_id, remote_session_id,
                 current_step_id, status, "[]", "{}", now, now),
            )
        row = conn.execute(
            "SELECT * FROM construct_sessions WHERE id=?", (session_id,)
        ).fetchone()
        return _row_to_dict(row)


def get_session(session_id: str) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM construct_sessions WHERE id=?", (session_id,)
        ).fetchone()
        return _row_to_dict(row) if row else None


def get_session_by_workflow(workflow_id: str) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM construct_sessions WHERE workflow_id=? ORDER BY updated_at DESC LIMIT 1",
            (workflow_id,),
        ).fetchone()
        return _row_to_dict(row) if row else None


def list_sessions(workflow_id: Optional[str] = None) -> List[Dict[str, Any]]:
    with get_conn() as conn:
        if workflow_id:
            rows = conn.execute(
                "SELECT * FROM construct_sessions WHERE workflow_id=? ORDER BY updated_at DESC",
                (workflow_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM construct_sessions ORDER BY updated_at DESC"
            ).fetchall()
        return [_row_to_dict(r) for r in rows]


def update_session_step(session_id: str, step_id: str, status: str = "active") -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            """UPDATE construct_sessions
               SET current_step_id=?, status=?, updated_at=?
               WHERE id=?""",
            (step_id, status, time.time(), session_id),
        )
        return cur.rowcount > 0


def append_step_history(
    session_id: str,
    step_id: str,
    action: str,
    result: str,
) -> None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT step_history FROM construct_sessions WHERE id=?", (session_id,)
        ).fetchone()
        if not row:
            return
        history = json.loads(row["step_history"] or "[]")
        history.append({
            "step_id": step_id,
            "entered_at": time.time(),
            "action": action,
            "result": result,
        })
        conn.execute(
            "UPDATE construct_sessions SET step_history=?, updated_at=? WHERE id=?",
            (json.dumps(history), time.time(), session_id),
        )


def set_session_variable(session_id: str, key: str, value: Any) -> None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT variables FROM construct_sessions WHERE id=?", (session_id,)
        ).fetchone()
        if not row:
            return
        variables = json.loads(row["variables"] or "{}")
        variables[key] = value
        conn.execute(
            "UPDATE construct_sessions SET variables=?, updated_at=? WHERE id=?",
            (json.dumps(variables), time.time(), session_id),
        )


def get_session_variable(session_id: str, key: str, default: Any = None) -> Any:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT variables FROM construct_sessions WHERE id=?", (session_id,)
        ).fetchone()
        if not row:
            return default
        variables = json.loads(row["variables"] or "{}")
        return variables.get(key, default)


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

def record_audit(
    session_id: str,
    event_type: str,
    *,
    step_id: str = "",
    event_data: Optional[Dict[str, Any]] = None,
) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO construct_audit
               (session_id, step_id, event_type, event_data, timestamp)
               VALUES (?, ?, ?, ?, ?)""",
            (session_id, step_id, event_type,
             json.dumps(event_data or {}), time.time()),
        )


def get_audit_events(
    session_id: str,
    limit: int = 100,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM construct_audit
               WHERE session_id=?
               ORDER BY timestamp DESC LIMIT ? OFFSET ?""",
            (session_id, limit, offset),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    # Parse JSON fields
    for key in ("tags", "url_mapping", "element_mappings", "input_values",
                "wait_conditions", "step_history", "variables", "event_data"):
        if key in d and d[key] is not None:
            try:
                d[key] = json.loads(d[key])
            except (json.JSONDecodeError, TypeError):
                d[key] = [] if key != "input_values" and key != "variables" and key != "event_data" else {}
    # Convert boolean-ish ints
    if "is_entry_point" in d:
        d["is_entry_point"] = bool(d["is_entry_point"])
    return d


def get_archive_page_html(page_id: str) -> Optional[str]:
    """Return just the HTML content for an archive page."""
    page = get_archive_page(page_id)
    return page.get("html") if page else None


def find_archive_page_by_url(url: str) -> Optional[Dict[str, Any]]:
    """Find an archive page whose url_mapping includes the given URL."""
    pages = list_archive_pages(limit=1000)
    for page in pages:
        mapping = page.get("url_mapping") or []
        if url in mapping:
            return page
    return None