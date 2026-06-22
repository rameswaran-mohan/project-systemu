"""WorkflowTracker — in-memory map of in-flight workflows and their pipeline stage.

A *workflow* is the chain capture → scroll → activity → execution that
represents one user intent moving through the system.  This tracker
gives the dashboard (and any other observer) a single place to ask
"where are my workflows right now" without each consumer re-walking
the vault.

Design

* **In-memory only.** No new persistence.  On daemon boot the tracker
  reconstructs current state by walking the vault once; after that it
  subscribes to the EventBus and applies updates incrementally.
* **Single source of truth = the vault.** The tracker never invents
  state — every update is anchored to a vault entity (scroll /
  activity / execution).
* **Cheap reads.** Listing in-flight workflows is O(N) over an
  in-process dict; building the per-stage counts is O(N) over the
  same dict.  Both are well under 1 ms for the dashboard refresh
  cadence.

The tracker is a singleton.  Boot it once from the daemon (it lives
for the daemon's lifetime); pages call ``WorkflowTracker.get()``.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
#  Pipeline stages
# ─────────────────────────────────────────────────────────────────────────────

# The order matters — the dashboard renders this list left-to-right.
STAGES: List[str] = ["capture", "scroll", "activity", "execution", "done"]

# Terminal stages do NOT count toward the "in-flight" total.
TERMINAL_STAGES = {"done", "failed"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─────────────────────────────────────────────────────────────────────────────
#  Data model
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class WorkflowSnapshot:
    """One workflow's current state — purely a view object."""

    workflow_id: str
    title: str
    stage: str                                # current STAGES entry
    status: str                               # vault status string (pending_approval, running, …)
    scroll_id: Optional[str] = None
    activity_id: Optional[str] = None
    execution_id: Optional[str] = None
    shadow_id: Optional[str] = None
    started_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    # Per-stage timeline: stage name → ISO timestamp the workflow entered the stage.
    # Useful for the per-workflow detail page and metrics.
    timeline: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "workflow_id":   self.workflow_id,
            "title":         self.title,
            "stage":         self.stage,
            "status":        self.status,
            "scroll_id":     self.scroll_id,
            "activity_id":   self.activity_id,
            "execution_id":  self.execution_id,
            "shadow_id":     self.shadow_id,
            "started_at":    self.started_at,
            "updated_at":    self.updated_at,
            "timeline":      dict(self.timeline),
        }

    @property
    def is_terminal(self) -> bool:
        return self.stage in TERMINAL_STAGES


# ─────────────────────────────────────────────────────────────────────────────
#  Tracker singleton
# ─────────────────────────────────────────────────────────────────────────────

