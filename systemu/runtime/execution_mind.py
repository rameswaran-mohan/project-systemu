"""ExecutionMind — per-Shadow Intelligent Supervisor (v0.4.0-d).

One ``ExecutionMind`` instance is created when a Shadow execution starts
(when ``config.intelligent_supervisor_enabled`` is True).  It:

1. Subscribes to the EventBus stream for events tagged with the shadow's
   execution_id.
2. Maintains a *hypothesis* about what the shadow is doing.
3. At boundary points (post-tool-call, snapshot tick, stall), decides
   whether to intervene via the bounded action vocabulary defined in
   ``systemu/prompts/supervisor_intervene.md``.
4. Writes every decision (with reasoning + hypothesis) to a per-execution
   audit JSONL file ``data/audit/exec_<execution_id>/supervisor.jsonl``
   so operators can see WHY the supervisor acted.

Design constraints (deliberately defensive):

* **The Mind never blocks the shadow.**  Its LLM calls run in a background
  thread with a timeout; on timeout or error the directive defaults to
  ``DO_NOTHING``.
* **Tier mix**: routine directives go through Tier-3 (cheap, free model);
  only real interventions (REFLECT/ROLLBACK/SWAP/TERMINATE) escalate to
  Tier-1.  Configurable via ``config.supervisor_tier_routine`` /
  ``supervisor_tier_intervention``.
* **Budget per run**: capped by ``config.supervisor_llm_budget_per_run``.
  Beyond the cap, every directive returns ``DO_NOTHING`` — preserves the
  shadow's existing behaviour and avoids cost runaway.
* **Hypothesis persistence**: each tick's hypothesis is appended to the
  audit file.  On restart, the file can be replayed by an operator-facing
  tool — the daemon does NOT auto-rehydrate (would couple Mind to disk
  IO on the hot path).
* **Killswitch fail-open**: when ``intelligent_supervisor_enabled`` is
  False the Mind is never constructed — callers see no supervisor at all.

The Mind ONLY emits *advice* to the shadow runtime (via context_builder
helpers).  It does not directly mutate the runtime; the shadow's
``_handle_tool_call`` consults a small per-runtime *directive inbox*
populated by the Mind.
"""
from __future__ import annotations

import collections
import json
import logging
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Bounded action vocabulary

ACTION_VOCABULARY = (
    "DO_NOTHING",
    "NUDGE",
    "INJECT_REFLECTION",
    "FORCE_REFLECT",
    "ROLLBACK",
    "SWAP_SHADOW",
    "ESCALATE",
    "TERMINATE",
    "SET_THINK_BUDGET",
    # tool inadequacy → propose recalibration to operator
    "RECALIBRATE_TOOL",
    # v0.6.0-d.5: skill inadequacy → re-author instructions_md.  Mirrors
    # RECALIBRATE_TOOL but targets the procedural-knowledge layer (Skills)
    # rather than the code layer.
    "RECALIBRATE_SKILL",
)

HIGH_IMPACT_ACTIONS = frozenset({
    "ROLLBACK", "SWAP_SHADOW", "TERMINATE",
    "RECALIBRATE_TOOL", "RECALIBRATE_SKILL",
})

# Actions that should NOT count against the "routine" budget — they hit
# the intervention tier.
INTERVENTION_ACTIONS = frozenset({
    "INJECT_REFLECTION", "FORCE_REFLECT", "ROLLBACK", "SWAP_SHADOW",
    "TERMINATE", "ESCALATE",
})


# ─────────────────────────────────────────────────────────────────────────────
# Data types

@dataclass
class Hypothesis:
    trying:         str = ""
    struggling_on:  str = ""
    confidence:     float = 0.0


@dataclass
class Directive:
    action:                str
    rationale:             str = ""
    hint:                  Optional[str] = None
    swap_to:               Optional[str] = None
    think_budget_delta:    Optional[int] = None

    @property
    def is_high_impact(self) -> bool:
        return self.action in HIGH_IMPACT_ACTIONS


