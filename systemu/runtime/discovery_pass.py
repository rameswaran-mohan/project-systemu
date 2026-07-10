"""Discovery-before-forge auto-reuse (R-A11b-2, §5.5 AC1/AC2/AC3).

ONE deterministic ranking pass over the vault DEPLOYED+enabled catalog at the
forge-request seam. A confident match reuses the existing tool; anything weaker
falls through to the honest forge/ESCALATE path. MCP tools are structurally
excluded — they are NOT vault ``Tool`` records, so ``list_tools`` never yields
them (Rider 1). Pure + deterministic: no LLM, no network, no durable writes,
never raises.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from systemu.runtime.tool_retrieval import rank_tools_scored

# Conservative starting floor (design §"confidence floor"). A single matching
# NAME token contributes _W_NAME=4, so 8.0 requires materially more than one
# incidental name-token collision (e.g. two name tokens, or one name token plus
# several description corroborations). Fall-through is the default posture: a
# redundant forge only costs a review card, a WRONG-tool reuse acts immediately.
# Tuned later from the CAP-10 signal (§5.9 / R-A13.5). Exact value is a start.
REUSE_FLOOR: float = 8.0


@dataclass(frozen=True)
class DiscoveryResult:
    reuse_tool_id: Optional[str]     # non-None ONLY on a confident hit
    reuse_tool_name: Optional[str]
    best_score: float
    searched: int                    # number of DEPLOYED+enabled tools ranked
    floor: float


def deployed_enabled_catalog(vault) -> list[dict[str, Any]]:
    """The reuse catalog: one ranking dict per DEPLOYED **and** enabled vault tool.

    Sourced from ``list_tools(status=DEPLOYED)`` headers (which already carry
    name/description/parameter_names — no N+1). ``enabled`` falsy is dropped
    (Gate-3-disabled). ``forge_rejected`` is NOT on the header, so that exclusion
    is enforced at Seam B's re-verify. MCP tools are not vault Tool records →
    never present. Never raises; a broken vault yields ``[]``.
    """
    try:
        from systemu.core.models import ToolStatus
        headers = vault.list_tools(status=ToolStatus.DEPLOYED)
    except Exception:
        return []
    catalog: list[dict[str, Any]] = []
    for h in headers or []:
        try:
            if not isinstance(h, dict) or not h.get("id"):
                continue
            if not h.get("enabled"):
                continue
            catalog.append({
                "id": h.get("id"),
                "name": h.get("name") or "",
                "description": h.get("description") or "",
                "parameter_names": list(h.get("parameter_names") or []),
            })
        except Exception:
            continue
    return catalog


def discovery_pass(
    requested_name: str,
    rationale: str,
    catalog: list[dict[str, Any]],
    floor: float = REUSE_FLOOR,
) -> DiscoveryResult:
    """ONE deterministic ranking pass. ``reuse_tool_id`` is set ONLY when the top
    candidate clears the floor OR is an exact normalized-name match (Tool.name is
    already lowercase snake_case per the model validator, so equality is a strong
    identity signal — the design's "exact/near-exact name" clause). Never raises.
    """
    searched = len(catalog or [])
    if not catalog:
        return DiscoveryResult(None, None, 0.0, 0, floor)
    # ── The reuse TRIGGER is an EXACT normalized-name match ONLY (R-A11b-2). ──
    # A Tool.name is already lowercase snake_case (model validator), so name
    # equality is a strong IDENTITY signal — the safe "this is a duplicate forge
    # of a tool I already have" case. A FUZZY (score-based) match is deliberately
    # NOT an auto-reuse trigger here: (a) the arbiter's is_reuse keys on the
    # REQUESTED name, so a differently-named fuzzy match could never LOW-GRANT
    # consistently anyway; (b) a description-driven fuzzy auto-reuse is the CAP-3
    # keyword-stuffing vector. Fuzzy near-match SURFACING (with operator confirm)
    # is R-CAP1's job. We STILL compute best_score below purely as the audit /
    # near-match signal (AC2 / the CAP-10 avoidable-forge metric) — it never
    # triggers a reuse. Found by a DIRECT catalog lookup (not ranked[0], which an
    # unrelated higher-scoring tool could otherwise mask).
    norm = (requested_name or "").strip().lower()
    exact = None
    if norm:
        for t in catalog:
            if (t.get("name") or "").strip().lower() == norm and t.get("id"):
                exact = t
                break
    # best_score: audit-only near-match signal (NOT a reuse trigger).
    query = f"{requested_name or ''} {rationale or ''}".strip()
    try:
        ranked = rank_tools_scored(query, catalog, k=5)
        best_score = float(ranked[0][0]) if ranked else 0.0
    except Exception:
        best_score = 0.0
    if exact is not None:
        return DiscoveryResult(
            reuse_tool_id=exact.get("id"),
            reuse_tool_name=exact.get("name") or "",
            best_score=best_score,
            searched=searched,
            floor=floor,
        )
    return DiscoveryResult(None, None, best_score, searched, floor)
