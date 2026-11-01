"""R-A13b-2i — the PRODUCTION independent-https readback client.

The ``ExternalVerifier``'s hardened ``api_readback`` path RE-READS an effect from
the SAME authenticated host to deterministically match a submission-unique,
provably-fresh token. This is the production transport for that read-back, and it
is SAFETY-CRITICAL: it issues an OUTBOUND GET on a URL that ultimately traces to
an agent-supplied directive, so it is an SSRF surface.

Guarantees:
  * a FRESH ``httpx.Client`` per call — NO cookies / credentials / session are ever
    shared with the submit path (an independent reader, not the effecting client).
  * ``follow_redirects=False`` — a followed redirect would defeat the upstream
    host-pin / this SSRF gate, so a redirect is NEVER chased (and its body is not
    read).
  * defense-in-depth SSRF re-validation BEFORE any connect (even though the
    verifier host-pins upstream): https-only + reject private / loopback /
    link-local / cloud-metadata (169.254.169.254) — literal IPs classified by the
    canonical ``net_safety`` classifiers, hostnames resolved ONCE through
    :func:`net_safety.resolve_pinned_ip` (R-A11b-1: one guard, no private copy).
  * DNS-rebind TOCTOU CLOSED — the socket is PINNED to the vetted IP: a hostname is
    resolved EXACTLY ONCE and the GET connects to THAT ip literal (no 2nd DNS query at
    connect), while the ``Host`` header + TLS SNI/cert-verification stay the original
    hostname via the httpx ``sni_hostname`` request extension (see ``readback`` /
    ``_resolve_pinned_ip``). A literal-IP host was never a rebind risk.
  * short timeout + a response-size cap (mirrors ``web.fetch_core.fetch_url``).
  * NEVER raises into a run — ANY error returns ``{}`` (⇒ the verifier fails closed).

Injected UNCONDITIONALLY at ``ShadowRuntime.__init__`` as ``runtime._external_api_client``
(since R-A14a): the tier-2 MCP actuation path drives an independent readback net-OFF, so the
client must be present even when the S4 stamp net is off. It is DORMANT unless something
actually drives verification — an OFF non-MCP run stamps no ``requires_external_verification``
and issues no outbound GET; the GET fires only for a known-mutation MCP actuation (or an armed
stamp net). ANY error still returns ``{}`` (⇒ the verifier fails closed).
"""
from __future__ import annotations

import ipaddress
import logging
import socket  # noqa: F401 -- the resolver seam the readback suite patches
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from systemu.runtime import net_safety

logger = logging.getLogger(__name__)

_MAX_BYTES = 1 * 1024 * 1024   # 1 MiB readback cap
_TIMEOUT_S = 8.0


# R-A11b-1: the address classification and the resolve-once-and-pin primitive are
# the CANONICAL ones in ``net_safety`` (this module is where they were first
# hardened; they were lifted there verbatim and this copy is now retired). The
# private names stay as aliases because the adversarial readback suite pins them.
_ip_is_non_global = net_safety.ip_is_non_global
_embedded_ipv4_is_non_global = net_safety.embedded_ipv4_is_non_global


def _resolve_pinned_ip(host: str) -> Optional[str]:
    """Resolve a HOSTNAME ONCE to a vetted-public IP literal to pin the socket to
    (closes the DNS-rebind TOCTOU). Exactly :func:`net_safety.resolve_pinned_ip`:
    one ``socket.getaddrinfo``, EVERY resolved address must be public, a mixed
    set is refused whole, None on any refusal. Looked up on the module at call
    time so the one implementation is the one that runs."""
    return net_safety.resolve_pinned_ip(host)


def _resolves_to_public(host: str) -> bool:
    """Resolve-then-reject SSRF guard for a HOSTNAME: True iff the host resolves and
    EVERY resolved address is public. A resolution failure / non-global address ⇒
    False (fail-closed). The canonical ``net_safety`` decision.

    Single source of truth: this is exactly ``_resolve_pinned_ip(host) is not None``
    (the readback path pins the socket to that vetted IP — see ``_resolve_pinned_ip``
    — so the DNS-rebind window the previous implementation documented is now CLOSED)."""
    return _resolve_pinned_ip(host) is not None


def _prevalidate_url(url: str) -> Optional["tuple[str, Any]"]:
    """https-only + host-present + literal-IP SSRF checks, WITHOUT resolving a
    hostname (so the readback path resolves exactly ONCE, in ``_resolve_pinned_ip``).

    Returns ``(host, ip)`` where ``ip`` is the ``ip_address`` object if the host is a
    LITERAL IP (already validated public), or ``None`` if the host is a HOSTNAME (the
    caller must still resolve + pin). Returns ``None`` outright if the url is
    inadmissible on scheme / host-presence / literal-IP grounds.

    R-A11b-1: this no longer calls ``url_safety.is_url_safe`` -- that check now
    RESOLVES the name (it delegates to ``net_safety``), and calling it here would
    resolve twice and reopen the rebind window between the check and the pin. The
    literal-IP branch uses the canonical classifiers directly."""
    if not isinstance(url, str) or not url:
        return None
    try:
        parsed = urlparse(url)
    except Exception:
        return None
    if (parsed.scheme or "").lower() != "https":   # https only (no http, file, ...)
        return None
    host = (parsed.hostname or "").lower().strip()
    if not host or host == "localhost":
        return None
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return (host, None)          # a hostname — the caller resolves + pins
    # A literal: the canonical classification -- loopback / private / link-local
    # (incl. 169.254.169.254) / reserved (NAT64 64:ff9b::/96) / multicast /
    # unspecified, and any IPv6 whose EMBEDDED IPv4 (6to4 / ipv4-mapped / teredo)
    # is non-global (2002:a9fe:a9fe:: is 169.254.169.254 behind a 6to4 relay).
    if _ip_is_non_global(ip) or _embedded_ipv4_is_non_global(ip):
        return None
    return (host, ip)                # a validated public literal — already pinned


