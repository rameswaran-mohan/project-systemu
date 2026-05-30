"""Recovery engine: inspect vault state, emit ordered RecoveryAction list."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Literal, Optional, List

from .classifier import classify_dry_run_error
from .links import recover_url

ActionKind = Literal[
    "DEP_PENDING", "GATE_1_PENDING", "GATE_2_PENDING", "GATE_3_DISABLED",
    "MEMORY_POISONED", "SKILL_MISSING", "FS_PERMISSION", "DRY_RUN_FAILED_BUG",
]
ScopeKind = Literal["tool", "shadow", "scroll", "activity"]
Severity = Literal["blocker", "warning", "info"]


@dataclass(frozen=True)
class RecoveryAction:
    scope_kind: ScopeKind
    scope_id: str
    kind: ActionKind
    reason: str
    fix_url: str
    fix_command: Optional[str]
    severity: Severity


class RecoveryEngine:
    def __init__(self, vault):
        self.vault = vault

    def diagnose_tool(self, tool_id: str) -> List[RecoveryAction]:
        tool = self.vault.find_tool(tool_id)
        if tool is None:
            return []

        actions: List[RecoveryAction] = []

        if tool.status == "proposed":
            actions.append(self._make(
                "tool", tool_id, "GATE_1_PENDING",
                f"Tool {tool.name} awaits Gate 1 (Spec Review).",
                f"sharing_on tools review {tool_id}",
                "blocker",
            ))
        elif tool.status == "forged":
            actions.append(self._make(
                "tool", tool_id, "GATE_2_PENDING",
                f"Tool {tool.name} awaits Gate 2 (Code Review).",
                f"sharing_on tools review {tool_id}",
                "blocker",
            ))

        if (tool.dry_run_status or "") == "failed":
            err_text = ""
            if tool.dry_run_evidence and isinstance(tool.dry_run_evidence, dict):
                err_text = tool.dry_run_evidence.get("error", "")
            classified = classify_dry_run_error(
                err_text,
                missing_packages=(tool.dry_run_evidence or {}).get("missing_packages")
                if isinstance(tool.dry_run_evidence, dict) else None,
            )
            if classified.kind == "DEP_PENDING":
                pkg = classified.missing_package or (
                    (tool.dependencies or [None])[0] if getattr(tool, "dependencies", None) else None
                ) or "a required package (see tool manifest)"
                actions.append(self._make(
                    "tool", tool_id, "DEP_PENDING",
                    f"Tool {tool.name} missing package: {pkg}",
                    f"sharing_on tools install-deps {tool_id}",
                    "blocker",
                ))
            elif classified.kind == "FS_PERMISSION":
                actions.append(self._make(
                    "tool", tool_id, "FS_PERMISSION",
                    f"Tool {tool.name} dry-run hit a filesystem permission error: {err_text[:120]}",
                    None,
                    "blocker",
                ))
            else:
                actions.append(self._make(
                    "tool", tool_id, "DRY_RUN_FAILED_BUG",
                    f"Tool {tool.name} dry-run failed unexpectedly: {err_text[:200]}",
                    None,
                    "blocker",
                ))

        if not tool.enabled and tool.status == "approved":
            actions.append(self._make(
                "tool", tool_id, "GATE_3_DISABLED",
                f"Tool {tool.name} is disabled (Gate 3). Enable to use.",
                f"sharing_on tools enable {tool_id}",
                "blocker",
            ))

        return actions

    MEMORY_POISON_THRESHOLD = 3

    def diagnose_shadow(self, shadow_id: str) -> List[RecoveryAction]:
        shadow = self.vault.find_shadow(shadow_id)
        if shadow is None:
            return []

        actions: List[RecoveryAction] = []

        log = shadow.execution_log or []
        failure_counts: dict[str, int] = {}
        for entry in log:
            if isinstance(entry, dict) and entry.get("status") == "failed":
                key = entry.get("tool") or entry.get("kind") or "<unknown>"
                failure_counts[key] = failure_counts.get(key, 0) + 1
        poisoned = [k for k, c in failure_counts.items()
                    if c >= self.MEMORY_POISON_THRESHOLD]
        if poisoned:
            actions.append(self._make(
                "shadow", shadow_id, "MEMORY_POISONED",
                f"execution_log has {sum(failure_counts.values())} failure entries "
                f"(tools: {', '.join(sorted(poisoned))}). May bias LLM against retry.",
                f"sharing_on shadows reset-memory {shadow_id} --keep-successes",
                "warning",
            ))

        for sid in shadow.skill_ids or []:
            if not self.vault.skill_exists(sid):
                actions.append(self._make(
                    "shadow", shadow_id, "SKILL_MISSING",
                    f"Shadow references unknown skill {sid}.",
                    None,
                    "blocker",
                ))

        for tid in shadow.available_tool_ids or []:
            actions.extend(self.diagnose_tool(tid))

        return self._dedupe(actions)

    def diagnose_activity(self, activity_id: str) -> List[RecoveryAction]:
        activity = self.vault.find_activity(activity_id)
        if activity is None:
            return []
        actions: List[RecoveryAction] = []
        for tid in activity.required_tool_ids or []:
            actions.extend(self.diagnose_tool(tid))
        if activity.assigned_shadow_id:
            actions.extend(self.diagnose_shadow(activity.assigned_shadow_id))
        for sid in activity.required_skill_ids or []:
            if not self.vault.skill_exists(sid):
                actions.append(self._make(
                    "activity", activity_id, "SKILL_MISSING",
                    f"Activity requires unknown skill {sid}.",
                    None,
                    "blocker",
                ))
        return self._dedupe(actions)

    def diagnose_scroll(self, scroll_id: str) -> List[RecoveryAction]:
        scroll = self.vault.find_scroll(scroll_id)
        if scroll is None:
            return []
        activity = self.vault.find_activity_for_scroll(scroll_id)
        if activity is None:
            return []
        return self.diagnose_activity(activity.id)

    @staticmethod
    def _dedupe(actions: List[RecoveryAction]) -> List[RecoveryAction]:
        seen = set()
        out = []
        for a in actions:
            key = (a.scope_kind, a.scope_id, a.kind)
            if key in seen:
                continue
            seen.add(key)
            out.append(a)
        return out

    @staticmethod
    def _make(scope, scope_id, kind, reason, fix_command, severity) -> RecoveryAction:
        return RecoveryAction(
            scope_kind=scope, scope_id=scope_id, kind=kind, reason=reason,
            fix_url=recover_url(scope, scope_id), fix_command=fix_command, severity=severity,
        )
