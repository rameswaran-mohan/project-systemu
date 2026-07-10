"""Decision-time tool retrieval for the systemu agent executor.

Provides keyword/BM25-style ranking of the generic tool catalog at execution
time, so the ReAct loop can surface the most relevant tools for the current
goal/step without relying solely on the extractor's pre-selected tool IDs.

No third-party dependencies — pure stdlib.

API
---
    rank_tools(query, tools, k=8) -> list[dict]
        Score and return the top-k tools from *tools* for the given *query*.

    ensure_core(ranked, tools) -> list[dict]
        Union ranked results with always-available core tools present in *tools*.

ALWAYS_AVAILABLE
----------------
    frozenset of tool *names* that are always included regardless of relevance
    score (e.g. fetch_json, web_search, file_read, file_write, run_command).

Scoring approach
----------------
Each tool's "searchable text" is assembled from three tiers, each with a
weight multiplier applied to token-hit counts:

    * Name tokens (snake_case split, e.g. fetch_json → ["fetch", "json"]): ×4
    * Description — first half (primary function description):              ×2
    * Description — second half (caveats / cross-references / "do NOT"):   ×1
    * Parameter-name tokens (each param name snake_case split):             ×1

Splitting the description at its midpoint and down-weighting the second half
is the key disambiguation heuristic: tools that list an activity as their
*primary purpose* mention it early ("USE THIS — to resolve IP geolocation…"),
whereas tools that warn *against* being used for something mention it later
("Do NOT use for IP→location lookups").  This lets ``fetch_json`` reliably
outrank ``web_search`` for IP/location queries even though both descriptions
mention those terms.

Token counting is frequency-based (Counter) so repeated terms get full credit.
Scoring is deterministic; ties are broken by original list order (first wins).
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Iterable

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ALWAYS_AVAILABLE: frozenset[str] = frozenset({
    "fetch_json",
    "web_search",
    "file_read",
    "file_write",
    "run_command",
})

# Tier weights
_W_NAME = 4          # name token match (snake_case split)
_W_DESC_FIRST = 2    # description token match — first half (primary purpose)
_W_DESC_SECOND = 1   # description token match — second half (caveats/negations)
_W_PARAM = 1         # parameter-name token match

# Split on any non-alphanumeric character (handles snake_case, camelCase,
# hyphens, spaces, dots, slashes, etc.)
_SPLIT_RE = re.compile(r"[^a-z0-9]+")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> list[str]:
    """Lowercase and split *text* into non-empty alpha-numeric tokens."""
    return [t for t in _SPLIT_RE.split(text.lower()) if t]


def _score(query_tokens: list[str], tool: dict) -> float:
    """Compute a weighted positional token-frequency score for *tool*.

    Parameters
    ----------
    query_tokens:
        Pre-tokenized query (output of :func:`_tokenize`).
    tool:
        Tool dict with at least a ``name`` field.  ``description`` and
        ``parameter_names`` are used when present.

    Returns
    -------
    float
        Non-negative score.  Higher is more relevant.
    """
    if not query_tokens:
        return 0.0

    name_toks = _tokenize(tool.get("name") or "")
    desc_toks = _tokenize(tool.get("description") or "")
    param_toks = _tokenize(" ".join(tool.get("parameter_names") or []))

    name_bag: set[str] = set(name_toks)

    # Split description at midpoint: first half = primary purpose,
    # second half = caveats / cross-references
    mid = max(1, len(desc_toks) // 2)
    desc_first = Counter(desc_toks[:mid])
    desc_second = Counter(desc_toks[mid:])

    param_cnt = Counter(param_toks)

    score = 0.0
    for qt in query_tokens:
        if qt in name_bag:
            score += _W_NAME
        score += desc_first[qt] * _W_DESC_FIRST
        score += desc_second[qt] * _W_DESC_SECOND
        score += param_cnt[qt] * _W_PARAM

    return score


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def rank_tools(query: str, tools: list[dict], k: int = 8) -> list[dict]:
    """Rank *tools* by relevance to *query* and return the top-*k* results.

    Parameters
    ----------
    query:
        Free-text goal or step description (e.g. "find my location from IP").
    tools:
        List of tool dicts.  Each must have at least a ``name`` field;
        ``description`` and ``parameter_names`` are used when present.
    k:
        Maximum number of tools to return.

    Returns
    -------
    list[dict]
        Up to *k* tool dicts ordered highest-score first.  Ties are broken by
        original list position (earlier index wins).
    """
    if not tools:
        return []

    query_tokens = _tokenize(query)

    # Pair each tool with its score, preserving original index for tie-breaking
    scored = [(_score(query_tokens, tool), i, tool) for i, tool in enumerate(tools)]

    # Sort descending by score; ties fall back to ascending original index
    scored.sort(key=lambda x: (-x[0], x[1]))

    return [tool for _, _, tool in scored[:k]]


def rank_tools_scored(query: str, tools: list[dict], k: int = 8) -> list[tuple[float, dict]]:
    """Like :func:`rank_tools` but return ``(score, tool)`` pairs, highest score
    first (ties broken by original index). Exposes the score so callers can apply
    a confidence floor (R-A11b-2 discovery-before-forge auto-reuse). Deterministic;
    reuses the same ``_score``/``_tokenize`` as ``rank_tools``."""
    if not tools:
        return []
    query_tokens = _tokenize(query)
    scored = [(_score(query_tokens, tool), i, tool) for i, tool in enumerate(tools)]
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [(float(s), tool) for s, _, tool in scored[:k]]


def ensure_core(ranked: list[dict], tools: Iterable[dict]) -> list[dict]:
    """Union *ranked* with always-available core tools present in *tools*.

    Any tool in *tools* whose ``name`` is in :data:`ALWAYS_AVAILABLE` is
    included in the result even if it did not make the top-k cut.  Duplicates
    are removed (ranked result order preserved first, then extra core tools
    appended in their original catalog order).

    Parameters
    ----------
    ranked:
        The output of :func:`rank_tools`.
    tools:
        The full catalog to look up core tools from (only names in
        :data:`ALWAYS_AVAILABLE` are considered).

    Returns
    -------
    list[dict]
        Merged list — ranked tools first, then any core tools not already
        present, in catalog order.
    """
    seen: set[str] = {t.get("name", "") for t in ranked}
    result = list(ranked)

    for tool in tools:
        name = tool.get("name", "")
        if name in ALWAYS_AVAILABLE and name not in seen:
            result.append(tool)
            seen.add(name)

    return result
