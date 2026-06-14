"""Plan 0 Build 3 (Task 3.4 — paper fleet): concurrent subagent fan-out + collation.

When a parent Shadow decomposes a goal into independent sub-tasks, the
:class:`SubagentFleet` spawns one child :class:`~systemu.runtime.shadow_runtime.ShadowRuntime`
per sub-task — each running on a freshly-built child shadow + activity
(:mod:`systemu.runtime.subagent_harness`). Concurrency is bounded by an
``asyncio.Semaphore`` sized from ``config.delegate_max_concurrent_children`` so a
wide fan-out cannot stampede the LLM/tool backend.

Children are run with ``asyncio.gather(..., return_exceptions=True)`` — a single
child timeout or exception is captured, never propagated, so siblings always run
to completion. The captured outcomes are then **collated**.

The collation embodies one operator decision: **partial failure is NOT total
failure.** :meth:`SubagentFleet.collate` returns an honest, intelligent synthesis
that states what each successful child produced AND explicitly names what failed
and what is therefore missing — never a blanket "everything failed" when some
children succeeded. It also sums each child's cost signals (tool-call counts /
rounds) so the parent retains cost visibility across the fan-out.

No LLM calls live here — the children make those. This module only fans out,
bounds, and collates.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List

from systemu.runtime.subagent_harness import build_child_activity, build_child_shadow

logger = logging.getLogger(__name__)

# Hard ceiling on the number of children a single fan-out may spawn. A goal that
# decomposes into more sub-tasks than this is almost certainly mis-planned; we
# refuse rather than fork an unbounded swarm.
MAX_CHILDREN = 8

# Statuses the child runtime can return. Only "success" counts as a usable child
# result; "partial" / "failure" / "cancelled" are treated as failures so the
# synthesis reports them honestly (with the runtime's own reason).
_SUCCESS_STATUSES = frozenset({"success"})

# Result keys that carry a per-child cost signal. We tolerate either the spec's
# ``tool_call_count`` or the runtime's real ``tool_calls`` (build_result emits
# the latter), and either ``iterations`` or ``rounds``.
_TOOL_CALL_KEYS = ("tool_call_count", "tool_calls")
_ITERATION_KEYS = ("iterations", "rounds")


def _zero_budget() -> Dict[str, int]:
    return {"tool_call_count": 0, "iterations": 0, "children": 0}


def _empty_outcome(synthesis: str) -> Dict[str, Any]:
    """The graceful empty / rejected return shape."""
    return {
        "succeeded": [],
        "failed": [],
        "missing": [],
        "children": [],
        "budget": _zero_budget(),
        "synthesis": synthesis,
        "any_succeeded": False,
        "all_succeeded": False,
    }


def _coerce_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _first_present(d: Dict[str, Any], keys) -> int:
    """Return the first present, int-coercible value among ``keys`` (else 0)."""
    if not isinstance(d, dict):
        return 0
    for k in keys:
        if k in d and d[k] is not None:
            return _coerce_int(d[k])
    return 0


def _is_success_result(result: Any) -> bool:
    return (
        isinstance(result, dict)
        and str(result.get("status", "")).lower() in _SUCCESS_STATUSES
    )


def _failure_reason(result: Any) -> str:
    """Human-readable reason a child failed (exception text or result error)."""
    if isinstance(result, BaseException):
        if isinstance(result, (asyncio.TimeoutError, TimeoutError)):
            return "timed out"
        return f"{type(result).__name__}: {result}"
    if isinstance(result, dict):
        status = str(result.get("status", "unknown")) or "unknown"
        err = result.get("error") or result.get("summary") or ""
        err = str(err).strip()
        return f"status={status}" + (f" ({err})" if err else "")
    return f"unexpected result type: {type(result).__name__}"


def _child_summary(result: Any) -> str:
    if isinstance(result, dict):
        return str(result.get("summary") or "").strip()
    return ""


class SubagentFleet:
    """Fans a goal's sub-tasks out to bounded, concurrent child subagents.

    Public API:
      * :meth:`spawn_children` — build + run one child runtime per task under a
        concurrency semaphore, then collate.
      * :meth:`collate` — turn raw child outcomes into the honest succeeded /
        failed / missing / budget / synthesis report.
    """

    def __init__(self, *, parent_execution_id: str, config, vault) -> None:
        self.parent_execution_id = parent_execution_id
        self.config = config
        self.vault = vault
        max_concurrent = max(
            1, _coerce_int(getattr(config, "delegate_max_concurrent_children", 2)) or 2
        )
        self._semaphore = asyncio.Semaphore(max_concurrent)

    # ── public ──────────────────────────────────────────────────────────────

    async def spawn_children(
        self,
        parent_shadow,
        parent_activity,
        tasks: List[str],
        *,
        per_child_timeout: float = 120.0,
    ) -> Dict[str, Any]:
        """Spawn one child subagent per task, bounded by the semaphore, and collate.

        Empty ``tasks`` short-circuits to a graceful empty synthesis. More than
        :data:`MAX_CHILDREN` tasks is refused with a failure dict naming the cap
        (we never fork an unbounded swarm). Each child runs under
        ``asyncio.wait_for(..., timeout=per_child_timeout)``; the whole batch
        runs under ``asyncio.gather(..., return_exceptions=True)`` so one child's
        timeout or exception cannot abort its siblings.
        """
        clean = [t for t in (tasks or []) if str(t).strip()]
        if not clean:
            return _empty_outcome("No sub-tasks requested.")

        if len(clean) > MAX_CHILDREN:
            return _empty_outcome(
                f"Refused to spawn {len(clean)} children: exceeds the maximum "
                f"of {MAX_CHILDREN} concurrent sub-tasks per goal. Re-plan the "
                f"goal into {MAX_CHILDREN} or fewer sub-tasks."
            )

        coros = [
            self._run_child(parent_shadow, parent_activity, task, i, per_child_timeout)
            for i, task in enumerate(clean)
        ]
        # return_exceptions=True: a child timeout/exception is captured as the
        # gathered value rather than cancelling the gather (siblings survive).
        results = await asyncio.gather(*coros, return_exceptions=True)

        child_outcomes = [
            {"task": task, "child_execution_id": f"{self.parent_execution_id}-child-{i}",
             "result": result}
            for i, (task, result) in enumerate(zip(clean, results))
        ]
        return self.collate(child_outcomes)

    def collate(self, child_outcomes: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Collate raw child outcomes into the honest fan-out report.

        Each entry of ``child_outcomes`` is ``{"task": str, "result": <dict|Exception>,
        ...}``. A success is a result dict whose ``status`` is ``"success"``;
        everything else (a raised exception, a timeout, a ``partial`` / ``failure``
        status dict) is a failure with a captured reason.

        Returns ``succeeded`` / ``failed`` / ``missing`` / ``children`` /
        ``budget`` / ``synthesis`` plus the ``any_succeeded`` and ``all_succeeded``
        booleans. The synthesis is a true-but-intelligent interpretation: it
        reports what each successful child produced AND explicitly names what
        failed and what is therefore missing.
        """
        succeeded: List[Dict[str, Any]] = []
        failed: List[Dict[str, Any]] = []
        missing: List[str] = []
        children: List[Dict[str, Any]] = []
        budget = _zero_budget()

        for entry in child_outcomes or []:
            task = str(entry.get("task", "")) if isinstance(entry, dict) else ""
            result = entry.get("result") if isinstance(entry, dict) else entry
            child_eid = (
                entry.get("child_execution_id") if isinstance(entry, dict) else None
            )

            budget["children"] += 1
            # Cost signals are only present on real (dict) results.
            budget["tool_call_count"] += _first_present(result, _TOOL_CALL_KEYS)
            budget["iterations"] += _first_present(result, _ITERATION_KEYS)

            if _is_success_result(result):
                summary = _child_summary(result) or "(no summary returned)"
                rec = {
                    "task": task,
                    "child_execution_id": child_eid,
                    "summary": summary,
                    "status": "success",
                }
                succeeded.append(rec)
                children.append({**rec, "ok": True})
            else:
                reason = _failure_reason(result)
                rec = {
                    "task": task,
                    "child_execution_id": child_eid,
                    "error": reason,
                    "status": "failed",
                }
                failed.append(rec)
                missing.append(task)
                children.append({**rec, "ok": False})

        any_succeeded = bool(succeeded)
        all_succeeded = bool(succeeded) and not failed
        synthesis = self._synthesise(succeeded, failed)

        return {
            "succeeded": succeeded,
            "failed": failed,
            "missing": missing,
            "children": children,
            "budget": budget,
            "synthesis": synthesis,
            "any_succeeded": any_succeeded,
            "all_succeeded": all_succeeded,
        }

    # ── internals ───────────────────────────────────────────────────────────

    async def _run_child(
        self,
        parent_shadow,
        parent_activity,
        task: str,
        index: int,
        per_child_timeout: float,
    ) -> Any:
        """Run a single child under the concurrency semaphore.

        Builds a fresh child shadow + activity (non-recursive — the harness
        strips delegate tools), constructs a brand-new ``ShadowRuntime`` so each
        child has isolated runtime state, and awaits its ``execute`` with a
        per-child timeout. Returns the result dict; lets ``TimeoutError`` /
        other exceptions propagate so ``gather(return_exceptions=True)`` captures
        them as that child's outcome (siblings unaffected).
        """
        child_id = f"child-{index}"
        async with self._semaphore:
            # Lazy import to avoid a circular import (shadow_runtime imports a lot
            # of the runtime package, which can transitively reach this module).
            from systemu.runtime.shadow_runtime import ShadowRuntime

            child_shadow = build_child_shadow(parent_shadow, child_id)
            child_activity = build_child_activity(
                parent_activity, task, child_id, self.vault
            )

            # v0.10.0 Item 1: isolate each child's action-audit into a per-child
            # namespace so concurrent children don't intermix in the parent's global
            # audit log. Per-execution artifacts are already id-isolated and there is
            # no corruption risk under asyncio; this is semantic separation. Tool/
            # memory layered isolation is deliberately out of scope (would break child
            # tool-reads; no corruption to justify it; feature is flag-off).
            _ns = None
            try:
                _ns = self.vault.create_child_execution_namespace(
                    self.parent_execution_id, child_id)
            except Exception:
                _ns = None

            child_runtime = ShadowRuntime(self.config, self.vault, audit_namespace=_ns)
            # Stash the deterministic child execution id so a runtime / mock can
            # surface it; the runtime mints its own internal id but this gives the
            # parent a stable handle.
            child_runtime._fleet_eid = f"{self.parent_execution_id}-child-{index}"

            origin = f"delegate-fleet-{self.parent_execution_id}"
            return await asyncio.wait_for(
                child_runtime.execute(child_shadow, child_activity, origin=origin),
                timeout=per_child_timeout,
            )

    def _synthesise(
        self,
        succeeded: List[Dict[str, Any]],
        failed: List[Dict[str, Any]],
    ) -> str:
        """Compose the honest natural-language synthesis.

        Never claims total failure when some children succeeded; never hides the
        failures when some children failed.
        """
        if not succeeded and not failed:
            return "No sub-tasks requested."

        parts: List[str] = []
        n_ok, n_bad = len(succeeded), len(failed)

        if succeeded:
            if n_bad:
                parts.append(
                    f"Partial result: {n_ok} of {n_ok + n_bad} sub-tasks completed."
                )
            else:
                parts.append(f"All {n_ok} sub-tasks completed.")
            parts.append("Produced:")
            for rec in succeeded:
                parts.append(f"  - {rec['task']}: {rec['summary']}")
        else:
            # No successes — be explicit, but enumerate WHY each failed rather
            # than emitting a contentless "everything failed".
            parts.append(f"No sub-tasks completed — all {n_bad} failed.")

        if failed:
            parts.append("Failed (and therefore missing):")
            for rec in failed:
                parts.append(f"  - {rec['task']}: {rec['error']}")
            if succeeded:
                missing_list = ", ".join(rec["task"] for rec in failed)
                parts.append(
                    "The above succeeded, but the following remain missing and "
                    f"would need a retry to complete the goal: {missing_list}."
                )

        return "\n".join(parts)
