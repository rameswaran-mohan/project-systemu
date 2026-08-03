"""DEC-31 fixture-length rule, made automatic.

**The rule.** Any test of a redaction/sanitisation path MUST use a secret
strictly LONGER than the largest cap on that path.

**Why it needs a helper rather than a comment.** The rule was already written
down, and the pin that was supposed to enforce it still passed while the bug
shipped — because its fixture was 45 chars and the cap it had to defeat was 64.
A secret that fits *under* the cap never reaches the truncation, so the test
encoded the blind spot instead of catching it. A hand-chosen constant cannot
know what the cap is; this module reads the cap out of the running code and
sizes the fixture from it, so raising a cap automatically lengthens every
fixture that depends on it and the pin keeps testing the same real hazard.

**Caps are read LIVE, never copied.** :func:`caps_on` imports the constant from
the module that owns it at call time. A duplicated literal here would drift
silently the first time somebody tuned the real one — and drift in the safe-
looking direction, since a stale-low copy still produces a passing test.

Usage::

    from tools.redaction_fixtures import secret_for, secret_longer_than

    def test_the_audit_row_never_holds_a_clear_credential():
        jwt = secret_for("audit_params")        # > every cap on that path
        ...

    def test_some_local_cap():
        jwt = secret_longer_than(64, 200)       # > 200

Every builder returns a secret whose SHAPE the project's redactors actually
recognise (JWT / Bearer / ``sk-`` / long hex). A shapeless secret (``hunter2``)
is deliberately not offered: no shape rule catches it, so a fixture built from
one would pin the wrong thing — see ``runtime.credentials.known_values`` for
what closes that gap instead.
"""
from __future__ import annotations

import base64
import json
from typing import Callable, Dict, Tuple


class RedactionFixtureError(RuntimeError):
    """The fixture could not be built, or would not have been long enough.

    Raised rather than returning a short secret: a too-short fixture is exactly
    the failure mode this module exists to prevent, and it fails INVISIBLY (the
    test goes green).
    """


# --------------------------------------------------------------------------- #
# live cap registry
# --------------------------------------------------------------------------- #
#
# path name -> callable returning {constant name: live value}. The callable is
# what keeps these honest: it imports at call time, so the value is whatever the
# code under test is using right now.

def _caps_audit_params() -> Dict[str, int]:
    from systemu.runtime.shadow_runtime import _AUDIT_PARAM_VALUE_CAP
    return {"shadow_runtime._AUDIT_PARAM_VALUE_CAP": int(_AUDIT_PARAM_VALUE_CAP)}


def _caps_outbox_component() -> Dict[str, int]:
    from systemu.runtime.outbox import MAX_SLUG_CHARS
    return {"outbox.MAX_SLUG_CHARS": int(MAX_SLUG_CHARS)}


#: Only paths whose cap is a MODULE-LEVEL constant can be registered — that is
#: what makes the live read possible. ``tool_sandbox.truncate_result`` is
#: deliberately absent: its cap is per-tool (``tool.max_result_size_chars``,
#: default ``None``) with no constant to read, so a test there must pass the
#: tool's own cap to :func:`secret_longer_than` explicitly. Inventing a constant
#: here so the registry looked complete would be the drift this module exists to
#: prevent.
CAP_READERS: Dict[str, Callable[[], Dict[str, int]]] = {
    "audit_params": _caps_audit_params,
    "outbox_component": _caps_outbox_component,
}


def caps_on(path: str) -> Dict[str, int]:
    """Every truncation cap on ``path``, read live from the owning module.

    Raises :class:`RedactionFixtureError` for an unknown path rather than
    returning ``{}``. ``{}`` would flow into :func:`largest_cap` and produce a
    tiny fixture — a silent downgrade to the exact defect this guards.
    """
    reader = CAP_READERS.get(path)
    if reader is None:
        raise RedactionFixtureError(
            f"unknown redaction path {path!r}; known paths: "
            f"{sorted(CAP_READERS)}. Add a cap reader rather than guessing a "
            f"length — a fixture shorter than the real cap passes vacuously.")
    caps = reader()
    if not caps:
        raise RedactionFixtureError(
            f"cap reader for {path!r} returned no caps — refusing to size a "
            f"fixture against nothing")
    return caps


