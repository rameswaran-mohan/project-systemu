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

import contextvars
import json
import logging
import os
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple, TYPE_CHECKING

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from systemu.vault.vault import Vault


_VALID_STATUS = ("pending", "resolved", "expired")

# IMPL-11: the reason stamped on every card a run cancellation withdraws
# (``context["expired_reason"]``). The run coordinates a card context carries are
# execution_id / activity_id / chat_submission_id — no card carries a
# root_execution_id; a resume mints a NEW execution_id but keeps the activity_id
# and the chat_submission_id, so those two are what span the whole run.
RUN_CANCELLED_REASON = "run cancelled"

# IMPL-11: what an answer to such a card gets back, on every surface (dashboard,
# CLI, phone). ASCII, a verdict string.
LATE_ANSWER_NOTICE = "That run was cancelled; this answer was not needed."


# ── R-P3b: the surface that resolved a card ──────────────────────────────────
#
# ``resolve`` is reached from a dozen surfaces and none of them said which one it
# was, so the ledger could only ever read "not recorded" for the resolving channel.
# Each resolve CALL SITE now names itself with ``resolution_channel(...)``, and both
# resolving paths stamp it into the decision's context (``resolved_via``) in the SAME
# save that resolves it. An ambient value rather than a new keyword: callers (and
# their test doubles) keep the exact ``resolve(id, choice=...)`` signature. Values:
# "dashboard", "cli", the phone bridge's channel name (e.g. "telegram"), and
# "auto:policy" for the gate-mode policy. No value set -> nothing is stamped.
_RESOLUTION_CHANNEL: "contextvars.ContextVar[Optional[str]]" = contextvars.ContextVar(
    "systemu_resolution_channel", default=None)
RESOLVED_VIA_KEY = "resolved_via"
CONSUMED_AT_KEY = "consumed_at"


@contextmanager
def resolution_channel(channel: str):
    """Name the surface resolving cards inside this block (see above)."""
    token = _RESOLUTION_CHANNEL.set(channel if (type(channel) is str and channel) else None)
    try:
        yield
    finally:
        _RESOLUTION_CHANNEL.reset(token)


def _stamp_resolution_channel(decision) -> None:
    channel = _RESOLUTION_CHANNEL.get()
    if type(channel) is str and channel:
        decision.context = {**(decision.context or {}), RESOLVED_VIA_KEY: channel}


class AnswerNotNeeded(ValueError):
    """Raised by ``resolve`` / ``resolve_with_context_patch`` for an answer to a
    card whose run was cancelled. Nothing was recorded. ``str()`` is
    :data:`LATE_ANSWER_NOTICE`; a ``ValueError`` so every caller that already
    reports a refused answer (the CLI, the dashboard toasts, the phone bridge)
    shows the notice instead of crashing.

    The refusal is NOT the fence: the card's persisted ``status="expired"`` is,
    and a caller that swallows this exception still leaves nothing resolved for
    the resume rail to act on (DEC-32)."""

    def __init__(self, decision_id: str = ""):
        super().__init__(LATE_ANSWER_NOTICE)
        self.decision_id = decision_id


def was_expired_by_cancel(decision: Any) -> bool:
    """True when ``decision`` is a card a run cancellation withdrew: status
    ``expired`` AND the persisted ``expired_reason`` is ``run cancelled``.
    Concrete types pinned before comparing (DEC-36) — both values are off disk."""
    status = getattr(decision, "status", None)
    context = getattr(decision, "context", None)
    if type(status) is not str or status != "expired" or type(context) is not dict:
        return False
    reason = context.get("expired_reason")
    return type(reason) is str and reason == RUN_CANCELLED_REASON


