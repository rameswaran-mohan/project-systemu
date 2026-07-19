"""The KNOWN-VALUE secret fence — the structural half of the secret guard.

WHY THIS EXISTS. The two shipped fences are both SHAPE fences:

* the NAME fence (``elicitation.is_secret_field``) inspects field NAMES only, so a
  secret parked under a neutral leaf sails through;
* the VALUE fence (``ask_promotion._value_is_secret``, delegating to
  ``messaging.gateway.mask_outbound``) inspects the value's SHAPE — URI userinfo,
  ``Bearer``, ``sk-``, ``ghp_``, ``AKIA``, JWT, Slack, ``--token``, long hex.

Neither can ever catch a secret with no recognisable shape. Measured directly against
the shipped detectors: ``hunter2``, ``correcthorsebatterystaple``, ``swordfish`` and a
bare 32-character hex run all return False from both. Widening the shape rules does not
close this. The false-positive measurement that settled it:

* lowering the long-hex rule 40→32 DOUBLED the false-positive rate on an ordinary-value
  corpus (2/41 → 4/41), and the values it newly flagged were a dashless UUID and an MD5
  checksum. Worse, it is a concrete in-codebase regression: ``external_verifier.
  mint_idempotency_key`` returns ``secrets.token_hex(16)`` — EXACTLY 32 hex chars — a
  deliberately NON-secret operational identifier that rides the money-move read-back
  path. Masking it would break the confirm it exists to make. NOT ADOPTED.
* a mixed-class Shannon-entropy backstop was swept over minlen 12–24 × entropy 3.0–3.8.
  There is NO operating point that catches anything at zero false-positive cost: every
  setting that caught machine-generated credentials flagged 7–11 of 55 ordinary values
  (``Report_Q3_2026_Final``, ``InvoiceNo98765432``, ``parseHTTPResponse2xx``), and NO
  setting ever caught a shapeless human secret (0/6 everywhere). A fence that flags
  ``Report_Q3_2026_Final`` is a fence that gets disabled. NOT ADOPTED.

So the shape rules are left exactly as shipped, and the gap is closed structurally
instead: no pattern will ever recognise ``hunter2``, but the system KNOWS the
operator's stored credential values. Anything equal to one of them is a secret by
identity rather than by resemblance — a fence with no false positives by construction.

THE "COMPARE, DON'T RECORD" CONTRACT. A credential value is never read into a log, a
digest corpus, an error message, or this module's cache. The cache holds ONLY keyed
digests produced by the EXISTING helper (``replay_metrics.value_ref`` /
``canonical_value_ref`` — per-vault HMAC-SHA256, non-reversible, no unkeyed fallback);
no new digest scheme is invented here. Matching runs in the same direction: tokens are
taken from the TEXT, digested, and tested for membership in the digest set. A plaintext
credential is therefore held only for the microseconds inside :func:`_digest_corpus`
between ``store.get`` and the HMAC, and never crosses this module's boundary. A memory
dump of the cache yields key-scoped MACs, not secrets.

WHY A MINIMUM LENGTH. A credential shorter than :data:`MIN_KNOWN_SECRET_LEN` does not
participate. A 4-character PIN would match inside ordinary prose and redact the output
into uselessness — the same "fence that gets disabled" failure, arrived at from the
other side. This is a deliberate, documented hole: short credentials keep only the
shape fences.

MATCHING IS TOKEN-EXACT, NEVER SUBSTRING-OF-A-TOKEN. Text is split on whitespace and
the structural characters that actually delimit a secret in the wild (``=`` in a query
string, ``:`` in a header, quotes a widget added), then each token is compared WHOLE.
This catches ``connecting with hunter2 now`` and ``?token=hunter2``, and it cannot
manufacture a match the way a raw substring scan would — an 8-character credential that
happened to be a substring of a longer legitimate identifier would otherwise redact it.

FAILURE DIRECTION IS DELIBERATELY ASYMMETRIC — see each call site. This module itself
never raises and never guesses: it reports "no known-value match" when it cannot build
a corpus, and each caller applies its own fail direction on top.
"""
from __future__ import annotations

import logging
import re
from typing import Any, FrozenSet, Optional, Set

logger = logging.getLogger(__name__)

#: Credentials shorter than this do not participate — see the module docstring.
MIN_KNOWN_SECRET_LEN = 8

#: Bound on the text scanned. A push is prose; anything past this is not a
#: notification and must not turn the chokepoint into a hot loop.
MAX_SCAN_CHARS = 20_000

#: Structural delimiters a secret is actually embedded behind. Deliberately does NOT
#: include ``.`` or ``-`` — splitting those would shatter a token that legitimately
#: contains them and weaken the whole-token comparison.
_SPLIT_RE = re.compile(r"[\s=:,;'\"()<>\[\]{}&?#|\\]+")

#: Non-token characters to strip from a token's edges before comparison, so a
#: trailing sentence period or a wrapping quote does not defeat the match.
_TRIM = ".,;:!?'\"`)(][}{<>"

_MASK = "***"


