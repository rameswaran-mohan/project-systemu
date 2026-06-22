"""Structured command result — the one envelope every shared-layer verb returns.

Renders three ways (spec §4.1):
  • Rich        — for humans at the console
  • exit-code   — for CI / shell callers
  • JSON        — for the dashboard / machine consumers
Plus a stream_ref so the dashboard rail can follow a specific streamed run.
"""
from __future__ import annotations

import json
from enum import Enum
from typing import Any, Dict

from pydantic import BaseModel, Field
from rich.panel import Panel


class CommandStatus(str, Enum):
    OK     = "ok"        # exit 0
    ERROR  = "error"     # exit 1
    QUEUED = "queued"    # exit 75 (EX_TEMPFAIL) — "queued for operator review"
    NOOP   = "noop"      # exit 0 — nothing to do (idempotent)


_EXIT_BY_STATUS = {
    CommandStatus.OK:     0,
    CommandStatus.NOOP:   0,
    CommandStatus.ERROR:  1,
    CommandStatus.QUEUED: 75,
}

_STYLE_BY_STATUS = {
    CommandStatus.OK:     "green",
    CommandStatus.NOOP:   "dim",
    CommandStatus.ERROR:  "red",
    CommandStatus.QUEUED: "yellow",
}


class CommandResult(BaseModel):
    """One result object for every command, rendered idiomatically per surface."""
    status:     CommandStatus
    summary:    str = ""
    data:       Dict[str, Any] = Field(default_factory=dict)
    stream_ref: str = ""          # links a streamed dashboard run to its events
    model_config = {"extra": "forbid"}

    @property
    def exit_code(self) -> int:
        return _EXIT_BY_STATUS[self.status]

    def to_json(self) -> str:
        return json.dumps(
            {"status": self.status.value, "exit_code": self.exit_code,
             "summary": self.summary, "data": self.data, "stream_ref": self.stream_ref},
            default=str,
        )

    def to_rich(self) -> Panel:
        return Panel(self.summary or self.status.value,
                     border_style=_STYLE_BY_STATUS[self.status])

    @classmethod
    def from_pending_decision(cls, pd) -> "CommandResult":
        """Map PendingOperatorDecision → the clean exit-75 'queued' signal."""
        return cls(
            status=CommandStatus.QUEUED,
            summary=f"Queued for operator review (decision {pd.decision_id}).",
            data={"decision_id": pd.decision_id, "dedup_key": pd.dedup_key,
                  "options": list(getattr(pd, "options", []))},
        )
