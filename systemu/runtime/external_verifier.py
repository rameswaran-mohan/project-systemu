"""S3 / R-A7 — the independent external verifier (wave 1: deterministic-only
scaffold + the MASK evidence-redaction pass).

The verifier is the ONLY thing that may set ``ExternalEvidence.confirmed = True``,
and it does so ONLY from a DETERMINISTIC equality/predicate match on captured
ground-truth — NEVER from any model output. This is the deterministic-match
requirement from the S4 fail-closed credit contract: a confirmed money-move must
be provable by exact token/text equality, not an LLM judgement.

Wave-1 scope (this file):
  * :class:`ExternalVerifier` — dispatches ``verify`` by strategy name to
    ``_api_readback`` / ``_email_confirm`` / ``_web_assertion`` /
    ``_operator_attest``. Strategies take INJECTED clients (constructor args) so a
    later wave can mock transports. No strategy touches an LLM.
  * The MONEY-MOVE hard gate: ``_web_assertion`` and ``_operator_attest`` are
    ADVISORY for a money-move — they can NEVER, alone, confirm one (a false
    positive there is a double-submit hazard). Only the strong deterministic
    ``_api_readback`` / ``_email_confirm`` token-echo path may confirm a
    money-move.
  * :func:`_mask_evidence` — key-targeted + value-regex secret redaction so no
    header/cookie/token/storage_state material is ever stored or logged (§5.8 AC).

Import discipline (mirrors effect_tags.py): this module imports only
``systemu.core.models.ExternalEvidence``, ``systemu.runtime.effect_tags``, and
``systemu.runtime.financial_signal`` — all foundational, cycle-free. It does NOT
import ``systemu.core.llm_router`` (the deterministic-only contract) nor the
separate top-level ``sharing_on`` package (the redaction backstop is vendored
locally to avoid coupling ``systemu.runtime → sharing_on``).
"""
from __future__ import annotations

import logging
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, List, Mapping, MutableMapping, Optional
from urllib.parse import urlparse

from systemu.core.models import ExternalEvidence
from systemu.runtime.financial_signal import money_move_net_applies

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
#  MASK — evidence secret redaction (§5.8)
# ─────────────────────────────────────────────────────────────────────────────

# Case-insensitive secret KEYS → their values are replaced wholesale. Recurses
# into nested dicts/lists. Substring match so "request_headers"→"authorization"
# (as a key) and "api_key"/"x-api-key" both trip.
_SECRET_KEY_HINTS = (
    "authorization",
    "cookie",          # covers "cookie" and "set-cookie"
    "token",           # covers "token", "access_token", "refresh_token"
    "session",
    "sess",            # covers "sess", "sess_id", "sessionid"
    "api_key",
    "apikey",
    "secret",
    "password",
    "passwd",
    "storage_state",
    # broadened (MEDIUM leak): common secret-carrying key names.
    "jwt",
    "bearer",
    "auth",            # also matches "authorization"/"oauth"; substring is fine
    "sig",             # "signature", "signing_key", "sig"
    "credential",      # "credential(s)"
    "key",             # "signing_key", "private_key", "secret_key", "api_key"
)

_REDACTED = "[REDACTED]"

# Value-level backstop for known secret SHAPES, applied to every string value even
# under a non-secret key (vendored — deliberately NOT importing sharing_on).
_VALUE_SECRET_PATTERNS: "list[re.Pattern]" = [
    re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE),          # Bearer …
    re.compile(r"sk-[A-Za-z0-9\-]{8,}"),                                    # sk-… (OpenAI-style)
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),                              # ghp_/gho_/ghs_/…
    re.compile(r"glpat-[A-Za-z0-9\-]{10,}"),                                # GitLab PAT
    re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"),                           # Slack token
    # ── MEDIUM leak: secrets under a NEUTRAL key whose value SHAPE wasn't listed ──
    # AWS access keys (AKIA/ASIA/AGPA/AIDA/AROA/ANPA/ANVA + 16 upper-alnum).
    re.compile(r"(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA|ANVA)[A-Z0-9]{16}"),
    # JWT — header.payload.signature (three base64url segments, "eyJ" header).
    re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}"),
    # high-entropy long hex/base64 backstop — a raw session id / token. Length-gated
    # at 32+ hex to avoid over-scrubbing benign short ids / git shas / order numbers.
    re.compile(r"\b[0-9a-fA-F]{32,}\b"),
]


