"""R-A11 — the ONE canonical fail-closed SSRF / net-safety module.

Before this, the adversarially-hardened outbound-address logic (reject loopback /
private / link-local / IMDS 169.254.169.254 / reserved / multicast / unspecified,
plus IPv6 whose EMBEDDED IPv4 via 6to4 / ipv4-mapped / teredo is non-global, plus
mixed-resolution fail-closed to close DNS-rebind) lived only in
``readback_client.py``. Meanwhile ``web_access._http_get/_http_post`` reached
``urllib.request.urlopen`` with NO guard at all — a live SSRF/confused-deputy hole
reachable from ``web_read`` / ``web_search`` / ``find_places`` / ``geocode``.

This module lifts that gold-standard logic VERBATIM so every outbound surface routes
through the same gate (the "dedupe"), and adds :func:`url_is_admissible` as the
surface-facing check. :func:`vet_host` is the one resolve-then-check primitive
underneath all of it (R-A11b-1): it returns the exact addresses it checked, so a
caller can connect to them instead of re-resolving, and it refuses non-canonical
numeric IPv4 spellings (decimal / octal / hex / short) without asking a resolver.
It is PURE — at most one ``socket.getaddrinfo`` per call, no durable writer, and
it NEVER raises (a failure is a refusal, never an admit).

Fail-closed contract (pinned in ``tests/test_net_safety.py``; the readback
adversarial suite is the byte-identical regression floor):
  * any non-global resolved/literal address ⇒ refuse;
  * a resolution failure or an unparseable/absent host ⇒ refuse;
  * a mixed set (one public + one private) ⇒ refuse the WHOLE set (rebind guard);
  * NAT64 / 6to4 / ipv4-mapped / teredo embeddings of a non-global IPv4 ⇒ refuse.

``allowed_hosts`` is the operator escape hatch (explicit opt-in only — a listed
host is admitted even if it resolves private); it is empty by default.

DNS-rebind TOCTOU: :func:`url_is_admissible` is resolve-then-reject (it validates the
host but a subsequent re-resolve at connect could rebind), so the SOCKET must dial the
vetted IP, not the name. :func:`resolve_pinned_ip` returns that vetted-public literal;
the money-move readback path and (as of the socket-pin follow-up) ``web_access`` both
pin their connections to it, so the address the kernel dials is byte-identical to the
address this module approved. The remaining resolve-then-reject-only surfaces (legacy
``web/fetch_core`` httpx, the Chromium render path) still close the gross hole
(IMDS/localhost/RFC-1918/file://); pinning them is a further follow-up.
"""
from __future__ import annotations

import ipaddress
import logging
import re
import socket
from dataclasses import dataclass
from typing import Any, Iterable, Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_HTTP_SCHEMES = ("http", "https")


def allowed_outbound_hosts() -> "set[str]":
    """Operator escape hatch (the single source for every egress surface): hosts
    admitted past the SSRF gate even if they resolve private. Comma-separated
    ``SYSTEMU_ALLOWED_OUTBOUND_HOSTS``; empty by default (explicit opt-in only)."""
    import os
    raw = os.environ.get("SYSTEMU_ALLOWED_OUTBOUND_HOSTS", "")
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


# ── address classification (lifted verbatim from readback_client) ────────────