def record_late_answer(decision: Any, *, channel: str = "") -> str:
    """Write the operator-readable event for an answer that arrived after its run
    was cancelled, and return the notice to show the answerer.

    ONE writer for every surface: the queue's resolve paths call it, and so does
    the phone bridge when it refuses a tap on such a card before reaching
    ``resolve``. Records nothing on the decision. Best-effort: an event-log
    failure never turns the refusal into a crash."""
    try:
        from systemu.interface.notifications import log_event
        ctx = {"decision_id": getattr(decision, "id", ""),
               "title": getattr(decision, "title", "")}
        if channel:
            ctx["channel"] = channel
        log_event("INFO", "approval", LATE_ANSWER_NOTICE, ctx)
    except Exception:
        logger.debug("[DecisionQueue] could not write the late-answer event",
                     exc_info=True)
    return LATE_ANSWER_NOTICE


# ── IMPL-11 ruling 3: the durable cancelled-run marker ─────────────────────────
#
# An answer given BEFORE a cancel leaves a RESOLVED card behind, which the
# cancellation cannot expire (it only retires pending cards) - so without a record
# of the cancel itself, the resume reconciler would re-dispatch the stopped run on
# its next poll. Every cancel records the run here, at cancel time, before any card
# is touched. Keyed on what the reconciler can check against a card: execution id,
# chat submission, root execution id. NEVER the activity - a later re-run of the
# same task shares it and must still resume.
#
# Store: ``<vault root>/cancelled_runs.json`` = {"version": 1, "runs": [
#   {"at": iso, "execution_id": [...], "chat_submission_id": [...],
#    "root_execution_id": [...]}, ...]}  - newest last, the newest
# CANCELLED_RUNS_RETENTION entries kept. LOCK+A: _CANCELLED_RUNS_LOCK around the
# read-modify-write, then tmp + os.replace. Writers are daemon threads only (the
# supervisor's cancel finalizer, the chat Stop); see docs/CONC-MAP.md.
CANCELLED_RUNS_RETENTION = 1000
_CANCELLED_RUNS_FILE = "cancelled_runs.json"
_CANCELLED_RUNS_LOCK = threading.Lock()
MARKER_KEYS = ("execution_id", "chat_submission_id", "root_execution_id")


def _cancelled_runs_path(vault: Any) -> Optional[Path]:
    root = getattr(vault, "root", None)
    if root is None:
        return None
    try:
        return Path(root) / _CANCELLED_RUNS_FILE
    except TypeError:
        return None


def _read_cancelled_runs(path: Path) -> List[Dict[str, Any]]:
    """The recorded runs, oldest first. A missing file is no cancels; a damaged one
    is logged LOUD and read as no cancels (the writer replaces it whole on the next
    cancel) - it must never turn a cancel or a reconcile tick into a crash."""
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("[DecisionQueue] cancelled-run marker %s is unreadable - "
                       "treating it as empty until the next cancel rewrites it",
                       path, exc_info=True)
        return []
    runs = data.get("runs") if type(data) is dict else None
    return [r for r in runs if type(r) is dict] if type(runs) is list else []


def record_cancelled_run(vault: Any, marks: Dict[str, Iterable[str]]) -> bool:
    """Durably record one cancelled run's coordinates (``MARKER_KEYS`` only).
    Returns True when written. Best-effort: never raises."""
    entry: Dict[str, Any] = {"at": datetime.now(tz=timezone.utc).isoformat()}
    for key in MARKER_KEYS:
        values = sorted({v for v in (marks.get(key) or ())
                         if type(v) is str and v})
        if values:
            entry[key] = values
    if len(entry) == 1:
        return False
    path = _cancelled_runs_path(vault)
    if path is None:
        logger.debug("[DecisionQueue] no vault root - cancelled-run marker not recorded")
        return False
    try:
        with _CANCELLED_RUNS_LOCK:
            runs = _read_cancelled_runs(path) + [entry]
            runs = runs[-CANCELLED_RUNS_RETENTION:]
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps({"version": 1, "runs": runs}, sort_keys=True),
                           encoding="utf-8")
            os.replace(tmp, path)
        return True
    except Exception:
        logger.warning("[DecisionQueue] could not record the cancelled-run marker",
                       exc_info=True)
        return False


