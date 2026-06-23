"""CGBTask dataclass + the OracleResult value type.

A CGBTask is one benchmark task with a *deliberately withheld* capability.  The
push condition has no recourse for the gap; the pull condition can
``REQUEST_HARNESS``.  An identical Scroll/Activity is used across conditions so
the comparison is a controlled A/B.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Tuple

FAMILIES = ("TOOL", "SKILL", "ACCESS", "COMPUTE", "SUBAGENT", "MCP")


@dataclass(frozen=True)
class OracleResult:
    """The external ground-truth verdict for one trial."""

    passed: bool
    details: str


@dataclass(frozen=True)
class CGBTask:
    """One benchmark task with a deliberately withheld capability.

    The goal string may contain ``{workspace}``; ``format_goal`` substitutes the
    per-trial workspace directory so tools receive absolute paths.
    """

    task_id: str
    family: str
    goal: str
    success_criteria: str
    provided_tools: Tuple[str, ...]          # starter-tool module names copied into the seed vault
    withheld: str                            # human description of the injected gap (for the paper)
    setup: Callable[[Path], None]            # writes input artifacts into the workspace
    # EXTERNAL ground truth — never the goal-verifier. Called as oracle(workspace)
    # for artifact graders, or oracle(workspace, vault) for graders that must read
    # the harness ledger / decision queue (ACCESS). The runner dispatches by arity.
    oracle: Callable[..., OracleResult]
    iteration_cap: int = 30                  # patched onto MAX_ITERATIONS for COMPUTE-gap tasks

    def __post_init__(self) -> None:
        if self.family not in FAMILIES:
            raise ValueError(f"family must be one of {FAMILIES}, got {self.family!r}")

    def format_goal(self, workspace: Path) -> str:
        return self.goal.replace("{workspace}", str(workspace).replace("\\", "/"))
