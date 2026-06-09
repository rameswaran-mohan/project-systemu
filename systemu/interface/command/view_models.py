"""One view-model per entity (spec §4.1) — both the Rich table and the NiceGUI
card render from the same object. Phase 2 ships ToolViewModel + the protocol;
the other six entities (scroll/activity/shadow/skill/evolution/decision) follow
this exact shape in later tasks.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Protocol, runtime_checkable


@runtime_checkable
class EntityViewModel(Protocol):
    def to_row(self) -> List[str]: ...
    def to_card(self) -> Dict[str, Any]: ...


@dataclass
class ToolViewModel:
    id: str
    name: str
    tool_type: str
    status: str
    description: str
    enabled: bool
    dry_run_status: str

    @classmethod
    def from_header(cls, h: Dict[str, Any]) -> "ToolViewModel":
        return cls(
            id=h.get("id", ""), name=h.get("name", ""),
            tool_type=h.get("tool_type", "—"), status=h.get("status", ""),
            description=h.get("description", "") or "",
            enabled=bool(h.get("enabled", False)),
            dry_run_status=h.get("dry_run_status", "not_run"),
        )

    def to_row(self) -> List[str]:
        return [self.id, self.name, self.tool_type, self.status,
                self.description or "—"]

    def to_card(self) -> Dict[str, Any]:
        return {"id": self.id, "name": self.name, "type": self.tool_type,
                "status": self.status, "description": self.description or "—",
                "enabled": self.enabled, "dry_run_status": self.dry_run_status}
