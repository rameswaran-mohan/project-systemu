"""The innermost meaningful cause of an MCP connect / list / call failure.

The SDK runs every transport inside an anyio task group, so any failure reaches
the manager wrapped in one or more ``ExceptionGroup`` layers, and the real cause
often sits on a suppressed ``__context__`` (httpcore re-raises its own
``ConnectError`` ``from None``). ``str()`` of the outer group is "unhandled
errors in a TaskGroup (1 sub-exception)", which names nothing the operator can
act on. :func:`describe_failure` walks the whole tree (group members,
``__cause__`` and ``__context__``, suppressed or not, cycle-safe and depth
bounded) and reports what actually happened:

  * connection refused  -> ``connection refused by <host>:<port>``
  * DNS failure         -> ``DNS lookup failed for '<name>'``
  * a stdio server that exited on its own -> its exit code and the last lines it
    wrote to stderr, with secrets redacted BEFORE any truncation (DEC-31)
  * otherwise the innermost leaf exception, never the group.

Verdict text is ASCII-only: server-supplied fragments are redacted first, then
capped, then escaped (``backslashreplace``) -- in that order.

Pure and MCP-free: no SDK import, no network, never raises.
"""
from __future__ import annotations

import errno
import socket
import ssl
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit

# How much of a stdio server's stderr is read back (the END of it).
STDERR_WINDOW_BYTES = 16 * 1024
# How many of the last stderr lines a reason quotes, and the cap on each line.
# The cap applies to the already-MASKED line (outbox.redact(..., max_len=)).
STDERR_LINES = 5
STDERR_LINE_CAP = 240
# A child env value shorter than this is not treated as a known secret (masking
# "1" or "true" everywhere would make the tail unreadable without protecting
# anything a shape rule would not).
KNOWN_VALUE_MIN = 6
_KNOWN_VALUE_MASK = "***"

_MAX_NODES = 64
_REFUSED_ERRNOS = {errno.ECONNREFUSED, 10061}
_REFUSED_WINERRORS = {10061, 1225}
_TIMEOUT_CLASS_NAMES = {"ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout",
                        "TimeoutException"}
_DNS_TEXT = ("getaddrinfo failed", "name or service not known",
             "nodename nor servname", "no address associated", "temporary failure in name")


def _ascii(text: str) -> str:
    return str(text).encode("ascii", "backslashreplace").decode("ascii")


def _short(text: Any, cap: int = 300) -> str:
    """Redact, THEN cap, THEN escape (DEC-31 order) a free-text fragment."""
    from systemu.runtime.outbox import redact
    return _ascii(redact(str(text), max_len=cap))


def _walk(exc: BaseException) -> List[BaseException]:
    """Every exception in ``exc``'s tree, outermost first, depth-first.

    Follows group members, ``__cause__`` and ``__context__`` (even when the
    context is suppressed -- that is exactly where httpcore leaves the OSError).
    Cycle-safe and bounded."""
    out: List[BaseException] = []
    seen: set = set()
    stack: List[BaseException] = [exc]
    while stack and len(out) < _MAX_NODES:
        e = stack.pop()
        if e is None or id(e) in seen:
            continue
        seen.add(id(e))
        out.append(e)
        children: List[BaseException] = []
        if isinstance(e, BaseExceptionGroup):
            children.extend(e.exceptions)
        if e.__cause__ is not None:
            children.append(e.__cause__)
        if e.__context__ is not None:
            children.append(e.__context__)
        stack.extend(reversed(children))
    return out


def _endpoint(spec: Dict[str, Any]) -> Tuple[str, str]:
    """(host, "host:port") for a remote spec; IPv6 hosts bracketed."""
    try:
        parts = urlsplit(str(spec.get("url") or ""))
        host = (parts.hostname or "").lower()
        port = parts.port
        if port is None:
            port = {"https": 443, "http": 80}.get((parts.scheme or "").lower())
    except Exception:
        return ("", "the server")
    if not host:
        return ("", "the server")
    shown = f"[{host}]" if ":" in host else host
    return (host, f"{shown}:{port}" if port is not None else shown)


def _basename(command: str) -> str:
    return command.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def _is_refused(e: BaseException) -> bool:
    if isinstance(e, ConnectionRefusedError):
        return True
    if isinstance(e, OSError):
        if getattr(e, "errno", None) in _REFUSED_ERRNOS:
            return True
        if getattr(e, "winerror", None) in _REFUSED_WINERRORS:
            return True
    return False


def _is_dns(e: BaseException) -> bool:
    if isinstance(e, socket.gaierror):
        return True
    return type(e).__name__.endswith("ConnectError") and any(
        t in str(e).lower() for t in _DNS_TEXT)


def _is_timeout(e: BaseException) -> bool:
    return isinstance(e, TimeoutError) or type(e).__name__ in _TIMEOUT_CLASS_NAMES