# ─────────────────────────────────────────────────────────────────────────────
# Audit helpers

def _audit_path(data_dir: Path, execution_id: str) -> Path:
    return data_dir / "audit" / f"exec_{execution_id}" / "supervisor.jsonl"


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


# ─────────────────────────────────────────────────────────────────────────────
# ExecutionMind

class ExecutionMind:
    """Per-shadow supervisor instance.  See module docstring for design.

    Args:
        execution_id:  The shadow run this Mind is shadowing.
        shadow_id:     For audit context.
        config:        Carries Tier-routine/intervention model names + budgets.
        directive_sink: Callable invoked with each :class:`Directive`.  Shadow
                       runtime passes a small inbox-append; tests can pass
                       a list.append.
        data_dir:      Root for the audit file.  Defaults to ``data/``.
    """

    def __init__(
        self,
        *,
        execution_id:   str,
        shadow_id:      Optional[str],
        config:         Any,
        directive_sink: Callable[["Directive"], None],
        data_dir:       Optional[Path] = None,
        force_enabled:  bool = False,
    ):
        self.execution_id   = execution_id
        self.shadow_id      = shadow_id
        self.config         = config
        self._sink          = directive_sink
        self.data_dir       = Path(data_dir or "data")
        self.hypothesis     = Hypothesis()
        self._actions:      List[str] = []
        self._calls_made    = 0
        self._high_impact_calls = 0
        # enable via global config OR force_enabled (per-shadow opt-in)
        self._enabled       = (
            bool(getattr(config, "intelligent_supervisor_enabled", False))
            or bool(force_enabled)
        )
        self._max_calls     = int(getattr(config, "supervisor_llm_budget_per_run", 10))
        self._lock          = threading.Lock()

    # ── Public API ────────────────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def budget_remaining(self) -> Dict[str, int]:
        # We reserve up to 30% of the budget for high-impact calls — past
        # that the routine path takes over so a buggy classifier can't
        # exhaust the budget on cheap NUDGEs and leave nothing for a real
        # ROLLBACK / SWAP_SHADOW need.
        with self._lock:
            return {
                "calls":             max(0, self._max_calls - self._calls_made),
                "high_impact_calls": max(0, int(self._max_calls * 0.3) - self._high_impact_calls),
            }

    def evaluate(
        self,
        *,
        trigger:        str,
        recent_events:  List[Dict[str, Any]],
        classifier:     Optional[str],
        consec_failures: int,
        iteration:      int,
        timeout_s:      Optional[float] = None,
    ) -> Directive:
        """Decide on the next intervention (or DO_NOTHING).

        Synchronous — but the Tier-routine/intervention LLM call is wrapped
        in an executor with a strict timeout so the shadow loop is never
        blocked for longer than ``timeout_s``.  Default timeout comes from
        ``config.supervisor_directive_timeout_s`` (5.0s).

        Always returns a :class:`Directive`.  On error / timeout / over-
        budget, returns ``Directive(action="DO_NOTHING")``.
        """
        if not self._enabled:
            return Directive(action="DO_NOTHING", rationale="supervisor disabled")

        with self._lock:
            if self._calls_made >= self._max_calls:
                return Directive(
                    action="DO_NOTHING",
                    rationale=f"supervisor LLM budget exhausted ({self._max_calls})",
                )
            self._calls_made += 1

        # per-hour / per-day USD cap check via the cost ledger.
        # When breached the kill switch trips and every future call returns
        # DO_NOTHING until operator reset (day) or hour rollover (hour).
        try:
            from systemu.runtime.supervisor_cost_ledger import get_ledger
            ledger = get_ledger(self.config)
            est = 0.01    # conservative default — Tier-3 calls usually $<0.001
            if not ledger.can_spend(est):
                return Directive(
                    action="DO_NOTHING",
                    rationale="supervisor cost ledger tripped — kill switch on",
                )
        except Exception:
            logger.debug("[ExecutionMind] cost ledger check skipped", exc_info=True)
            ledger = None

        timeout = float(
            timeout_s if timeout_s is not None
            else getattr(self.config, "supervisor_directive_timeout_s", 5.0)
        )
        directive = self._run_llm_with_timeout(
            trigger=trigger,
            recent_events=recent_events,
            classifier=classifier,
            consec_failures=consec_failures,
            iteration=iteration,
            timeout=timeout,
        )

        # consult the operator-rejection store before committing
        # to a non-trivial action.  If the operator recently dismissed a
        # similar proposal (same pattern_signature), downgrade to DO_NOTHING
        # with a clear rationale so the audit log shows why we backed off.
        if directive.action != "DO_NOTHING":
            try:
                from systemu.runtime.rejection_store import get_rejection_store
                from systemu.core.memory_types import pattern_signature as _ps
                sig = _ps(
                    error_type=classifier or "unknown",
                    tool_name=None,
                    error_message=(directive.rationale or "")[:200],
                )
                if get_rejection_store().is_recently_rejected(sig):
                    directive = Directive(
                        action="DO_NOTHING",
                        rationale=(
                            f"operator recently dismissed similar "
                            f"(signature={sig}); backing off"
                        ),
                    )
            except Exception:
                logger.debug("[ExecutionMind] rejection-store check skipped", exc_info=True)

        if directive.is_high_impact:
            with self._lock:
                self._high_impact_calls += 1

        with self._lock:
            self._actions.append(directive.action)

        # record the cost of the call we just made.  Conservative
        # estimate ($0.01 per Tier-3 call, $0.05 per Tier-1) — refined later
        # if we wire up real pricing.  Best-effort.
        try:
            if ledger is not None:
                ledger.record(0.05 if directive.action in INTERVENTION_ACTIONS else 0.01)
        except Exception:
            logger.debug("[ExecutionMind] cost ledger record skipped", exc_info=True)

        self._write_audit(
            trigger=trigger,
            iteration=iteration,
            directive=directive,
            classifier=classifier,
            consec_failures=consec_failures,
        )

        try:
            self._sink(directive)
        except Exception:
            logger.exception("[ExecutionMind] directive sink raised")

        # publish a strategy-stream tick so the chat feed shows
        # the supervisor reasoning in real time.  Audit file remains the
        # source of forensic truth; this is the operator-facing view.
        try:
            from systemu.interface.event_bus import EventBus
            from systemu.core.memory_types import pattern_signature as _ps
            sig = _ps(
                error_type=classifier or "unknown",
                tool_name=None,
                error_message=(directive.rationale or "")[:200],
            )
            EventBus.get().publish_supervisor_action(
                execution_id=self.execution_id,
                action=directive.action,
                rationale=directive.rationale,
                classifier=classifier,
                consec_failures=consec_failures,
                iteration=iteration,
                shadow_id=self.shadow_id,
                pattern_signature=sig,
            )
        except Exception:
            logger.debug("[ExecutionMind] strategy-stream publish skipped", exc_info=True)

        return directive

    # ── Internals ─────────────────────────────────────────────────────────

    def _run_llm_with_timeout(
        self,
        *,
        trigger:         str,
        recent_events:   List[Dict[str, Any]],
        classifier:      Optional[str],
        consec_failures: int,
        iteration:       int,
        timeout:         float,
    ) -> Directive:
        result_queue: "queue.Queue[Directive]" = queue.Queue(maxsize=1)

        def _worker():
            try:
                directive = self._call_llm(
                    trigger=trigger,
                    recent_events=recent_events,
                    classifier=classifier,
                    consec_failures=consec_failures,
                    iteration=iteration,
                )
                result_queue.put(directive)
            except Exception as exc:
                logger.warning("[ExecutionMind] LLM directive call failed: %s", exc)
                result_queue.put(Directive(
                    action="DO_NOTHING",
                    rationale=f"directive LLM raised: {exc}"[:160],
                ))

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        try:
            return result_queue.get(timeout=timeout)
        except queue.Empty:
            logger.warning(
                "[ExecutionMind] directive timed out after %ss — defaulting DO_NOTHING",
                timeout,
            )
            return Directive(
                action="DO_NOTHING",
                rationale=f"directive timed out after {timeout}s",
            )

    def _call_llm(
        self,
        *,
        trigger:         str,
        recent_events:   List[Dict[str, Any]],
        classifier:      Optional[str],
        consec_failures: int,
        iteration:       int,
    ) -> Directive:
        # Tier-mix: routine vs intervention.  Reflection / rollback /
        # swap / terminate / escalate count as interventions and use the
        # higher tier; everything else goes through the routine tier.
        routine_tier_name = (getattr(self.config, "supervisor_tier_routine", "tier_3") or "tier_3")
        interv_tier_name  = (getattr(self.config, "supervisor_tier_intervention", "tier_1") or "tier_1")

        # Cheap rule-based gate: if there are no recent failures AND the
        # classifier didn't flag anything, the answer is overwhelmingly
        # DO_NOTHING.  We still call Tier-3 once per run so hypothesis is
        # updated, but interventions skip when the signal is absent.
        is_intervention_warranted = (
            consec_failures >= 1
            or (classifier and classifier != "unknown")
            or trigger == "stall"
        )
        tier_label = interv_tier_name if is_intervention_warranted else routine_tier_name

        # surface live cost-pressure to the supervisor LLM so it
        # can prefer cheaper directives (NUDGE, DO_NOTHING) when budgets
        # are near exhaustion.  We pass utilisation ratios + absolute
        # spend so the model can reason about both proportion and headroom.
        cost_pressure: Dict[str, Any] = {}
        try:
            from systemu.runtime.supervisor_cost_ledger import get_ledger
            ledger = get_ledger(self.config)
            snap = ledger.snapshot()
            hour_cap = float(ledger.max_per_hour or 0.0)
            day_cap  = float(ledger.max_per_day or 0.0)
            hour_spent = float(snap.get("hour_spent_usd", 0.0))
            day_spent  = float(snap.get("day_spent_usd", 0.0))
            cost_pressure = {
                "hour_spent_usd":   round(hour_spent, 4),
                "hour_cap_usd":     hour_cap,
                "hour_utilisation": round(hour_spent / hour_cap, 3) if hour_cap > 0 else 0.0,
                "day_spent_usd":    round(day_spent, 4),
                "day_cap_usd":      day_cap,
                "day_utilisation":  round(day_spent / day_cap, 3) if day_cap > 0 else 0.0,
                # Convenience flag — when high, prompt rules tell the model
                # to bias toward cheap actions.
                "near_cap":         (
                    (hour_cap > 0 and hour_spent / hour_cap >= 0.75)
                    or (day_cap > 0 and day_spent / day_cap >= 0.75)
                ),
            }
        except Exception:
            logger.debug("[ExecutionMind] cost-pressure payload skipped", exc_info=True)

        payload = {
            "shadow_id":       self.shadow_id,
            "execution_id":    self.execution_id,
            "iteration":       iteration,
            "trigger":         trigger,
            "hypothesis":      self.hypothesis.__dict__,
            "recent_events":   recent_events[-3:],   # at most 3 — token discipline
            "classifier":      classifier,
            "consec_failures": consec_failures,
            "actions_so_far":  list(self._actions),
            "budget_remaining": self.budget_remaining,
            "cost_pressure":   cost_pressure,
        }

        # Map tier label → numeric tier expected by llm_router.
        tier_num = 1 if "1" in tier_label else (3 if "3" in tier_label else 2)

        try:
            from systemu.core.llm_router import llm_call_json
            from systemu.core.utils import load_prompt
            raw = llm_call_json(
                tier=tier_num,
                system=load_prompt("supervisor_intervene.md"),
                user=json.dumps(payload, ensure_ascii=False),
                config=self.config,
                temperature=0.2,
                max_tokens=384,
            )
        except Exception as exc:
            logger.warning("[ExecutionMind] LLM router error: %s", exc)
            return Directive(
                action="DO_NOTHING",
                rationale=f"llm error: {exc}"[:160],
            )

        return self._parse_directive(raw)

    def _parse_directive(self, raw: Any) -> Directive:
        if not isinstance(raw, dict):
            return Directive(
                action="DO_NOTHING",
                rationale="directive LLM returned non-object",
            )
        # Update hypothesis from this tick's output regardless of action.
        hyp_upd = raw.get("hypothesis_update") or {}
        if isinstance(hyp_upd, dict):
            try:
                self.hypothesis = Hypothesis(
                    trying=str(hyp_upd.get("trying") or self.hypothesis.trying)[:300],
                    struggling_on=str(hyp_upd.get("struggling_on") or self.hypothesis.struggling_on)[:300],
                    confidence=float(hyp_upd.get("confidence", self.hypothesis.confidence)),
                )
            except Exception:
                logger.debug("[ExecutionMind] could not parse hypothesis update", exc_info=True)
        action = str(raw.get("action") or "DO_NOTHING").upper()
        if action not in ACTION_VOCABULARY:
            logger.warning(
                "[ExecutionMind] LLM emitted invalid action %r — falling back to DO_NOTHING",
                action,
            )
            action = "DO_NOTHING"
        return Directive(
            action=action,
            rationale=str(raw.get("rationale") or "")[:300],
            hint=(str(raw.get("hint")) if raw.get("hint") else None),
            swap_to=(str(raw.get("swap_to")) if raw.get("swap_to") else None),
            think_budget_delta=(int(raw["think_budget_delta"])
                                if isinstance(raw.get("think_budget_delta"), (int, float)) else None),
        )

    def _write_audit(
        self,
        *,
        trigger:         str,
        iteration:       int,
        directive:       Directive,
        classifier:      Optional[str],
        consec_failures: int,
    ) -> None:
        try:
            path = _audit_path(self.data_dir, self.execution_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            row = {
                "ts":              _now_iso(),
                "execution_id":    self.execution_id,
                "shadow_id":       self.shadow_id,
                "iteration":       iteration,
                "trigger":         trigger,
                "classifier":      classifier,
                "consec_failures": consec_failures,
                "action":          directive.action,
                "rationale":       directive.rationale,
                "hypothesis": {
                    "trying":        self.hypothesis.trying,
                    "struggling_on": self.hypothesis.struggling_on,
                    "confidence":    self.hypothesis.confidence,
                },
                "budget_remaining": self.budget_remaining,
            }
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception:
            logger.debug(
                "[ExecutionMind] could not write audit row — supervisor remains best-effort",
                exc_info=True,
            )


# ─────────────────────────────────────────────────────────────────────────────
# Inbox primitive for shadow_runtime integration

class DirectiveInbox:
    """Thread-safe FIFO inbox shadow_runtime polls between iterations.

    Lifecycle: created at execution start, populated by ExecutionMind,
    drained by shadow_runtime's main loop.  Maxlen bounded so a
    pathological supervisor can't OOM the runtime.
    """

    def __init__(self, maxlen: int = 32):
        self._buf:  "collections.deque[Directive]" = collections.deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def append(self, directive: Directive) -> None:
        with self._lock:
            self._buf.append(directive)

    def drain(self) -> List[Directive]:
        with self._lock:
            out = list(self._buf)
            self._buf.clear()
        return out

    def __len__(self) -> int:
        with self._lock:
            return len(self._buf)
