"""Say a thing once, not once per event.

An app is not killed by logging — it is killed by logging on a hot path. A
warning attached to every pointer move, every keystroke or every frame costs
real time (string formatting, a handler lock, a write, a flush) and buries the
one line that matters under thousands of identical ones.

``throttled()`` keeps the first line and then at most one per window, with a
count of what was suppressed. Nothing is lost — a repeat rate is still visible —
and the hot path costs a dictionary lookup instead of a console write.

    from log_utils import throttled
    throttled(logger, "input-drop", "[INPUT][drop] session=%s inputs off", sid)
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional, Tuple

# key -> (last_logged_at, suppressed_since)
_SEEN: Dict[str, Tuple[float, int]] = {}

# One line per key per window. Long enough that a gesture cannot flood a
# terminal, short enough that a real change is noticed.
DEFAULT_EVERY = 30.0


def throttled(
    logger: logging.Logger,
    key: str,
    message: str,
    *args: Any,
    every: float = DEFAULT_EVERY,
    level: int = logging.WARNING,
    **kwargs: Any,
) -> bool:
    """Log ``message`` at most once per ``every`` seconds for ``key``.

    Returns True when the line was actually written. The first call always
    writes; later calls inside the window only count.
    """
    now = time.monotonic()
    prev: Optional[Tuple[float, int]] = _SEEN.get(key)
    if prev is not None:
        last, suppressed = prev
        if (now - last) < every:
            _SEEN[key] = (last, suppressed + 1)
            return False
        _SEEN[key] = (now, 0)
        # The count is appended as plain text: the placeholders in `message`
        # are filled by `args` before this suffix means anything.
        message = message + " (+%d more in the last %.0fs)" % (suppressed, every)
    else:
        _SEEN[key] = (now, 0)
    try:
        logger.log(level, message, *args, **kwargs)
    except Exception:
        # Logging must never be the thing that breaks a session.
        return False
    return True


def reset() -> None:
    """Forget every window (tests, and a session restart)."""
    _SEEN.clear()


def suppressed(key: str) -> int:
    """How many lines were swallowed for ``key`` in the current window."""
    prev = _SEEN.get(key)
    return prev[1] if prev else 0
