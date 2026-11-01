"""R-A11b-1 -- the ONE SSRF verdict for every remote MCP connect.

Before this, the MCP manager carried its own weaker resolve-and-check copy
(``_ssrf_precheck``), ``remote_policy.mcp_host_allowed`` admitted ANY hostname
without resolving it, and the list / call paths had no check at all. Every
remote MCP connect now asks :func:`admit_url`, which delegates the address
decision to the canonical guard (:func:`systemu.runtime.net_safety.vet_host`):
resolve the name ONCE, check EVERY resolved address against the private /
loopback / link-local / metadata / reserved ranges, and hand back the exact
addresses that were checked so the connection can be made to them (the
connect-time pinning lives in ``compat``; see that module for which lines pin).

The operator's explicit allowance
---------------------------------
``HarnessPolicy.allowed_mcp_hosts`` (config ``allowed_mcp_hosts`` / env
``SYSTEMU_HARNESS_ALLOWED_MCP_HOSTS``) is the only widening. Entries are either

  * ``host:port`` (``[v6]:port`` for IPv6) -- admits exactly that host AND port;
  * a bare ``host`` -- admits that host on ANY port. Kept so existing operator
    configs (``127.0.0.1``, ``localhost``) do not silently start refusing.

An allowed entry is admitted WITHOUT the public-address requirement and is not
pinned: the operator vouched for the NAME, so there is no checked address to
hold the connection to. Nothing else widens the guard -- in particular NOT the
server addresses saved in ``<vault>/connections/mcp.json``: that file has
model-reachable writers (the runtime-connect provisioner and the dispatch
rug-pull re-pin), so by DEC-34 the verifier may not consult it.

The verdict is a VALUE (DEC-32): an :class:`EgressVerdict` can only be minted
here, carries the URL it was issued for, and the connect seam checks its
concrete type before opening anything remote.
"""
from __future__ import annotations

import ipaddress
import logging
import threading
from dataclasses import dataclass, field
from typing import Iterable, Optional, Tuple
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

_MINT = object()          # only admit_url holds this; see EgressVerdict.__post_init__
_SCHEMES = {"http": 80, "https": 443}
ALLOWANCE_ENV = "SYSTEMU_HARNESS_ALLOWED_MCP_HOSTS"
# Operator-declared servers (v0.9.5 semantics). Each entry counts as an allowance
# for EXACTLY its host:port (R-2 c): set by the operator at process start, the
# same trust as ALLOWANCE_ENV. Its only model-reachable writer (credential
# promotion) is fenced by R-4 (systemu/core/reserved_env.py).
SERVER_URLS_ENV = "SYSTEMU_MCP_SERVER_URLS"


@dataclass(frozen=True)
class McpAllowance:
    """One parsed operator allowance entry. ``port is None`` = any port."""

    host: str
    port: Optional[int]


def _norm_host(host: str) -> str:
    """Lower-case; an IP literal in its canonical compressed form."""
    h = str(host or "").strip().lower()
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    try:
        return str(ipaddress.ip_address(h))
    except ValueError:
        return h


def parse_allowance(entries: Iterable[str]) -> Tuple[McpAllowance, ...]:
    """Parse ``allowed_mcp_hosts`` entries. Malformed entries are dropped (and
    logged): a typo can only ever NARROW the allowance, never widen it."""
    out = []
    for raw in entries or ():
        text = str(raw or "").strip()
        if not text:
            continue
        try:
            # A bare IPv6 literal ("::1") has no port and no brackets.
            try:
                out.append(McpAllowance(host=str(ipaddress.ip_address(text.lower())),
                                        port=None))
                continue
            except ValueError:
                pass
            parts = urlsplit("//" + text)
            host = parts.hostname
            port = parts.port            # ValueError for a malformed port
        except ValueError:
            logger.warning("[MCP] ignoring malformed allowed_mcp_hosts entry %r", text)
            continue
        if not host or parts.path or parts.query or parts.fragment or parts.username:
            logger.warning("[MCP] ignoring malformed allowed_mcp_hosts entry %r", text)
            continue
        out.append(McpAllowance(host=_norm_host(host), port=port))
    return tuple(out)


def format_entry(host: str, port: int) -> str:
    """The exact allowance entry for host:port (IPv6 bracketed)."""
    h = _norm_host(host)
    return f"[{h}]:{port}" if ":" in h else f"{h}:{port}"


