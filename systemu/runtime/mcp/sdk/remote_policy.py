"""Remote MCP transport hardening — TLS enforcement, SSRF host-policy, bounds,
and a small reconnect/health tracker. Pure: no `mcp` SDK import; manager.py
calls these before opening a remote transport."""
from __future__ import annotations

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


def _operator_allows(url: str, allowed_hosts) -> bool:
    """True when ``url``'s host[:port] is named in the operator allowance (a bare
    host entry matches any port; ``host:port`` matches exactly)."""
    from systemu.runtime.mcp.sdk.egress import allowance_matches
    return allowance_matches(url, allowed_hosts or ())


def enforce_tls(url: str, *, allowed_hosts: Set[str] | None = None) -> None:
    """Require https for remote MCP URLs. Plain http is permitted ONLY when the
    host (or host:port) is explicitly in ``allowed_hosts`` (an operator-pinned
    local/dev server). Raises InsecureTransportError otherwise."""
    try:
        scheme = (urlparse(url).scheme or "").lower()
    except Exception:
        scheme = ""
    if scheme == "https":
        return
    if scheme == "http" and _operator_allows(url, allowed_hosts):
        logger.info("[MCP] allowing plaintext http to allowlisted host %s", _host(url))
        return
    raise InsecureTransportError(
        f"refusing plaintext MCP transport for {url!r} — use https or add the "
        f"host to allowed_mcp_hosts")


def mcp_host_allowed(url: str, *, allowed_hosts: Set[str]) -> bool:
    """SSRF host-policy -- the canonical verdict (R-A11b-1).

    Delegates to :func:`egress.admit_url` (and through it ``net_safety``): the
    name is RESOLVED and every address must be public, unless the operator named
    this host[:port] in ``allowed_hosts``. It used to admit ANY hostname without
    resolving it ("DNS-rebinding defense is a manager-level concern"), so a name
    pointing at 169.254.169.254 passed. Fail-closed on a malformed URL."""
    from systemu.runtime.mcp.sdk.egress import admit_url
    return admit_url(url, allowed_hosts=allowed_hosts or ()).admitted is True


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
