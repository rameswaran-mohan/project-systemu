"""R-A10 step B9 (AC4): fold a runtime error into a REQUIREMENT + precede-objective.

A tool failure that is really a MISSING REQUIREMENT — a 401/403 auth failure (a
missing credential) or a 422/404 bad-request (an unresolved decision) — is NOT a
lack of progress. Counting it toward the stuck bound would fail the run for the
wrong reason. Instead we FOLD it:

  * ``auth``     → a ``Requirement(kind="credential", source="runtime_error",
                   state="missing", value_origin="operator")`` + a precede-objective
                   goal ``"Obtain credential for {service}"``.
  * ``semantic`` → a ``Requirement(kind="decision", source="runtime_error", …)`` +
                   a precede-objective goal ``"Resolve the request error for {tool}"``.

The precede is inserted BEFORE the CURRENT objective (the one whose tool call
failed), origin ``"backchain"``, wired so the current objective WAITS for it (the
precede inherits the current objective's original upstream deps; the current
objective's ``depends_on`` gains the precede's id). The caller then SUSPENDS via
the INPUT rail so the operator supplies the credential/decision; on resume the
precede is satisfied and the original objective retries.

IDEMPOTENCE: a repeated 401 for the SAME service (e.g. across a resume) must NOT
insert a second credential precede — we guard on an existing ``origin="backchain"``
precede that already carries a matching requirement.

NEVER raises: any structural problem → ``None`` (the caller degrades to the normal
reflection + stuck path).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class FoldResult:
    """The outcome of a runtime-error fold.

    Two shapes:
      * a SUCCESSFUL fold — ``already_pending`` is False and ``objectives`` /
        ``requirement`` / ``precede_id`` carry the inserted precede.
      * an IDEMPOTENT-PENDING no-op — ``already_pending`` is True: a precede for
        this service is ALREADY inserted and still missing (e.g. a repeated 401
        across the loop / a resume). The caller must STILL apply the depth-exemption
        and must NOT count this iteration toward the stuck bound — it is the exact
        thing B9 prevents. Distinct from a bare ``None`` (genuinely unfoldable:
        unresolvable current objective / non-auth-semantic), which DOES take the
        normal reflection + stuck path.
    """
    objectives: List[Any]          # NEW objective list with the precede inserted (or the unchanged tree for a pending no-op)
    next_id: int                   # bumped id-allocator floor
    requirement: Any               # the built Requirement (models.Requirement) — None for a pending no-op
    precede_id: int                # the inserted precede-objective's id — the EXISTING precede's id for a pending no-op
    already_pending: bool = False  # True ⇒ idempotent no-op (still-missing precede already present)
    # Fix A (HIGH, safety): set to the reused precede's id ONLY when this is a
    # wrong-credential RE-ASK (a satisfied state="have" precede flipped back to
    # "missing"). That precede was CREDITED into completed_objectives on the prior
    # resume, so the seam MUST discard its id from every set that tracks precede
    # completion BEFORE re-suspending — otherwise the original objective's
    # depends_on gate stays open and the LLM can finish it UNAUTHENTICATED. None
    # for a fresh insert (never credited) or an idempotent-pending no-op
    # (still-missing → never credited).
    reask_precede_id: Optional[int] = None


def _service_label(service_hint: Optional[str], tool_name: str) -> str:
    """Human label for the service/tool the failing call targeted."""
    label = (service_hint or "").strip() or (tool_name or "").strip()
    return label or "the service"


def _schema_path_for(service_hint: Optional[str], tool_name: str) -> str:
    """Stable identity for the missing requirement — used both as the
    Requirement.schema_path AND the idempotence key. Prefers the service hint so
    two different tools hitting the SAME service dedupe to one credential ask."""
    return _service_label(service_hint, tool_name)


def _existing_pending_precede_id(
    objectives: List[Any], *, kind: str, schema_path: str
) -> Optional[int]:
    """Return the id of an ``origin="backchain"`` precede that already carries a
    STILL-MISSING runtime-error requirement of the same kind + schema_path (the
    idempotence guard), or ``None`` if none. A requirement already flipped to
    "have"/"resolvable" (satisfied on a prior resume) does NOT count as pending —
    the fold may re-insert only if the prior one was satisfied and cleared. Never
    raises."""
    try:
        for o in objectives:
            if getattr(o, "origin", None) != "backchain":
                continue
            for req in (getattr(o, "requirements", None) or []):
                if (getattr(req, "source", None) == "runtime_error"
                        and getattr(req, "kind", None) == kind
                        and getattr(req, "schema_path", None) == schema_path
                        and getattr(req, "state", None) == "missing"):
                    return getattr(o, "id", None)
    except Exception:
        return None
    return None


def _existing_satisfied_precede_id(
    objectives: List[Any], *, kind: str, schema_path: str
) -> Optional[int]:
    """Return the id of an ``origin="backchain"`` precede that already carries a
    runtime-error requirement of the same kind + schema_path whose ``state`` is
    "have" (a credential/decision was supplied on a prior resume, but the retried
    call FAILED AGAIN — e.g. a WRONG credential re-401s). Bounding this reuses the
    existing precede instead of inserting a new one each wrong-credential cycle.
    ``None`` if none. Never raises."""
    try:
        for o in objectives:
            if getattr(o, "origin", None) != "backchain":
                continue
            for req in (getattr(o, "requirements", None) or []):
                if (getattr(req, "source", None) == "runtime_error"
                        and getattr(req, "kind", None) == kind
                        and getattr(req, "schema_path", None) == schema_path
                        and getattr(req, "state", None) == "have"):
                    return getattr(o, "id", None)
    except Exception:
        return None
    return None


def _reask_satisfied_precede(
    objectives: List[Any], *, precede_id: int, kind: str, schema_path: str
) -> List[Any]:
    """Flip the matching (kind+schema_path, state="have") runtime_error requirement
    on ``precede_id`` BACK to state="missing" so the operator is re-asked and the run
    re-suspends on the SAME precede (no new precede inserted). Returns a rebuilt
    objective list. Never raises — on any problem returns the input unchanged."""
    try:
        out: List[Any] = []
        for o in objectives:
            if getattr(o, "id", None) != precede_id:
                out.append(o)
                continue
            _new_reqs = []
            for req in (getattr(o, "requirements", None) or []):
                if (getattr(req, "source", None) == "runtime_error"
                        and getattr(req, "kind", None) == kind
                        and getattr(req, "schema_path", None) == schema_path
                        and getattr(req, "state", None) == "have"):
                    # bound_value_digest goes with the ref: it is a digest of the value
                    # THAT ref stood for, so leaving it behind would let a stale digest
                    # be compared against a later answer (R-A16 §5.9). The F2 canonical
                    # twin is the SAME value under a form-insensitive rule, so it goes
                    # with it — and it is the more dangerous of the two to leave behind,
                    # being deliberately easier to match.
                    _new_reqs.append(req.model_copy(
                        update={"state": "missing", "bound_value_ref": None,
                                "bound_value_digest": None,
                                "bound_value_canon_digest": None}))
                else:
                    _new_reqs.append(req)
            out.append(o.model_copy(update={"requirements": _new_reqs}))
        return out
    except Exception:
        logger.debug("[RuntimeFold] re-ask flip skipped", exc_info=True)
        return objectives


def fold_runtime_error(
    *,
    objectives: List[Any],
    current_obj_id: Any,
    sub: str,
    tool_name: str,
    service_hint: Optional[str],
    next_id: int,
) -> Optional[FoldResult]:
    """Fold an auth/semantic runtime error into a Requirement + precede-objective.

    Returns a :class:`FoldResult`, or ``None`` when the current objective can't be
    resolved, the sub-class isn't foldable, the tree is empty, or an existing fold
    already covers this service (idempotence). NEVER raises.
    """
    try:
        if sub not in ("auth", "semantic"):
            return None
        if not objectives:
            return None

        # Resolve the CURRENT objective (the one whose tool call failed). Degrade
        # to the normal path if it isn't in the tree.
        target = next(
            (o for o in objectives if getattr(o, "id", None) == current_obj_id),
            None,
        )
        if target is None:
            return None

        from systemu.core.models import Requirement
        from systemu.runtime.open_world_planner import _insert_precede_objectives

        label = _service_label(service_hint, tool_name)
        schema_path = _schema_path_for(service_hint, tool_name)

        if sub == "auth":
            kind = "credential"
            goal = f"Obtain credential for {label}"
            success = (
                f"A valid credential for {label} is available so the "
                f"call to {tool_name} can authenticate."
            )
            rationale = (
                f"The call to {tool_name} failed with an auth error (401/403); "
                f"a credential for {label} is a missing requirement."
            )
        else:  # semantic
            kind = "decision"
            goal = f"Resolve the request error for {tool_name} ({label})"
            success = (
                f"The request to {tool_name} is corrected so it no longer "
                f"returns a 4xx bad-request."
            )
            rationale = (
                f"The call to {tool_name} failed with a bad-request error "
                f"(422/404); how to correct the request is a missing decision."
            )

        # WRONG-CREDENTIAL BOUND (Fix 3): a re-401 for a service that ALREADY has a
        # backchain precede whose requirement is state="have" (a credential/decision
        # was supplied but the retried call FAILED AGAIN — e.g. a wrong credential).
        # Without this, the still-missing idempotence guard below would NOT match the
        # have-state precede, so a NEW backchain precede would be inserted every
        # wrong-credential cycle → slow unbounded graph growth (+1 precede/cycle).
        # REUSE the existing precede instead: flip its requirement back to "missing"
        # (re-ask the operator) and re-suspend on it. Return a normal (non-pending)
        # FoldResult naming the REUSED precede so the seam builds a fresh INPUT card +
        # re-suspends — no new objective, no growth. Checked BEFORE the still-missing
        # guard so a have→missing re-ask wins over the pending no-op.
        _satisfied_pid = _existing_satisfied_precede_id(
            objectives, kind=kind, schema_path=schema_path)
        if _satisfied_pid is not None:
            _reasked = _reask_satisfied_precede(
                objectives, precede_id=int(_satisfied_pid),
                kind=kind, schema_path=schema_path)
            # The reused precede already carries the requirement (now missing again).
            _reused_req = None
            try:
                _reused_obj = next(
                    (o for o in _reasked if getattr(o, "id", None) == _satisfied_pid),
                    None)
                for _r in (getattr(_reused_obj, "requirements", None) or []):
                    if (getattr(_r, "source", None) == "runtime_error"
                            and getattr(_r, "kind", None) == kind
                            and getattr(_r, "schema_path", None) == schema_path):
                        _reused_req = _r
                        break
            except Exception:
                _reused_req = None
            return FoldResult(
                objectives=_reasked,       # tree with the requirement re-asked (no new obj)
                next_id=int(next_id),      # unchanged floor — nothing allocated
                requirement=_reused_req,
                precede_id=int(_satisfied_pid),
                # Fix A: this reused precede was CREDITED on the prior resume (its
                # requirement WAS state="have"). Signal the re-ask so the seam
                # un-credits it (discards from completed_objectives + the resume
                # precede set) BEFORE re-suspending → the original objective's
                # depends_on gate RE-CLOSES and it genuinely waits for the new cred.
                reask_precede_id=int(_satisfied_pid),
            )

        # IDEMPOTENCE: don't insert a second precede for the same service if one is
        # already pending (guards a fold-storm on repeated 401s across a resume).
        # Return a DISTINCT already_pending sentinel (NOT a bare None) so the seam
        # can still apply the depth-exemption + skip the stuck counters for this
        # iteration — a repeated 401 on a still-missing credential must NEVER count
        # toward the stuck bound (Fix 2).
        _existing_pid = _existing_pending_precede_id(
            objectives, kind=kind, schema_path=schema_path)
        if _existing_pid is not None:
            return FoldResult(
                objectives=objectives,     # unchanged tree
                next_id=int(next_id),      # unchanged floor
                requirement=None,
                precede_id=int(_existing_pid),
                already_pending=True,
            )

        requirement = Requirement(
            kind=kind,
            schema_path=schema_path,
            state="missing",
            source="runtime_error",
            value_origin="operator",
            rationale=rationale,
        )

        new_objectives = _insert_precede_objectives(
            objectives=objectives,
            precede=[{
                "precede_before_objective_id": getattr(target, "id", current_obj_id),
                "goal": goal,
                "success_criteria": success,
            }],
            next_id=int(next_id),
            origin="backchain",
        )
        # No valid insert (identity return) → nothing folded → degrade.
        if new_objectives is objectives:
            return None

        precede_id = int(next_id)  # _insert allocates from next_id upward
        # Attach the requirement to the inserted precede so the idempotence guard
        # (and downstream requirement report) can see it on the durable graph.
        try:
            new_objectives = [
                o.model_copy(update={"requirements": list(getattr(o, "requirements", []) or []) + [requirement]})
                if getattr(o, "id", None) == precede_id else o
                for o in new_objectives
            ]
        except Exception:
            logger.debug("[RuntimeFold] attaching requirement to precede failed", exc_info=True)

        return FoldResult(
            objectives=new_objectives,
            next_id=int(next_id) + 1,
            requirement=requirement,
            precede_id=precede_id,
        )
    except Exception:
        logger.debug("[RuntimeFold] fold_runtime_error degraded to None", exc_info=True)
        return None
