# systemu/runtime/reference_resolver.py
"""R-A11a §5.4 — resolve a file/artifact reference to a concrete granted-root path.

PURE + deterministic + fail-safe: reads only the situation dict's pre-surveyed salient
handles (name/ext/mtime already captured by situational_inventory.build_roots) and the
GrantedRoots store. NO disk walk, NO stat, NO new durable writer. Any exception degrades
to a 'missing' verdict — the resolver can only WIDEN an ask, never drop a requirement."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Optional

from systemu.runtime.reference_synonyms import synonym_exts

logger = logging.getLogger(__name__)

try:
    from systemu.runtime.requirement_binder import T_HIGH as _T_HIGH
except Exception:                                   # pragma: no cover - import cycle guard
    _T_HIGH = 0.80

_AMBIGUITY_EPS = 0.05
_STOP = frozenset({"the", "my", "a", "an", "this", "that", "please", "open", "update",
                   "send", "file", "from", "to", "of", "in", "on", "for", "with", "and"})


@dataclass(frozen=True)
class ReferenceVerdict:
    state: str                 # "resolvable" | "missing"
    referent: Optional[str]    # canonical path of the best candidate, or None
    confidence: float
    candidate_count: int
    why: str


def _tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", (text or "").lower()) if t and t not in _STOP}


def _recency(mtime: float, *, now: float, span: float = 30 * 24 * 3600.0) -> float:
    """1.0 for just-now, decaying linearly to 0.0 at `span` old (30d). Best-effort."""
    try:
        age = max(0.0, now - float(mtime))
        return max(0.0, 1.0 - age / span)
    except Exception:
        return 0.0


def _score(fh: dict, ref_tokens: set[str], want_exts: frozenset[str], *, now: float) -> float:
    name = str(fh.get("name") or "")
    ext = str(fh.get("ext") or "").lower()
    # strip the extension off the name before tokenizing — the ext is scored
    # separately (ext_match), and situational_inventory sets name=basename WITH the
    # ext, so leaving it in leaks a token that dilutes the name-token overlap.
    stem = name[: -len(ext)] if (ext and name.lower().endswith(ext)) else name
    name_tokens = _tokens(stem)
    matched = name_tokens & ref_tokens
    # containment overlap (intersection over the smaller token set): a reference that
    # carries extra verbs ("summarize my resume") must NOT dilute a strong filename
    # match — a name fully covered by the reference (or vice-versa) scores 1.0.
    denom = min(len(name_tokens), len(ref_tokens))
    overlap = (len(matched) / denom) if denom else 0.0
    ext_match = 1.0 if (want_exts and ext in want_exts) else 0.0
    # relevance gate: recency ALONE never qualifies an unrelated recent file — a
    # candidate must carry a real reference signal (a matched name token OR the
    # synonym-ext hint), so a genuine no-match yields ZERO candidates → 'missing'.
    if not matched and not ext_match:
        return 0.0
    rec = _recency(fh.get("mtime", 0.0), now=now)
    return 0.6 * overlap + 0.15 * ext_match + 0.25 * rec


def resolve_reference(text: str, *, situation: dict, granted: Any,
                      key: Optional[str] = None) -> ReferenceVerdict:
    import time
    try:
        ref_tokens = _tokens(text) | _tokens(key or "")
        want_exts: frozenset[str] = frozenset()
        for tok in list(ref_tokens):
            want_exts |= synonym_exts(tok)
        if not ref_tokens and not want_exts:
            return ReferenceVerdict("missing", None, 0.0, 0, "no reference tokens")

        roots = situation.get("roots") if isinstance(situation, dict) else None
        if not isinstance(roots, list):
            return ReferenceVerdict("missing", None, 0.0, 0, "no roots")

        now = time.time()
        scored: list[tuple[float, str]] = []
        for root in roots:
            salient = (root or {}).get("salient") if isinstance(root, dict) else None
            if not isinstance(salient, list):
                continue
            for fh in salient:
                if not isinstance(fh, dict):
                    continue
                path = fh.get("path")
                if not path or not isinstance(path, str):
                    continue
                # confinement re-gate (defense-in-depth; a revoked/moved root drops it)
                if granted is not None:
                    try:
                        if not granted.is_within_granted(path):
                            continue
                    except Exception:
                        continue
                s = _score(fh, ref_tokens, want_exts, now=now)
                if s > 0.0:
                    scored.append((s, path))

        if not scored:
            return ReferenceVerdict("missing", None, 0.0, 0, "no scored candidate")

        scored.sort(key=lambda t: t[0], reverse=True)
        top_score, top_path = scored[0]
        ambiguous = len(scored) >= 2 and (scored[0][0] - scored[1][0]) < _AMBIGUITY_EPS
        # Ambiguity or a genuinely weak single can NEVER auto-accept (AC b): cap just
        # below T_high so the state gate forces a 'resolvable' ask, not a 'have'.
        conf = min(top_score, _T_HIGH - 0.01) if ambiguous else top_score
        why = (f"{len(scored)} candidate(s); top '{top_path}' @ {top_score:.2f}"
               + (" (ambiguous — capped)" if ambiguous else ""))
        return ReferenceVerdict("resolvable", top_path, float(conf), len(scored), why)
    except Exception:
        logger.debug("[resolver] resolve_reference degraded to missing", exc_info=True)
        return ReferenceVerdict("missing", None, 0.0, 0, "resolver error (fail-safe)")
