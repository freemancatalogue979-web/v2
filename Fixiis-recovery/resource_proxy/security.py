"""Security controls for the reverse resource proxy.

Implements SSRF protection, destination allowlisting, and header filtering
per the DOM Streaming Engine plan (Part 2, Sections 11-13).

The proxy must NOT become a way to bypass access controls, authentication,
or origin protections.  Explicit authorization and strict destination
allowlisting prevent SSRF.
"""

from __future__ import annotations

import ipaddress
import logging
from typing import Optional, Set, Tuple

from . import config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Destination validation
# ---------------------------------------------------------------------------

def is_allowed_host(host: str) -> bool:
    """Return True when ``host`` is explicitly allowed for proxying.

    Checks against ALLOWED_HOSTS, with optional subdomain matching.
    Private IP ranges and localhost are rejected unless explicitly enabled.
    """
    if not host:
        return False

    host = host.strip().lower()

    # Reject empty or invalid hosts.
    if not host or host in ("localhost", "localhost.localdomain"):
        return config.ALLOW_LOCALHOST

    # Parse the host to separate hostname from port.
    hostname = host
    if ":" in host:
        hostname = host.split(":", 1)[0]

    # Reject IP literals unless private IPs are explicitly allowed.
    try:
        ip = ipaddress.ip_address(hostname)
        if ip.is_private or ip.is_loopback or ip.is_reserved or ip.is_unspecified:
            if not config.ALLOW_PRIVATE_IPS:
                logger.debug("SSRF: rejected private/reserved IP %s", hostname)
                return False
            # Private IPs allowed — fall through to the allowlist check below.
    except ValueError:
        pass  # Not an IP literal — treat as a hostname.

    # Direct allowlist match.
    if hostname in config.ALLOWED_HOSTS:
        return True

    # Optional subdomain matching.
    if config.ALLOW_SUBDOMAINS and config.ALLOWED_HOSTS:
        for allowed in config.ALLOWED_HOSTS:
            if hostname == allowed or hostname.endswith("." + allowed):
                return True

    logger.debug("SSRF: host not in allowlist: %s", host)
    return False


def is_private_ip(host: str) -> bool:
    """Return True when ``host`` resolves to a private/reserved IP address."""
    try:
        ip = ipaddress.ip_address(host.split(":", 1)[0])
        return ip.is_private or ip.is_loopback or ip.is_reserved or ip.is_unspecified
    except ValueError:
        return False


def validate_destination(url: str) -> Tuple[bool, Optional[str]]:
    """Validate that ``url`` points to an allowed destination.

    Returns (True, None) when the destination is permitted, or
    (False, reason) when it is blocked.
    """
    if not url:
        return False, "empty URL"

    # Reject non-HTTP(S) schemes (file://, ftp://, javascript:, data:, etc.).
    scheme = url.split("://", 1)[0].lower() if "://" in url else ""
    if scheme not in ("http", "https"):
        return False, f"unsupported scheme: {scheme} or none"

    # Extract the host.
    try:
        from urllib.parse import urlsplit
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        if not host:
            return False, "no host"
    except Exception as exc:  # noqa: BLE001
        return False, f"invalid URL: {exc}"

    if not is_allowed_host(host):
        return False, f"host not allowed: {host}"

    return True, None


def strip_forward_blacklist(headers: dict) -> dict:
    """Remove headers that must never be forwarded to the origin."""
    return {
        k: v for k, v in headers.items()
        if k.lower() not in config.FORWARD_BLACKLIST_HEADERS
    }


def filter_response_headers(headers: dict) -> dict:
    """Strip hop-by-hop and origin-specific security headers from a proxied
    response before returning it to the client."""
    out: dict = {}
    for k, v in headers.items():
        kl = k.lower()
        if kl in config.HOP_BY_HOP_HEADERS:
            continue
        if kl in config.ORIGIN_SECURITY_HEADERS:
            continue
        out[k] = v
    return out


__all__ = [
    "is_allowed_host",
    "is_private_ip",
    "validate_destination",
    "strip_forward_blacklist",
    "filter_response_headers",
]