def entry_for_url(url: str) -> str:
    """The exact ``host:port`` allowance entry a URL needs ("" if malformed)."""
    try:
        parts = urlsplit(str(url or ""))
        scheme = (parts.scheme or "").lower()
        host = parts.hostname or ""
        port = parts.port
    except ValueError:
        return ""
    if scheme not in _SCHEMES or not host:
        return ""
    return format_entry(host, _SCHEMES[scheme] if port is None else port)


def env_server_allowance() -> Tuple[str, ...]:
    """Each ``SYSTEMU_MCP_SERVER_URLS`` entry as an EXACT host:port allowance."""
    import os
    raw = os.environ.get(SERVER_URLS_ENV, "") or ""
    entries = {entry_for_url(u.strip()) for u in raw.split(",") if u.strip()}
    return tuple(sorted(e for e in entries if e))


def operator_allowance() -> Tuple[str, ...]:
    """The operator's MCP allowance: ``HarnessPolicy.allowed_mcp_hosts`` (read
    the way the runtime-connect path reads it -- the runtime config, env
    overriding) plus every env-declared server's exact host:port. An unreadable
    policy contributes nothing (fail-closed)."""
    policy: Tuple[str, ...] = ()
    try:
        from systemu.runtime.harness_policy import HarnessPolicy
        try:
            from sharing_on.config import Config
            cfg = Config.from_env()
        except Exception:
            cfg = None
        policy = tuple(HarnessPolicy.from_config(cfg).allowed_mcp_hosts or ())
    except Exception:
        logger.debug("[MCP] operator allowance unreadable -- empty", exc_info=True)
    return tuple(sorted(set(policy) | set(env_server_allowance())))


