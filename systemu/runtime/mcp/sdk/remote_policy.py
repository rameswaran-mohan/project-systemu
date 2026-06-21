"""Remote MCP transport hardening — TLS enforcement, SSRF host-policy, bounds,
and a small reconnect/health tracker. Pure: no `mcp` SDK import; manager.py
calls these before opening a remote transport."""
from __future__ import annotations

import ipaddress
import logging
from typing import Set, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


class InsecureTransportError(RuntimeError):
    """Raised when a remote MCP URL would use plaintext http without an explicit
    operator allowlist entry for its host."""


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower().strip()
    except Exception:
        return ""


def enforce_tls(url: str, *, allowed_hosts: Set[str] | None = None) -> None:
    """Require https for remote MCP URLs. Plain http is permitted ONLY when the
    host is explicitly in ``allowed_hosts`` (an operator-pinned local/dev server).
    Raises InsecureTransportError otherwise."""
    allowed_hosts = allowed_hosts or set()
    try:
        scheme = (urlparse(url).scheme or "").lower()
    except Exception:
        scheme = ""
    if scheme == "https":
        return
    if scheme == "http" and _host(url) in allowed_hosts:
        logger.info("[MCP] allowing plaintext http to allowlisted host %s", _host(url))
        return
    raise InsecureTransportError(
        f"refusing plaintext MCP transport for {url!r} — use https or add the "
        f"host to allowed_mcp_hosts")


def mcp_host_allowed(url: str, *, allowed_hosts: Set[str]) -> bool:
    """SSRF host-policy: a public host is allowed; loopback/link-local/RFC1918/
    metadata (169.254.169.254) is DENIED unless the host string is explicitly in
    ``allowed_hosts``. Fail-closed on a malformed URL."""
    host = _host(url)
    if not host:
        return False
    if host in allowed_hosts:
        return True
    if host == "localhost":
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # Hostname (not a literal IP) and not allowlisted: treat as public-ok.
        # (DNS-rebinding defense is a manager-level concern; out of P4 scope.)
        return True
    if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_unspecified:
        return False
    # Cloud metadata endpoint is link-local (169.254.169.254) → already denied above.
    return True


def bounded(value: str, *, max_chars: int) -> Tuple[str, bool]:
    """Cap a string at ``max_chars``. Returns (possibly-truncated, was_truncated)."""
    value = value if isinstance(value, str) else str(value)
    if len(value) <= max_chars:
        return value, False
    return value[:max_chars], True


class RemoteHealth:
    """Tiny consecutive-failure health tracker for a remote MCP endpoint. The
    manager flips to a reconnect path when ``healthy`` goes False; a success
    resets it (a fresh stateless-reissue connection recovers)."""

    def __init__(self, *, fail_threshold: int = 3):
        self.fail_threshold = max(1, int(fail_threshold))
        self._consecutive_failures = 0

    @property
    def healthy(self) -> bool:
        return self._consecutive_failures < self.fail_threshold

    def record_failure(self) -> None:
        self._consecutive_failures += 1

    def record_success(self) -> None:
        self._consecutive_failures = 0