def cancelled_run_coordinates(vault: Any) -> Set[Tuple[str, str]]:
    """``{(key, value)}`` over every recorded cancelled run (``MARKER_KEYS``)."""
    path = _cancelled_runs_path(vault)
    if path is None:
        return set()
    out: Set[Tuple[str, str]] = set()
    for run in _read_cancelled_runs(path):
        for key in MARKER_KEYS:
            values = run.get(key)
            if type(values) is not list:
                continue
            out.update((key, v) for v in values if type(v) is str and v)
    return out


def run_was_cancelled(context: Any, marks: Set[Tuple[str, str]]) -> bool:
    """True when a card's run is in the cancelled-run marker (``marks`` from
    :func:`cancelled_run_coordinates`) or the card itself carries the persisted
    cancellation reason. A card's execution id may be the run's ROOT (the root
    execution's own cards), so execution and root ids are checked both ways."""
    if type(context) is not dict:
        return False
    own = context.get("expired_reason")
    if type(own) is str and own == RUN_CANCELLED_REASON:
        return True
    chat = context.get("chat_submission_id")
    if type(chat) is str and chat and ("chat_submission_id", chat) in marks:
        return True
    for key in ("execution_id", "root_execution_id"):
        value = context.get(key)
        if type(value) is str and value and (
                ("execution_id", value) in marks
                or ("root_execution_id", value) in marks):
            return True
    return False


def _metrics(vault: "Vault"):
    """Best-effort ``MetricsStore`` bound to this vault's root.

    S1b (PLAN-11) approval-fatigue counters. Callers MUST wrap every use of
    the returned store — INCLUDING this constructor call — in try/except: a
    metrics failure (construction error, ``vault.root`` unusable, or a store
    write error) must never break decision posting/resolving. This function
    does NOT swallow construction errors itself; it lets them propagate to the
    caller's try/except, which every current call site has.
    """
    from pathlib import Path as _Path

    from systemu.runtime.metrics_store import MetricsStore
    return MetricsStore(_Path(vault.root) / "metrics")


# IMPL-11 ruling 2: which cards ``activity_id`` ALONE may match. A RUN posts these
# while it executes (they carry its coordinates via the run's contextvars), so a
# card of this type naming the activity belongs to a run of it. Every other card
# that names an activity is ABOUT the activity - its tools, its scroll, its shadow -
# and outlives any one run (approving "Task blocked -- N tool(s) not ready" enables
# tools for every future run), so one run's cancel never withdraws it on the
# activity alone. Every gate type in the tree must sit in exactly one of the two
# gate sets (pinned by tests/test_impl11_activity_scoped_cards.py).
RUN_SCOPED_GATE_TYPES = frozenset({
    "tool", "command", "harness", "mcp_call", "mcp_oauth", "sampling", "operator",
})
ACTIVITY_SCOPED_GATE_TYPES = frozenset({
    "tools_blocked", "forge", "dep", "scroll", "evolution", "recovery", "tool_bulk",
})
RUN_SCOPED_KINDS = frozenset({"structured_question"})


def _is_run_scoped(context: Dict[str, Any]) -> bool:
    """True for a card a run posts while it executes (allowlist: an unknown or
    absent kind is NOT run-scoped). Types pinned before membership (DEC-36)."""
    kind = context.get("kind")
    if type(kind) is not str:
        return False
    if kind in RUN_SCOPED_KINDS:
        return True
    gate_type = context.get("gate_type")
    return (kind == "gate" and type(gate_type) is str
            and gate_type in RUN_SCOPED_GATE_TYPES)


def _belongs_to_run(context: Any, run: Dict[str, str], activity_id: str) -> bool:
    """A pending card belongs to the cancelled run when it carries one of the run's
    own coordinates (execution / chat submission), or names the run's activity AND
    is a run-scoped card type."""
    if type(context) is not dict:
        return False
    if _carries_run_coordinate(context, run):
        return True
    if not activity_id:
        return False
    got = context.get("activity_id")
    return (type(got) is str and got == activity_id) and _is_run_scoped(context)