@dataclass(frozen=True)
class EgressVerdict:
    """The SSRF verdict for ONE remote MCP URL. Minted only by :func:`admit_url`.

    ``via``: ``"public"`` (every resolved address checked public; ``addresses``
    are exactly those addresses), ``"allowance"`` (the operator named this
    host[:port]; ``addresses`` is empty -- nothing to pin to) or ``"refused"``."""

    url: str
    admitted: bool
    via: str
    scheme: str
    host: str
    port: Optional[int]
    addresses: Tuple[str, ...]
    reason: str
    allowance: Tuple[str, ...] = ()      # the operator allowance it was issued under
    # refused: the class of refusal ("non_public", "dns", "non_canonical",
    # "scheme", "malformed", ...); and, for a local/private endpoint, the EXACT
    # host:port entry the operator could allow (what Settings offers to write).
    kind: str = ""
    allow_entry: str = ""
    _mint: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._mint is not _MINT:
            raise TypeError("an EgressVerdict is minted only by egress.admit_url")
        if type(self.admitted) is not bool:
            raise TypeError("EgressVerdict.admitted must be a bool")
        if self.admitted:
            if self.via not in ("public", "allowance"):
                raise ValueError(f"an admitted verdict cannot be via {self.via!r}")
            if self.via == "public" and not self.addresses:
                raise ValueError("a public verdict must carry the checked addresses")
        elif self.via != "refused" or not self.reason:
            raise ValueError("a refused verdict must say why")

    @property
    def endpoint(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{host}:{self.port}" if self.port is not None else host


def _verdict(url: str, *, admitted: bool, via: str, scheme: str = "", host: str = "",
             port: Optional[int] = None, addresses: Tuple[str, ...] = (),
             reason: str = "", allowance: Iterable[str] = (), kind: str = "",
             allow_entry: str = "") -> EgressVerdict:
    return EgressVerdict(url=url, admitted=admitted, via=via, scheme=scheme, host=host,
                         port=port, addresses=tuple(addresses), reason=reason,
                         allowance=tuple(sorted(str(a) for a in (allowance or ()))),
                         kind=kind, allow_entry=allow_entry, _mint=_MINT)


def _allowed(host: str, port: int, allowance: Tuple[McpAllowance, ...]) -> bool:
    return any(a.host == host and (a.port is None or a.port == port) for a in allowance)


def allowance_matches(url: str, allowed_hosts: Iterable[str] = ()) -> bool:
    """True iff ``url``'s host and port are named in the operator allowance.
    Pure: parses only, never resolves. A malformed URL is False."""
    try:
        parts = urlsplit(str(url or ""))
        scheme = (parts.scheme or "").lower()
        host = _norm_host(parts.hostname or "")
        port = parts.port
    except ValueError:
        return False
    if scheme not in _SCHEMES or not host:
        return False
    return _allowed(host, _SCHEMES[scheme] if port is None else port,
                    parse_allowance(allowed_hosts))


def admit_url(url: str, *, allowed_hosts: Iterable[str] = ()) -> EgressVerdict:
    """THE SSRF verdict for a remote MCP URL. Fail-closed; never raises.

    Refuses a non-http(s) scheme, a missing host or a malformed port. Admits an
    operator-allowed ``host:port`` (or bare host) as named; otherwise delegates
    to :func:`net_safety.vet_host` and admits only when every resolved address
    is public, carrying those addresses for the connection to use."""
    text = str(url or "")
    try:
        parts = urlsplit(text)
        scheme = (parts.scheme or "").lower()
        host = _norm_host(parts.hostname or "")
        port = parts.port
    except ValueError:
        return _verdict(text, admitted=False, via="refused", kind="malformed",
                        reason="the server URL is malformed (bad host or port)")
    if scheme not in _SCHEMES:
        return _verdict(text, admitted=False, via="refused", scheme=scheme, kind="scheme",
                        reason=f"unsupported scheme {scheme!r} (only http/https)")
    if not host:
        return _verdict(text, admitted=False, via="refused", scheme=scheme, kind="malformed",
                        reason="the server URL has no host")
    if port is None:
        port = _SCHEMES[scheme]
    if _allowed(host, port, parse_allowance(allowed_hosts)):
        return _verdict(text, admitted=True, via="allowance", scheme=scheme,
                        host=host, port=port, allowance=allowed_hosts)

    from systemu.runtime import net_safety
    hv = net_safety.vet_host(host)
    if type(hv) is not net_safety.HostVerdict or hv.admitted is not True:
        why = getattr(hv, "reason", "") or "host could not be vetted"
        kind = str(getattr(hv, "kind", "") or "error")
        if kind != "non_public":
            return _verdict(text, admitted=False, via="refused", scheme=scheme, host=host,
                            port=port, kind=kind, reason=why)
        entry = format_entry(host, port)
        return _verdict(text, admitted=False, via="refused", scheme=scheme, host=host,
                        port=port, kind=kind, allow_entry=entry,
                        reason=(f"{why}. To allow this local or private server, use "
                                f'"Allow {entry}" next to it in Settings > MCP servers, '
                                f"or add '{entry}' to allowed_mcp_hosts / {ALLOWANCE_ENV}"))
    return _verdict(text, admitted=True, via="public", scheme=scheme, host=host,
                    port=port, addresses=hv.addresses, allowance=allowed_hosts)


class PinnedRoutes:
    """Where every request of ONE remote MCP session may connect.

    Built from an ADMITTING verdict. The verdict's own endpoint routes to the
    addresses the guard checked (or, for an operator-allowed endpoint, by name:
    the operator vouched for the name, there is no checked address). Any OTHER
    host a request goes to -- a redirect hop, an auth server -- is vetted the
    same way the first one was (operator allowance by host:port, else ONE
    resolution through ``net_safety.vet_host`` with every address public) and
    is then pinned too; a refused host raises PermissionError. Every host is
    resolved at most once per session: a cached route is never re-resolved."""

    def __init__(self, verdict: "EgressVerdict") -> None:
        if type(verdict) is not EgressVerdict or verdict.admitted is not True:
            raise PermissionError("pinned routes need an admitting SSRF verdict")
        self._allowance = parse_allowance(verdict.allowance)
        self._routes: dict = {(verdict.host, verdict.port): tuple(verdict.addresses)}
        self._lock = threading.Lock()

    def cached(self, host: str, port: int) -> Optional[Tuple[str, ...]]:
        with self._lock:
            return self._routes.get((_norm_host(host), port))

    def route(self, scheme: str, host: str, port: int) -> Tuple[str, ...]:
        """The addresses to dial for ``host:port`` -- ``()`` means "by name"
        (operator allowance only). Blocking (may resolve once). Raises
        PermissionError when the host is refused."""
        scheme = str(scheme or "").lower()
        if scheme not in _SCHEMES:
            raise PermissionError(f"blocked by SSRF/DNS guard: unsupported scheme {scheme!r}")
        name = _norm_host(host)
        key = (name, port)
        with self._lock:
            if key in self._routes:
                return self._routes[key]
        if _allowed(name, port, self._allowance):
            addresses: Tuple[str, ...] = ()
        else:
            from systemu.runtime import net_safety
            hv = net_safety.vet_host(name)
            if type(hv) is not net_safety.HostVerdict or hv.admitted is not True:
                raise PermissionError(
                    f"blocked by SSRF/DNS guard: {getattr(hv, 'reason', '') or name}")
            addresses = tuple(hv.addresses)
        with self._lock:
            return self._routes.setdefault(key, addresses)
