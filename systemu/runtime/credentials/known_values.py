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
credential is therefore held only for the microseconds inside :func:`_digest_into`
between the ``store.get`` / ``os.environ.get`` read and the HMAC, and never crosses this
module's boundary. A memory dump of the cache yields key-scoped MACs, not secrets.

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

WHERE THE CORPUS COMES FROM — TWO SOURCES, NOT ONE. ``CredentialStore.list_names()``
is a registry maintained by ``CredentialStore.set``, so on its own it misses every
credential that reaches the process another way. The one that matters is the
environment: ``CredentialResolver.resolve`` falls through keyring → ``os.environ``
(``resolver.py``), so a credential provisioned the ``.env`` way resolves normally,
source ``"env"``, and was invisible to BOTH fences — free to be promoted into a fact
that is read verbatim into a system prompt on every later run. Reproduced before
fixing: with the credential live in ``os.environ`` the corpus measured EMPTY and
``ask_promotion._value_is_secret`` returned ``False``.

THE ENVIRONMENT HALF ENUMERATES DECLARED KEYS — IT DOES NOT SCAN THE ENVIRONMENT.
``resolve`` only ever reads ``os.environ.get(req.key)``, and ``req`` is a
:class:`~systemu.core.models.CredentialRequirement` persisted on the tool as
``Tool.requires_credentials``. So the set of environment names that can ever hold a
credential in this system is CLOSED and already written down. This module reads that
declaration — the tool ROSTER → ``get_tool(id)`` → ``requires_credentials`` — and looks
up exactly those names.

IT READS THE ROSTER THROUGH THE **STRICT, WITNESSED** READER, AND THAT IS LOAD-BEARING.
The plain ``Vault.load_index`` goes through ``_read_json``, which swallows
``JSONDecodeError`` and ``OSError`` and returns ``[]``. Sixteen callers depend on that (a
corrupt sidecar should grey out a panel, not crash a page), so it was left alone. But it
means the file backend — the default, and the one ``vault.factory`` falls back to —
CANNOT RAISE here, so a could-not-build signal keyed on an exception from ``load_index``
is dead code, and a truncated index / an index that is a directory / an index that
parses to a dict all arrive as the same empty list as a vault that has genuinely never
registered a tool. The fix does NOT try to detect that after the fact — three prior
attempts each patched one more swallow and the fourth (a vanished vault root) still read
as "empty, complete". Instead the reader is ``Vault.load_tool_index_strict``, which by
CONSTRUCTION cannot represent "unreadable" as ``[]``: it ``os.stat``s the root (a missing
root RAISES — it is never an empty vault), ``os.scandir``s the tools dir (a genuinely
absent dir, its verified parent already stat-checked, is the ONE branch that returns
``[]``; a FILE where the dir belongs RAISES), then reads / parses / shape-checks the
index and RAISES ``VaultUnreadable`` at whichever stage fails. ``Path.exists()`` appears
NOWHERE on that path — it swallows ENOENT/ENOTDIR itself and is exactly how the vanished
root read as present-and-empty. Completeness here is WITNESSED (total header resolution),
never inferred from the absence of an error.

That ordering is the whole design. Harvesting by NAME PATTERN instead would be a shape
rule again, and this module exists because shape rules were measured and rejected: any
rule wide enough to catch ``SENDGRID_KEY`` also catches ``TOKENIZERS_PARALLELISM`` and
``SSH_AUTH_SOCK``, putting ordinary values into a corpus that then redacts them out of
pushes — the "fence that gets disabled" failure. Worse, no pattern over
:data:`~systemu.runtime.elicitation._SECRET_NAME_TOKENS` catches these at all: that
tuple carries ``api_key``/``apikey``/``access_key``/``private_key`` but no bare ``key``,
so every ``<VENDOR>_KEY`` name fails the match outright. ``CredentialRequirement.key``
validates only ``^[A-Z][A-Z0-9_]{1,63}$`` — nothing requires a secret-ish token, and
this repository's own fixtures are exactly that shape (``WEATHER_KEY``, ``OWM_KEY``,
``X_KEY``, ``K_KEY``). Enumerating the declaration needs no name rule, has no
false-positive surface by construction, and closes the hole for EVERY declared key
whatever it is called.