def _corpus_digests(vault: Any) -> FrozenSet[str]:
    """The keyed digests of every stored credential value long enough to participate.

    Reads each value, digests it with the EXISTING per-vault keyed helper, and drops
    the plaintext immediately. Returns digests only — the caller can never obtain a
    credential value through this function. An empty set means "nothing to compare
    against", which every caller treats as no-match (the shape fences still apply).

    NOT cached. The obvious optimisation — memoise the digest set per vault — was
    rejected: the corpus would go stale exactly when it matters most (the operator
    just stored the credential that is now about to leak), and a stale-open cache on a
    secret fence is a worse failure than the cost of rebuilding. The cost is bounded
    by the number of registered credential names, which is a handful, and the keyed
    helper caches the expensive part (the per-vault key derivation) already.
    """
    try:
        from systemu.runtime.credentials.store import CredentialStore
        from systemu.runtime.replay_metrics import value_ref, canonical_value_ref
    except Exception:
        logger.debug("[known-values] corpus unavailable — import failed", exc_info=True)
        return frozenset()

    out: Set[str] = set()
    try:
        root = getattr(vault, "root", None)
        store = CredentialStore(base_dir=(root if root is not None else vault))
        names = store.list_names()
    except Exception:
        # NEVER log the exception payload here — a store error can carry a key name,
        # and a traceback from the file backend can carry a line of the decrypted
        # blob. A bare count is the most this may ever say.
        logger.debug("[known-values] could not enumerate credential names")
        return frozenset()

    for name in names:
        try:
            value = store.get(name)
            if not isinstance(value, str) or len(value) < MIN_KNOWN_SECRET_LEN:
                continue
            for ref in (value_ref(value, vault), canonical_value_ref(value, vault)):
                if ref:
                    out.add(ref)
        except Exception:
            # Same rule: no payload, no key name. One unreadable credential must not
            # discard the others.
            logger.debug("[known-values] skipped one unreadable credential")
            continue
        finally:
            value = None  # noqa: F841 - drop the plaintext reference promptly

    if out:
        logger.debug("[known-values] corpus built: %d digest(s)", len(out))
    return frozenset(out)


def _token_matches(token: str, digests: FrozenSet[str], vault: Any) -> bool:
    """True when ``token`` digests to something in ``digests``."""
    if len(token) < MIN_KNOWN_SECRET_LEN:
        return False
    try:
        from systemu.runtime.replay_metrics import value_ref, canonical_value_ref
        for ref in (value_ref(token, vault), canonical_value_ref(token, vault)):
            if ref and ref in digests:
                return True
    except Exception:
        return False
    return False


def contains_known_secret(text: Any, vault: Any) -> bool:
    """True when ``text`` is, or contains as a whole token, a stored credential value.

    Never raises. Returns False when no corpus can be built — the caller decides what
    that means for its own fail direction.
    """
    if not isinstance(text, str) or len(text) < MIN_KNOWN_SECRET_LEN or vault is None:
        return False
    try:
        digests = _corpus_digests(vault)
        if not digests:
            return False
        scan = text[:MAX_SCAN_CHARS]
        # The whole value first: the promotion fence's answer IS the value, and a
        # credential legitimately containing a delimiter would be split by the
        # tokenizer below.
        if _token_matches(scan.strip(), digests, vault):
            return True
        for token in _SPLIT_RE.split(scan):
            if _token_matches(token.strip(_TRIM), digests, vault):
                return True
    except Exception:
        logger.debug("[known-values] containment check failed", exc_info=True)
        return False
    return False


def redact_known_secrets(text: Any, vault: Any, mask: str = _MASK) -> Any:
    """Replace every whole token that is a stored credential value with ``mask``.

    Never raises: returns the input unchanged on any failure, matching the outbound
    chokepoint's standing contract that masking must never break a push.
    """
    if not isinstance(text, str) or len(text) < MIN_KNOWN_SECRET_LEN or vault is None:
        return text
    try:
        digests = _corpus_digests(vault)
        if not digests:
            return text
        if len(text) > MAX_SCAN_CHARS:
            # Do not silently scan a prefix and hand back a string the operator would
            # read as fully masked. Fall back to the all-or-nothing whole-value test.
            return mask if _token_matches(text.strip(), digests, vault) else text

        out = []
        pos = 0
        for m in _SPLIT_RE.finditer(text):
            out.append(_redact_token(text[pos:m.start()], digests, vault, mask))
            out.append(m.group(0))
            pos = m.end()
        out.append(_redact_token(text[pos:], digests, vault, mask))
        return "".join(out)
    except Exception:
        logger.debug("[known-values] redaction failed — text unchanged", exc_info=True)
        return text


def _redact_token(tok: str, digests: FrozenSet[str], vault: Any, mask: str) -> str:
    """Mask ``tok`` if it matches, preserving any punctuation it was wrapped in."""
    if not tok:
        return tok
    core = tok.strip(_TRIM)
    if not core or not _token_matches(core, digests, vault):
        return tok
    lead = tok[:len(tok) - len(tok.lstrip(_TRIM))]
    trail = tok[len(tok.rstrip(_TRIM)):]
    return f"{lead}{mask}{trail}"
