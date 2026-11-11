"""Streaming support for large resources in the reverse resource proxy.

Implements HTTP range requests, partial responses, cancellation support,
and proper connection/memory release.  Implements Part 2, Section 11
(streaming.py).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional, AsyncIterator, Dict, Any

from .client import ProxyHttpClient

logger = logging.getLogger(__name__)


class StreamedResponse:
    """A streamed proxied response with range request support and cancellation."""

    def __init__(
        self,
        status_code: int,
        headers: Dict[str, str],
        aiter: AsyncIterator[bytes],
        on_close: Optional[asyncio.Future] = None,
        url: Optional[str] = None,
    ) -> None:
        self.status_code = status_code
        self.headers = headers
        self._aiter = aiter
        self._on_close = on_close
        self.url = url
        self._closed = False

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "")

    @property
    def accept_ranges(self) -> str:
        return self.headers.get("accept-ranges", "")

    @property
    def content_range(self) -> str:
        return self.headers.get("content-range", "")

    @property
    def content_length(self) -> str:
        return self.headers.get("content-length", "")

    async def __aiter__(self) -> AsyncIterator[bytes]:
        try:
            async for chunk in self._aiter:
                yield chunk
        finally:
            await self.close()

    async def close(self) -> None:
        """Release the underlying connection.  Safe to call multiple times."""
        if self._closed:
            return
        self._closed = True
        if self._on_close is not None and not self._on_close.done():
            try:
                self._on_close.set_result(None)
            except Exception:
                pass
        logger.debug("Stream closed: %s", self.url)


class RangeRequest:
    """Represents an HTTP Range request parsed from a client header."""

    def __init__(self, start: Optional[int], end: Optional[int]) -> None:
        self.start = start
        self.end = end  # inclusive

    @classmethod
    def parse(cls, header: Optional[str], total_size: Optional[int] = None) -> Optional["RangeRequest"]:
        """Parse a ``Range: bytes=start-end`` header.

        Returns None when the header is absent or invalid.
        """
        if not header or not header.lower().startswith("bytes="):
            return None
        spec = header[6:].strip()
        if "-" not in spec:
            return None
        start_s, end_s = spec.split("-", 1)
        start = int(start_s) if start_s else None
        end = int(end_s) if end_s else None

        if start is None and end is not None:
            # Suffix range: bytes=-500 means last 500 bytes.
            start = max(0, (total_size or 0) - end)
            end = (total_size or 0) - 1 if total_size else None
        elif start is not None and end is None:
            # Open-ended: bytes=500- means from 500 to EOF.
            end = None

        return cls(start, end)

    def apply_to_headers(self, headers: Dict[str, str]) -> None:
        """Add a Range header to a request dict when this range is set."""
        if self.start is not None:
            end = self.end if self.end is not None else ""
            headers["Range"] = f"bytes={self.start}-{end}"

    def content_range_header(self, total_size: int) -> str:
        """Build a Content-Range response header for this range."""
        end = self.end if self.end is not None else total_size - 1
        return f"bytes {self.start}-{end}/{total_size}"


__all__ = [
    "StreamedResponse",
    "RangeRequest",
]
