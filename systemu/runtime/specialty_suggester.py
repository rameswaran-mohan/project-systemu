"""Specialty auto-suggest from consolidated shadow memory (v0.4.4-c).

The Workshop UI (v0.4.3-b) lets operators set ``Shadow.specialty`` as a
free-form tag — but most operators won't know which value to pick.  This
module analyses each shadow's consolidated ``SHADOW_MEMORY.md`` and
``memory_buffer.jsonl`` and suggests a specialty based on the most-
referenced domain keywords.

Why not LLM-driven: deterministic keyword analysis is sufficient and
runs without API cost.  A future v0.5+ refinement could promote
suggestions to Tier-3 LLM calls when the keyword analysis is ambiguous,
but the cost / latency / observability story is much better with rules.

Suggestion mechanism:

1. Walk the shadow's ``memory_buffer.jsonl`` + tool-call history (from
   ``execution_log``) and count keyword hits in a small curated
   dictionary mapping keywords → specialty tag.
2. The specialty with the highest hit count wins, provided it crosses
   a minimum-confidence threshold (default: 5 hits AND ≥40% of total
   matched hits).
3. Below threshold → no suggestion (operator continues to set manually).

The output is a :class:`SpecialtySuggestion` carrying:

* ``suggested_specialty``  — the tag, or empty string when no clear winner
* ``confidence``           — fraction of matched hits attributable to the
                             top tag (0.0 to 1.0)
* ``total_hits``           — sum of all keyword matches
* ``by_specialty``         — full hit breakdown for debugging

Used by:

* CLI: ``sharing_on debug suggest-specialty <shadow_id>`` — operator
  inspects the suggestion before applying it via Workshop.
* Future: a periodic job that publishes suggestion approval cards via
  the v0.3.6 supervisor-flash bus (deferred to v0.4.5).

Always returns a result — never raises into the caller.  Empty/missing
memory → ``suggested_specialty=""``.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from systemu.vault.vault import Vault

logger = logging.getLogger(__name__)


# Curated dictionary mapping keywords → specialty.  Each value is the
# specialty tag the suggester will recommend.  Tags follow the kebab-case
# convention from the Workshop UI placeholder text.  Add entries here
# (rather than relying on an LLM) so suggestions are inspectable and
# easy to tune from operator feedback.

_KEYWORD_TO_SPECIALTY: Dict[str, str] = {
    # browser / web automation
    "browser":          "browser",
    "screenshot":       "browser",
    "playwright":       "browser",
    "selenium":         "browser",
    "puppeteer":        "browser",
    "click":            "browser",
    "navigate":         "browser",
    "dom":              "browser",
    "html":             "browser",
    "css":              "browser",

    # data pipeline / analysis
    "csv":              "data-pipeline",
    "parquet":          "data-pipeline",
    "pandas":           "data-pipeline",
    "dataframe":        "data-pipeline",
    "etl":              "data-pipeline",
    "transform":        "data-pipeline",
    "aggregate":        "data-pipeline",
    "sql":              "data-pipeline",

    # devops / infra
    "docker":           "devops",
    "kubernetes":       "devops",
    "kubectl":          "devops",
    "terraform":        "devops",
    "ansible":          "devops",
    "deploy":           "devops",
    "ci":               "devops",
    "github":           "devops",
    "gitlab":           "devops",

    # filesystem / documents
    "word_doc":         "document-generation",
    "docx":             "document-generation",
    "pdf":              "document-generation",
    "pptx":             "document-generation",
    "markdown":         "document-generation",
    "letter":           "document-generation",

    # api / integration
    "api":              "api-integration",
    "rest":             "api-integration",
    "webhook":          "api-integration",
    "json":             "api-integration",
    "graphql":          "api-integration",

    # ml / nlp
    "embedding":        "ml-nlp",
    "vector":           "ml-nlp",
    "tokenize":         "ml-nlp",
    "transformer":      "ml-nlp",
    "huggingface":      "ml-nlp",
    "openai":           "ml-nlp",
}

# Minimum thresholds for a suggestion to be returned.
_MIN_HITS_FOR_SUGGESTION = 5
_MIN_CONFIDENCE          = 0.40   # winner must have ≥40% of matched hits


@dataclass(frozen=True)
class SpecialtySuggestion:
    suggested_specialty: str
    confidence:          float
    total_hits:          int
    by_specialty:        Dict[str, int] = field(default_factory=dict)
    sources_scanned:     int = 0


def suggest_specialty(
    shadow_id: str,
    *,
    vault: "Vault",
) -> SpecialtySuggestion:
    """Analyse a shadow's memory and suggest a specialty tag.

    Args:
        shadow_id:  Owner of the memory we'll scan.
        vault:      Used to load SHADOW_MEMORY.md + memory_buffer.jsonl.

    Returns:
        :class:`SpecialtySuggestion`.  ``suggested_specialty`` is the
        empty string when no clear winner emerged.
    """
    try:
        md_text, buffer_entries = vault.load_shadow_memory(shadow_id)
    except Exception:
        logger.debug("[SpecialtySuggester] load_shadow_memory failed", exc_info=True)
        return SpecialtySuggestion(
            suggested_specialty="", confidence=0.0, total_hits=0,
            by_specialty={}, sources_scanned=0,
        )

    text_chunks: List[str] = []
    sources = 0
    if md_text:
        text_chunks.append(md_text)
        sources += 1
    for entry in buffer_entries or []:
        if not isinstance(entry, dict):
            continue
        lesson = entry.get("lesson") or ""
        if lesson:
            text_chunks.append(str(lesson))
            sources += 1
        # Tool name in evidence — strong signal for tool-related specialties.
        for ab in (entry.get("evidence_action_blocks") or []):
            text_chunks.append(str(ab))

    if not text_chunks:
        return SpecialtySuggestion(
            suggested_specialty="", confidence=0.0, total_hits=0,
            by_specialty={}, sources_scanned=0,
        )

    combined = " ".join(text_chunks).lower()
    hits: Dict[str, int] = {}
    for keyword, specialty in _KEYWORD_TO_SPECIALTY.items():
        # Count word-bounded occurrences only — avoids matching "doc" inside "docker".
        n = len(re.findall(rf"\b{re.escape(keyword)}\b", combined))
        if n > 0:
            hits[specialty] = hits.get(specialty, 0) + n

    total = sum(hits.values())
    if not hits or total == 0:
        return SpecialtySuggestion(
            suggested_specialty="", confidence=0.0, total_hits=0,
            by_specialty={}, sources_scanned=sources,
        )

    # Pick the specialty with the highest count; break ties by lexical sort
    # so two operators running the analyser get the same answer.
    top_specialty, top_count = max(
        sorted(hits.items()), key=lambda kv: (kv[1], kv[0]),
    )
    # Re-find the winning specialty with the actual maximum (max returns
    # a stable element when multiple share the max, but we want
    # deterministic by count desc + name asc).
    sorted_hits = sorted(hits.items(), key=lambda kv: (-kv[1], kv[0]))
    top_specialty, top_count = sorted_hits[0]
    confidence = top_count / total

    if top_count < _MIN_HITS_FOR_SUGGESTION or confidence < _MIN_CONFIDENCE:
        # Not confident enough to recommend.
        return SpecialtySuggestion(
            suggested_specialty="", confidence=confidence,
            total_hits=total, by_specialty=hits, sources_scanned=sources,
        )

    return SpecialtySuggestion(
        suggested_specialty=top_specialty,
        confidence=confidence,
        total_hits=total,
        by_specialty=hits,
        sources_scanned=sources,
    )
