"""Rule-based failure classifier — cheap, deterministic, no LLM.

v0.4.0-b foundation.  Given a ``ToolResult``-shaped object from
``shadow_runtime._handle_tool_call``, decide which failure category the
result falls into.  Output is used to:

1. Pick the right Reflection block to inject on the next iteration (v0.4.0-b).
2. Build the ``_pattern_signature`` for memory writes (v0.4.0-a).
3. Provide the rule-based first pass before the Intelligent Supervisor's
   Tier-3 hop (v0.4.0-d), so 80%+ of classifications cost nothing.

Categories are a small, closed set so consumers can dispatch on them
exhaustively:

  * ``missing_dependency``    — pip package missing or unsatisfied manifest
  * ``param_error``           — JSON-schema mismatch / wrong arg names
  * ``timeout``               — exceeded the tool's timeout
  * ``http_error``            — 4xx / 5xx in stderr
  * ``network_error``         — connection refused / DNS / TLS
  * ``permission_error``      — EACCES / PermissionError
  * ``file_not_found``        — ENOENT / FileNotFoundError
  * ``parse_error``           — JSON / YAML / structural parse failure
  * ``api_error``             — upstream LLM provider error
  * ``unknown``               — nothing matched

Tests in ``tests/test_failure_classifier.py`` pin the regex set.

This module **never** imports anything heavy at module load — the goal
is microsecond classification.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Closed enum of categories

CATEGORIES = (
    "missing_dependency",
    "param_error",
    "timeout",
    "http_error",
    "network_error",
    "permission_error",
    "file_not_found",
    "parse_error",
    "api_error",
    "tool_inadequate",
    # Plan 0 / Build 1 — pull-decision (reverse-harness) failure modes.
    # These describe *governance* mistakes around a capability request,
    # not tool-execution errors, and are produced by classify_pull_failure().
    "premature_request",   # requested a grant before exhausting available tools
    "wasted_request",      # request was denied but a viable fallback existed
    "unused_grant",        # grant was issued but never actually invoked
    "cap_exceeded",        # request denied by the per-run cap (over-delegation)
    "unknown",
)


# v0.5.0-c: error categories that signal an environment / param issue
# rather than a structural tool flaw.  When ≥3 consecutive failures
# happen and NONE of them fall into these buckets, the supervisor's
# diagnostic LLM is invoked to decide whether the tool itself is
# structurally inadequate.
_RECOVERABLE_CATEGORIES = frozenset({
    "missing_dependency",
    "param_error",
    "timeout",
    "network_error",
})

# Keyword hints that suggest a tool is structurally inadequate.  Used by
# the rule-based pre-filter before the diagnostic Tier-1 hop fires —
# saves an LLM call when the error is clearly *not* an inadequacy.
_INADEQUACY_HINTS = (
    "doesn't support", "does not support", "unable to",
    "not implemented", "not supported", "would need", "missing functionality",
    "no way to", "cannot handle", "unsupported", "no such method",
)


def looks_like_inadequacy_signal(category: str, error_text: str) -> bool:
    """Cheap rule-based pre-filter for tool_inadequate suspicion.

    Returns True when the category is NOT in the recoverable bucket AND
    the error text contains at least one inadequacy hint.  Used by the
    supervisor to decide whether to fire the Tier-1 diagnostic.
    """
    if category in _RECOVERABLE_CATEGORIES:
        return False
    if not error_text:
        return False
    lowered = error_text.lower()
    return any(hint in lowered for hint in _INADEQUACY_HINTS)


@dataclass(frozen=True)
class Classification:
    category:   str               # one of CATEGORIES
    confidence: str               # "high" (explicit error_type) | "medium" (regex) | "low" (fallback)
    keyword:    Optional[str]     # extracted token for pattern_signature
    matched_rule: Optional[str]   # name of the rule that fired, for debugging


# ─────────────────────────────────────────────────────────────────────────────
# Rule definitions
#
# Order matters — first match wins.  Each rule is (category, name, predicate,
# keyword-extractor).  The predicate receives the lowercased combined error
# text + the raw parsed dict.

_HTTP_STATUS_RE = re.compile(r"\b(4\d{2}|5\d{2})\b")
_MODULE_RE      = re.compile(r"no module named ['\"]?([\w\.\-]+)")
_PARAM_RE       = re.compile(r"(missing\s+(?:required\s+)?(?:argument|parameter|field|key)\s*[:\-]?\s*['\"]?(\w+))")

# ── R-A10 B9 (Fix 3 + Fix B): ANCHORED HTTP-status detection, matrix-designed ──
# A bare 4xx/5xx token is NOT enough — `GET /v1/items/401`, `line 401`, `id 403`,
# `returned 404 rows` all contain one. A CONFIDENT http status is the code bound to
# a RELIABLE anchor. Fix B redesigns the matcher against an explicit fold/don't-fold
# matrix (parametrized in tests/test_ra10_runtime_fold.py):
#
#   MUST FOLD  — real library/tool/structured shapes:
#     "401 Client Error: Unauthorized for url: ..."  (requests raise_for_status)
#     "ClientResponseError: 401, message='Unauthorized'"  (aiohttp)
#     "HTTP 401" / "HTTP/1.1 403 Forbidden" / "status_code=404" / "status: 403"
#     "401 Unauthorized" / "401: Unauthorized" (code + reason at start)
#     OAuth: "invalid_token ... (401)" / "OAuthError: 401 invalid_grant"
#   MUST NOT FOLD — benign counts / ids / frames in FAILED output:
#     "GET /v1/items/401" / 'line 401' / "app.py:404" / "error 40100"
#     "returned 404 rows" / "status 404 tasks remaining" / "with 404 items"
#     "response had 404 duplicate keys" / "the request returned 404 matching users"
#     "row status for id 403 not found"  (the reason-phrase leak: `403 not found`
#       must NOT match when preceded by a benign id/row/for token)
#
# Design: prefer the structured parsed status_code (always fold — handled in the
# rule/subclass). For free-text, anchor the code to `<code> Client/Server Error`,
# `<Word>Error: <code>`, `HTTP[/x.y] <code>`, `status(_code)?[:=] <code>` (adjacent),
# and `<code>[:)]? <ReasonPhrase>` — the reason-phrase alt GUARDED so a benign noun
# before the code blocks it. Count-ambiguous markers (returned/with/got/response +
# code + a plural count noun) do NOT fold — the code must be terminal / punctuated
# and NOT followed by a count noun. Each alt keeps the path/frame/longer-number
# code sub-guard. Used by BOTH classify_tool_result AND http_error_subclass.
_HTTP_REASON_PHRASE = (
    r"unauthorized|forbidden|not\s+found|unprocessable(?:\s+entity)?|"
    r"bad\s+request|payment\s+required|conflict|gone|too\s+many\s+requests|"
    r"internal\s+server\s+error|bad\s+gateway|service\s+unavailable|gateway\s+timeout"
)
# The status code itself, forbidden from being part of a path/frame/longer number
# (not preceded by another digit, a '/', a '.' or a ':', and not followed by a digit).
_ANCHORED_CODE = r"(?<![\d/.:])(4\d{2}|5\d{2})(?!\d)"
# A code preceded by an EXPLICIT marker (the marker already proves confidence), so
# a ':' or '=' separator is allowed here (`"code":401`, `status_code=404`) — but the
# path/frame/longer-number guards still hold (no '/' or '.' before it, no digit
# either side). This is used ONLY inside marker-PRECEDED alternatives.
_MARKED_CODE = r"(?<![\d/.])(4\d{2}|5\d{2})(?!\d)"
# GUARD (kills the reason-phrase leak `id 403 not found`): a code immediately
# preceded by a benign noun/token (`id`/`row`/`for`/`of`/`#`) is an identifier, not
# a status — block the reason-phrase alt in that position. Each lookbehind branch is
# independently fixed-width (Python re requirement).
_NOT_BENIGN_NOUN_BEFORE = r"(?<!id )(?<!row )(?<!for )(?<!of )(?<!#)(?<!# )"
# GUARD (kills the count-noun false positives `returned 404 rows`, `with 404 items`,
# `status 404 tasks remaining`, `response had 404 duplicate keys`, `returned 404
# matching users`): after a marker-preceded code, a following plural count/quantity
# noun means the number is a COUNT, not a status. Require the code NOT be followed by
# one. `Got 401 back` / `responded with 401` (terminal) still pass.
_NOT_COUNT_NOUN_AFTER = (
    r"(?!\s+(?:rows|records|items|tasks|users|keys|entries|results|matching|"
    r"duplicate|remaining|files|objects|bytes|times|attempts|retries|ms|"
    r"seconds|hits|matches|documents|docs|nodes|columns|fields)\b)"
)
# Count-ambiguous marker words that, when adjacent to the code, only make it a
# CONFIDENT status if the code is terminal / punctuated and NOT followed by a count
# noun. Deliberately NARROW: NO bare `error`/`line`/`id`.
_HTTP_MARKER = (
    r"with|responded|rejected|returned|got|code|status(?:_code)?|http|had"
)
# Each alternative anchors the code to a reliable status marker or reason-phrase.
_HTTP_ANCHORED_ALTS = [
    # HTTP <code>  /  HTTP/1.1 <code>  (reliable: the `http` token proves it)
    r"https?\b[^\d]{0,12}" + _ANCHORED_CODE,
    # status <code> / status_code = <code> / status: <code>  (ADJACENT: ≤ one
    # separator between `status[_code]` and the code, so `status 404 tasks` — where
    # 404 is a count with `tasks` after — is blocked by the count-noun guard).
    r"status(?:_code)?\b[\s:=\"'()\[\]]{0,3}" + _MARKED_CODE + _NOT_COUNT_NOUN_AFTER,
    # <Word>Error: <code>  (aiohttp `ClientResponseError: 401`, `OAuthError: 401`).
    r"\w*error\b\s*[:=]?\s*" + _MARKED_CODE,
    # requests raise_for_status() shape: "<code> Client Error" / "<code> Server Error"
    _ANCHORED_CODE + r"\s+(?:client|server)\s+error",
    # <code>[:)]? Reason-Phrase   ("401 Unauthorized", "401: Unauthorized",
    # "403 Forbidden") — GUARDED so a benign id/row/for/of/# token before the code
    # blocks it (`id 403 not found` must NOT match).
    _NOT_BENIGN_NOUN_BEFORE + _ANCHORED_CODE + r"\s*[:)]?\s*(?:" + _HTTP_REASON_PHRASE + r")",
    # Reason-Phrase (<code>)  — reason precedes, code parenthesised after
    r"(?:" + _HTTP_REASON_PHRASE + r")\s*\(?\s*" + _ANCHORED_CODE,
    # OAuth best-effort: an auth-ish token adjacent (≤ ~40 non-digit chars, same
    # clause) to a (parenthesised or bare) code — "invalid_token: the access token
    # expired (401)". The auth token proves intent; the `(?!\d)` sub-guard blocks a
    # longer number.
    r"(?:invalid_token|invalid_grant|invalid_client|access[_\s]token|oauth|"
    r"bearer|unauthorized|authentication)\b[^\d]{0,40}\(?\s*"
    + _ANCHORED_CODE,
    # count-ambiguous marker-PRECEDED form: a status marker adjacent to the code,
    # code terminal / punctuated and NOT followed by a count noun — "Server responded
    # with 401", "Bearer token rejected: 401", "Request failed with 401", "Got 401
    # back", "code":401, "status_code=404". Blocks "returned 404 rows", "with 404
    # items", "response had 404 duplicate keys".
    r"\b(?:" + _HTTP_MARKER + r")\b[\s:=\"'()\[\]]{0,3}" + _MARKED_CODE + _NOT_COUNT_NOUN_AFTER,
]
_HTTP_ANCHORED_RE = re.compile(
    "(?:" + "|".join(_HTTP_ANCHORED_ALTS) + ")", re.IGNORECASE
)


def _anchored_http_status(text: str) -> Optional[str]:
    """Return the CONFIDENT http status code as a string, or None when the text
    has no status anchored to a marker/reason-phrase (a bare 4xx token in a path,
    frame, or id is NOT confident). Never raises."""
    try:
        m = _HTTP_ANCHORED_RE.search(text or "")
        if not m:
            return None
        # Return whichever capture group matched (only one code group fires).
        for g in m.groups():
            if g:
                return g
    except Exception:
        return None
    return None


def _has_structured_http_status(parsed: Any) -> bool:
    """True when the parsed payload carries an INT status_code / http_status_code
    (bool excluded — it is an int subclass but never a real status). Never raises."""
    try:
        if not isinstance(parsed, dict):
            return False
        for key in ("status_code", "http_status_code"):
            val = parsed.get(key)
            if isinstance(val, int) and not isinstance(val, bool):
                return True
    except Exception:
        return False
    return False


def _extract_module(text: str) -> Optional[str]:
    m = _MODULE_RE.search(text)
    return m.group(1) if m else None


def _extract_http_status(text: str) -> Optional[str]:
    # Prefer the anchored (confident) code; fall back to a bare token only for the
    # keyword extractor's pattern_signature (never for the fold decision).
    anchored = _anchored_http_status(text)
    if anchored:
        return anchored
    m = _HTTP_STATUS_RE.search(text)
    return m.group(1) if m else None


def _extract_missing_param(text: str) -> Optional[str]:
    m = _PARAM_RE.search(text)
    return m.group(2) if m else None


def _first_path_like(text: str) -> Optional[str]:
    """Pull the first filename / path-like token out of text for FNF keywords."""
    m = re.search(r"['\"]?([\w/\\\-\.]+\.\w{1,5})['\"]?", text)
    return m.group(1) if m else None


# Each rule: (category, name, predicate, keyword_fn)
# predicate signature: (low_text: str, parsed: dict) -> bool
# keyword_fn  signature: (low_text: str, parsed: dict) -> Optional[str]
_RULES = [
    (
        "missing_dependency", "explicit_error_type",
        lambda t, p: (p or {}).get("error_type") in (
            "missing_dependency",
            "dependency_install_blocked",
            "dependency_install_pending_approval",
            "dependency_install_failed",
        ),
        lambda t, p: ((p or {}).get("missing_packages") or [None])[0],
    ),
    (
        "missing_dependency", "no_module_named",
        lambda t, p: "no module named" in t or "modulenotfounderror" in t,
        lambda t, p: _extract_module(t),
    ),
    (
        "timeout", "timed_out_marker",
        lambda t, p: (p or {}).get("timed_out") or "timed out" in t or "timeout" in t,
        lambda t, p: "timeout",
    ),
    (
        "param_error", "json_schema_mismatch",
        lambda t, p: any(s in t for s in (
            "missing required argument",
            "missing required parameter",
            "missing required field",
            "missing required key",
            "validation error",
            "schema validation",
            "unexpected keyword argument",
            "got an unexpected keyword argument",
        )),
        lambda t, p: _extract_missing_param(t),
    ),
    (
        "http_error", "http_status_code",
        # Fix 3: fire on a STRUCTURED parsed status_code, OR an ANCHORED free-text
        # status (code adjacent to HTTP/status/response marker or a reason-phrase).
        # A bare 4xx token co-occurring with an http-ish word is NO LONGER enough —
        # `GET /v1/items/401`, `line 401`, `id 403` must not misclassify as
        # http_error (and thus never fold a credential card).
        lambda t, p: _has_structured_http_status(p) or bool(_anchored_http_status(t)),
        lambda t, p: _extract_http_status(t),
    ),
    (
        "network_error", "connection_marker",
        lambda t, p: any(s in t for s in (
            "connection refused", "connection reset", "name or service not known",
            "name resolution failed", "ssl", "tls", "dns",
            "max retries exceeded", "failed to establish a new connection",
        )),
        lambda t, p: "network",
    ),
    (
        "permission_error", "eaccess_marker",
        lambda t, p: any(s in t for s in (
            "permission denied", "permissionerror", "eacces", "access is denied",
        )),
        lambda t, p: "permission",
    ),
    (
        "file_not_found", "enoent_marker",
        lambda t, p: any(s in t for s in (
            "no such file", "filenotfound", "enoent", "errno 2",
        )),
        lambda t, p: _first_path_like(t) or "file",
    ),
    (
        "parse_error", "parse_marker",
        lambda t, p: any(s in t for s in (
            "json decode", "jsondecodeerror", "yaml.error", "yamlerror",
            "invalid json", "could not parse", "unexpected token",
            "expecting value", "expecting property",
        )),
        lambda t, p: "parse",
    ),
    (
        "api_error", "upstream_marker",
        lambda t, p: any(s in t for s in (
            "rate limit", "ratelimiterror", "openrouter", "openai",
            "anthropic", "deepseek", "service unavailable", "bad gateway",
        )),
        lambda t, p: "api",
    ),
]


# ─────────────────────────────────────────────────────────────────────────────
# Public API

def classify_tool_result(result: Any) -> Classification:
    """Classify a ToolResult-shaped failure result.

    Args:
        result: ToolResult instance OR a dict-shaped equivalent.  Reads
                ``.success`` / ``.error`` / ``.stderr`` / ``.parsed``
                where present.

    Returns:
        :class:`Classification` with the matched category and keyword.
        For a successful result returns ``Classification("unknown", "low", None, None)``
        — callers should only invoke this when ``result.success`` is False.
    """
    # Tolerate either ToolResult instances or plain dicts (tests, replays).
    def _get(attr, default=None):
        if hasattr(result, attr):
            return getattr(result, attr)
        if isinstance(result, dict):
            return result.get(attr, default)
        return default

    success = _get("success", True)
    if success:
        return Classification("unknown", "low", None, None)

    parsed   = _get("parsed", {}) or {}
    error    = _get("error", "") or ""
    stderr   = _get("stderr", "") or ""

    # Concatenated lowercased text for regex / keyword matching.
    combined = " ".join(str(x) for x in (error, stderr, parsed.get("error", "")) if x).lower()

    # Explicit error_type from the structured parsed payload is high-confidence.
    declared_type = parsed.get("error_type")

    for category, name, predicate, kw_fn in _RULES:
        try:
            if predicate(combined, parsed):
                confidence = "high" if (declared_type and category in declared_type) or name == "explicit_error_type" else "medium"
                keyword = None
                try:
                    keyword = kw_fn(combined, parsed)
                except Exception:
                    keyword = None
                return Classification(
                    category=category,
                    confidence=confidence,
                    keyword=keyword,
                    matched_rule=name,
                )
        except Exception:
            logger.debug("[FailureClassifier] rule '%s' raised; skipping", name, exc_info=True)
            continue

    return Classification(
        category="unknown",
        confidence="low",
        keyword=None,
        matched_rule=None,
    )


# ─────────────────────────────────────────────────────────────────────────────
# R-A10 B9 (AC4) — http_error status sub-classifier
#
# When ``classify_tool_result(result).category == "http_error"``, this decides
# whether the 4xx/5xx is a MISSING-REQUIREMENT signal (auth / bad-request) that
# should fold into a Requirement + precede-objective rather than count toward the
# stuck bound. It is a cheap, deterministic, NEVER-raising helper. It shares the
# module-level ANCHORED matcher (``_anchored_http_status``) with the http_error
# rule so both agree on what a CONFIDENT status is (Fix 3).


def _http_sub_for_code(code: int) -> str:
    """Map an integer HTTP status to the B9 sub-class."""
    if code in (401, 403):
        return "auth"
    if code in (422, 404):
        return "semantic"
    return "other"


def http_error_subclass(result: Any) -> str:
    """Sub-classify an http_error into ``"auth"`` (401/403 — a MISSING credential),
    ``"semantic"`` (422/404 — a MISSING decision / bad request), or ``"other"``
    (500/other/unparseable — a transient/server fault that must take the normal
    reflection + stuck path).

    Only meaningful when the parent ``classify_tool_result`` category is
    ``"http_error"``; callers gate on that. Extraction order:

      1. Prefer an INT ``result.parsed["status_code"]`` or
         ``result.parsed["http_status_code"]`` if present.
      2. Else regex-scan ``result.stderr`` / ``result.error`` (and the parsed
         ``error`` blob) for a STANDALONE 4xx/5xx code.

    NEVER raises — any parse issue (missing attrs, non-int, junk object) → ``"other"``.
    """
    try:
        def _get(attr, default=None):
            if hasattr(result, attr):
                return getattr(result, attr)
            if isinstance(result, dict):
                return result.get(attr, default)
            return default

        parsed = _get("parsed", {}) or {}
        if isinstance(parsed, dict):
            # Prefer an explicit parsed status code (either key). Bool is an int
            # subclass but is never a real status — exclude it.
            for key in ("status_code", "http_status_code"):
                val = parsed.get(key)
                if isinstance(val, int) and not isinstance(val, bool):
                    return _http_sub_for_code(val)

        # ANCHORED regex fallback over the free-text error surfaces (Fix 3). A bare
        # 4xx token in a URL path segment, a source line-number/frame, or a longer
        # number is NOT a confident status — only a code adjacent to a status marker
        # or reason-phrase counts. So `GET /v1/items/401`, `line 401`, `id 403`
        # correctly degrade to "other" (normal reflection path, never a fold).
        parsed_err = parsed.get("error", "") if isinstance(parsed, dict) else ""
        text = " ".join(
            str(x) for x in (_get("stderr", ""), _get("error", ""), parsed_err) if x
        )
        code = _anchored_http_status(text)
        if code:
            return _http_sub_for_code(int(code))
    except Exception:
        return "other"
    return "other"


def classify_pull_failure(
    *,
    attempts_before: int,
    decision: str,
    fallback_ok: Optional[bool],
    used_after_grant: Optional[bool],
    kind: str = "",
    cap_exceeded: bool = False,
) -> str:
    """Classify a *pull-decision* (reverse-harness) failure mode.

    Unlike :func:`classify_tool_result`, which inspects a tool-execution
    error, this looks at the governance shape of a capability request and
    returns one of the three pull categories — or ``"unknown"`` when the
    decision looks well-behaved.

    Args:
        attempts_before:   How many available tools the shadow tried before
                           resorting to a request (``HarnessRequest.attempts_before_request``).
        decision:          The harness decision — e.g. ``"request"``,
                           ``"grant"``, ``"deny"``.  A truthy value means a
                           request was actually made.
        fallback_ok:       Whether a viable fallback existed at decision time.
                           Only meaningful for a ``"deny"`` decision.
        used_after_grant:  Whether a granted capability was subsequently
                           invoked.  Only meaningful for a ``"grant"`` decision.

    Returns:
        One of ``"premature_request"``, ``"wasted_request"``,
        ``"unused_grant"``, or ``"unknown"``.

    Rules (first match wins):
        * ``cap_exceeded``      — the request was denied by the per-run request
          cap (``cap_exceeded`` is True). Highest precedence for a deny: this is
          an over-delegation signal, NOT a fallback judgement, so it is never
          folded into ``wasted_request`` (v0.9.41).
        * ``premature_request`` — a request was made with no prior tool
          attempts (``attempts_before < 1``) **for a kind where trying a local
          tool first is expected** (``tool``/``skill``). For concrete-gap kinds
          (``access``/``mcp``/``compute``/``subagent``) there is no local
          alternative to try, so an immediate request is correct, not premature
          (v0.9.38 Bug 12). With ``kind`` unset the rule is conservative — it
          does NOT flag premature.
        * ``wasted_request``    — the request was denied yet a viable
          fallback existed (``decision == "deny"`` and ``fallback_ok``).
        * ``unused_grant``      — a grant was issued but never invoked
          (``decision == "grant"`` and ``used_after_grant`` is False).
        * ``unknown``           — none of the above.
    """
    decision = (decision or "").strip().lower()
    kind = (kind or "").strip().lower()
    request_made = bool(decision)

    try:
        attempts = int(attempts_before)
    except (TypeError, ValueError):
        attempts = 0

    if decision == "deny" and bool(cap_exceeded):
        return "cap_exceeded"
    if request_made and attempts < 1 and kind in ("tool", "skill"):
        return "premature_request"
    if decision == "deny" and bool(fallback_ok):
        return "wasted_request"
    if decision == "grant" and used_after_grant is False:
        return "unused_grant"
    return "unknown"


def reflection_strategies_for(category: str) -> Iterable[str]:
    """Return the recommended strategy options for the LLM, ordered.

    The Reflection block emits these as an enumerated choice so the LLM
    must explicitly pick one rather than blindly retry.  Order signals
    the recommended preference; the LLM may still pick any.
    """
    base = ("RETRY_WITH_DIFFERENT_PARAMS", "TRY_DIFFERENT_TOOL", "LOAD_RESOURCE", "FAIL")
    if category == "missing_dependency":
        return ("FAIL",)  # only resolution is operator approval — don't try the tool again
    if category == "param_error":
        return ("RETRY_WITH_DIFFERENT_PARAMS", "LOAD_RESOURCE", "FAIL")
    if category == "timeout":
        return ("RETRY_WITH_DIFFERENT_PARAMS", "TRY_DIFFERENT_TOOL", "FAIL")
    if category == "http_error":
        return ("RETRY_WITH_DIFFERENT_PARAMS", "TRY_DIFFERENT_TOOL", "FAIL")
    if category == "network_error":
        return ("RETRY_WITH_DIFFERENT_PARAMS", "FAIL")
    if category == "permission_error":
        return ("FAIL",)
    if category == "file_not_found":
        return ("RETRY_WITH_DIFFERENT_PARAMS", "FAIL")
    if category == "parse_error":
        return ("RETRY_WITH_DIFFERENT_PARAMS", "FAIL")
    if category == "api_error":
        return ("RETRY_WITH_DIFFERENT_PARAMS", "FAIL")
    return base