def ip_is_non_global(ip: Any) -> bool:
    """True if an ``ip_address`` is NON-public: loopback / private / link-local
    (incl. 169.254.169.254 metadata) / reserved / multicast / unspecified."""
    return bool(ip.is_loopback or ip.is_private or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


def embedded_ipv4_is_non_global(ip: Any) -> bool:
    """For an IPv6 address, EXTRACT any embedded IPv4 (6to4 ``ip.sixtofour``,
    ``ip.ipv4_mapped``, teredo client ``ip.teredo[1]``) and return True if that
    embedded IPv4 is NON-global.

    Why: a 6to4 literal (2002::/16) embedding an RFC1918/metadata IPv4 classifies as
    ``is_global`` on the OUTER IPv6 (none of the is_* flags set), so it would pass
    the gate — yet a 6to4 tunnel translates 2002:a9fe:a9fe:: back to 169.254.169.254
    (IMDS) / 2002:a00:5:: → 10.0.0.5. Same threat class as the NAT64 sibling
    (64:ff9b::/96, is_reserved). ipv4-mapped (::ffff:a.b.c.d) is already caught via
    is_private/is_reserved on the outer address; re-checking here is harmless
    belt-and-braces. Never raises."""
    try:
        if not isinstance(ip, ipaddress.IPv6Address):
            return False
        embedded: "list[Any]" = []
        for attr in ("sixtofour", "ipv4_mapped"):
            v = getattr(ip, attr, None)
            if v is not None:
                embedded.append(v)
        teredo = getattr(ip, "teredo", None)
        if teredo:
            try:
                embedded.append(teredo[1])   # (server_ipv4, client_ipv4)
            except Exception:
                pass
        return any(ip_is_non_global(v4) for v4 in embedded)
    except Exception:
        # defensive: never let an attribute quirk turn a refusal into an admit.
        return False


# A host made only of digits / hex runs / dots that ``ipaddress`` did NOT accept
# as a canonical literal: decimal ("2130706433"), octal ("0177.0.0.1"), hex
# ("0x7f.1") and short ("127.1") IPv4 spellings. The OS resolver happily maps
# every one of them to an address (all of the above are 127.0.0.1), but whether
# a given resolver parses or forwards them varies, so they are refused
# STRUCTURALLY -- no resolver is consulted. No real DNS name is all-numeric.
_NONCANONICAL_IPV4 = re.compile(r"^(0x[0-9a-f]*|[0-9]+)(\.(0x[0-9a-f]*|[0-9]+)){0,3}\.?$",
                                re.IGNORECASE)


@dataclass(frozen=True)
class HostVerdict:
    """The outcome of vetting ONE host: admitted with the exact addresses that
    were checked (connect to these and nothing else), or refused with a reason.

    ``addresses`` are normalised literals (IPv6 UNBRACKETED), de-duplicated, in
    resolver order; empty when refused."""

    host: str
    admitted: bool
    addresses: Tuple[str, ...]
    reason: str
    # why it was refused, for callers that act on the class of refusal:
    # "non_public" | "non_canonical" | "dns" | "no_address" | "no_host" | "error"
    kind: str = ""


def _refuse(host: str, reason: str, kind: str) -> HostVerdict:
    return HostVerdict(host=host, admitted=False, addresses=(), reason=reason, kind=kind)


def vet_host(host: str) -> HostVerdict:
    """THE resolve-then-check primitive every egress surface uses.

    * a canonical IP literal is classified directly (no DNS);
    * a non-canonical numeric spelling of an IPv4 is refused structurally;
    * a hostname is resolved with exactly ONE ``socket.getaddrinfo(host, None)``
      and EVERY resolved address must be public (reject loopback / private /
      link-local [incl. 169.254.169.254] / reserved / multicast / unspecified,
      plus any IPv6 whose embedded IPv4 [6to4 / ipv4-mapped / teredo] is
      non-global). A mixed set is refused WHOLE (rebind guard).

    The caller connects to ``addresses`` -- never re-resolves the name (that
    second resolution is the DNS-rebind TOCTOU). Fail-closed; never raises."""
    try:
        name = str(host or "").strip().lower()
        if name.startswith("[") and name.endswith("]"):
            name = name[1:-1]
        if not name:
            return _refuse(name, "no host", "no_host")
        try:
            literal = ipaddress.ip_address(name)
        except ValueError:
            literal = None
        if literal is not None:
            if ip_is_non_global(literal) or embedded_ipv4_is_non_global(literal):
                return _refuse(name, f"{name} is a non-public address", "non_public")
            return HostVerdict(host=name, admitted=True, addresses=(str(literal),), reason="")
        if _NONCANONICAL_IPV4.match(name):
            return _refuse(name, f"{name!r} is a non-canonical numeric IP spelling",
                           "non_canonical")
        try:
            infos = socket.getaddrinfo(name, None)
        except Exception as exc:
            return _refuse(name, f"DNS lookup failed for {name!r} ({type(exc).__name__})",
                           "dns")
        vetted: "list[str]" = []
        for info in infos:
            addr = info[4][0]
            try:
                ip = ipaddress.ip_address(addr)
            except ValueError:
                continue
            if ip_is_non_global(ip) or embedded_ipv4_is_non_global(ip):
                # fail-closed on ANY non-global address in the set
                return _refuse(name, f"{name} resolves to non-public address {ip}",
                               "non_public")
            literal_text = str(ip)
            if literal_text not in vetted:
                vetted.append(literal_text)
        if not vetted:
            return _refuse(name, f"{name} resolved to no usable address", "no_address")
        return HostVerdict(host=name, admitted=True, addresses=tuple(vetted), reason="")
    except Exception:
        # defensive: a quirk anywhere above is a refusal, never an admit.
        return _refuse(str(host or ""), "host could not be vetted", "error")


def resolve_pinned_ip(host: str) -> Optional[str]:
    """Resolve a HOSTNAME to a single VETTED-PUBLIC IP LITERAL to PIN a socket to.

    Exactly :func:`vet_host` (ONE ``socket.getaddrinfo`` for a hostname, EVERY
    resolved address must be public, a mixed set refused whole, non-canonical
    numeric spellings refused without resolving). Returns the FIRST vetted
    address as a normalised literal (IPv6 UNBRACKETED), or None when refused
    (fail-closed). Never raises."""
    verdict = vet_host(host)
    if verdict.admitted is not True or not verdict.addresses:
        return None
    return verdict.addresses[0]


def host_resolves_public(host: str) -> bool:
    """Resolve-then-reject SSRF guard for a HOSTNAME: True iff the host resolves and
    EVERY resolved address is public. Failure / non-global ⇒ False (fail-closed).
    Exactly ``resolve_pinned_ip(host) is not None``."""
    return resolve_pinned_ip(host) is not None


# ── the surface gate ─────────────────────────────────────────────────────────

def url_is_admissible(url: str, *, allowed_hosts: Iterable[str] = frozenset(),
                      require_https: bool = False) -> bool:
    """True iff ``url`` is safe to fetch: an http(s) scheme, a present host, and the
    host is either operator-allowlisted, a public literal IP, or a hostname whose
    resolution is entirely public. Fail-closed and never raises.

    ``allowed_hosts`` (case-insensitive) is the operator escape hatch — a listed
    host is admitted WITHOUT the public-resolution requirement (explicit widening
    only). ``require_https`` rejects plain http."""
    try:
        parsed = urlparse(url or "")
    except Exception:
        return False
    scheme = (parsed.scheme or "").lower()
    if scheme not in _HTTP_SCHEMES:
        return False
    if require_https and scheme != "https":
        return False
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return False

    allow = {h.strip().lower() for h in (allowed_hosts or ())}
    if host in allow:
        return True   # operator-configured widening — admitted regardless of resolution

    # Literal IP host → validate directly (no DNS). Covers IMDS + NAT64/6to4 literals.
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        return not (ip_is_non_global(ip) or embedded_ipv4_is_non_global(ip))

    # Hostname → resolve-then-reject (every resolved address must be public).
    return host_resolves_public(host)
