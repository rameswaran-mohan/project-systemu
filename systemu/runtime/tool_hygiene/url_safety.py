"""URL safety checks for outbound network tools.

Allowlist scheme + denylist private/loopback IPs to prevent SSRF + local
filesystem reads disguised as URL fetches.
"""
from __future__ import annotations

import ipaddress
from urllib.parse import urlparse

_ALLOWED_SCHEMES = frozenset({"http", "https"})


def is_url_safe(url: str) -> bool:
    """Return True if the URL targets a public hostname over http(s).

    Rejects:
    - file://, javascript:, etc. — non-http(s) schemes
    - localhost, 127.0.0.1, ::1
    - RFC1918 private ranges: 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16
    - Empty / malformed URLs
    """
    if not url or not isinstance(url, str):
        return False
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    scheme = (parsed.scheme or "").lower()
    if scheme not in _ALLOWED_SCHEMES:
        return False
    host = (parsed.hostname or "").lower().strip()
    if not host:
        return False
    if host in {"localhost"}:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # Hostname — assume public; production callers can layer DNS-level checks.
        return True
    if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_unspecified:
        return False
    return True