WHAT THE DECLARED-KEY RULE DOES NOT COVER, STATED RATHER THAN HIDDEN. A credential in
``os.environ`` under a name NO tool declares is not harvested. That is the deliberate
boundary: such a value is not a credential *of this system* — nothing here can resolve
or spend it — and admitting it would require exactly the name heuristic rejected above.
A tool that reads a credential straight out of ``os.environ`` without declaring it is
therefore unfenced; declaring it is the fix, and Gate-4 already pushes tools that way.

The residual the strict reader CLOSED, recorded so it is not re-opened by accident: a
tool RECORD on disk whose INDEX file has been DELETED (its ``tools`` dir still present)
now REFUSES rather than reading as "no tools" — an absent index inside a verified dir is
not the legitimate-absence branch (only an absent tools DIR is), so it raises
``VaultUnreadable(stage="read")`` and the fence fails closed. ``Vault.__init__``
scaffolds the index on every open and there is no tool-deletion path in the repository,
so a real vault never sits in that state; a manually-broken one now over-refuses (safe)
instead of leaking, and self-heals on the next Vault open.

COST, MEASURED RATHER THAN ESTIMATED. The declaration lives in per-tool records, not in
the index header, so a corpus build reads one JSON per tool: ~1.5 ms/tool, ~58 ms to
enumerate and ~74 ms for a full build on a 40-tool vault (the size of the real one).
Each ``mask_outbound`` call that gets past the length floor rebuilds it, so a Telegram
push costs one build per string long enough to participate. That is a background,
rate-limited daemon path, not a request path, and it is stated here so it is not
mistaken for free.

NOT cached, for the reason the store half is not: a stale-open cache on a secret fence
is a worse failure than the rebuild. Carrying the keys in the index header would be one
read instead of N, but it would be INERT on every EXISTING vault until each tool happened
to be re-saved — a fence silent about being inert is the exact failure this packet
exists to fix. If the per-push cost ever matters, the sound fix is to build the corpus
ONCE per outbound message rather than once per string.