def largest_cap(path: str) -> int:
    return max(caps_on(path).values())


# --------------------------------------------------------------------------- #
# shape builders
# --------------------------------------------------------------------------- #

def _b64u(obj) -> str:
    raw = json.dumps(obj, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


#: A decodable payload, so a leak is DEMONSTRABLE in a failure message rather
#: than merely asserted: base64-decoding whatever survived a slice prints the
#: claims in clear.
_JWT_CLAIMS = {
    "sub": "dec31-fixture",
    "role": "admin",
    "apikey": "SECRET-VALUE-DO-NOT-LOG",
}
_JWT_SIG = "cC9nWQ0vX2pKb1RhSm9zaFNpZ25hdHVyZUJ5dGVzMDEyMzQ1Njc4OQ"


def _jwt_longer_than(n: int) -> str:
    """A real three-segment JWT strictly longer than ``n``.

    Grown through the PAYLOAD so the header and the signature segments stay
    well-formed — the redactors' JWT pattern needs all three, and a fixture that
    only matched because it was malformed would prove nothing.
    """
    header = _b64u({"alg": "HS256", "typ": "JWT"})
    pad = 0
    for _ in range(64):
        claims = dict(_JWT_CLAIMS)
        if pad:
            claims["pad"] = "A" * pad
        tok = f"{header}.{_b64u(claims)}.{_JWT_SIG}"
        if len(tok) > n:
            return tok
        pad += max(16, n - len(tok) + 16)
    raise RedactionFixtureError(f"could not grow a JWT past {n} chars")


def _bearer_longer_than(n: int) -> str:
    body = "A"
    for _ in range(64):
        tok = f"Bearer {body}"
        if len(tok) > n:
            return tok
        body += "B" * max(16, n - len(tok) + 16)
    raise RedactionFixtureError(f"could not grow a Bearer token past {n} chars")


def _sk_longer_than(n: int) -> str:
    body = "A" * 8
    for _ in range(64):
        tok = f"sk-{body}"
        if len(tok) > n:
            return tok
        body += "B" * max(16, n - len(tok) + 16)
    raise RedactionFixtureError(f"could not grow an sk- key past {n} chars")


def _hex_longer_than(n: int) -> str:
    tok = "deadbeef" * max(4, (n // 8) + 2)
    if len(tok) <= n:
        raise RedactionFixtureError(f"could not grow a hex token past {n} chars")
    return tok


_BUILDERS: Dict[str, Callable[[int], str]] = {
    "jwt": _jwt_longer_than,
    "bearer": _bearer_longer_than,
    "sk": _sk_longer_than,
    "hex": _hex_longer_than,
}

KINDS: Tuple[str, ...] = tuple(sorted(_BUILDERS))


def secret_longer_than(*caps: int, kind: str = "jwt") -> str:
    """A secret-SHAPED string strictly longer than every cap in ``caps``.

    Fails loud on no caps, a non-integer cap, or a builder that could not reach
    the length. The one thing it will never do is hand back something short
    enough to slip under a cap, because that is the defect, not the test.
    """
    builder = _BUILDERS.get(kind)
    if builder is None:
        raise RedactionFixtureError(
            f"unknown secret kind {kind!r}; known: {KINDS}")
    if not caps:
        raise RedactionFixtureError(
            "no caps given — a fixture sized against nothing is the blind spot "
            "DEC-31 was written about")
    ints = []
    for c in caps:
        if isinstance(c, bool) or not isinstance(c, int):
            raise RedactionFixtureError(f"cap {c!r} is not an int")
        if c < 0:
            raise RedactionFixtureError(f"cap {c!r} is negative")
        ints.append(c)
    target = max(ints)
    out = builder(target)
    if len(out) <= target:
        raise RedactionFixtureError(
            f"{kind} builder returned {len(out)} chars for a cap of {target} — "
            f"refusing to return a fixture that cannot reach the truncation")
    return out


def secret_for(path: str, *, kind: str = "jwt") -> str:
    """A secret longer than the LARGEST live cap on ``path``."""
    return secret_longer_than(*caps_on(path).values(), kind=kind)


# --------------------------------------------------------------------------- #
# longer-than-the-cap is NECESSARY BUT NOT SUFFICIENT
# --------------------------------------------------------------------------- #
#
# A fixture merely longer than the cap can still be recognised after the cap
# truncates it, and then the test cannot tell the two ORDERS apart — it goes
# green either way, which is the same blind spot DEC-31 is about, one level up.
#
# Measured on this tree against a 200-char cap:
#
#     kind    | truncating at the cap defeats detection?
#     --------+------------------------------------------
#     jwt     | only once the token exceeds ~290 chars — at 238 the signature
#             |   segment still survives the slice and the pattern matches
#     bearer  | NEVER — "Bearer <prefix>" matches the same pattern
#     sk      | NEVER — sk-[A-Za-z0-9-]{8,} matches any long-enough prefix
#     hex     | NEVER — \b[0-9a-fA-F]{32,}\b matches any 32+ char prefix
#
# So the shapes whose detection depends on material at the END (the JWT's third
# segment) are the truncation-vulnerable ones. :func:`truncation_blind_secret`
# does not assume any of that: it VERIFIES the property against the shipped
# detectors, so it cannot drift when a pattern changes.

_OUTBOX_MARKER = "[redacted - looked like a credential]"


def _detected(value: str) -> bool:
    """True if EITHER shipped detector recognises ``value`` as secret.

    Both are consulted so a fixture is valid on either path — the audit/evidence
    path (``_scrub_value_shapes``) and the file-export path (``outbox.redact``,
    whole-value fence plus ``mask_outbound``).
    """
    if not value:
        return False
    from systemu.runtime.external_verifier import _scrub_value_shapes
    from systemu.runtime.outbox import redact
    if "[REDACTED]" in _scrub_value_shapes(value):
        return True
    out = redact(value)
    return out == _OUTBOX_MARKER or "***" in out


#: Kinds for which :func:`truncation_blind_secret` can succeed. Derived by
#: measurement, exposed so a test can assert the others genuinely cannot.
TRUNCATION_BLINDABLE_KINDS: Tuple[str, ...] = ("jwt",)


def truncation_blind_secret(*caps: int, kind: str = "jwt") -> str:
    """A secret that is longer than every cap AND that truncation HIDES.

    Guarantees three properties, each verified against the shipped detectors
    rather than assumed:

      * ``len(secret) > max(caps)``
      * the FULL value is detected
      * the value truncated at ``max(caps)`` is NOT detected

    That third property is what makes a test able to distinguish
    redact-then-truncate from truncate-then-redact. Without it the test passes
    under both orders and pins nothing — verified: an earlier version of this
    module returned a 236-char JWT for a 200-char cap, and the true order-flip
    mutation SURVIVED every pin built on it.

    Raises for a kind whose prefixes stay recognisable (``bearer``/``sk``/
    ``hex``): no fixture of that shape can pin an ordering, and returning one
    anyway would hand back a test that cannot fail.
    """
    if kind not in _BUILDERS:
        raise RedactionFixtureError(f"unknown secret kind {kind!r}; known: {KINDS}")
    if not caps:
        raise RedactionFixtureError("no caps given")
    target = max(int(c) for c in caps if not isinstance(c, bool))
    grow = max(target * 2, target + 128)
    for _ in range(12):
        candidate = _BUILDERS[kind](grow)
        if (len(candidate) > target
                and _detected(candidate)
                and not _detected(candidate[:target])):
            return candidate
        grow = int(grow * 1.6) + 128
    raise RedactionFixtureError(
        f"no {kind!r} secret can be built whose {target}-char prefix escapes "
        f"detection — every prefix of this shape still matches. Truncation "
        f"cannot hide it, so it cannot pin a redaction ORDER. Use one of "
        f"{TRUNCATION_BLINDABLE_KINDS}.")


def truncation_blind_secret_for(path: str, *, kind: str = "jwt") -> str:
    """:func:`truncation_blind_secret` sized against the live caps on ``path``."""
    return truncation_blind_secret(*caps_on(path).values(), kind=kind)
