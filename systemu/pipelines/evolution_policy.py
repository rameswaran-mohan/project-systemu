"""evolution_policy.py — Field classification and workshop edit records.

Defines which fields of each artifact type are CONTRACT (immutable without
a new version), BEHAVIOR (editable, may need smoke validation), or METADATA
(freely editable).  Provides three public helpers:

  classify_edit(artifact_type, changed_fields) -> "contract" | "behavior" | "metadata"
  active_shadow_lock(shadow_id, vault)          -> raises RuntimeError if ACTIVE
  record_workshop_edit(...)                     -> saves Evolution + returns it
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Dict, List, Set

if TYPE_CHECKING:
    from systemu.core.models import Evolution
    from systemu.vault.vault import Vault

# ─────────────────────────────────────────────────────────────────────────────
#  Field classification tables
# ─────────────────────────────────────────────────────────────────────────────

# Tool: contract fields define the public interface — changing them breaks callers.
TOOL_CONTRACT_FIELDS:  Set[str] = {"tool_type", "parameters_schema", "return_schema"}
TOOL_BEHAVIOR_FIELDS:  Set[str] = {"description", "implementation_notes", "dependencies"}
TOOL_METADATA_FIELDS:  Set[str] = {"name"}

# Skill: purely declarative knowledge — no contract surface.
SKILL_CONTRACT_FIELDS:  Set[str] = set()
SKILL_BEHAVIOR_FIELDS:  Set[str] = {"instructions_md", "required_tool_ids", "required_tool_names", "category"}
SKILL_METADATA_FIELDS:  Set[str] = {"name", "description", "proficiency_level"}

# Shadow: no hard contract, but system_prompt governs execution behaviour.
SHADOW_CONTRACT_FIELDS:  Set[str] = set()
SHADOW_BEHAVIOR_FIELDS:  Set[str] = {"system_prompt", "available_tool_ids", "skill_ids"}
SHADOW_METADATA_FIELDS:  Set[str] = {"name", "description"}

_CLASSIFICATION_MAP: Dict[str, Dict[str, Set[str]]] = {
    "tool": {
        "contract": TOOL_CONTRACT_FIELDS,
        "behavior": TOOL_BEHAVIOR_FIELDS,
        "metadata": TOOL_METADATA_FIELDS,
    },
    "skill": {
        "contract": SKILL_CONTRACT_FIELDS,
        "behavior": SKILL_BEHAVIOR_FIELDS,
        "metadata": SKILL_METADATA_FIELDS,
    },
    "shadow": {
        "contract": SHADOW_CONTRACT_FIELDS,
        "behavior": SHADOW_BEHAVIOR_FIELDS,
        "metadata": SHADOW_METADATA_FIELDS,
    },
}


# ─────────────────────────────────────────────────────────────────────────────
#  Public helpers
# ─────────────────────────────────────────────────────────────────────────────

def classify_edit(artifact_type: str, changed_fields: Set[str]) -> str:
    """Return the most restrictive classification for the changed fields.

    Returns one of: "contract" | "behavior" | "metadata".
    Raises ValueError for unknown artifact_type.
    """
    if artifact_type not in _CLASSIFICATION_MAP:
        raise ValueError(f"Unknown artifact type: {artifact_type!r}")

    buckets = _CLASSIFICATION_MAP[artifact_type]
    if changed_fields & buckets["contract"]:
        return "contract"
    if changed_fields & buckets["behavior"]:
        return "behavior"
    return "metadata"


def active_shadow_lock(shadow_id: str, vault: "Vault") -> None:
    """Raise RuntimeError if the shadow is currently ACTIVE (mid-execution).

    Call this before applying any edit to a shadow record.
    """
    from systemu.core.models import ShadowStatus
    try:
        shadow = vault.get_shadow(shadow_id)
    except KeyError:
        raise RuntimeError(f"Shadow not found: {shadow_id}")
    if shadow.status == ShadowStatus.ACTIVE:
        raise RuntimeError(
            f"Shadow '{shadow.name}' is currently ACTIVE and cannot be edited. "
            "Wait for the current execution to complete before making changes."
        )


def record_workshop_edit(
    artifact_type: str,
    artifact_id: str,
    fields_changed: List[str],
    previous_values: Dict,
    new_values: Dict,
    vault: "Vault",
) -> "Evolution":
    """Create and persist an APPLIED Evolution record for a workshop edit.

    This is the single call-site for audit trail creation — always call it
    after the entity save succeeds so the record reflects a real change.
    """
    from systemu.core.models import Evolution, EvolutionStatus, EvolutionType

    classification = classify_edit(artifact_type, set(fields_changed))

    evo = Evolution(
        id=uuid.uuid4().hex[:12],
        evolution_type=EvolutionType.UPGRADE,
        target_entity_type=artifact_type,
        target_entity_ids=[artifact_id],
        description=(
            f"Workshop edit — {artifact_type} [{artifact_id}]: "
            f"{', '.join(fields_changed)}"
        ),
        rationale=f"User-initiated workshop edit via UI. Classification: {classification}.",
        before_snapshot=previous_values,
        after_snapshot=new_values,
        status=EvolutionStatus.APPLIED,
        edit_classification=classification,
        fields_changed=list(fields_changed),
        reverted=False,
    )
    vault.save_evolution(evo)
    return evo
