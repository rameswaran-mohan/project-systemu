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
    "unknown",
)


# error categories that signal an environment / param issue
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


def _extract_module(text: str) -> Optional[str]:
    m = _MODULE_RE.search(text)
    return m.group(1) if m else None


def _extract_http_status(text: str) -> Optional[str]:
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
        lambda t, p: bool(_HTTP_STATUS_RE.search(t)) and any(s in t for s in (
            "http", "status", "response", "request",
        )),
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