def _scrub_value_shapes(text: str) -> str:
    """Replace any known secret-shaped substring with the redaction marker."""
    for pat in _VALUE_SECRET_PATTERNS:
        text = pat.sub(_REDACTED, text)
    return text


def _key_is_secret(key: Any) -> bool:
    k = str(key).lower()
    return any(hint in k for hint in _SECRET_KEY_HINTS)


def _mask_evidence(ev: Any) -> Any:
    """Recursively redact secret material from a captured-evidence structure.

    Two layers:
      1. Key-targeted: a value under a secret-named key (authorization / cookie /
         set-cookie / token / session / storage_state / api_key / secret /
         password, case-insensitive, substring) is replaced with ``[REDACTED]``.
      2. Value backstop: every surviving string value is scanned for known secret
         SHAPES (Bearer …, sk-…, ghp_…, glpat-…, xox…-) and those substrings are
         scrubbed — so a secret leaked under a neutral key (e.g. a response body)
         is still removed.

    Never raises; returns a new structure (does not mutate the input). Non-secret
    scalars pass through unchanged.
    """
    try:
        if isinstance(ev, Mapping):
            out = {}
            for k, v in ev.items():
                if _key_is_secret(k):
                    out[k] = _REDACTED
                else:
                    out[k] = _mask_evidence(v)
            return out
        if isinstance(ev, (list, tuple)):
            return [_mask_evidence(x) for x in ev]
        if isinstance(ev, str):
            return _scrub_value_shapes(ev)
        # int / float / bool / None / other scalars: nothing to redact
        return ev
    except Exception:
        # a redaction failure must fail SAFE — return the marker, never the raw ev.
        logger.debug("[ExternalVerifier] _mask_evidence failed — returning marker", exc_info=True)
        return _REDACTED


# ─────────────────────────────────────────────────────────────────────────────
#  ExternalVerifier
# ─────────────────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _as_token_list(v: Any) -> "list[str]":
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    if isinstance(v, (list, tuple, set)):
        return [str(x) for x in v]
    return [str(v)]


def _tokens_all_present(expected: Iterable[str], observed: Iterable[str]) -> bool:
    """Deterministic predicate: EVERY expected token is echoed (as a substring of
    some observed value, or an exact member). Empty expected ⇒ nothing proven ⇒
    False (fail-closed — an empty expectation can't confirm anything)."""
    exp = [str(e) for e in expected if str(e)]
    obs = [str(o) for o in observed]
    if not exp:
        return False
    obs_blob = "\n".join(obs)
    return all(any(e == o for o in obs) or (e in obs_blob) for e in exp)


# ─────────────────────────────────────────────────────────────────────────────
#  URL host/scheme — a THIN mirror of the MCP connect host-pin/TLS gate
#  (manager.connect_and_discover → remote_policy.enforce_tls/mcp_host_allowed).
#  We deliberately reuse the CONCEPT (host-pin + https-only), not the socket path:
#  the readback host must equal the submit host and the scheme must be https. No
#  DNS/socket is opened here — the injected transport client owns real I/O.
# ─────────────────────────────────────────────────────────────────────────────

def _url_host(url: Any) -> str:
    try:
        return (urlparse(str(url or "")).hostname or "").lower().strip()
    except Exception:
        return ""


def _url_scheme(url: Any) -> str:
    try:
        return (urlparse(str(url or "")).scheme or "").lower().strip()
    except Exception:
        return ""


