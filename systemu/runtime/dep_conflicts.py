"""Cross-tool dependency conflict detection (v0.3.5).

In local mode every tool's pip dependencies install into the same
interpreter's site-packages, so two tools that disagree about the
acceptable version range of a shared package will silently last-write-
wins.  ``find_conflicts()`` walks a list of Tools and returns the
packages whose combined specifier set is unsatisfiable — i.e. there is
no version that all declaring tools would accept.

The detector is deliberately conservative: it flags only conflicts that
the standard library's :class:`packaging.specifiers.SpecifierSet` rejects
for every literal version mentioned across the contributing specifiers.
This catches:

* Direct opposites: ``>=2.0`` vs ``<2.0``
* Pinned-vs-pinned mismatches: ``==1.4`` vs ``==1.5``
* Disjoint ranges: ``>=2.0,<3.0`` vs ``>=4.0,<5.0``

It will not catch exotic disagreements where neither side's literal
versions land in the other's range *but* the union still has no
real-world solution.  Those are rare and the conservative approach
keeps false positives low.

Bare names (no specifier) do not contribute to conflicts — they accept
any version, so they intersect with every range.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ConflictingSpec:
    tool_name: str
    tool_id:   Optional[str]
    spec:      str           # raw specifier string, e.g. ">=2.0,<3.0"


@dataclass(frozen=True)
class DependencyConflict:
    package: str
    specs:   Tuple[ConflictingSpec, ...]
    reason:  str             # human-readable explanation


# Capture "name[extras]<spec>" in a single pass, leaving the spec to be
# split on commas.  Tolerates whitespace around operators and the bare-
# name case where the spec is empty.
_SPEC_SPLIT_RE = re.compile(
    r"""
    ^
    (?P<name>[A-Za-z0-9][A-Za-z0-9_\-\.]*)         # canonical pip name
    (?:\[[A-Za-z0-9_,\-]+\])?                       # ignore extras
    (?P<spec>(?:\s*(?:==|>=|<=|~=|!=|<|>)\s*[A-Za-z0-9_\-\.\*\+]+(?:,)?)*)
    $
    """,
    re.VERBOSE,
)


def _parse_dep(raw: str) -> Optional[Tuple[str, str]]:
    """Split ``"python-docx>=1.0"`` into ``("python-docx", ">=1.0")``.

    Returns ``None`` when the input doesn't match the conservative grammar
    we accept elsewhere.  Caller treats None as "skip this entry"; the
    installer-side validator is the source of truth and will reject
    malformed entries with a clearer error.
    """
    if not isinstance(raw, str):
        return None
    raw = raw.strip()
    if not raw:
        return None
    m = _SPEC_SPLIT_RE.match(raw)
    if not m:
        return None
    return m.group("name").lower(), (m.group("spec") or "").strip()


def find_conflicts(
    tool_records: Iterable[Dict],
) -> List[DependencyConflict]:
    """Return the dependency conflicts in the supplied tools.

    Args:
        tool_records: Iterable of dicts (vault index rows) or Pydantic
                      Tool instances.  Each must yield ``name``, ``id``,
                      and ``dependencies``.  Empty/missing dependencies
                      contribute nothing.

    Returns:
        A list of :class:`DependencyConflict` — one per package with an
        unsatisfiable combined SpecifierSet.  Empty list when the inputs
        all agree.
    """
    # Late import: ``packaging`` is a runtime dep; absence shouldn't
    # crash callers that don't actually need conflict checking.
    try:
        from packaging.specifiers import InvalidSpecifier, SpecifierSet
        from packaging.version import InvalidVersion, Version
    except Exception:
        logger.warning(
            "[DepConflicts] packaging library not available — skipping conflict scan"
        )
        return []

    # package_name -> [(spec_str, tool_name, tool_id), ...]
    per_pkg: Dict[str, List[ConflictingSpec]] = {}
    for rec in tool_records:
        deps = _get_deps(rec)
        tool_name = _get_name(rec)
        tool_id   = _get_id(rec)
        if not deps:
            continue
        for raw in deps:
            parsed = _parse_dep(raw)
            if parsed is None:
                continue
            pkg_name, spec_str = parsed
            per_pkg.setdefault(pkg_name, []).append(
                ConflictingSpec(tool_name=tool_name, tool_id=tool_id, spec=spec_str)
            )

    conflicts: List[DependencyConflict] = []
    for pkg, contribs in per_pkg.items():
        # No constraint clashes with no constraint — drop bare-name entries
        # from the satisfiability check.
        constrained = [c for c in contribs if c.spec]
        if len(constrained) < 2:
            continue
        # Build the combined SpecifierSet (PEP 508 conjunction).
        try:
            combined = SpecifierSet(",".join(c.spec for c in constrained))
        except InvalidSpecifier as exc:
            logger.debug("[DepConflicts] invalid combined specifier for %s: %s", pkg, exc)
            continue
        # Pull every literal version mentioned across the contributing
        # specifiers; if none satisfies the combined set, flag conflict.
        candidates: List[str] = []
        for c in constrained:
            try:
                for part in SpecifierSet(c.spec):
                    candidates.append(part.version)
            except InvalidSpecifier:
                continue
        satisfied = False
        for v_str in candidates:
            try:
                if combined.contains(v_str, prereleases=True):
                    satisfied = True
                    break
            except InvalidVersion:
                continue
        if not satisfied:
            spec_summary = " AND ".join(
                f"{c.tool_name}: '{c.spec}'" for c in constrained
            )
            conflicts.append(DependencyConflict(
                package=pkg,
                specs=tuple(constrained),
                reason=(
                    f"No version of '{pkg}' satisfies the combined constraints — "
                    f"{spec_summary}"
                ),
            ))
    return conflicts


# ─────────────────────────────────────────────────────────────────────────────
# Adapters: accept index dicts OR Pydantic Tool instances

def _get_deps(rec) -> Sequence[str]:
    if isinstance(rec, dict):
        return rec.get("dependencies") or []
    return getattr(rec, "dependencies", None) or []


def _get_name(rec) -> str:
    if isinstance(rec, dict):
        return rec.get("name") or rec.get("id") or "<unknown>"
    return getattr(rec, "name", None) or getattr(rec, "id", None) or "<unknown>"


def _get_id(rec) -> Optional[str]:
    if isinstance(rec, dict):
        return rec.get("id")
    return getattr(rec, "id", None)