def _carries_run_coordinate(context: Any, wanted: Dict[str, str]) -> bool:
    """True when ``context`` (a card context off disk) carries any of ``wanted``'s
    run coordinates. ``type(x) is T`` before any comparison (DEC-36)."""
    if type(context) is not dict:
        return False
    for key, value in wanted.items():
        got = context.get(key)
        if type(got) is str and got and got == value:
            return True
    return False


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
        # R-P1 SEC-1: stamp a persisted, fail-closed remote-resolvability class
        # at the decision-creation chokepoint. ALLOWLIST-style: default "floor";
        # only positively-recognized safe shapes become "remotely_resolvable".
        # Additive (decision.context is a fresh dict) and persisted (it rides in
        # to_dict()'s "context"). A caller that already stamped a class is NOT
        # overwritten — a caller can only tighten, and a resumed/re-posted
        # decision must never be silently upgraded. Import is lazy to avoid any
        # approval<->messaging import cycle; a failure fails CLOSED to "floor".
        if "resolution_class" not in decision.context:
            try:
                from systemu.messaging.decision_bridge import classify_resolution
                decision.context["resolution_class"] = classify_resolution(decision.context)
            except Exception:
                logger.warning(
                    "[DecisionQueue] classify_resolution failed for %s — "
                    "defaulting resolution_class=floor (fail-closed).",
                    decision.id, exc_info=True)
                decision.context["resolution_class"] = "floor"
        self._vault.save_decision(decision)
        logger.info(
            "[DecisionQueue] posted '%s' (id=%s, dedup_key=%r, options=%s)",
            title, decision.id, dedup_key, options,
        )
        # S1b (PLAN-11): count a newly-CREATED card. Only reached on the
        # non-dedup branch above (the dedup short-circuit returns earlier),
        # so a re-attempted command that collapses onto an existing pending
        # card is never double-counted. Best-effort: never break posting.
        try:
            kind = (decision.context or {}).get("kind")
            if kind == "gate":
                _metrics(self._vault).incr("gate_cards_created")
            elif kind == "structured_question":
                _metrics(self._vault).incr("asks_created")
        except Exception:
            logger.debug("[DecisionQueue] metrics incr (created) failed", exc_info=True)
        # v0.8.22 (C): emit EventBus event so chat UI can render an inline card.
        # W5.3: self-describing event — top-level ts/level/message so EVERY
        # pane (incl. the right-rail Live stream, which reads event["message"])
        # renders it; the bare-context shape drew a blank "[INFO] " line.
        try:
            from systemu.interface.event_bus import EventBus
            # R-P1: enrich the event so the Telegram push (event_pusher) can
            # render a [tag] + inline option buttons WITHOUT a vault re-read on
            # the pure-translator path. ``tag`` is the short deterministic
            # decision tag; ``decision_context`` carries the persisted
            # resolution_class / gate_type / requested_schema / body that
            # render_options keys on. Computed best-effort — a tag/enrich failure
            # falls back to the plain "open the dashboard" push.
            try:
                from systemu.messaging.decision_bridge import (
                    decision_tag, disambiguate_tag,
                )
                open_tags = {
                    decision_tag(d.id)
                    for d in self.list_pending()
                    if d.id != decision.id
                }
                tag = disambiguate_tag(decision.id, open_tags)
            except Exception:
                tag = ""
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
                    "tag": tag,
                    # The persisted decision context (resolution_class etc.) plus
                    # the body, so render_options + full-detail push have what
                    # they need off the event alone.
                    "decision_context": {
                        **dict(decision.context or {}),
                        "body": decision.body,
                    },
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
        decision = self.get_resolved_decision(dedup_key)
        return decision.choice if decision is not None else None

    def get_resolved_decision(self, dedup_key: str) -> Optional[OperatorDecision]:
        """The newest RESOLVED decision for a dedup_key -- choice AND context --
        WITHOUT consuming it, or None. The non-consuming sibling of
        :meth:`consume_resolved_decision`; :meth:`get_resolved_choice` is its label.

        P21: the MCP gate reads a typed "Approve once" here to check that it covers
        THIS call before it consumes anything, so a decision about a different call
        on the same params-independent dedup key is never spent by this one.
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
            return decision
        return None

    def resolve(self, decision_id: str, *, choice: str) -> OperatorDecision:
        """Mark a decision resolved with the operator's chosen option.

        Raises ValueError if the choice is not in the decision's options.
        """
        try:
            decision = self._vault.get_decision(decision_id)
        except Exception as exc:
            raise KeyError(f"OperatorDecision {decision_id} not found: {exc}")
        self._refuse_if_run_cancelled(decision)
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
        _stamp_resolution_channel(decision)
        self._vault.save_decision(decision)
        logger.info(
            "[DecisionQueue] resolved %s -> %r (dedup_key=%r)",
            decision_id, choice, decision.dedup_key,
        )
        self._record_resolution(decision)
        self._publish_resolved(decision)
        return decision

    def resolve_with_context_patch(self, decision_id: str, *, choice: str,
                                   context_patch: dict) -> "OperatorDecision":
        """Atomically merge ``context_patch`` into the decision context AND resolve
        it in a single reload+save. Avoids the write-then-resolve lost update
        (``resolve`` reloads a fresh decision and would discard a separately
        mutated copy). Used by the amend-then-approve flow to stamp ``amended_spec``
        (and any band-escalation record) before resolving as "Approve".
        """
        try:
            decision = self._vault.get_decision(decision_id)
        except Exception as exc:
            raise KeyError(f"OperatorDecision {decision_id} not found: {exc}")
        self._refuse_if_run_cancelled(decision)
        # Same membership contract as resolve(): capability gates carry
        # options=["Deny","Approve"], so choice="Approve" passes the strict check.
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
        decision.context = {**(decision.context or {}), **(context_patch or {})}
        decision.status = "resolved"
        decision.choice = choice
        decision.resolved_at = datetime.now(tz=timezone.utc)
        _stamp_resolution_channel(decision)
        self._vault.save_decision(decision)
        logger.info("[DecisionQueue] resolved+patched %s -> %r (keys=%s)",
                    decision_id, choice, sorted((context_patch or {}).keys()))
        self._record_resolution(decision)
        # IMPL-2 (LOW): publish the SAME event ``resolve`` does. This path resolves a
        # decision every bit as much as its sibling, but published nothing — so the
        # in-process EventBus fast path (resume_on_decision's subscriber) never fired
        # for it and every reclassify waited on the ~15s reconciler poll instead. Safe
        # for the other caller (amend-then-approve): ``_dispatch_resume`` returns False
        # for anything that is not a tool/command gate, an operator-attest card or a
        # structured_question, and ``event_pusher`` drops this category outright, so no
        # subscriber gains a new side effect.
        self._publish_resolved(decision)
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

        IMPL-2: this is the CHOICE-ONLY view of :meth:`consume_resolved_decision`.
        A caller that must establish WHICH CARD the choice came from — the tool gate,
        while an operator reclassification is pending — cannot do it from a label and
        calls the sibling instead. Kept as its own method so the callers that only ever
        needed the label (MCP dispatch, the sampling gate, the command gate) are
        byte-for-byte unchanged.
        """
        decision = self.consume_resolved_decision(dedup_key)
        return decision.choice if decision is not None else None

    def consume_resolved_decision(self, dedup_key: str) -> Optional[OperatorDecision]:
        """Atomically read AND retire the newest RESOLVED decision for a dedup_key,
        returning the DECISION — choice *and* context — rather than just the label.

        IMPL-2 (adversarial review, CRITICAL). A gate dedup key is
        params-INDEPENDENT (``tool:<sig>`` covers every call to one tool body), and a
        resolved decision row is never retired by the ordinary approve→resume flow:
        the dispatcher mints a ``resume_pending`` bridge, the run resumes through the
        BRIDGE, and the row sits at ``status="resolved"`` indefinitely. So "the newest
        non-deny choice on this dedup key" is NOT evidence that the operator approved
        the call in front of the gate — it may be a leftover from an unrelated benign
        call on the same signature. Deciding that needs the card's CONTEXT
        (``reclassified`` / ``assigned_class`` / ``args_fingerprint``), which the
        string return could not express.

        Consumption semantics are identical to :meth:`consume_resolved_choice` (it is
        implemented in terms of this method): single-use, fail-CLOSED if the retiring
        save fails, newest-first by ``created_at``.
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
            # R-P3b: the resolution time is the operator's decision and is KEPT; the
            # consumption is its own fact. (This used to overwrite resolved_at.)
            decision.context = {**(decision.context or {}),
                                CONSUMED_AT_KEY: datetime.now(tz=timezone.utc).isoformat()}
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
            return decision
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

    def expire_cards_for_run(
        self,
        *,
        execution_id: Optional[str] = None,
        activity_id: Optional[str] = None,
        chat_submission_id: Optional[str] = None,
        decision_id: Optional[str] = None,
        root_execution_id: Optional[str] = None,
    ) -> List[str]:
        """IMPL-11: withdraw every PENDING card that belongs to a cancelled run.

        THE one place a run cancellation retires its decision cards. Called from
        the supervisor's CANCELLED finalizer (``Supervisor._expire_cards_on_cancel``,
        the sink of request_cancel / request_cancel_by_activity / a run that
        finalizes ``cancelled`` itself) and from the chat Stop
        (``chat_task_registry._withdraw_gate``, with the row's coordinates and
        the exact parked card).
        A card belongs to the run when its context carries one of the run's own
        coordinates (``execution_id`` / ``chat_submission_id``), when it names the
        run's ``activity_id`` AND is a run-scoped card type (``_is_run_scoped`` —
        an activity-scoped card such as ``tools_blocked`` is never withdrawn on the
        activity alone), or when it IS ``decision_id`` (the card a caller knows the
        run is parked on, even if that card carries no coordinates). Each match becomes
        ``status="expired"`` with ``context["expired_reason"] = "run cancelled"``
        — the persisted witness a late answer and the resume reconciler key off.

        FIRST it records the run in the durable cancelled-run marker
        (:func:`record_cancelled_run`: execution / chat submission / root, plus each
        expired card's own execution and chat submission), so an answer given
        BEFORE the cancel - a resolved card this function cannot expire - never
        resumes the run either (IMPL-11 ruling 3; the reconciler consults it).
        ``root_execution_id`` is a marker coordinate only: no card carries it.

        No coordinate given -> nothing expires (never "match all"). Coordinates
        are compared only after their concrete type is pinned to a non-empty
        ``str`` in this frame: a card context is data off disk (DEC-36).
        Idempotent. Returns the ids it expired.
        """
        run = {}
        for key, value in (("execution_id", execution_id),
                           ("chat_submission_id", chat_submission_id)):
            if type(value) is str and value:
                run[key] = value
        activity = activity_id if (type(activity_id) is str and activity_id) else ""
        exact = decision_id if (type(decision_id) is str and decision_id) else ""
        if not run and not activity and not exact:
            return []
        matched = [d for d in self.list_pending()
                   if d.id == exact or _belongs_to_run(d.context, run, activity)]
        marks: Dict[str, set] = {key: set() for key in MARKER_KEYS}
        for key, value in run.items():
            marks[key].add(value)
        if type(root_execution_id) is str and root_execution_id:
            marks["root_execution_id"].add(root_execution_id)
        for decision in matched:
            ctx = decision.context if type(decision.context) is dict else {}
            for key in ("execution_id", "chat_submission_id"):
                value = ctx.get(key)
                if type(value) is str and value:
                    marks[key].add(value)
        record_cancelled_run(self._vault, marks)
        expired: List[str] = []
        for decision in matched:
            decision.status = "expired"
            decision.resolved_at = datetime.now(tz=timezone.utc)
            decision.context = {**(decision.context or {}),
                                "expired_reason": RUN_CANCELLED_REASON}
            self._vault.save_decision(decision)
            expired.append(decision.id)
            logger.info("[DecisionQueue] expired %s (dedup_key=%r) — run cancelled",
                        decision.id, decision.dedup_key)
            self._publish_expired(decision, reason=RUN_CANCELLED_REASON)
        return expired

    # ── Private helpers ──────────────────────────────────────────────

    @staticmethod
    def _refuse_if_run_cancelled(decision: "OperatorDecision") -> None:
        """IMPL-11: an answer to a card whose run was cancelled records nothing.
        Runs BEFORE any mutation or save in both resolving paths: no status
        change, no choice, no resolved event (so neither resume path fires), no
        approval metric — only the operator-readable event, then the notice."""
        if was_expired_by_cancel(decision):
            record_late_answer(decision)
            raise AnswerNotNeeded(decision.id)

    def _publish_expired(self, decision: "OperatorDecision", *, reason: str) -> None:
        """Best-effort ``operator_decision_expired`` event (same shape as
        ``expire_by_dedup_key``'s, plus the reason) so the chat inline card and
        the Needs-you rail drop the withdrawn card. Never blocks the saved status."""
        try:
            from systemu.interface.event_bus import EventBus
            EventBus.get().publish({
                "ts": decision.resolved_at.isoformat() if decision.resolved_at else "",
                "level": "INFO",
                "message": f"Expired: {decision.title}",
                "category": "operator_decision_expired",
                "context": {
                    "decision_id": decision.id,
                    "dedup_key": decision.dedup_key,
                    "reason": reason,
                    "chat_submission_id": (decision.context or {}).get("chat_submission_id"),
                },
            })
        except Exception:
            pass

    def _publish_resolved(self, decision: "OperatorDecision") -> None:
        """v0.8.22 (C): emit the EventBus event so the chat UI hides the inline card and
        the daemon's resume subscriber can re-dispatch a parked run without waiting for
        the reconciler poll. W5.3: self-describing (ts/level/message) — see post().

        ONE publisher for BOTH resolving paths (``resolve`` and
        ``resolve_with_context_patch``), so a subscriber never has to care which one ran.
        Best-effort: EventBus is optional and must never block a vault-saved resolution.
        """
        try:
            from systemu.interface.event_bus import EventBus
            EventBus.get().publish({
                "ts": decision.resolved_at.isoformat() if decision.resolved_at else "",
                "level": "INFO",
                "message": f"Resolved: {decision.title}",
                "category": "operator_decision_resolved",
                "context": {
                    "decision_id": decision.id,
                    "choice": decision.choice,
                    "chat_submission_id": (decision.context or {}).get("chat_submission_id"),
                },
            })
        except Exception:
            pass

    def _record_resolution(self, decision: "OperatorDecision") -> None:
        """S1b (PLAN-11): record a resolution against the approval-fatigue
        metrics store. Called from BOTH ``resolve`` and
        ``resolve_with_context_patch`` (the two decision-resolving paths) so
        the single-writer invariant holds — ``consume_resolved_choice``
        (resolved -> consumed) and ``expire_by_dedup_key`` (pending ->
        expired) are NOT resolutions and must never call this.

        Best-effort: a metrics failure must never break decision resolving.
        """
        try:
            kind = (decision.context or {}).get("kind")
            if kind == "gate":
                created_at = decision.created_at
                resolved_at = decision.resolved_at
                latency_ms = (
                    (resolved_at - created_at).total_seconds() * 1000
                    if created_at is not None and resolved_at is not None
                    else 0.0
                )
                ts = resolved_at.timestamp() if resolved_at is not None else 0.0
                _metrics(self._vault).record_resolution(
                    latency_ms, ts=ts, choice=decision.choice or "")
            elif kind == "structured_question":
                _metrics(self._vault).incr("asks_resolved")
        except Exception:
            logger.debug("[DecisionQueue] metrics record_resolution failed", exc_info=True)

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
