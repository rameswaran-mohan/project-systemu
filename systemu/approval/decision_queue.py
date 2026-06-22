"""OperatorDecisionQueue — vault-backed cache of operator decisions.

v0.8.0 Pattern 1 from the 2026-05-26 architecture audit. Used by the
queue-mode branch of ``notify_user`` (in interface/notifications.py)
so that when a CLI subprocess is spawned by the dashboard JobManager
without a TTY, its operator-decision prompts are persisted here for
the operator to resolve via the dashboard, rather than being silently
auto-picked to actions[0] ("Skip").

The queue is a thin wrapper over the vault's existing index/get/save
abstraction so all three vault backends (file / sqlite / postgres)
work without backend-specific code.

Vault contract (interface methods this module assumes exist on
vault objects):
  - ``vault.load_index("decisions") -> list[dict]``  — returns the
    persisted index of decision headers (lightweight summaries).
  - ``vault.get_decision(decision_id: str) -> OperatorDecision``  — load
    a single decision by id.
  - ``vault.save_decision(decision: OperatorDecision) -> None``  — persist
    a decision (upsert).

Backends implement these in Task 3 (file_vault) and Task 4 (sqlite_vault).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, TYPE_CHECKING

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from systemu.vault.vault import Vault


_VALID_STATUS = ("pending", "resolved", "expired")


@dataclass
class OperatorDecision:
    """A single operator decision record.

    Attributes:
        id:            Globally unique id, prefixed "dec_".
        title:         Short heading shown to the operator.
        body:          Detail / explanation shown under the title.
        options:       Ordered list of choice labels the operator can pick.
                       options[0] is the safe-by-default for emergency
                       auto-resolve scenarios (timeouts, etc.).
        context:       Arbitrary dict carried with the decision (scroll_id,
                       tool_id, etc.) — consumed by the resuming caller.
        dedup_key:     String the caller passes to short-circuit duplicate
                       prompts for the same logical question (e.g.
                       "tool_forge:tool_x"). Two posts with the same
                       dedup_key while pending return the existing id.
        status:        "pending" | "resolved" | "expired".
        choice:        The operator's chosen option label (None until resolved).
        created_at:    UTC timestamp of post.
        resolved_at:   UTC timestamp of resolve (None when pending).
    """

    id: str
    title: str
    body: str
    options: List[str]
    context: Dict[str, Any] = field(default_factory=dict)
    dedup_key: str = ""
    status: str = "pending"
    choice: Optional[str] = None
    created_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id":          self.id,
            "title":       self.title,
            "body":        self.body,
            "options":     list(self.options),
            "context":     dict(self.context),
            "dedup_key":   self.dedup_key,
            "status":      self.status,
            "choice":      self.choice,
            "created_at":  self.created_at.isoformat() if self.created_at else None,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "OperatorDecision":
        def _parse(ts):
            if not ts:
                return None
            return datetime.fromisoformat(ts)
        return cls(
            id=raw["id"],
            title=raw.get("title", ""),
            body=raw.get("body", ""),
            options=list(raw.get("options", [])),
            context=dict(raw.get("context", {})),
            dedup_key=raw.get("dedup_key", ""),
            status=raw.get("status", "pending"),
            choice=raw.get("choice"),
            created_at=_parse(raw.get("created_at")),
            resolved_at=_parse(raw.get("resolved_at")),
        )


class OperatorDecisionQueue:
    """Vault-backed cache of operator decisions."""

    def __init__(self, vault: "Vault"):
        self._vault = vault

    def post(
        self,
        *,
        title: str,
        body: str,
        options: List[str],
        context: Optional[Dict[str, Any]] = None,
        dedup_key: str = "",
    ) -> str:
        """Persist a pending decision (or return existing pending id).

        If a pending decision already exists with the same ``dedup_key``,
        returns that decision's id without creating a duplicate. This is
        what lets a CLI command be re-attempted safely while the operator
        is still deciding.

        Returns the decision id.
        """
        if not options:
            raise ValueError("OperatorDecisionQueue.post: options must be non-empty")

        # Check for existing pending decision with same dedup_key
        if dedup_key:
            existing = self._find_pending_by_dedup_key(dedup_key)
            if existing is not None:
                return existing.id

        decision = OperatorDecision(
            id=f"dec_{uuid.uuid4().hex[:8]}",
            title=title,
            body=body,
            options=list(options),
            context=dict(context or {}),
            dedup_key=dedup_key,
            status="pending",
            choice=None,
            created_at=datetime.now(tz=timezone.utc),
            resolved_at=None,
        )
        self._vault.save_decision(decision)
        logger.info(
            "[DecisionQueue] posted '%s' (id=%s, dedup_key=%r, options=%s)",
            title, decision.id, dedup_key, options,
        )
        # v0.8.22 (C): emit EventBus event so chat UI can render an inline card.
        # W5.3: self-describing event — top-level ts/level/message so EVERY
        # pane (incl. the right-rail Live stream, which reads event["message"])
        # renders it; the bare-context shape drew a blank "[INFO] " line.
        try:
            from systemu.interface.event_bus import EventBus
            EventBus.get().publish({
                "ts": decision.created_at.isoformat(),
                "level": "WARNING",
                "message": f"Needs you: {decision.title}",
                "category": "operator_decision_posted",
                "context": {
                    "decision_id": decision.id,
                    "dedup_key": decision.dedup_key,
                    "title": decision.title,
                    "options": list(decision.options),
                    "chat_submission_id": (decision.context or {}).get("chat_submission_id"),
                },
            })
        except Exception:
            pass  # EventBus is optional — never block a vault-saved decision
        return decision.id

    def list_pending(self) -> List[OperatorDecision]:
        """Return all decisions with status='pending'."""
        try:
            headers = self._vault.load_index("decisions") or []
        except Exception:
            logger.exception("[DecisionQueue] could not load decisions index")
            return []
        pending_ids = [h["id"] for h in headers if h.get("status") == "pending"]
        result = []
        for did in pending_ids:
            try:
                result.append(self._vault.get_decision(did))
            except Exception as exc:
                # v0.8.0.1: promoted from debug to warning.  The original
                # debug-level log made it impossible to discover that the
                # FileVault wrapper was missing get_decision/save_decision
                # proxies in v0.8.0 — the dashboard's Pending Actions tab
                # silently rendered empty even when CLI saw pending records.
                # Surfaces structural bugs like that immediately in dev / UAT.
                logger.warning(
                    "[DecisionQueue] could not load %s — %r "
                    "(header was in pending list but get_decision failed)",
                    did, exc,
                )
        return result

    def get_resolved_choice(self, dedup_key: str) -> Optional[str]:
        """Return the operator's choice for the given dedup_key, or None.

        Returns None if no decision exists for that dedup_key, OR if the
        decision is still pending. Returns the choice string if resolved.
        """
        if not dedup_key:
            return None
        try:
            headers = self._vault.load_index("decisions") or []
        except Exception:
            logger.exception("[DecisionQueue] could not load decisions index")
            return None

        # Walk decisions newest-first by created_at (if present in header)
        for h in sorted(
            headers,
            key=lambda x: x.get("created_at") or "",
            reverse=True,
        ):
            if h.get("dedup_key") != dedup_key:
                continue
            if h.get("status") != "resolved":
                continue
            try:
                decision = self._vault.get_decision(h["id"])
            except Exception as exc:
                # v0.8.4: was silent `continue` — masked vault corruption
                # (e.g. index header without a matching body file from the
                # v0.8.2 init-seed-copy bug class).  Operator would be
                # re-prompted for an already-resolved decision and their
                # original click would be lost.  Surface it as WARNING so
                # the issue is visible in daemon logs.
                logger.warning(
                    "[DecisionQueue] resolved header %r (dedup_key=%r) has no "
                    "loadable body — vault may be corrupt.  Operator will be "
                    "re-prompted.  Error: %r",
                    h.get("id"), dedup_key, exc,
                )
                continue
            return decision.choice
        return None

    def resolve(self, decision_id: str, *, choice: str) -> OperatorDecision:
        """Mark a decision resolved with the operator's chosen option.

        Raises ValueError if the choice is not in the decision's options.
        """
        try:
            decision = self._vault.get_decision(decision_id)
        except Exception as exc:
            raise KeyError(f"OperatorDecision {decision_id} not found: {exc}")
        # v0.8.19 (R3): structured-question answers are JSON, not an option label —
        # skip the membership check for that kind; plain decisions stay strict.
        # v0.9.35 (P1): an elicitation FORM gate (carries a non-empty
        # requested_schema in its context) likewise resolves with a JSON form
        # answer (build_elicitation_answer output) rather than an option label —
        # so accept a non-option choice for it too. A free-text ASK_OPERATOR /
        # INPUT gate (harness_kind == "input") similarly carries the operator's
        # answer as the choice, which need not be one of the fixed option labels.
        # The safe-default option (Decline) still passes the strict path below.
        _ctx = decision.context or {}
        _structured = _ctx.get("kind") == "structured_question"
        _elicitation_form = bool(_ctx.get("requested_schema"))
        _input_gate = str(_ctx.get("harness_kind") or "").lower() == "input"
        if (not (_structured or _elicitation_form or _input_gate)
                and choice not in decision.options):
            raise ValueError(
                f"choice {choice!r} not in options {decision.options!r} "
                f"for decision {decision_id}"
            )
        decision.status = "resolved"
        decision.choice = choice
        decision.resolved_at = datetime.now(tz=timezone.utc)
        self._vault.save_decision(decision)
        logger.info(
            "[DecisionQueue] resolved %s -> %r (dedup_key=%r)",
            decision_id, choice, decision.dedup_key,
        )
        # v0.8.22 (C): emit EventBus event so the chat UI hides the inline card.
        # W5.3: self-describing (ts/level/message) — see post() note.
        try:
            from systemu.interface.event_bus import EventBus
            EventBus.get().publish({
                "ts": decision.resolved_at.isoformat(),
                "level": "INFO",
                "message": f"Resolved: {decision.title}",
                "category": "operator_decision_resolved",
                "context": {
                    "decision_id": decision.id,
                    "choice": choice,
                    "chat_submission_id": (decision.context or {}).get("chat_submission_id"),
                },
            })
        except Exception:
            pass
        return decision

    def consume_resolved_choice(self, dedup_key: str) -> Optional[str]:
        """Atomically read AND retire the newest RESOLVED choice for a dedup_key.

        v0.9.32 (D.4 review FIX-2): a one-shot "Approve once" must be SINGLE-USE.
        ``get_resolved_choice`` is non-consuming — it returns the newest resolved
        choice forever, so a LATER identical command (same dedup_key) replayed a
        stale "Approve once" and auto-ran without fresh consent (fail-OPEN). The
        gate now CONSUMES the decision when it honors it: this flips its status
        from ``resolved`` -> ``consumed`` so ``get_resolved_choice`` (which only
        returns ``status == "resolved"``) no longer sees it, and the next
        identical command RE-ASKS.

        Returns the consumed choice string, or None if there was no resolved
        decision to consume. "Always allow" never reaches here — it persists in
        the CommandApprovalStore and is checked before the one-shot bypass.
        """
        if not dedup_key:
            return None
        try:
            headers = self._vault.load_index("decisions") or []
        except Exception:
            logger.exception("[DecisionQueue] could not load decisions index")
            return None

        for h in sorted(
            headers,
            key=lambda x: x.get("created_at") or "",
            reverse=True,
        ):
            if h.get("dedup_key") != dedup_key:
                continue
            if h.get("status") != "resolved":
                continue
            try:
                decision = self._vault.get_decision(h["id"])
            except Exception as exc:
                logger.warning(
                    "[DecisionQueue] consume: resolved header %r (dedup_key=%r) "
                    "has no loadable body — skipping. Error: %r",
                    h.get("id"), dedup_key, exc,
                )
                continue
            choice = decision.choice
            decision.status = "consumed"
            decision.resolved_at = datetime.now(tz=timezone.utc)
            try:
                self._vault.save_decision(decision)
                logger.info(
                    "[DecisionQueue] consumed one-shot %s -> %r (dedup_key=%r); "
                    "a repeat command will re-ask.",
                    decision.id, choice, dedup_key,
                )
            except Exception:
                # Fail-closed: if we cannot retire the decision, do NOT report
                # it as consumed (return None) so the caller re-gates rather
                # than risk a future replay of a choice we failed to expire.
                logger.warning(
                    "[DecisionQueue] could not retire consumed one-shot %s "
                    "(dedup_key=%r) — re-gating to stay fail-closed.",
                    decision.id, dedup_key, exc_info=True)
                return None
            return choice
        return None

    def expire_by_dedup_key(self, dedup_key: str) -> bool:
        """Mark a PENDING decision (by dedup key) as expired so it drops out of
        list_pending()/the Inbox. Idempotent: returns False if none pending.

        Used by the recovery reconciler when a diagnosed action self-heals — the
        Inbox row is no longer actionable, so it is retired (not resolved: the
        operator never chose anything). Mirrors ``resolve``'s save call, the
        now-helper, and the EventBus publish shape (a best-effort emit that
        never blocks the vault-saved status change)."""
        decision = self._find_pending_by_dedup_key(dedup_key)
        if decision is None:
            return False
        decision.status = "expired"
        decision.resolved_at = datetime.now(tz=timezone.utc)
        self._vault.save_decision(decision)
        logger.info(
            "[DecisionQueue] expired %s (dedup_key=%r) — diagnosed action self-healed",
            decision.id, dedup_key,
        )
        try:
            from systemu.interface.event_bus import EventBus
            EventBus.get().publish({
                "ts": decision.resolved_at.isoformat(),
                "level": "INFO",
                "message": f"Expired: {decision.title}",
                "category": "operator_decision_expired",
                "context": {
                    "decision_id": decision.id,
                    "dedup_key": decision.dedup_key,
                    "chat_submission_id": (decision.context or {}).get("chat_submission_id"),
                },
            })
        except Exception:
            pass
        return True

    # ── Private helpers ──────────────────────────────────────────────

    def _find_pending_by_dedup_key(self, dedup_key: str) -> Optional[OperatorDecision]:
        try:
            headers = self._vault.load_index("decisions") or []
        except Exception:
            return None
        for h in headers:
            if h.get("dedup_key") == dedup_key and h.get("status") == "pending":
                try:
                    return self._vault.get_decision(h["id"])
                except Exception:
                    continue
        return None
