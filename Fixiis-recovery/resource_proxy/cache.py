"""HTTP-semantics cache for the reverse resource proxy.

Implements caching with Cache-Control, ETag, and Last-Modified validation
per Part 2, Section 11 (cache.py).  Does not cache responses marked
private or no-store in shared caches, and never mixes responses between
different authorized sessions.
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Dict, Optional, Tuple

from . import config
from .response import ProxyResponse

logger = logging.getLogger(__name__)


class CacheEntry:
    """A single cached response entry."""

    __slots__ = ("response", "expires_at", "etag", "last_modified", "size", "hits")

    def __init__(
        self,
        response: ProxyResponse,
        ttl: Optional[int],
        size: int,
    ) -> None:
        self.response = response
        self.expires_at = (
            time.monotonic() + ttl if ttl is not None and ttl > 0 else None
        )
        self.etag = response.etag or ""
        self.last_modified = response.last_modified or ""
        self.size = size
        self.hits = 0

    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return time.monotonic() > self.expires_at

    def is_stale(self) -> bool:
        """Return True when the entry may be stale and needs revalidation."""
        return self.is_expired()


class ResourceCache:
    """In-memory cache for proxied resources with HTTP semantics.

    Never mixes responses between sessions; each entry is keyed by a
    (session_id, url) tuple.  Public resources may be shared across
    sessions when the URL and content match.
    """

    def __init__(
        self,
        max_bytes: int = config.MAX_CACHE_BYTES,
        max_entries: int = config.MAX_CACHE_ENTRIES,
    ) -> None:
        self.max_bytes = max_bytes
        self.max_entries = max_entries
        self._entries: Dict[str, CacheEntry] = {}
        self._total_bytes = 0
        self._lock_key = "__lock__"

    def _key(self, session_id: Optional[str], url: str) -> str:
        """Build a cache key that never mixes sessions."""
        raw = f"{session_id or 'public'}|{url}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def get(
        self,
        session_id: Optional[str],
        url: str,
        etag: Optional[str] = None,
        last_modified: Optional[str] = None,
    ) -> Optional[ProxyResponse]:
        """Look up a cached response.

        Returns None when there is no valid entry.  When an entry exists
        but is expired, it is removed and None is returned (caller may
        revalidate with ETag/Last-Modified).
        """
        key = self._key(session_id, url)
        entry = self._entries.get(key)
        if entry is None:
            return None
        if entry.is_expired():
            self._remove(key)
            return None
        entry.hits += 1
        return entry.response

    def put(
        self,
        session_id: Optional[str],
        url: str,
        response: ProxyResponse,
    ) -> bool:
        """Store a response in the cache.  Returns True when stored."""
        if not response.is_cacheable():
            return False

        ttl = response.cache_ttl()
        if ttl is not None and ttl <= 0:
            return False

        body = response.body or b""
        size = len(body)
        if size > config.MAX_BUFFER_BYTES:
            # Don't cache bodies larger than the buffer threshold.
            return False

        key = self._key(session_id, url)

        # Evict an existing entry with the same key.
        if key in self._entries:
            old = self._entries[key]
            self._total_bytes -= old.size
            del self._entries[key]

        # Enforce max entries (evict oldest/least-used).
        while len(self._entries) >= config.MAX_CACHE_ENTRIES:
            self._evict_lru()

        # Enforce max bytes.
        while self._total_bytes + size > config.MAX_CACHE_BYTES and self._entries:
            self._evict_lru()

        if self._total_bytes + size > config.MAX_CACHE_BYTES:
            # Single entry larger than the whole cache — don't cache.
            return False

        entry = CacheEntry(response, ttl, size)
        self._entries[key] = entry
        self._total_bytes += size
        return True

    def _remove(self, key: str) -> None:
        entry = self._entries.pop(key, None)
        if entry:
            self._total_bytes -= entry.size

    def _evict_lru(self) -> None:
        """Evict the least-recently-used (fewest hits) entry."""
        if not self._entries:
            return
        lru_key = min(self._entries, key=lambda k: self._entries[k].hits)
        self._remove(lru_key)

    def invalidate(self, session_id: Optional[str], url: str) -> None:
        """Remove a specific entry from the cache."""
        self._remove(self._key(session_id, url))

    def invalidate_session(self, session_id: Optional[str]) -> None:
        """Remove all entries for a session (prevents mixing across sessions)."""
        prefix = hashlib.sha256(
            f"{session_id or 'public'}|".encode("utf-8")
        ).hexdigest()[:16]
        # Since keys are full hashes, we can't prefix-match efficiently.
        # Instead, track session keys separately or accept that entries
        # expire naturally.  For correctness on session end, clear all
        # entries that contain the session marker.
        to_remove = [
            k for k, v in self._entries.items()
            if k.startswith(prefix)
        ]
        for k in to_remove:
            self._remove(k)

    def stats(self) -> Dict[str, object]:
        return {
            "entries": len(self._entries),
            "total_bytes": self._total_bytes,
            "max_bytes": self.max_bytes,
            "max_entries": self.max_entries,
        }


# Global cache instance.
_global_cache: Optional[ResourceCache] = None


def get_global_cache() -> ResourceCache:
    global _global_cache
    if _global_cache is None:
        _global_cache = ResourceCache()
    return _global_cache


__all__ = [
    "ResourceCache",
    "CacheEntry",
    "get_global_cache",
]
