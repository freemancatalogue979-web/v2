"""
Construct Engine - Connects custom HTML client interfaces to remote browser sessions.

The engine is the heart of the CONSTRUCT hybrid architecture:

CLIENT SIDE (browser-based custom HTML):
- Receives and renders custom HTML pages from the Construct Archive
- Allows users to interact with the custom interface
- Sends permitted actions and input events to the Construct execution engine
- Does NOT receive the original website's page as its interface

REMOTE BROWSER SIDE:
- Maintains the actual browser session
- Navigates to real websites
- Executes browser actions (click, input, navigate, wait)
- Interacts with the actual website DOM
- Returns execution results and state to the Construct engine

CONSTRUCT ENGINE:
- Connects the client HTML interface to the remote browser
- Tracks the current workflow step
- Loads the appropriate attached HTML page
- Maps client interface events to browser operations
- Manages transitions between workflow steps
- Keeps the client interface and remote browser state synchronized

Security:
- No credential capture, password collection, or private keystroke logging
- Form input values are NOT logged or stored
- Element mappings only reference selectors, never sensitive values
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Dict, List, Optional

import construct_store
from config import CONFIG

# Construct engine manages its own lightweight remote browser pages —
# no NeoStreamingSession, no DOM capture, no /ws streaming pipeline.
# Construct mode is standalone like LPV: just connect, render pages,
# forward events. Remote pages are used only for executing actions
# (click, input, navigate, wait) on the real website.
_remote_pages: Dict[str, Dict[str, Any]] = {}
_remote_pages_lock = asyncio.Lock()


async def register_remote_page(session_id: str, page: Any,
                               context: Any = None,
                               browser: Any = None,
                               target_url: str = "") -> None:
    """Register a lightweight remote browser page (plus context/browser) for a construct session.

    Storing context/browser lets us recreate a page if the underlying window
    gets closed (e.g. target website opens a new tab or closes the window).
    Storing target_url lets us re-navigate after recreation.
    """
    async with _remote_pages_lock:
        _remote_pages[session_id] = {
            "page": page,
            "context": context,
            "browser": browser,
            "target_url": target_url,
        }

async def get_remote_page(session_id: str) -> Optional[Any]:
    """Get a registered remote browser page (the .page entry)."""
    async with _remote_pages_lock:
        entry = _remote_pages.get(session_id)
    if entry is None:
        return None
    return entry.get("page")

async def get_remote_context(session_id: str) -> Optional[Any]:
    """Get the registered browser context for a construct session."""
    async with _remote_pages_lock:
        entry = _remote_pages.get(session_id)
    if entry is None:
        return None
    return entry.get("context")

async def unregister_remote_page(session_id: str) -> None:
    """Remove a remote page and close it."""
    async with _remote_pages_lock:
        entry = _remote_pages.pop(session_id, None)
    if entry is not None:
        page = entry.get("page")
        if page is not None:
            try:
                await page.close()
            except Exception:
                pass

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-memory session registry: construct_session_id -> ConstructSession
# ---------------------------------------------------------------------------

_active_sessions: Dict[str, "ConstructSession"] = {}
_session_lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# Action result types
# ---------------------------------------------------------------------------

class ActionResult:
    """Result of executing a browser action in the remote session."""

    def __init__(self, success: bool, data: Optional[Dict] = None,
                 error: Optional[str] = None, next_step: Optional[str] = None):
        self.success = success
        self.data = data or {}
        self.error = error
        self.next_step = next_step

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "data": self.data,
            "error": self.error,
            "next_step": self.next_step,
        }


# ---------------------------------------------------------------------------
# ConstructSession
# ---------------------------------------------------------------------------

class ConstructSession:
    """
    A single Construct runtime session. Bridges one client WebSocket
    to one remote browser session, driven by one workflow.
    """

    def __init__(self, session_id: str, workflow_id: str,
                 client_websocket: Any, client_id: str = ""):
        self.session_id = session_id
        self.workflow_id = workflow_id
        self.client_websocket = client_websocket
        self.client_id = client_id
        self.remote_session_id: Optional[str] = None
        self.current_step_id: Optional[str] = None
        self.status = "initializing"
        self.variables: Dict[str, Any] = {}
        self.step_history: List[Dict[str, Any]] = []
        self._created_at = time.time()
        self._updated_at = time.time()
        self._lock = asyncio.Lock()

    # -- persistence -------------------------------------------------------

    async def persist(self) -> None:
        construct_store.save_session(
            self.session_id, self.workflow_id,
            client_id=self.client_id,
            remote_session_id=self.remote_session_id or "",
            current_step_id=self.current_step_id or "",
            status=self.status,
        )
        construct_store.set_session_variable(
            self.session_id, "__engine_state",
            {"status": self.status, "current_step": self.current_step_id},
        )

    async def record_audit(self, event_type: str, *,
                           step_id: str = "",
                           event_data: Optional[Dict] = None) -> None:
        construct_store.record_audit(
            self.session_id, event_type,
            step_id=step_id or (self.current_step_id or ""),
            event_data=event_data,
        )

    # -- step navigation ---------------------------------------------------

    async def enter_step(self, step_id: str) -> bool:
        """Transition to a workflow step. Loads the attached page and notifies the client."""
        step = construct_store.get_step(step_id)
        if not step:
            logger.error("[construct] step %s not found in workflow %s", step_id, self.workflow_id)
            # Step referenced by transition_to doesn't exist — complete workflow gracefully
            await self.complete("error")
            await self._send_client({
                "type": "construct_ack",
                "message": f"Step '{step_id}' not found — workflow ended",
            })
            return False

        self.current_step_id = step_id
        self.status = "active"
        self._updated_at = time.time()
        self.step_history.append({
            "step_id": step_id,
            "entered_at": self._updated_at,
            "action": "enter",
            "result": "ok",
        })
        await self.persist()
        await self.record_audit("step_entered", step_id=step_id,
                                event_data={"step_name": step.get("step_name"),
                                            "action_type": step.get("action_type")})

        # Load the attached HTML page for the client
        attached_page_id = step.get("attached_page_id") or ""
        page_html = None
        page_name = step.get("attached_page_name") or ""
        if attached_page_id:
            page_html = construct_store.get_archive_page_html(attached_page_id)
            if not page_html and page_name:
                page = construct_store.get_archive_page_by_name(page_name)
                if page:
                    page_html = page.get("html")

        # Get workflow target_url for client branding (loading spinner, favicon)
        workflow = construct_store.get_workflow(self.workflow_id) or {}
        target_url = workflow.get("target_url", "") or ""

        # When the step has an attached HTML page, notify the client to render
        # it in the iframe. When there is no attached page, the client does not
        # need to be notified — the step just runs a browser action
        # (click/input/navigate/wait) silently on the remote browser.
        if page_html:
            await self._send_client({
                "type": "construct_step",
                "step_id": step_id,
                "step_name": step.get("step_name", ""),
                "action_type": step.get("action_type", ""),
                "browser_action": step.get("browser_action", ""),
                "target_selector": step.get("target_selector", ""),
                "page_html": page_html or "",
                "page_name": page_name,
                "target_url": target_url,
                "element_mappings": step.get("element_mappings") or [],
                "input_values": step.get("input_values") or {},
                "wait_conditions": step.get("wait_conditions") or [],
                "transition_to": step.get("transition_to") or "",
                "is_identical": bool(step.get("is_identical", False)),
            })

        logger.info("[construct] session %s entered step %s (%s) page=%s",
                    self.session_id, step_id, step.get("step_name"), bool(page_html))

        # Auto-execute browser action when there is no attached HTML page
        # for the user to interact with (e.g. Navigate, Click, Input, Wait
        # steps that act directly on the remote browser page).
        if not page_html and step.get("action_type"):
            await self._auto_execute_step(step)

        return True

    def _build_action_payload(self, step: Dict[str, Any],
                              event: Optional[Dict[str, Any]] = None
                              ) -> Dict[str, Any]:
        """Build the execute_browser_action payload from a step record.

        Maps step fields (target_selector, input_values, browser_action) to
        the keys execute_browser_action actually reads (url, selector, value,
        seconds, script).
        """
        action_type = step.get("action_type", "")
        payload: Dict[str, Any] = {}

        if action_type == "show":
            # Show Only — no browser action, just display the attached page
            return payload
        if action_type == "navigate":
            # target_selector holds the URL for navigate steps
            payload["url"] = step.get("target_selector", "")
        elif action_type == "click":
            payload["selector"] = step.get("target_selector", "")
        elif action_type == "input":
            payload["selector"] = step.get("target_selector", "")
            # Prefer an explicit event value, then step default
            if event and event.get("value"):
                payload["value"] = event["value"]
            else:
                iv = step.get("input_values") or {}
                if isinstance(iv, dict) and iv.get("value"):
                    payload["value"] = iv["value"]
                elif isinstance(iv, str):
                    payload["value"] = iv
        elif action_type == "wait":
            # target_selector holds seconds (a number), not a CSS selector
            try:
                payload["seconds"] = float(step.get("target_selector", "") or 0)
            except (ValueError, TypeError):
                payload["seconds"] = 0
        elif action_type == "navigate_and_wait":
            payload["url"] = step.get("target_selector", "")
            iv = step.get("input_values") or {}
            if isinstance(iv, dict) and iv.get("seconds"):
                try:
                    payload["seconds"] = float(iv["seconds"])
                except (ValueError, TypeError):
                    payload["seconds"] = 0
        elif action_type == "custom":
            payload["script"] = step.get("browser_action", "")

        return payload

    async def _auto_execute_step(self, step: Dict[str, Any]) -> None:
        """Auto-execute a step's browser action on the remote browser.

        Used for steps without an attached client page — the action runs
        immediately when the step is entered, then transitions on success.
        """
        action_type = step.get("action_type", "")
        payload = self._build_action_payload(step)

        if action_type == "navigate" and not payload.get("url"):
            await self._send_client({
                "type": "construct_ack",
                "message": f"Navigate step {step.get('step_name')} has no URL",
            })
            return

        result = await self.execute_browser_action(action_type, payload)
        await self.send_action_result(result)

        if result.success:
            next_step = step.get("transition_to") or ""
            await self.transition_to(next_step or "")
        else:
            failure_to = step.get("transition_on_failure") or ""
            if failure_to:
                await self.transition_to(failure_to)
            else:
                await self._send_client({
                    "type": "construct_ack",
                    "message": f"Action {action_type} failed: {result.error}",
                })

    async def transition_to(self, next_step_id: str, result: str = "ok") -> None:
        """Move to the next step in the workflow.
        Empty transition_to keeps the session alive instead of completing it."""
        if not next_step_id:
            logger.info("[construct] transition_to empty — keeping session alive on step %s", self.current_step_id)
            await self.send_state_sync()
            return
        await self.enter_step(next_step_id)

    async def complete(self, status: str = "completed") -> None:
        self.status = status
        self._updated_at = time.time()
        await self.persist()
        await self.record_audit("session_complete", event_data={"status": status})
        await self._send_client({
            "type": "construct_complete",
            "status": status,
            "session_id": self.session_id,
        })
        logger.info("[construct] session %s completed with status %s",
                    self.session_id, status)

    # -- client messaging --------------------------------------------------

    async def _send_client(self, message: Dict[str, Any]) -> bool:
        """Send a JSON message to the client WebSocket."""
        ws = self.client_websocket
        if ws is None:
            return False
        try:
            if hasattr(ws, "send_json"):
                await ws.send_json(message)
            else:
                await ws.send_text(json.dumps(message))
            return True
        except Exception as exc:
            logger.debug("[construct] client send failed: %s", exc)
            return False

    async def send_action_result(self, result: ActionResult) -> None:
        """Send the result of a browser action back to the client."""
        await self._send_client({
            "type": "construct_action_result",
            "step_id": self.current_step_id or "",
            "result": result.to_dict(),
        })

    async def send_state_sync(self) -> None:
        """Push current engine state to the client (re-sync after reconnect)."""
        step = construct_store.get_step(self.current_step_id) if self.current_step_id else None
        await self._send_client({
            "type": "construct_state_sync",
            "session_id": self.session_id,
            "workflow_id": self.workflow_id,
            "status": self.status,
            "current_step_id": self.current_step_id or "",
            "step_name": step.get("step_name", "") if step else "",
            "variables": self.variables,
        })

    # -- browser action execution ------------------------------------------

    async def _ensure_remote_page(self) -> Optional[Any]:
        """Ensure we have a live remote page. If the page's window is closed,
        try to create a new page from the stored context."""
        page = await get_remote_page(self.remote_session_id)
        if page is None:
            return None

        # Quick check: try evaluating something cheap to see if page is alive
        try:
            await page.evaluate("1")
            return page
        except Exception:
            # Page might be closed — try to recreate from context
            logger.warning("[construct] remote page %s appears closed, recreating from context",
                           self.remote_session_id)
            context = await get_remote_context(self.remote_session_id)
            if context is None:
                logger.error("[construct] no context available to recreate page for %s",
                             self.remote_session_id)
                return None
            try:
                new_page = await context.new_page()
                # Re-navigate to the target URL so we're back on the right site
                async with _remote_pages_lock:
                    entry = _remote_pages.get(self.remote_session_id)
                    target_url = entry.get("target_url", "") if entry else ""
                if target_url:
                    try:
                        await new_page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
                        logger.info("[construct] re-navigated recreated page to %s", target_url)
                    except Exception as nav_exc:
                        logger.warning("[construct] re-navigate to %s failed: %s", target_url, nav_exc)
                # Update the stored entry with the new page
                async with _remote_pages_lock:
                    entry = _remote_pages.get(self.remote_session_id)
                    if entry:
                        entry["page"] = new_page
                logger.info("[construct] recreated remote page for session %s", self.remote_session_id)
                return new_page
            except Exception as recreate_exc:
                logger.error("[construct] failed to recreate remote page: %s", recreate_exc)
                return None

    async def execute_browser_action(self, action_type: str,
                                     payload: Dict[str, Any]) -> ActionResult:
        """
        Execute a browser action in the remote session.

        Supported action types:
        - navigate: Go to a URL
        - click: Click an element by selector
        - input: Type into a form field
        - wait: Wait for a condition
        - navigate_and_wait: Navigate then wait
        - custom: Arbitrary JavaScript
        - show: Show attached page only, no browser action
        """
        if not self.remote_session_id:
            return ActionResult(False, error="No remote browser session bound")

        try:
            page = await self._ensure_remote_page()
            if page is None:
                return ActionResult(False, error="No active page in remote session")

            if action_type == "show":
                # Show Only — no browser action needed, page is rendered in client iframe
                return ActionResult(True, data={"action": "show_only"})
            if action_type == "navigate":
                url = payload.get("url", "")
                if url:
                    await page.goto(url, wait_until="domcontentloaded")
                    return ActionResult(True, data={"url": url})
                return ActionResult(False, error="No URL provided for navigate")

            elif action_type == "click":
                selector = payload.get("selector", "")
                if selector:
                    # Wait for the element to exist before clicking — iCloud is a
                    # SPA that renders its sign-in form dynamically, so the
                    # element may not be in the DOM immediately after navigation.
                    try:
                        await page.wait_for_selector(selector, timeout=10000)
                    except Exception:
                        pass  # fall through to click; let click report the real error
                    await page.click(selector, timeout=10000)
                    return ActionResult(True, data={"selector": selector})
                return ActionResult(False, error="No selector provided for click")

            elif action_type == "input":
                selector = payload.get("selector", "")
                value = payload.get("value", "")
                if selector:
                    try:
                        await page.wait_for_selector(selector, timeout=10000)
                    except Exception:
                        pass
                    await page.fill(selector, value)
                    return ActionResult(True, data={"selector": selector})
                return ActionResult(False, error="No selector provided for input")

            elif action_type == "wait":
                seconds = float(payload.get("seconds", 0) or 0)
                if seconds > 0:
                    await page.wait_for_timeout(seconds * 1000)
                    return ActionResult(True, data={"waited_ms": int(seconds * 1000)})
                return ActionResult(False, error="No wait seconds provided")

            elif action_type == "navigate_and_wait":
                url = payload.get("url", "")
                seconds = float(payload.get("seconds", 0) or 0)
                if url:
                    await page.goto(url, wait_until="domcontentloaded")
                    if seconds > 0:
                        await page.wait_for_timeout(seconds * 1000)
                    return ActionResult(True, data={"url": url, "waited_ms": int(seconds * 1000)})
                return ActionResult(False, error="No URL provided for navigate_and_wait")

            elif action_type == "custom":
                script = payload.get("script", "")
                if script:
                    result = await page.evaluate(script)
                    return ActionResult(True, data={"result": result})
                return ActionResult(False, error="No script provided for custom")

            else:
                return ActionResult(False, error=f"Unknown action type: {action_type}")

        except Exception as exc:
            logger.error("[construct] browser action %s failed: %s", action_type, exc)
            return ActionResult(False, error=str(exc))

    # -- client event handling ---------------------------------------------

    async def handle_client_event(self, event: Dict[str, Any]) -> None:
        """Handle an event from the client (click, input, navigation request, etc.)."""
        event_type = event.get("type", "")
        step_id = event.get("step_id") or self.current_step_id or ""

        logger.info(
            "[construct] handle_client_event type=%s step_id=%s current_step=%s",
            event_type, step_id, self.current_step_id,
        )

        await self.record_audit("client_event", step_id=step_id,
                                event_data={"event_type": event_type, "payload": event})

        if event_type == "construct_click":
            custom_id = event.get("custom_id", "")
            tag = event.get("tag", "") or ""
            name = event.get("name", "") or ""
            href = event.get("href", "") or ""
            step = construct_store.get_step(self.current_step_id) if self.current_step_id else None
            is_identical = step.get("is_identical") if step else False

            logger.info(
                "[construct] construct_click custom_id=%s tag=%s name=%s href=%s is_identical=%s step=%s",
                custom_id, tag, name, href, is_identical, self.current_step_id,
            )

            if is_identical:
                # Identical mode: try multiple selector strategies.
                # 1. ID selector (#custom_id)
                # 2. Name selector ([name="..."])
                # 3. Href selector ([href="..."])
                # 4. CSS path (html > body > ...) — pass through as-is
                selectors = []
                cid = (custom_id or "").strip()
                if cid:
                    # If it's already a CSS selector or path, use as-is
                    if cid.startswith(("#", ".", "[")):
                        selectors.append(cid)
                    # CSS path from client — use directly
                    elif " > " in cid:
                        selectors.append(cid)
                    else:
                        selectors.append("#" + cid)
                    # Streaming-style fallback: data-mid
                    selectors.append('[data-mid="' + cid + '"]')
                    if name:
                        selectors.append('[name="' + name + '"]')
                    if href:
                        selectors.append('[href="' + href + '"]')
                for selector in selectors:
                    logger.info("[construct] identical click trying selector=%s", selector)
                    result = await self.execute_browser_action("click", {"selector": selector})
                    await self.send_action_result(result)
                    if result.success:
                        next_step = step.get("transition_to") if step else ""
                        await self.transition_to(next_step or "")
                        return
                # All selectors failed
                await self._send_client({
                    "type": "construct_ack",
                    "message": f"Identical click failed for element {custom_id} (tried {selectors})",
                })
                return

            mapping = self._find_element_mapping(custom_id)
            if mapping:
                browser_selector = mapping.get("browser_selector", "")
                action = mapping.get("action", "click")
                logger.info("[construct] mapped click custom_id=%s selector=%s action=%s", custom_id, browser_selector, action)
                result = await self.execute_browser_action(action, {"selector": browser_selector})
                await self.send_action_result(result)
                if result.success and mapping.get("transition_on_success"):
                    await self.transition_to(mapping["transition_on_success"])
                elif result.success:
                    next_step = step.get("transition_to") if step else ""
                    await self.transition_to(next_step or "")
                return
            await self._send_client({
                "type": "construct_ack",
                "message": f"No browser mapping for element {custom_id}",
            })

        elif event_type == "construct_input":
            custom_id = event.get("custom_id", "")
            value = event.get("value", "")
            tag = event.get("tag", "") or ""
            step = construct_store.get_step(self.current_step_id) if self.current_step_id else None
            is_identical = step.get("is_identical") if step else False

            logger.info(
                "[construct] construct_input custom_id=%s tag=%s value=%s is_identical=%s step=%s",
                custom_id, tag, value, is_identical, self.current_step_id,
            )

            if is_identical:
                # Identical mode: try multiple selector strategies.
                # 1. ID selector (#custom_id)
                # 2. Name selector ([name="..."])
                # 3. CSS path (html > body > ...) — pass through as-is
                selectors = []
                cid = (custom_id or "").strip()
                if cid:
                    # If it's already a CSS selector or path, use as-is
                    if cid.startswith(("#", ".", "[")):
                        selectors.append(cid)
                    # CSS path from client — use directly
                    elif " > " in cid:
                        selectors.append(cid)
                    else:
                        selectors.append("#" + cid)
                    # Streaming-style fallback: data-mid
                    selectors.append('[data-mid="' + cid + '"]')
                    name = event.get("name", "") or ""
                    if name:
                        selectors.append('[name="' + name + '"]')
                for selector in selectors:
                    logger.info("[construct] identical input trying selector=%s", selector)
                    result = await self.execute_browser_action("input", {
                        "selector": selector,
                        "value": value,
                    })
                    await self.send_action_result(result)
                    if result.success:
                        next_step = step.get("transition_to") if step else ""
                        await self.transition_to(next_step or "")
                        return
                await self._send_client({
                    "type": "construct_ack",
                    "message": f"Identical input failed for element {custom_id} (tried {selectors})",
                })
                return

            mapping = self._find_element_mapping(custom_id)
            if mapping:
                browser_selector = mapping.get("browser_selector", "")
                action = mapping.get("action", "input")
                logger.info("[construct] mapped input custom_id=%s selector=%s action=%s", custom_id, browser_selector, action)
                result = await self.execute_browser_action(action, {
                    "selector": browser_selector,
                    "value": value,
                })
                await self.send_action_result(result)
                if result.success:
                    step2 = construct_store.get_step(self.current_step_id) if self.current_step_id else None
                    next_step = step2.get("transition_to") if step2 else ""
                    await self.transition_to(next_step or "")
                return

        elif event_type == "construct_navigate":
            url = event.get("url", "")
            result = await self.execute_browser_action("navigate", {"url": url})
            await self.send_action_result(result)

        elif event_type == "construct_sync":
            await self.send_state_sync()

        elif event_type == "construct_step_action":
            step = construct_store.get_step(self.current_step_id) if self.current_step_id else None
            if step:
                action_type = step.get("action_type", "")
                payload = self._build_action_payload(step, event)
                result = await self.execute_browser_action(action_type, payload)
                await self.send_action_result(result)
                if result.success:
                    next_step = step.get("transition_to") or ""
                    await self.transition_to(next_step or "")

    def _find_element_mapping(self, custom_id: str) -> Optional[Dict[str, Any]]:
        """Find an element mapping for a custom HTML element ID."""
        step = construct_store.get_step(self.current_step_id) if self.current_step_id else None
        if not step:
            return None
        mappings = step.get("element_mappings") or []
        for m in mappings:
            if m.get("custom_id") == custom_id:
                return m
        return None

    @staticmethod
    def _to_css_selector(custom_id: str) -> str:
        """
        Convert a custom element ID to a CSS selector for Identical Mode.
        If it already looks like a CSS selector (starts with '#', '.', '[',
        or is a path like 'html > body > ...'), use it as-is.
        Otherwise prefix with '#' (ID selector).
        """
        cid = (custom_id or "").strip()
        if not cid:
            return ""
        if cid.startswith(("#", ".", "[")):
            return cid
        # CSS path (contains ' > ') — pass through as-is
        if " > " in cid:
            return cid
        return "#" + cid

    # -- lifecycle ---------------------------------------------------------

    async def bind_remote_session(self, remote_session_id: str) -> None:
        """Bind this construct session to a remote browser session."""
        self.remote_session_id = remote_session_id
        self._updated_at = time.time()
        await self.persist()
        await self.record_audit("remote_bound",
                                event_data={"remote_session_id": remote_session_id})

    async def start_workflow(self) -> bool:
        """Start the workflow from its entry point step."""
        steps = construct_store.list_steps(self.workflow_id)
        entry_steps = [s for s in steps if s.get("is_entry_point")]
        if not entry_steps:
            if steps:
                entry_steps = [steps[0]]
            else:
                await self.complete("error")
                return False

        await self.enter_step(entry_steps[0]["id"])
        return True


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------

async def get_or_create_session(session_id: str, workflow_id: str,
                                client_websocket: Any,
                                client_id: str = "") -> ConstructSession:
    async with _session_lock:
        if session_id in _active_sessions:
            return _active_sessions[session_id]
        sess = ConstructSession(session_id, workflow_id, client_websocket, client_id)
        _active_sessions[session_id] = sess
        await sess.persist()
        return sess


async def get_session(session_id: str) -> Optional[ConstructSession]:
    async with _session_lock:
        return _active_sessions.get(session_id)


async def remove_session(session_id: str) -> None:
    async with _session_lock:
        _active_sessions.pop(session_id, None)
    await unregister_remote_page(session_id)


async def list_active_sessions() -> List[Dict[str, Any]]:
    async with _session_lock:
        return [
            {
                "session_id": s.session_id,
                "workflow_id": s.workflow_id,
                "client_id": s.client_id,
                "remote_session_id": s.remote_session_id,
                "current_step_id": s.current_step_id,
                "status": s.status,
                "created_at": s._created_at,
            }
            for s in _active_sessions.values()
        ]


async def shutdown_all_sessions() -> None:
    async with _session_lock:
        for s in list(_active_sessions.values()):
            try:
                await s._send_client({"type": "construct_shutdown"})
            except Exception:
                pass
        _active_sessions.clear()