def _leaf(exc: BaseException) -> BaseException:
    """The innermost exception on the EXPLICIT chain: group members, then
    ``__cause__``, then a SUPPRESSED ``__context__`` (a deliberate ``raise ...
    from None`` hides the real cause there). An unsuppressed ``__context__``
    is incidental -- "while handling X, Y happened" -- and is never the reason.
    Cycle-safe and bounded."""
    e: BaseException = exc
    seen: set = set()
    while id(e) not in seen and len(seen) < _MAX_NODES:
        seen.add(id(e))
        if isinstance(e, BaseExceptionGroup) and e.exceptions:
            e = e.exceptions[-1]
        elif e.__cause__ is not None:
            e = e.__cause__
        elif e.__context__ is not None and e.__suppress_context__:
            e = e.__context__
        else:
            break
    return e


def stderr_tail(raw: bytes, *, truncated: bool,
                known_values: Iterable[str] = ()) -> List[str]:
    """The last :data:`STDERR_LINES` non-empty stderr lines, safe to show.

    Order is the whole point (DEC-31): (1) if ``raw`` is the END of a longer
    stream, drop its first, cut line entirely -- a fragment of a line is never
    shown, so a secret cut by the read window cannot leak half of itself;
    (2) mask every known secret value (the child's own env values) by identity;
    (3) shape-redact the whole text (``outbox.redact``); (4) only THEN pick the
    last lines and cap each already-masked line; (5) escape to ASCII."""
    from systemu.runtime.outbox import redact
    text = bytes(raw or b"").decode("utf-8", "backslashreplace")
    if truncated:
        nl = text.find("\n")
        text = "" if nl < 0 else text[nl + 1:]
    for value in sorted({str(v) for v in known_values if v and len(str(v)) >= KNOWN_VALUE_MIN},
                        key=len, reverse=True):
        text = text.replace(value, _KNOWN_VALUE_MASK)
    # Whole lines are the unit: each one is redacted IN FULL (whole-value
    # credential check + span masking) and only the masked form is capped.
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return [_ascii(redact(ln, max_len=STDERR_LINE_CAP)) for ln in lines[-STDERR_LINES:]]


def describe_failure(exc: BaseException, spec: Dict[str, Any], *,
                     witness: Any = None, timeout_s: Optional[float] = None) -> str:
    """One ASCII sentence naming the innermost meaningful cause. Never raises.

    ``witness`` is the stdio :class:`~systemu.runtime.mcp.sdk.transports.StdioWitness`
    for a stdio spec (exit code + captured stderr), or ``None``."""
    try:
        return _describe(exc, spec, witness=witness, timeout_s=timeout_s)
    except Exception as inner:  # never let the describer hide the failure
        return _ascii(f"{type(exc).__name__} (cause could not be described: "
                      f"{type(inner).__name__})")


def _stderr_suffix(witness: Any) -> str:
    try:
        lines = witness.stderr_lines() if witness is not None else []
    except Exception:
        lines = []
    if not lines:
        return "; it wrote nothing to stderr"
    return "; last stderr lines: " + " | ".join(lines)


def _describe(exc: BaseException, spec: Dict[str, Any], *, witness: Any,
              timeout_s: Optional[float]) -> str:
    nodes = _walk(exc)
    transport = str(spec.get("transport") or "").lower()
    # The command's base name only: enough to identify the server, and it keeps
    # the operator's home/install paths out of reasons, logs and the UI.
    command = _short(_basename(str(spec.get("command") or "")), 120)

    if transport == "stdio":
        for e in nodes:
            if isinstance(e, (FileNotFoundError, NotADirectoryError)):
                return f"could not start the server process '{command}': command not found"
            if isinstance(e, PermissionError):
                return (f"could not start the server process '{command}': "
                        "permission denied")
        code = getattr(witness, "exit_code", None) if witness is not None else None
        if type(code) is int:
            return (f"the server process '{command}' exited with code {code}"
                    + _stderr_suffix(witness))

    host, ep = _endpoint(spec)
    for e in nodes:   # a pinned transport refusing a hop (a redirect, another host)
        text = str(e)
        if "blocked by SSRF/DNS guard" in text:
            return _short(text[text.index("blocked by SSRF/DNS guard"):])
    for e in nodes:
        if _is_refused(e):
            return f"connection refused by {ep}"
    for e in nodes:
        if _is_dns(e):
            return f"DNS lookup failed for '{_ascii(host)}'" if host else "DNS lookup failed"
    for e in nodes:
        if isinstance(e, ssl.SSLError):
            return f"TLS handshake with {ep} failed: {_short(e)}"
    for e in nodes:
        status = getattr(getattr(e, "response", None), "status_code", None)
        if type(status) is int:
            return f"{ep} answered HTTP {status}"
    for e in nodes:
        if _is_timeout(e):
            if transport == "stdio":
                waited = f" after {timeout_s:g}s" if timeout_s else ""
                return ("the server process did not answer the MCP handshake"
                        f"{waited}" + _stderr_suffix(witness))
            if type(e).__name__ == "ConnectTimeout":
                return f"timed out connecting to {ep}"
            waited = f" after {timeout_s:g}s" if timeout_s else ""
            return f"{ep} did not answer the MCP handshake{waited}"
    for e in nodes:
        if type(e).__name__.endswith("ConnectError"):
            return f"could not connect to {ep}: {_short(e)}"

    leaf = _leaf(exc)
    if transport == "stdio" and "connection closed" in str(leaf).lower():
        return "the server process closed the connection" + _stderr_suffix(witness)
    return f"{type(leaf).__name__}: {_short(leaf) or 'no detail'}"