FAILURE DIRECTION IS DELIBERATELY ASYMMETRIC — see each call site. This module never
raises, but it does NOT collapse "no match" and "could not check" into one answer — that
collapse is the fail-OPEN bug this fence exists to close. :func:`known_secret_status`
returns a TRI-STATE — ``MATCH`` / ``NO_MATCH`` / ``UNKNOWN`` — where ``UNKNOWN`` means the
corpus could not be fully built (unreadable tool roster, unreadable tool record,
per-vault HMAC unavailable, ``replay_metrics`` import failure). A ``NO_MATCH`` is only
ever emitted when the corpus was built IN FULL and the value is genuinely absent from it,
so the failure path can never wear the healthy no-secret answer. Each caller then applies
its own fail direction on top: the promotion fence refuses on ``UNKNOWN`` (fails CLOSED —
cost of a false refusal is one un-promoted fact), while the outbound mask treats
``UNKNOWN`` as no-match (fails OPEN — masking must never break a push). The two corpus
sources are independently guarded so a MATCH found in one still wins when the other
failed, but absent a match, any source failure yields ``UNKNOWN`` rather than a silent
empty corpus.
"""
from __future__ import annotations

import enum
import logging
import os
import re
from typing import Any, FrozenSet, Optional, Set, Tuple

from systemu.vault.vault import VaultUnreadable

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


class KnownSecret(enum.Enum):
    """The tri-state answer to "is ``text`` a stored credential value?".

    Three DISTINCT members, never conflated — the whole point of this type. ``UNKNOWN``
    is the "could-not-build" state, and it must never be substituted by ``NO_MATCH``:
    a failure path that emitted the healthy no-secret answer is exactly the fail-OPEN
    defect this fence closes (see the module docstring).
    """
    MATCH = "match"        # ``text`` is, or contains as a whole token, a stored value
    NO_MATCH = "no_match"  # the corpus was built IN FULL and ``text`` is not in it
    UNKNOWN = "unknown"    # the corpus could not be fully built — caller must fail closed


def _declared_credential_keys(vault: Any) -> Tuple[Set[str], bool, Optional[str]]:
    """``(keys, complete, reason)`` — the credential NAMES this vault's tools declare.

    No values ever leave this function, and it NEVER RAISES. ``complete`` is a WITNESSED
    signal, not an inferred one: it starts ``False`` and is set ``True`` at exactly ONE
    place — after the whole roster has resolved. An incompleteness is therefore never
    *detected and flipped back*; the default until totality is proven is "incomplete".
    ``reason`` is a diagnostic string (the failure's stage/path, or ``None`` on success)
    for the fail-closed caller's operator-visible log — never a credential value or key.

    Spelled out the same way ``interface.pages.settings.connection_rows`` spells it, and
    for the reason its own comment gives: the tool INDEX HEADER does not carry
    ``requires_credentials`` (see ``vault._tool_header``), so the full record has to be
    read per tool. Every tool is read regardless of status — a PROPOSED tool's declared
    key still names an environment variable that may hold a live credential.

    A requirement declaring ``auth_type == "none"`` is skipped — ``resolver.resolve``
    short-circuits it to ``("", "none")`` and never consults the environment for it, so
    its name does not denote a credential here.

    THREE ways ``complete`` is ``False``, each a real inability to enumerate:

    1. the tool roster could not be read — ``load_tool_index_strict`` RAISED
       ``VaultUnreadable`` (unreadable/absent root, a ``tools`` file where the dir
       belongs, a corrupt / mis-shaped index). The reader cannot spell "unreadable" as
       ``[]``, so this is a real exception, not an inference.
    2. the vault exposes ``load_index`` but no ``load_tool_index_strict`` (or its
       attribute access raised) — a tool surface whose readability we cannot witness.
       Answering "complete" there would reinstate the collapse for any backend not taught
       the strict reader.
    3. a tool record listed in the roster could not be read — it may have declared the
       key naming the value we are about to check, so a single unresolved header makes
       the whole enumeration non-authoritative.

    A vault with NO ``load_index`` at all is a DIFFERENT thing: a store-only shim declares
    no credentials THROUGH tools, which is a real, COMPLETE empty answer the store half
    still guards. The absence of the tool API is itself the witness — nothing was
    swallowed to reach it — so it is not fenced shut.
    """
    keys: Set[str] = set()

    # Even the attribute LOOKUP goes through a guard: ``getattr(o, n, default)`` swallows
    # only AttributeError, so a lazily-connecting proxy whose ``load_index`` is a property
    # raising anything else would propagate straight out of this module and past the
    # never-raises claim above.
    try:
        has_index = callable(getattr(vault, "load_index", None))
    except Exception:
        return keys, False, "vault tool surface could not be inspected"

    if not has_index:
        # Store-only shim — witnessed no tool API, a COMPLETE empty declaration.
        return keys, True, None

    try:
        headers = vault.load_tool_index_strict()
    except VaultUnreadable as exc:
        return keys, False, (
            "tool roster unreadable (stage=%s, path=%s)" % (exc.stage, exc.path))
    except Exception:
        # Has a tool surface but no strict reader (or its access raised): the roster's
        # readability cannot be witnessed, so completeness cannot be claimed.
        return keys, False, "tool roster reader unavailable"

    if not isinstance(headers, list):
        # The reader's postcondition is a LIST of header dicts; every shipped backend
        # (Vault/FileVault/SqliteVault/ParallelVault) enforces it and refuses a non-list
        # itself, so no production path reaches this. It is the NON-CONFORMING-BACKEND
        # guard that keeps the loop below — and this function's never-raises claim — true
        # against a duck-typed double, and it refuses rather than iterating a scalar into
        # ``TypeError`` or a dict into its KEYS.
        return keys, False, "tool roster reader returned a non-list"

    unresolved: list = []
    for header in headers:
        tool_id = header.get("id") if isinstance(header, dict) else None
        if not isinstance(tool_id, str) or not tool_id:
            # NEVER a bare ``continue``: a header we cannot resolve to a tool_id might be
            # the one whose record declares the key holding the value we are checking, so
            # it makes the enumeration incomplete rather than merely getting skipped. (The
            # strict reader already guarantees a string id, so this is defence in depth
            # for a laxer backend — pinned, not assumed.)
            unresolved.append(header)
            continue
        try:
            tool = vault.get_tool(tool_id)
        except Exception:
            # This tool's declaration is now unknown; same reasoning — incomplete, not a
            # silent skip. No key name or payload is logged (a store/record error can
            # carry one).
            unresolved.append(tool_id)
            continue
        for req in (getattr(tool, "requires_credentials", None) or []):
            if getattr(req, "auth_type", "api_key") == "none":
                continue  # resolver short-circuits: this name denotes no credential
            key = getattr(req, "key", None)
            if isinstance(key, str) and key:
                keys.add(key)

    if unresolved:
        return keys, False, "%d tool record(s) unreadable" % len(unresolved)

    complete = True  # the ONE point completeness is asserted — the whole roster resolved
    return keys, complete, None


def _digest_into(out: Set[str], value: Any, vault: Any) -> bool:
    """Add the keyed digests of ``value`` to ``out``. Returns ``True`` when the value was
    digested (or is too short to participate — nothing to add, nothing failed), and
    ``False`` when a PARTICIPATING value could not be digested at all: ``value_ref``
    documents ``None`` as the fail-closed signal for an unavailable per-vault HMAC, and a
    value silently dropped from the corpus is a value a later text can never be matched
    against. The plaintext never leaves this frame."""
    if not isinstance(value, str) or len(value) < MIN_KNOWN_SECRET_LEN:
        return True
    from systemu.runtime.replay_metrics import value_ref, canonical_value_ref
    added = False
    for ref in (value_ref(value, vault), canonical_value_ref(value, vault)):
        if ref:
            out.add(ref)
            added = True
    # A participating value that produced NEITHER digest (per-vault HMAC unavailable /
    # both refs None) is a hole in the corpus — report it so the build is marked
    # incomplete instead of silently omitting the value.
    return added


def _declared_env_digests(vault: Any, declared: Set[str]) -> Tuple[Set[str], bool]:
    """Keyed digests of the DECLARED credentials that arrived through ``os.environ``.

    Looks up EXACTLY the declared names — this never iterates ``os.environ`` and never
    inspects a name it was not handed, so an undeclared variable cannot enter the corpus
    however it is spelled. No value plausibility rule is applied either: the name is a
    declared credential, so the value IS one, whatever shape it has.

    Returns ``(digests, complete)``; ``complete`` is ``False`` when a declared env value
    could not be digested — see :func:`_digest_into`.
    """
    out: Set[str] = set()
    complete = True
    for key in declared:
        try:
            if not _digest_into(out, os.environ.get(key), vault):
                complete = False
        except Exception:
            logger.debug("[known-values] skipped one declared credential")
            complete = False
            continue
    return out, complete


def _store_secret_digests(vault: Any, declared: Set[str]) -> Tuple[Set[str], bool]:
    """Keyed digests of every credential the ``CredentialStore`` can reach.

    Reads each value, digests it with the EXISTING per-vault keyed helper, and drops the
    plaintext immediately. Returns digests only — the caller can never obtain a credential
    value through this function.

    Enumerates the ``set``-maintained name registry UNION the declared keys: a declared
    credential written into the keyring or ``.credentials.json`` by anything other than
    ``CredentialStore.set`` is readable through ``store.get`` but was never registered,
    and the declaration finds it with no extra false-positive surface.

    Returns ``(digests, complete)``; ``complete`` is ``False`` when the store could not be
    enumerated, or when any single credential could not be read or digested.
    """
    out: Set[str] = set()
    try:
        from systemu.runtime.credentials.store import CredentialStore
        root = getattr(vault, "root", None)
        store = CredentialStore(base_dir=(root if root is not None else vault))
        names = set(store.list_names()) | set(declared)
    except Exception:
        # NEVER log the exception payload here — a store error can carry a key name, and a
        # traceback from the file backend can carry a line of the decrypted blob. A bare
        # count is the most this may ever say. Enumeration failed, so the store half is
        # INCOMPLETE, not empty.
        logger.debug("[known-values] could not enumerate credential names")
        return out, False

    complete = True
    for name in names:
        value = None
        try:
            value = store.get(name)
            if not _digest_into(out, value, vault):
                complete = False
        except Exception:
            # Same rule: no payload, no key name. One unreadable credential must not
            # discard the others — but it does mark the corpus incomplete.
            logger.debug("[known-values] skipped one unreadable credential")
            complete = False
            continue
        finally:
            value = None  # noqa: F841 - drop the plaintext reference promptly
    return out, complete


def _build_corpus(vault: Any) -> Tuple[FrozenSet[str], bool, Optional[str]]:
    """``(digests, complete, reason)`` — the primitive both the fail-open and fail-closed
    views are built from.

    TWO independently-guarded sources — the credential store and the declared keys
    resolved from the environment. ``complete`` is ``True`` only when the declared-key
    enumeration AND both digest sources were fully read; ANY swallowed failure makes it
    ``False`` and records the FIRST cause in ``reason``. The digests gathered so far are
    still returned so a definite MATCH found in one source still wins when another failed
    — that is the "degrade to the other" resilience — but a fail-closed caller reads
    ``complete`` and refuses to treat an incomplete corpus as an authoritative
    "nothing matched".

    NOT cached — see the module docstring.
    """
    declared, complete, reason = _declared_credential_keys(vault)

    out: Set[str] = set()
    for source in (_store_secret_digests, _declared_env_digests):
        try:
            digests, ok = source(vault, declared)
            out |= digests
            if not ok:
                complete = False
                reason = reason or "a credential value could not be read or digested"
        except Exception:
            # A source raising outright (not merely swallowing internally) is itself an
            # incompleteness signal — never a reason to pretend the corpus is whole.
            logger.debug("[known-values] one corpus source failed")
            complete = False
            reason = reason or "a corpus source failed"

    if out:
        logger.debug("[known-values] corpus built: %d digest(s)", len(out))
    return frozenset(out), complete, reason


def _corpus_digests(vault: Any) -> FrozenSet[str]:
    """The FAIL-OPEN view of the corpus: digests only, with any incompleteness collapsed
    to whatever was built. Used by the outbound redaction path, whose standing contract is
    that masking must never break a push, so it treats an incomplete corpus exactly as it
    treats an empty one. A fail-CLOSED caller must NOT use this — it cannot distinguish
    empty from unreadable; that caller uses :func:`known_secret_status`."""
    return _build_corpus(vault)[0]


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


def _text_contains_match(text: str, digests: FrozenSet[str], vault: Any) -> bool:
    """True when ``text`` is, or contains as a whole token, a digest in ``digests``."""
    scan = text[:MAX_SCAN_CHARS]
    # The whole value first: the promotion fence's answer IS the value, and a credential
    # legitimately containing a delimiter would be split by the tokenizer below.
    if _token_matches(scan.strip(), digests, vault):
        return True
    for token in _SPLIT_RE.split(scan):
        if _token_matches(token.strip(_TRIM), digests, vault):
            return True
    return False


def _status_with_reason(text: Any, vault: Any) -> Tuple[KnownSecret, Optional[str]]:
    """The tri-state answer AND the diagnostic reason behind an ``UNKNOWN``.

    ``reason`` is non-``None`` only when the answer is ``UNKNOWN``, and it carries the
    failure's stage/path (never a value or key) for a fail-closed caller to log. Kept
    separate from :func:`known_secret_status` so the enum-only entry point the pins use
    stays a bare enum while the fence can surface WHY it refused. Never raises.
    """
    if not isinstance(text, str) or len(text) < MIN_KNOWN_SECRET_LEN or vault is None:
        return KnownSecret.NO_MATCH, None
    try:
        digests, complete, reason = _build_corpus(vault)
        if digests and _text_contains_match(text, digests, vault):
            return KnownSecret.MATCH, None
        if complete:
            return KnownSecret.NO_MATCH, None
        return KnownSecret.UNKNOWN, reason
    except Exception:
        # A BACKSTOP, and stated as one rather than left to look like coverage. Every path
        # inside the try guards itself — `_declared_credential_keys` never raises
        # (including its attribute lookups), `_build_corpus` catches per source, and
        # `_token_matches` catches its own import and digest — so no vault shape reaches
        # this line and a mutation of it SURVIVES the suite. It is kept because letting an
        # exception escape a secret fence is worse than an untestable branch, and it
        # returns UNKNOWN so that even if a future refactor makes it reachable it CANNOT
        # emit the healthy answer. What IS pinned is the invariant it stands behind:
        # neither `_build_corpus` nor this function raises for a battery of hostile vaults.
        logger.debug("[known-values] containment check failed")
        return KnownSecret.UNKNOWN, "known-value check raised"


def known_secret_status(text: Any, vault: Any) -> KnownSecret:
    """TRI-STATE: is ``text`` a stored credential value? Never raises.

    * ``MATCH`` — ``text`` is (or contains as a whole token) a stored value. A match wins
      even when another corpus source failed, so a definite hit is never lost.
    * ``NO_MATCH`` — the corpus was built IN FULL and ``text`` is genuinely absent.
    * ``UNKNOWN`` — the corpus could NOT be fully built, so this half cannot answer. The
      healthy ``NO_MATCH`` is unreachable from any failure path, which is the whole point:
      the promotion fence refuses on ``UNKNOWN`` (fail closed), the outbound mask passes
      through (fail open).

    A ``text`` too short to participate, or a missing ``vault``, is ``NO_MATCH`` — there
    is nothing this half could ever have matched, so it is not a failure to report.
    """
    return _status_with_reason(text, vault)[0]


def contains_known_secret(text: Any, vault: Any) -> bool:
    """BOOL shim: ``True`` only on a definite :attr:`KnownSecret.MATCH`.

    This collapses ``UNKNOWN`` (could-not-build) to ``False``, so it is FAIL-OPEN and MUST
    NOT be used where a caller has to fail closed — use :func:`known_secret_status` there,
    which the promotion fence does through :func:`_status_with_reason`. Never raises.

    Kept because the pre-existing pins in ``test_known_value_secret_fence`` are written
    against it — it is, today, effectively test-only. Anything new that reaches for a bool
    here is almost certainly the fail-closed case and wants :func:`known_secret_status`.
    """
    return known_secret_status(text, vault) is KnownSecret.MATCH


def redact_known_secrets(text: Any, vault: Any, mask: str = _MASK) -> Any:
    """Replace every whole token that is a stored credential value with ``mask``.

    Never raises: returns the input unchanged on any failure, matching the outbound
    chokepoint's standing contract that masking must never break a push. Reads the
    FAIL-OPEN corpus view (:func:`_corpus_digests`), so an incomplete corpus masks by
    whatever was built rather than refusing — the opposite direction from the promotion
    fence, by design.
    """
    if not isinstance(text, str) or len(text) < MIN_KNOWN_SECRET_LEN or vault is None:
        return text
    try:
        digests = _corpus_digests(vault)
        if not digests:
            return text
        if len(text) > MAX_SCAN_CHARS:
            # Do not silently scan a prefix and hand back a string the operator would read
            # as fully masked. Fall back to the all-or-nothing whole-value test.
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
