"""Canonical memory claim-type enums.

Single source of truth for the values that may appear in the
``category`` field of a buffer entry.  Imported by both the writing
side (``pipelines/refinery.py``) and the validating side
(``Vault.append_shadow_memory_buffer``) so a new category added in one
place cannot drift away from the other.

See ``docs/memory-model.md`` for the tier model and write contract.
"""

from __future__ import annotations

from typing import FrozenSet


# ─────────────────────────────────────────────────────────────────────────────
#  Shadow tier — closed enum
# ─────────────────────────────────────────────────────────────────────────────
# These are the canonical category values written by the refinery
# pipeline.  Plural form is intentional and matches what the LLM emits.
# A new entry here MUST be coordinated with the prompt in
# `systemu/prompts/extract_memory_candidates.md` so the LLM knows the
# category is valid output.
SHADOW_CLAIM_TYPES: FrozenSet[str] = frozenset({
    "heuristics",
    "failure_patterns",
    "tool_quirks",
    "domain_glossary",
    "self_assessment",
})


# ─────────────────────────────────────────────────────────────────────────────
#  Elder tier — recommended (not enforced)
# ─────────────────────────────────────────────────────────────────────────────
# Elder claim categories are LLM-driven and open-ended.  The list below
# is documentation only — the vault accepts any string here that doesn't
# collide with a Shadow-tier name.  Use the strings here when emitting
# Elder entries deliberately; the LLM may emit others.
ELDER_RECOMMENDED_TYPES: FrozenSet[str] = frozenset({
    "workflow_patterns",          # canonical (snake_case)
    "Workflow Patterns",          # legacy form emitted by the evolution_engine default
    "user_preference",
    "naming_convention",
    "output_path",
    "recurring_variable",
    "cross_shadow_pattern",
})


# ─────────────────────────────────────────────────────────────────────────────
#  Augment buffer entry — pure helper shared across vault implementations
# ─────────────────────────────────────────────────────────────────────────────

from datetime import datetime, timezone
from typing import Any, Dict


def augment_buffer_entry(
    entry: Dict[str, Any],
    *,
    tier: str,
    source: str,
    allowed: FrozenSet[str],
    forbidden: FrozenSet[str],
    strict: bool,
) -> Dict[str, Any]:
    """Validate + stamp a buffer entry with tier metadata.

    Pure function (no I/O, no global state) used by both ``Vault`` and
    ``SqliteVault`` so the contract is enforced identically across
    storage backends.  See ``docs/memory-model.md`` for the contract.

    The discriminator field is ``category`` (canonical, matches the
    production refinery + evolution_engine pipelines).  A legacy ``type``
    field is accepted as a deprecated alias so callers can migrate
    gradually — but if both are set to *different* values, the entry is
    rejected because silent precedence would be a footgun.

    Args:
        entry:     The caller's claim dict.  Mutated only via shallow copy.
        tier:      ``"shadow"`` or ``"elder"`` — stamped into ``_tier``.
        source:    Caller identifier — stamped into ``_source``.
        allowed:   Canonical set of categories valid for this tier.
                   Empty means "open-ended" (Elder); validation is skipped.
        forbidden: Categories rejected for this tier (= the other tier's
                   canonical set, enforcing the cross-tier wall).
        strict:    When True (Shadow default), unknown categories that
                   aren't in ``allowed`` are rejected.  When False
                   (Elder default, and Shadow opt-out for legacy data),
                   unknown categories pass with a debug log.

    Returns:
        The augmented entry actually persisted.  Carries ``category``,
        ``_tier``, ``_source``, ``_ts`` keys (the deprecated ``type``
        field is dropped if present).

    Raises:
        ValueError: For non-dict input, missing category, conflicting
                    type/category, cross-tier rejection, or
                    unknown-category rejection under strict mode.
    """
    import logging
    logger = logging.getLogger("systemu.core.memory_types")

    if not isinstance(entry, dict):
        raise ValueError(
            f"buffer entry must be a dict, got {type(entry).__name__}"
        )

    # Resolve the discriminator.  `category` is canonical; `type` is the
    # deprecated alias.  Conflicting values are rejected — silent
    # precedence would be a footgun.
    type_val     = entry.get("type")
    category_val = entry.get("category")
    if type_val and category_val and type_val != category_val:
        raise ValueError(
            f"buffer entry has conflicting 'type'={type_val!r} and "
            f"'category'={category_val!r}.  Use 'category' (canonical) "
            f"and drop 'type', or set them to the same value."
        )
    claim_category = category_val or type_val
    if not claim_category:
        raise ValueError(
            "buffer entry missing 'category' field "
            "(required for tier discrimination; see docs/memory-model.md)"
        )

    if claim_category in forbidden:
        raise ValueError(
            f"category {claim_category!r} belongs to the opposite tier "
            f"and cannot be written to the {tier} buffer "
            f"(write-contract Rule 1; see docs/memory-model.md)"
        )

    if strict and allowed and claim_category not in allowed:
        raise ValueError(
            f"unrecognised {tier}-tier category {claim_category!r}.  "
            f"Pick one of: {sorted(allowed)}.  To replay pre-audit data "
            f"with ad-hoc categories, construct the vault with "
            f"strict_tier_types=False."
        )
    if not strict and allowed and claim_category not in allowed:
        logger.debug(
            "[memory_types] %s-tier write: unrecognised category %r — "
            "accepting (strict_tier_types=False)", tier, claim_category,
        )

    # Stamp on a shallow copy so the caller's dict isn't mutated.  Drop
    # the deprecated `type` alias so downstream consumers see only
    # `category`.
    augmented = dict(entry)
    augmented["category"] = claim_category
    augmented.pop("type", None)
    augmented["_tier"]    = tier
    augmented["_source"]  = source
    augmented.setdefault("_ts", datetime.now(timezone.utc).isoformat())
    return augmented


