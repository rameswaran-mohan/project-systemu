"""URL safety checks for outbound network tools.

R-A11b-1: this is no longer a check of its own. It delegates to the ONE
canonical SSRF guard, :func:`systemu.runtime.net_safety.url_is_admissible`:
an http(s) scheme, a host, and either a public literal IP or a hostname whose
resolution is ENTIRELY public (resolved once; loopback / RFC-1918 / link-local
incl. 169.254.169.254 / reserved / multicast / unspecified, IPv6 with an
embedded non-public IPv4, and non-canonical numeric IP spellings all refused).

It used to admit ANY hostname unresolved ("production callers can layer
DNS-level checks") -- a name pointing at an internal address passed. A caller
that will CONNECT must not re-resolve the name after this check; it pins to the
vetted address with :func:`net_safety.resolve_pinned_ip` instead (see
``readback_client``).
"""
from __future__ import annotations


def is_url_safe(url: str) -> bool:
    """True iff ``url`` passes the canonical SSRF guard (``net_safety``).

    Fail-closed: a non-string, an unparseable URL, a non-http(s) scheme, a
    missing host, a DNS failure or any non-public resolved address is False.
    Never raises."""
    if not isinstance(url, str) or not url:
        return False
    from systemu.runtime.net_safety import url_is_admissible
    return url_is_admissible(url) is True