def _url_is_admissible(url: str) -> bool:
    """https-only + public-host SSRF gate (runs BEFORE any connect). Rejects
    non-https, and private/loopback/link-local/metadata targets — literal IPs via
    the canonical classifiers (no network), hostnames via ``_resolves_to_public`` (a DNS
    resolve, not a connect). Thin composition of ``_prevalidate_url`` + the hostname
    resolve, kept for callers that only need an admissibility verdict."""
    pre = _prevalidate_url(url)
    if pre is None:
        return False
    host, ip = pre
    if ip is None:                   # a hostname — resolve-then-reject
        return _resolves_to_public(host)
    return True                      # a validated public literal


def _tokens_from_json(data: Any, _depth: int = 0) -> "list[str]":
    """Flatten a JSON structure into scalar token strings for exact matching
    (bounded recursion; never raises). Free-text bodies are matched separately via
    ``response_body``, so this only needs the scalar leaves."""
    out: "list[str]" = []
    if _depth > 6:
        return out
    try:
        if isinstance(data, dict):
            for v in data.values():
                out.extend(_tokens_from_json(v, _depth + 1))
        elif isinstance(data, (list, tuple)):
            for v in data:
                out.extend(_tokens_from_json(v, _depth + 1))
        elif isinstance(data, bool):
            return out                      # skip booleans (never a token)
        elif isinstance(data, (str, int, float)):
            s = str(data).strip()
            if s:
                out.append(s)
    except Exception:
        return out
    return out


class ProdReadbackClient:
    """A credential-less, independent-https readback transport for the hardened
    ``api_readback`` path.

    ``readback(url) -> {"observed_tokens": [...], "response_body": <str>}``. NEVER
    raises (returns ``{}`` on any error / refusal ⇒ the verifier fails closed).
    """

    def __init__(self, *, timeout: float = _TIMEOUT_S, max_bytes: int = _MAX_BYTES,
                 transport: Any = None) -> None:
        self._timeout = timeout
        self._max_bytes = max_bytes
        self._transport = transport    # injectable httpx transport for hermetic tests

    def readback(self, url: str) -> Dict[str, Any]:
        try:
            # SSRF gate FIRST (no hostname resolve here) — refuse before any connect.
            pre = _prevalidate_url(url)
            if pre is None:
                logger.debug("[ProdReadbackClient] refused inadmissible readback url (SSRF gate)")
                return {}
            host, ip = pre
            import httpx
            headers = {
                "User-Agent": "SystemuReadback/1.0",
                "Accept": "application/json, text/plain, */*",
                # do NOT send cookies/auth — this is an INDEPENDENT reader.
            }
            # PIN the socket to the vetted IP (closes the DNS-rebind TOCTOU). For a
            # HOSTNAME: ONE resolve → the vetted public IP, connect to THAT ip (no 2nd
            # DNS query at connect), while the Host header + TLS SNI/cert-verification
            # stay the ORIGINAL hostname via the httpx ``sni_hostname`` extension. For
            # a LITERAL IP (already validated public): connect directly — already pinned.
            request_url = url
            req_kwargs: Dict[str, Any] = {}
            if ip is None:                      # a hostname — resolve exactly once + pin
                pinned = _resolve_pinned_ip(host)
                if pinned is None:
                    logger.debug("[ProdReadbackClient] hostname did not resolve to a vetted public IP — refused")
                    return {}
                # rewrite host → the pinned IP (copy_with auto-brackets IPv6, keeps
                # scheme / port / path / query); the socket connects to the pinned IP.
                request_url = str(httpx.URL(url).copy_with(host=pinned))
                port = urlparse(url).port
                host_header = host if (port is None or port == 443) else f"{host}:{port}"
                # Host stays the hostname (vhost routing); sni_hostname drives the TLS
                # handshake → SNI + cert verification against the ORIGINAL hostname
                # (httpcore: server_hostname = sni_hostname or origin.host).
                req_kwargs["headers"] = {"Host": host_header}
                req_kwargs["extensions"] = {"sni_hostname": host}
            client_kwargs: Dict[str, Any] = dict(
                follow_redirects=False, timeout=self._timeout, headers=headers)
            if self._transport is not None:
                client_kwargs["transport"] = self._transport
            # a FRESH client per call — no shared cookie jar / session with submit.
            with httpx.Client(**client_kwargs) as c:
                r = c.get(request_url, **req_kwargs)
                # follow_redirects=False ⇒ a 3xx is returned WITHOUT following. Never
                # chase / read a redirect target (that would defeat the host-pin).
                if r.is_redirect:
                    logger.debug("[ProdReadbackClient] readback returned a redirect — not followed")
                    return {}
                body = r.text[:self._max_bytes]
                observed: "list[str]" = []
                ctype = (r.headers.get("content-type") or "").lower()
                if "json" in ctype:
                    try:
                        observed = _tokens_from_json(r.json())
                    except Exception:
                        observed = []
                return {"observed_tokens": observed, "response_body": body}
        except Exception:
            logger.debug("[ProdReadbackClient] readback failed — returning {} (fail-closed)",
                         exc_info=True)
            return {}