# ─────────────────────────────────────────────────────────────────────────────
#  Pattern signature (v0.4.0-a) — cross-shadow promotion key
# ─────────────────────────────────────────────────────────────────────────────
# The signature is a deterministic string used to detect that ≥N shadows
# independently observed the SAME failure mode, so the refinery (v0.4.0-e)
# can promote a consolidated lesson into ``global_memory.md``.
#
# Format: "<error_type>|<tool_name>|<top_keyword>" with lowercase / strip /
# fall-backs to "unknown".  No LLM in the signature path — it must be
# deterministic so two processes computing the signature for the same
# observed failure always agree.  Operators can also query the failure
# index by exact signature without semantic similarity machinery.

import re as _re

_KEYWORD_STOPWORDS: FrozenSet[str] = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "to", "of", "in",
    "on", "at", "for", "and", "or", "but", "not", "no", "this",
    "that", "with", "by", "from", "be", "been", "it",
})


def pattern_signature(
    *,
    error_type: str | None,
    tool_name:  str | None,
    error_message: str | None = None,
    top_keyword:   str | None = None,
) -> str:
    """Build the canonical signature used for cross-shadow pattern matching.

    Args:
        error_type:     The classifier output (e.g. ``"missing_dependency"``).
        tool_name:      Tool that failed, if applicable.
        error_message:  Free-form error text — if ``top_keyword`` is not
                        supplied, the first non-stopword token from this
                        message is extracted as the keyword.
        top_keyword:    Explicit keyword override (takes priority).

    Returns:
        Lowercase pipe-separated string ``"<err>|<tool>|<keyword>"`` where
        missing parts are replaced by ``"unknown"``.  Length-capped to 200
        chars to keep index lookups bounded.
    """
    err = (error_type or "unknown").strip().lower() or "unknown"
    tool = (tool_name or "unknown").strip().lower() or "unknown"
    kw   = (top_keyword or "").strip().lower()
    if not kw and error_message:
        kw = _extract_first_keyword(error_message)
    if not kw:
        kw = "unknown"
    sig = f"{err}|{tool}|{kw}"
    return sig[:200]


def _extract_first_keyword(text: str) -> str:
    """Return the first non-stopword alphanumeric token of *text*, lowered.

    Used when callers don't pass an explicit keyword.  Conservative — only
    looks at the leading 200 chars to bound work.
    """
    for raw in _re.findall(r"[a-zA-Z][a-zA-Z0-9_\-]{2,}", (text or "")[:200]):
        tok = raw.lower()
        if tok in _KEYWORD_STOPWORDS:
            continue
        return tok
    return ""


__all__ = [
    "SHADOW_CLAIM_TYPES",
    "ELDER_RECOMMENDED_TYPES",
    "augment_buffer_entry",
    "pattern_signature",
]