# ─────────────────────────────────────────────────────────────────────────────
#  IMPL-6 — the ambiguous-outcome / double-submit protocol (§5.8)
#
#  On ANY transport-ambiguous failure of an EFFECTFUL call (timeout AFTER send,
#  connection reset, 5xx-after-send), the loop MUST run a read-back BEFORE any
#  retry decision, keyed to a CLIENT-generated idempotency key (a UUID written
#  INTO the request BEFORE send — an Idempotency-Key header / tool param), NOT a
#  server-assigned token. On the lost-response case, a server token was never
#  received, so keying to it makes "confirmed-absent" undecidable and risks
#  reading a lost SUCCESS as absent ⇒ a DOUBLE-SUBMITTED money-move.
#
#  Three branches:
#    * confirmed-present  ⇒ credit path, NO re-submit.
#    * confirmed-absent   ⇒ a retry is SAFE and permitted (ONLY when the target
#      deterministically shows the CLIENT key was NOT processed).
#    * indeterminate      ⇒ operator card — never a silent retry, never a silent
#      give-up. Where the target offers NO idempotency primitive to key the
#      read-back deterministically, IMPL-6 falls HERE — it must NEVER risk a
#      confirmed-absent false negative.
# ─────────────────────────────────────────────────────────────────────────────

#: an Idempotency-Key HTTP header is the canonical MCP-transport carrier.
IDEMPOTENCY_HEADER = "Idempotency-Key"


def mint_idempotency_key() -> str:
    """Mint a CLIENT-generated idempotency key BEFORE send. Cryptographically
    unique (``secrets.token_hex(16)`` ⇒ 32 hex chars). The loop writes THIS into
    the request (header / tool param) before dispatch and keys the post-failure
    read-back to it. Tests can inject their own key instead of calling this."""
    return secrets.token_hex(16)


def inject_idempotency_key(
    request: MutableMapping[str, Any],
    key: str,
    *,
    target: str = "mcp_headers",
    idempotency_field: Optional[str] = None,
) -> bool:
    """Write the CLIENT idempotency ``key`` INTO ``request`` BEFORE send, so the
    post-failure read-back can be keyed to it deterministically.

    Two supported carriers (returns True when the key was actually written):
      * ``target="mcp_headers"`` — into ``request["headers"][IDEMPOTENCY_HEADER]``
        (the MCP transport spec). Creates the headers dict if absent.
      * ``target="tool_params"`` — into ``request[idempotency_field]`` when the
        tool's schema DECLARES an idempotency field (``idempotency_field`` given).

    Returns FALSE when there is NO idempotency primitive to key to (e.g.
    ``tool_params`` with ``idempotency_field=None``). A False here is what routes
    IMPL-6 to the operator card (never confirmed-absent). Never raises."""
    if not key:
        return False
    try:
        if target == "mcp_headers":
            headers = request.get("headers")
            if not isinstance(headers, MutableMapping):
                headers = {}
                request["headers"] = headers
            headers[IDEMPOTENCY_HEADER] = key
            return True
        if target == "tool_params":
            if not idempotency_field:
                return False  # no declared field ⇒ no primitive ⇒ unsupported
            request[str(idempotency_field)] = key
            return True
    except Exception:
        logger.debug("[IMPL-6] inject_idempotency_key failed — treating as unsupported",
                     exc_info=True)
        return False
    return False


@dataclass
class Impl6Outcome:
    """The three-branch result of :meth:`ExternalVerifier.handle_ambiguous_effect`.

    ``decision`` is one of ``"confirmed_present"`` / ``"confirmed_absent"`` /
    ``"indeterminate"``. The other fields are the derived signals the loop acts on:

      * ``allow_retry`` — True ONLY for a deterministic ``confirmed_absent``. IMPL-6
        NEVER re-submits itself; it only signals that a retry is SAFE.
      * ``operator_card`` — True for ``indeterminate`` (incl. the no-primitive case):
        the loop enqueues an operator card + parks. Never a silent retry / give-up.
      * ``evidence`` — a persisted-ready ``ExternalEvidence(confirmed=True,
        idempotency_key=key)`` for ``confirmed_present`` (route to the credit path,
        no re-submit); None otherwise.
    """
    decision: str
    allow_retry: bool = False
    operator_card: bool = False
    evidence: Optional[ExternalEvidence] = None
    detail: str = ""


