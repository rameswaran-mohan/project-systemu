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
    link-local / cloud-metadata (169.254.169.254) — literal IPs via
    :func:`tool_hygiene.url_safety.is_url_safe`, hostnames via a resolve-then-reject
    step mirroring ``mcp.sdk.manager._ssrf_precheck``.
  * DNS-rebind TOCTOU CLOSED — the socket is PINNED to the vetted IP: a hostname is
    resolved EXACTLY ONCE and the GET connects to THAT ip literal (no 2nd DNS query at
    connect), while the ``Host`` header + TLS SNI/cert-verification stay the original
    hostname via the httpx ``sni_hostname`` request extension (see ``readback`` /
    ``_resolve_pinned_ip``). A literal-IP host was never a rebind risk.
  * short timeout + a response-size cap (mirrors ``web.fetch_core.fetch_url``).
  * NEVER raises into a run — ANY error returns ``{}`` (⇒ the verifier fails closed).

Injected at ``ShadowRuntime.__init__`` as ``runtime._external_api_client`` ONLY when
the S4 stamp net is armed (mode != off), so OFF issues no outbound GET at all.
"""
from __future__ import annotations

import ipaddress
import logging
import socket
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from systemu.runtime.tool_hygiene.url_safety import is_url_safe

logger = logging.getLogger(__name__)

_MAX_BYTES = 1 * 1024 * 1024   # 1 MiB readback cap
_TIMEOUT_S = 8.0


def _ip_is_non_global(ip: Any) -> bool:
    """True if an ``ip_address`` is NON-public: loopback / private / link-local
    (incl. 169.254.169.254 metadata) / reserved / multicast / unspecified."""
    return bool(ip.is_loopback or ip.is_private or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


def _embedded_ipv4_is_non_global(ip: Any) -> bool:
    """For an IPv6 address, EXTRACT any embedded IPv4 (6to4 ``ip.sixtofour``,
    ``ip.ipv4_mapped``, teredo client ``ip.teredo[1]``) and return True if that
    embedded IPv4 is NON-global.

    Why this matters: a 6to4 literal (2002::/16) embedding an RFC1918/metadata
    IPv4 classifies as ``is_global`` on the OUTER IPv6 (none of is_loopback/
    is_private/is_link_local/is_reserved/is_multicast/is_unspecified is set), so it
    would otherwise PASS the gate — yet a 6to4 tunnel translates 2002:a9fe:a9fe::
    back to 169.254.169.254 (IMDS) / 2002:a00:5:: → 10.0.0.5. The SAME threat class
    the NAT64 sibling (64:ff9b::/96, is_reserved) was closed for. ipv4-mapped
    (::ffff:a.b.c.d) is already caught via is_private/is_reserved on the outer
    address, but re-checking it here is harmless belt-and-braces. Never raises."""
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
            # teredo == (server_ipv4, client_ipv4); the client IPv4 is the tunnel
            # endpoint that a teredo relay would translate to.
            try:
                embedded.append(teredo[1])
            except Exception:
                pass
        return any(_ip_is_non_global(v4) for v4 in embedded)
    except Exception:
        # defensive: never let an attribute quirk turn a refusal into an admit.
        return False


def _resolve_pinned_ip(host: str) -> Optional[str]:
    """Resolve a HOSTNAME to a single VETTED-PUBLIC IP LITERAL to PIN the socket to —
    the mechanism that CLOSES the DNS-rebind TOCTOU: ONE resolve, connect to THAT ip,
    never a second resolve.

    Does exactly ONE ``socket.getaddrinfo(host, None)`` and validates that EVERY
    resolved address is public (the exact ``_resolves_to_public`` checks: reject
    loopback / private / link-local [incl. 169.254.169.254 metadata] / reserved /
    multicast / unspecified, plus any IPv6 whose EMBEDDED IPv4 [6to4 / ipv4-mapped /
    teredo] is non-global). RETURNS the FIRST vetted address as a normalised literal
    (family-appropriate; IPv6 is returned UNBRACKETED — the URL rewriter brackets it).
    Returns None if ANY address is non-global OR resolution fails OR nothing resolved
    (fail-closed). Never raises."""
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return None
    first_public: Optional[str] = None
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if _ip_is_non_global(ip) or _embedded_ipv4_is_non_global(ip):
            return None            # fail-closed on ANY non-global address in the set
        if first_public is None:
            first_public = str(ip)  # a normalised literal (no zone/scope suffix)
    return first_public


def _resolves_to_public(host: str) -> bool:
    """Resolve-then-reject SSRF guard for a HOSTNAME: True iff the host resolves and
    EVERY resolved address is public. A resolution failure / non-global address ⇒
    False (fail-closed). Mirrors ``mcp.sdk.manager._ssrf_precheck``.

    Single source of truth: this is exactly ``_resolve_pinned_ip(host) is not None``
    (the readback path pins the socket to that vetted IP — see ``_resolve_pinned_ip``
    — so the DNS-rebind window the previous implementation documented is now CLOSED)."""
    return _resolve_pinned_ip(host) is not None


def _prevalidate_url(url: str) -> Optional["tuple[str, Any]"]:
    """https-only + host-present + literal-IP SSRF checks, WITHOUT resolving a
    hostname (so the readback path can resolve exactly ONCE, in ``_resolve_pinned_ip``).

    Returns ``(host, ip)`` where ``ip`` is the ``ip_address`` object if the host is a
    LITERAL IP (already validated public), or ``None`` if the host is a HOSTNAME (the
    caller must still resolve + pin). Returns ``None`` outright if the url is
    inadmissible on scheme / host-presence / literal-IP grounds."""
    if not is_url_safe(url):        # scheme http(s) + literal-IP private/loopback/link-local reject
        return None
    parsed = urlparse(url)
    if (parsed.scheme or "").lower() != "https":   # is_url_safe also allows http — tighten to https
        return None
    host = (parsed.hostname or "").lower().strip()
    if not host:
        return None
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return (host, None)          # a hostname — the caller resolves + pins
    # is_url_safe already rejected loopback/private/link-local/unspecified, but it
    # does NOT reject is_reserved / is_multicast — so a NAT64 well-known-prefix
    # literal that embeds a metadata/RFC1918 IPv4 (64:ff9b::169.254.169.254, which
    # a NAT64/IPv6-only gateway translates back to 169.254.169.254 → IMDS) and
    # IPv6/IPv4 multicast literals slipped through here. Mirror _resolve_pinned_ip
    # EXACTLY so the literal-IP branch is as strict as the hostname branch.
    if _ip_is_non_global(ip):
        return None
    # a 6to4 / teredo / ipv4-mapped IPv6 literal that classifies as is_global on the
    # OUTER address but EMBEDS an internal IPv4 (2002:a9fe:a9fe:: → 169.254.169.254)
    # must also be refused — same threat class as the NAT64 literal above.
    if _embedded_ipv4_is_non_global(ip):
        return None
    return (host, ip)                # a validated public literal — already pinned


def _url_is_admissible(url: str) -> bool:
    """https-only + public-host SSRF gate (runs BEFORE any connect). Rejects
    non-https, and private/loopback/link-local/metadata targets — literal IPs via
    ``is_url_safe`` (no network), hostnames via ``_resolves_to_public`` (a DNS
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