class WorkflowTracker:
    """In-process tracker for workflow → pipeline-stage mappings.

    Public API:
        WorkflowTracker.init(vault, events)   — boot + warm cache + subscribe
        WorkflowTracker.get()                 — fetch the singleton
        .list_active()                        — workflows in non-terminal stages
        .list_all()                           — every known workflow
        .get_workflow(workflow_id)            — single snapshot
        .counts_by_stage()                    — dict of stage → count
        .upsert(...)                          — apply an update (event handler)
    """

    _instance: Optional["WorkflowTracker"] = None
    _init_lock = threading.Lock()

    def __init__(self) -> None:
        self._workflows: Dict[str, WorkflowSnapshot] = {}
        self._lock = threading.RLock()
        self._unsubscribe = None  # set by .init() when wiring to EventBus

    # ── Singleton plumbing ─────────────────────────────────────────────

    @classmethod
    def get(cls) -> "WorkflowTracker":
        if cls._instance is None:
            with cls._init_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @classmethod
    def init(cls, vault: Any = None, events: Any = None) -> "WorkflowTracker":
        """Bootstrap the tracker — warm the cache from the vault and
        subscribe to the EventBus.

        Both arguments are optional so unit tests can build an isolated
        tracker without a real vault / bus.  In production this is called
        once from ``dashboard.run_dashboard()`` and again is a no-op.
        """
        tracker = cls.get()
        if vault is not None:
            try:
                tracker._reconstruct_from_vault(vault)
            except Exception as exc:
                logger.warning(
                    "[WorkflowTracker] reconstruct_from_vault failed (%s) — "
                    "starting from empty state", exc,
                )
        if events is not None and tracker._unsubscribe is None:
            try:
                tracker._unsubscribe = events.subscribe(
                    tracker._handle_event, replay=False,
                )
            except Exception as exc:
                logger.warning(
                    "[WorkflowTracker] EventBus subscribe failed (%s) — "
                    "tracker will only have vault-warmed state", exc,
                )
        # Remember the vault reference so callers can ask for a fresh
        # reconcile later (the EventBus subscription path is best-effort —
        # any pipeline that writes vault rows without publishing a
        # matching status event would otherwise leave the tracker stale).
        if vault is not None:
            tracker._vault = vault
        return tracker

    def refresh_from_vault(self) -> None:
        """Re-walk the vault and update tracker state for any changes
        that aren't reflected in the in-memory cache.

        Cheap to call (O(N) over scrolls + activities).  The Workflow
        Pipeline component invokes this on each render so the dashboard
        always reflects the latest persisted state — even when the
        runtime updates an activity's status without publishing a
        matching EventBus event.
        """
        vault = getattr(self, "_vault", None)
        if vault is None:
            return
        try:
            self._reconstruct_from_vault(vault)
        except Exception as exc:
            logger.debug("[WorkflowTracker] refresh_from_vault failed: %s", exc)

    # ── Public reads ────────────────────────────────────────────────────

    def list_active(self) -> List[WorkflowSnapshot]:
        """Workflows whose current stage is non-terminal."""
        with self._lock:
            return [w for w in self._workflows.values() if not w.is_terminal]

    def list_all(self) -> List[WorkflowSnapshot]:
        with self._lock:
            return list(self._workflows.values())

    def get_workflow(self, workflow_id: str) -> Optional[WorkflowSnapshot]:
        with self._lock:
            return self._workflows.get(workflow_id)

    def counts_by_stage(self) -> Dict[str, int]:
        """Count workflows in each stage.  Convenient for the pipeline card."""
        counts = {s: 0 for s in STAGES}
        with self._lock:
            for w in self._workflows.values():
                counts[w.stage] = counts.get(w.stage, 0) + 1
        return counts

    # ── Public mutate ───────────────────────────────────────────────────

    def upsert(
        self,
        workflow_id: str,
        *,
        stage: Optional[str] = None,
        status: Optional[str] = None,
        title: Optional[str] = None,
        scroll_id: Optional[str] = None,
        activity_id: Optional[str] = None,
        execution_id: Optional[str] = None,
        shadow_id: Optional[str] = None,
    ) -> WorkflowSnapshot:
        """Idempotently create-or-update a workflow snapshot.

        Stage transitions are anchored — we never silently downgrade.
        The tracker preserves the highest-progress stage we've seen.
        """
        with self._lock:
            snap = self._workflows.get(workflow_id)
            if snap is None:
                initial_stage = stage or "capture"
                snap = WorkflowSnapshot(
                    workflow_id=workflow_id,
                    title=title or workflow_id,
                    stage=initial_stage,
                    status=status or "unknown",
                )
                # Record the initial stage in the timeline so the
                # workflow_detail page can always show *when* the
                # workflow entered its first observed stage.
                snap.timeline[initial_stage] = _now()
                self._workflows[workflow_id] = snap

            if title:
                snap.title = title
            if scroll_id:
                snap.scroll_id = scroll_id
            if activity_id:
                snap.activity_id = activity_id
            if execution_id:
                snap.execution_id = execution_id
            if shadow_id:
                snap.shadow_id = shadow_id
            if status:
                snap.status = status

            if stage and _stage_rank(stage) >= _stage_rank(snap.stage):
                # Record any stage we pass through, including the current
                # one if its timestamp is missing.  setdefault preserves
                # the earliest timestamp we saw for that stage.
                snap.timeline.setdefault(stage, _now())
                snap.stage = stage

            snap.updated_at = _now()
            return snap

    # ── Reconstruction from the vault on boot ──────────────────────────

    def _reconstruct_from_vault(self, vault: Any) -> None:
        """Walk vault indexes once to seed the tracker.

        Strategy: every Scroll seeds a workflow.  Activities and
        executions associated with a Scroll advance its stage.  The
        ``scroll_id`` is the workflow id (workflows are 1:1 with
        scrolls today).
        """
        try:
            scrolls    = vault.load_index("scrolls")
            activities = vault.load_index("activities")
        except Exception as exc:
            logger.debug("[WorkflowTracker] load_index failed: %s", exc)
            return

        # 1) seed from scrolls
        for s in scrolls or []:
            sid = s.get("id")
            if not sid:
                continue
            stage  = _stage_for_scroll_status(s.get("status", ""))
            status = s.get("status") or "unknown"
            self.upsert(
                sid,
                stage=stage,
                status=status,
                title=s.get("name") or sid,
                scroll_id=sid,
            )

        # 2) advance with activity info
        for a in activities or []:
            sid = a.get("scroll_id")
            if not sid:
                continue
            stage  = _stage_for_activity_status(a.get("status", ""))
            self.upsert(
                sid,
                stage=stage,
                status=a.get("status") or "unknown",
                activity_id=a.get("id"),
                shadow_id=a.get("shadow_id"),
            )

    # ── EventBus integration ────────────────────────────────────────────

    def _handle_event(self, event: Dict[str, Any]) -> None:
        """Translate an EventBus event into an upsert.

        We only react to events that mention an entity we can map back
        to a workflow.  Everything else is ignored — the tracker is
        deliberately lossy on irrelevant events.
        """
        try:
            ctx = event.get("context") or {}
            category = event.get("category") or ""

            scroll_id    = ctx.get("scroll_id")
            activity_id  = ctx.get("activity_id")
            execution_id = ctx.get("execution_id")
            shadow_id    = ctx.get("shadow_id")
            new_status   = ctx.get("status") or ctx.get("new_status")

            # Map category → stage (best-effort, conservative).
            stage = None
            if category == "shadow":
                stage = "execution"
            elif category == "supervisor" and execution_id:
                stage = "execution"
            elif category in {"capture", "sharing_on"}:
                stage = "capture"
            elif category in {"scroll", "scroll_refinery"}:
                stage = "scroll"
            elif category in {"activity", "activity_extraction"}:
                stage = "activity"
            elif new_status in {"completed", "done", "success"}:
                stage = "done"
            elif new_status == "failed":
                stage = "failed"

            wid = scroll_id or ctx.get("workflow_id")
            if not wid:
                return  # nothing to anchor on
            self.upsert(
                wid,
                stage=stage,
                status=new_status,
                scroll_id=scroll_id,
                activity_id=activity_id,
                execution_id=execution_id,
                shadow_id=shadow_id,
            )
        except Exception as exc:
            logger.debug("[WorkflowTracker] handle_event failed: %s", exc)

    # ── Reset / shutdown ────────────────────────────────────────────────

    def shutdown(self) -> None:
        if self._unsubscribe is not None:
            try:
                self._unsubscribe()
            except Exception:
                pass
            self._unsubscribe = None

    def reset(self) -> None:
        """Test helper — clear all in-memory state."""
        with self._lock:
            self._workflows.clear()


# ─────────────────────────────────────────────────────────────────────────────
#  Status → stage mappings
# ─────────────────────────────────────────────────────────────────────────────

def _stage_rank(stage: str) -> int:
    try:
        return STAGES.index(stage)
    except ValueError:
        return -1


def _stage_for_scroll_status(status: str) -> str:
    status = (status or "").lower()
    if status in {"pending_approval", "draft", "refining"}:
        return "scroll"
    if status in {"approved", "active", "linked"}:
        return "activity"
    if status in {"executed", "completed"}:
        return "done"
    return "scroll"


def _stage_for_activity_status(status: str) -> str:
    status = (status or "").lower()
    if status in {"unassigned", "assigned"}:
        return "activity"
    if status in {"running", "queued", "in_progress"}:
        return "execution"
    if status in {"completed", "done", "success"}:
        return "done"
    if status == "failed":
        return "failed"
    return "activity"