class ExternalVerifier:
    """Turns a strategy's captured ground-truth into an ``ExternalEvidence``.

    Constructor takes INJECTED clients so a later wave can mock the API / email /
    web transports without touching this class:

        ExternalVerifier(api_client=..., email_client=..., web_client=...)

    Wave 1 does not call any transport — the deterministic match runs over the
    tokens/text already present in ``evidence_input`` — but the seams are here.
    """

    #: strategies that MAY confirm a money-move (strong deterministic token echo)
    _MONEY_MOVE_STRONG = frozenset({"api_readback", "email_confirm"})

    def __init__(
        self,
        api_client: Any = None,
        email_client: Any = None,
        web_client: Any = None,
        operator_channel: Any = None,
    ) -> None:
        self.api_client = api_client
        self.email_client = email_client
        self.web_client = web_client
        self.operator_channel = operator_channel

    # ── public entry point ────────────────────────────────────────────────
    def verify(
        self,
        objective: Any,
        effect_class: Any = None,
        evidence_input: Optional[Mapping[str, Any]] = None,
    ) -> ExternalEvidence:
        """Dispatch to a strategy and return an ExternalEvidence. Never raises —
        any error / unknown strategy / missing input fails CLOSED (confirmed=False).
        """
        oid = self._objective_id(objective)
        ev = ExternalEvidence(objective_id=oid, confirmed=False)
        try:
            if not isinstance(evidence_input, Mapping):
                return ev
            strategy = str(evidence_input.get("strategy") or "").strip().lower()
            if not strategy:
                # default dispatch by effect_class → advisory web assertion
                strategy = "web_assertion"

            handler = {
                "api_readback": self._api_readback,
                "email_confirm": self._email_confirm,
                "web_assertion": self._web_assertion,
                "operator_attest": self._operator_attest,
            }.get(strategy)
            if handler is None:
                ev.method = strategy
                return ev  # unknown strategy ⇒ fail-closed

            confirmed, method, detail = handler(objective, effect_class, evidence_input)
            # ── the MONEY-MOVE hard gate (BLOCKER-3 + BLOCKER-2) ──
            # If the objective is caught by the money-move net, only a STRONG
            # deterministic strategy may confirm; advisory strategies are demoted
            # to confirmed=False no matter what they observed.
            if confirmed and self._is_money_move(objective, effect_class):
                # BLOCKER-2: the LEGACY inline api_readback path (no readback_url)
                # does bare token equality with NO host-pin/https/create-once proof.
                # Although "api_readback" is nominally a STRONG method, the inline
                # variant lacks the hardened proof a money-move requires — so it is
                # NOT strong enough here. Demote it like any advisory strategy.
                legacy_inline_readback = (
                    method == "api_readback"
                    and not evidence_input.get("readback_url"))
                if method not in self._MONEY_MOVE_STRONG or legacy_inline_readback:
                    confirmed = False
                    detail = (detail + " | money-move: advisory strategy cannot confirm").strip(" |")
                    if legacy_inline_readback:
                        detail = (detail
                                  + " (inline api_readback lacks hardened readback_url path)")

            ev.confirmed = bool(confirmed)
            ev.method = method
            ev.detail = detail
            ev.stamped_at = _now_iso() if ev.confirmed else ev.stamped_at
            return ev
        except Exception:
            logger.debug("[ExternalVerifier] verify failed — fail-closed", exc_info=True)
            return ExternalEvidence(objective_id=oid, confirmed=False)

    # ── strategies (each returns (confirmed, method, detail)) ──────────────
    def _api_readback(self, objective, effect_class, ev_in) -> "tuple[bool, str, str]":
        """STRONG: RE-READ the effect from the SAME authenticated host over https
        and match a submission-unique, PROVABLY-FRESH token deterministically. The
        one path allowed to confirm a money-move.

        Hardening (wave 2), all deterministic, all fail-CLOSED:
          * host-pin — the readback host MUST equal the submit host (``submit_host``).
          * https-only — the readback URL scheme MUST be https.
          * token-freshness — every matched token MUST be provably absent pre-submit
            (``pre_submit_absent`` AND not in ``presubmit_tokens``); a token already
            present pre-submit is STALE (can't prove THIS run produced it).
          * exception-safe — ANY transport error (TLS/timeout/connection) via the
            INJECTED ``api_client.readback(url)`` ⇒ NOT confirmed, never raises.

        Back-compat: when no ``readback_url`` is present (the wave-1 shape), fall
        back to the inline ``observed_tokens`` deterministic token-equality path —
        the host-pin/https/freshness hardening applies to the ``readback_url`` path
        only (the shape the loop wiring will actually populate).
        """
        method = "api_readback"
        try:
            expected = _as_token_list(ev_in.get("expected_tokens"))
            readback_url = ev_in.get("readback_url")

            if not readback_url:
                # ── legacy wave-1 inline path: deterministic token equality ──
                # Fail-closed hardening (BLOCKER-2): this path has NO host-pin/https/
                # create-once proof. Two guards:
                #   (1) apply token-freshness GENERALLY — a STALE token (present
                #       pre-submit) can never confirm, even for a benign effect
                #       (freshness is a create-once invariant regardless of money).
                #   (2) the money-move REFUSAL for this path is enforced in verify()
                #       (a confirmed api_readback with no readback_url + money-move ⇒
                #       demoted): the inline path cannot supply the hardened proof a
                #       money-move requires.
                observed = _as_token_list(ev_in.get("observed_tokens"))
                if not _tokens_all_present(expected, observed):
                    return False, method, "no token match"
                fresh, why = self._tokens_are_fresh(expected, ev_in)
                if not fresh:
                    return False, method, why
                return True, method, "token echo matched (inline, fresh)"

            # ── the hardened, host-pinned, https-only, fresh readback path ──
            submit_host = str(ev_in.get("submit_host") or "").lower().strip()
            rb_host = _url_host(readback_url)
            rb_scheme = _url_scheme(readback_url)
            # host-pin: readback host must equal the submit host
            if not submit_host or rb_host != submit_host:
                return False, method, "host-pin refused: readback host != submit host"
            # https-only
            if rb_scheme != "https":
                return False, method, "https required for readback"
            # fetch the readback via the INJECTED transport (mocked in tests).
            # ANY exception here fails CLOSED (caught below).
            if self.api_client is None or not hasattr(self.api_client, "readback"):
                return False, method, "no api_client for readback"
            envelope = self.api_client.readback(readback_url)
            observed = self._observed_from_envelope(envelope)

            if not _tokens_all_present(expected, observed):
                return False, method, "no token match"

            # ── token-freshness gate (the create-once proof) ──
            fresh, why = self._tokens_are_fresh(expected, ev_in)
            if not fresh:
                return False, method, why
            return True, method, "token echo matched (host-pinned https, fresh)"
        except Exception:
            logger.debug("[ExternalVerifier] _api_readback failed — fail-closed", exc_info=True)
            return False, method, "readback error — fail-closed"

    def _email_confirm(self, objective, effect_class, ev_in) -> "tuple[bool, str, str]":
        """STRONG: a confirmation email/receipt echoes the exact expected token(s).

        Deterministic predicate: EVERY expected token (a confirmation-number /
        receipt id) must appear by exact equality/substring in the email subject
        or body. When an ``email_client`` is injected it is FETCHED (mocked in
        tests); ANY transport error fails CLOSED. Back-compat: inline
        ``observed_tokens`` still match when no client is injected. No LLM read.
        """
        method = "email_confirm"
        try:
            expected = _as_token_list(ev_in.get("expected_tokens"))
            observed = _as_token_list(ev_in.get("observed_tokens"))
            if self.email_client is not None and hasattr(self.email_client, "fetch"):
                envelope = self.email_client.fetch(ev_in.get("email_query"))
                observed = observed + self._observed_from_envelope(envelope)
            ok = _tokens_all_present(expected, observed)
            return ok, method, ("confirmation token matched" if ok else "no token match")
        except Exception:
            logger.debug("[ExternalVerifier] _email_confirm failed — fail-closed", exc_info=True)
            return False, method, "email fetch error — fail-closed"

    def _web_assertion(self, objective, effect_class, ev_in) -> "tuple[bool, str, str]":
        """ADVISORY: a DOM/UI text-equality assertion. May confirm a NON-money
        effect on exact text equality, but is HARD-GATED to never confirm a
        money-move (handled by the money-move gate in verify()). Accepts either an
        explicit ``assertion_passed`` bool OR an expected/observed text pair — but
        confirmation is ALWAYS a deterministic equality, never a model judgement.
        """
        expected = ev_in.get("expected_text")
        observed = ev_in.get("observed_text")
        if expected is not None and observed is not None:
            ok = str(expected) == str(observed)
        else:
            # an explicit assertion_passed is a deterministic predicate the caller
            # already evaluated (e.g. element-present). It is advisory; the money-
            # move gate still demotes it for a money-move.
            ok = ev_in.get("assertion_passed") is True
        return ok, "web_assertion", ("text/assertion matched" if ok else "assertion failed")

    def _operator_attest(self, objective, effect_class, ev_in) -> "tuple[bool, str, str]":
        """ADVISORY: a human operator attests the effect occurred. A deterministic
        boolean predicate (``attested is True``) — but, like web_assertion, cannot
        alone confirm a money-move (the money-move gate demotes it)."""
        ok = ev_in.get("attested") is True
        return ok, "operator_attest", ("operator attested" if ok else "not attested")

    # ── IMPL-6 — the ambiguous-outcome / double-submit protocol ─────────────
    def handle_ambiguous_effect(
        self,
        *,
        objective: Any,
        effect_class: Any,
        idempotency_key: str,
        readback_url: Any = None,
        submit_host: Any = None,
        retry_fn: Optional[Callable[[], Any]] = None,  # noqa: ARG002 — deliberately NOT called
    ) -> Impl6Outcome:
        """On a transport-ambiguous failure of an EFFECTFUL call, decide the
        three-branch outcome BEFORE any retry — keyed to the CLIENT
        ``idempotency_key`` (NOT a server token).

        ``retry_fn`` is accepted but DELIBERATELY NEVER called here: IMPL-6 never
        re-submits itself. It only classifies and signals ``allow_retry`` — the
        loop performs the (safe) retry, keeping the "no silent re-submit" contract
        auditable at the call site. Deterministic-only (no LLM). Never raises — any
        error fails to ``indeterminate`` (operator card), NEVER confirmed-absent.

        Branches:
          * confirmed_present ⇒ the read-back keyed to the CLIENT key shows the
            effect LANDED ⇒ ``ExternalEvidence(confirmed=True, idempotency_key=key)``
            + ``allow_retry=False`` (credit, NO re-submit).
          * confirmed_absent ⇒ the read-back DETERMINISTICALLY shows the CLIENT key
            was NOT processed (server supports idempotency AND reports it absent) ⇒
            ``allow_retry=True``.
          * indeterminate ⇒ the read-back can't decide, the target has NO
            idempotency primitive, or NO client key was minted ⇒
            ``operator_card=True`` + ``allow_retry=False``. NEVER confirmed-absent.
        """
        oid = self._objective_id(objective)
        try:
            decision, detail = self._impl6_readback(
                idempotency_key=idempotency_key,
                readback_url=readback_url,
                submit_host=submit_host,
            )
        except Exception:
            # a read-back error must fall to INDETERMINATE — never confirmed-absent.
            logger.debug("[IMPL-6] read-back raised — indeterminate (operator card)",
                         exc_info=True)
            decision, detail = "indeterminate", "read-back error — operator card"

        if decision == "confirmed_present":
            ev = ExternalEvidence(
                objective_id=oid,
                confirmed=True,
                method="impl6_idempotency_readback",
                detail=detail,
                idempotency_key=str(idempotency_key or ""),
                stamped_at=_now_iso(),
            )
            return Impl6Outcome(
                decision="confirmed_present", allow_retry=False,
                operator_card=False, evidence=ev, detail=detail)

        if decision == "confirmed_absent":
            # a retry is SAFE — but ONLY the loop re-submits, never IMPL-6 itself.
            return Impl6Outcome(
                decision="confirmed_absent", allow_retry=True,
                operator_card=False, evidence=None, detail=detail)

        # indeterminate (incl. no-primitive / no-key / read-back error): NEVER
        # confirmed-absent, NEVER a silent retry — an operator card + park.
        return Impl6Outcome(
            decision="indeterminate", allow_retry=False,
            operator_card=True, evidence=None, detail=detail)

    def _impl6_readback(
        self,
        *,
        idempotency_key: str,
        readback_url: Any,
        submit_host: Any,
    ) -> "tuple[str, str]":
        """The CLIENT-key read-back. Returns ``(decision, detail)`` where decision ∈
        {confirmed_present, confirmed_absent, indeterminate}. DETERMINISTIC.

        Fail-closed defaults (all ⇒ indeterminate, NEVER confirmed-absent):
          * NO client key was minted/injected before send ⇒ can't key the read-back.
          * NO api_client / no readback_url ⇒ can't read back.
          * host-pin refused (readback host != submit host) or non-https ⇒ refuse.
          * the target has NO idempotency primitive
            (``supports_idempotency`` is not True) ⇒ can't decide by client key.
          * the read-back envelope doesn't carry a processed-keys signal ⇒ unknown.

        Decisions:
          * confirmed_present ⇒ the CLIENT key IS in the server's processed set.
          * confirmed_absent  ⇒ the server SUPPORTS idempotency AND the CLIENT key
            is NOT in its processed set (a deterministic "not processed").
        """
        key = str(idempotency_key or "").strip()
        if not key:
            # NO client key ⇒ a server-token read-back would be undecidable. This is
            # exactly the lost-response hazard: fall to indeterminate, never absent.
            return "indeterminate", "no client idempotency key — cannot key read-back"

        if self.api_client is None or not hasattr(self.api_client, "readback"):
            return "indeterminate", "no api_client for idempotency read-back"

        if not readback_url:
            return "indeterminate", "no readback_url for idempotency read-back"

        # host-pin + https-only (mirror the hardened _api_readback gate): the read-
        # back must hit the SAME authenticated host over https, else refuse.
        sub_host = str(submit_host or "").lower().strip()
        rb_host = _url_host(readback_url)
        if sub_host and rb_host and rb_host != sub_host:
            return "indeterminate", "host-pin refused: readback host != submit host"
        if _url_scheme(readback_url) != "https":
            return "indeterminate", "https required for idempotency read-back"

        # fetch via the INJECTED transport (mocked in tests). ANY exception here is
        # caught by handle_ambiguous_effect ⇒ indeterminate (never absent).
        envelope = self.api_client.readback(readback_url)
        if not isinstance(envelope, Mapping):
            return "indeterminate", "read-back envelope not a mapping"

        # the target MUST expose an idempotency primitive to key the read-back
        # deterministically. Absent it, IMPL-6 CANNOT conclude absent (a landed
        # effect could be invisible) ⇒ indeterminate.
        if envelope.get("supports_idempotency") is not True:
            return "indeterminate", "target has no idempotency primitive — operator card"

        # the deterministic processed-keys signal keyed to the CLIENT key.
        processed = envelope.get("processed_idempotency_keys")
        if not isinstance(processed, (list, tuple, set)):
            # the server supports idempotency but returned no processed-keys set ⇒
            # unknown ⇒ indeterminate (never guess absent).
            return "indeterminate", "no processed-keys signal — operator card"
        processed_set = {str(k) for k in processed}

        if key in processed_set:
            return "confirmed_present", "client idempotency key found in processed set"
        # server SUPPORTS idempotency AND the client key is NOT processed ⇒ a
        # deterministic confirmed-absent (the ONLY branch that permits a retry).
        return "confirmed_absent", "client idempotency key NOT processed (deterministic)"

    # ── operator-attest artifact (render-only; NOT an auto-confirm) ─────────
    def build_operator_attest_artifact(self, objective, evidence_input=None):
        """Render RAW, REDACTED, AGENT-UNINTERPRETED evidence into an operator-
        facing artifact — a :class:`GateDescriptor` typed ``operator`` (a render-
        only inbox gate). This is NOT a confirmation: the verifier never sets
        ``confirmed`` from this path. The operator reads the raw ground-truth and
        judges for themselves.

        Two invariants (asserted by the tests):
          * the agent's INTERPRETED success prose is EXCLUDED — only raw evidence
            is shown, so a persuasive-but-wrong agent narrative can't drive the
            operator's decision.
          * every value is run through :func:`_mask_evidence` first, so a token /
            secret echoed in the raw evidence is REDACTED before it is rendered.
        """
        from systemu.interface.command.gate import GateDescriptor

        ev_in = evidence_input if isinstance(evidence_input, Mapping) else {}
        raw = ev_in.get("raw_evidence")
        # redact FIRST — never render un-masked evidence.
        masked = _mask_evidence(raw)
        try:
            import json as _json
            inspect = _json.dumps(masked, default=str, indent=2, sort_keys=True)
        except Exception:
            inspect = str(masked)
        oid = self._objective_id(objective)
        options = ["Dismiss", "Attest occurred"]
        return GateDescriptor(
            title=f"Operator: verify external effect (objective {oid})",
            risk="high",
            # RAW redacted evidence only — the agent's success prose is deliberately
            # NOT included here.
            inspect=inspect,
            options=options,
            safe_default=options[0],
            what_approve_does=("Records that YOU (the operator) confirmed the "
                               "external effect really occurred, from the raw "
                               "evidence above."),
            dedup=f"operator_attest:{oid}",
        )

    # ── helpers ────────────────────────────────────────────────────────────
    @staticmethod
    def _observed_from_envelope(envelope: Any) -> "list[str]":
        """Flatten a transport envelope (readback / email) into observed token
        strings for deterministic matching. Accepts an explicit ``observed_tokens``
        list, and/or free-text fields (subject/body/response fields) which are
        included whole so a token can match as a substring. Never raises."""
        out: "list[str]" = []
        try:
            if isinstance(envelope, Mapping):
                out.extend(_as_token_list(envelope.get("observed_tokens")))
                for key in ("subject", "body", "response_body", "text", "content"):
                    v = envelope.get(key)
                    if isinstance(v, str) and v:
                        out.append(v)
            elif isinstance(envelope, str):
                out.append(envelope)
            elif isinstance(envelope, (list, tuple, set)):
                out.extend(str(x) for x in envelope)
        except Exception:
            return out
        return out

    @staticmethod
    def _tokens_are_fresh(expected: "list[str]", ev_in: Mapping) -> "tuple[bool, str]":
        """Token-freshness (create-once proof): a matched token may confirm ONLY if
        it is provably ABSENT from the pre-submit snapshot — otherwise it is STALE
        and cannot prove THIS run produced the effect.

        Rule (fail-closed):
          * a token present in ``presubmit_tokens`` ⇒ STALE ⇒ not fresh.
          * ``pre_submit_absent`` True asserts a pre-submit readback found the
            effect ABSENT — the strongest freshness proof.
          * with neither an absent-proof nor any distinguishing presubmit snapshot
            we cannot establish freshness ⇒ not fresh.
        """
        exp = [str(e) for e in expected if str(e)]
        presubmit = set(_as_token_list(ev_in.get("presubmit_tokens")))
        pre_absent = ev_in.get("pre_submit_absent") is True
        # any expected token already present pre-submit is stale
        for e in exp:
            if e in presubmit:
                return False, "stale token: already present pre-submit"
        if pre_absent:
            return True, "fresh (pre_submit_absent)"
        # no absent-proof: require a non-empty presubmit snapshot that the expected
        # tokens are demonstrably NOT in (a distinguishing create-once set).
        if presubmit:
            return True, "fresh (distinct from presubmit snapshot)"
        return False, "freshness unprovable: no pre-submit snapshot"

    def _is_money_move(self, objective, effect_class) -> bool:
        effect_tags = self._effect_tags(objective, effect_class)
        # gather every text field the objective may carry (goal/success_criteria on
        # the real model; ``text`` on the test stub) so the verb scan is complete.
        text = " ".join(
            str(getattr(objective, a, "") or "")
            for a in ("text", "goal", "success_criteria")
        ).strip()
        params = getattr(objective, "params", None) or {}
        # the real Objective exposes ``requires_external_verification``; the test
        # stub exposes the short ``requires_external`` — honour either so the
        # fail-closed UNKNOWN-effect fallback engages on both.
        requires_external = bool(
            getattr(objective, "requires_external", False)
            or getattr(objective, "requires_external_verification", False))
        return money_move_net_applies(effect_tags, text, params, requires_external)

    @staticmethod
    def _effect_tags(objective, effect_class) -> "set":
        tags = set(getattr(objective, "effect_tags", None) or set())
        if effect_class is not None:
            tags.add(effect_class)
        return tags

    @staticmethod
    def _objective_id(objective) -> int:
        for attr in ("objective_id", "id"):
            v = getattr(objective, attr, None)
            if isinstance(v, int):
                return v
            if isinstance(v, str) and v.isdigit():
                return int(v)
        return 0
