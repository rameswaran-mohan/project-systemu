"""DEC-13 -- the ONE mint for the verification badge (DEC-43).

Every outcome the operator sees says how far it is confirmed:

  * ``verified``     -- an independent read-back confirmed the effect (a receipt);
  * ``claimed``      -- the agent or the tool says so; nothing independent
                        confirmed it. Missing evidence is ALWAYS claimed;
  * ``measured``     -- the S4 shadow meter's record-only measurement. It credits
                        nothing, so it is never ``verified``;
  * ``failed``       -- the run ended without reaching its goal;
  * ``handoff``      -- the run stopped and handed back to the operator; what it
                        already committed is listed, each effect badged (IMPL-7);
  * ``not_recorded`` -- the evidence record exists but could not be read. Its own
                        state (DEC-27 / DEC-37): never verified, never a blank.

A status that claims no outcome yet (running, queued, a scroll awaiting approval)
mints ``no_outcome``, which every surface renders as nothing.

**One mint, many surfaces.** The inputs -- a run's status string and its durable
receipts (``receipts_store.read_receipts_witnessed``) -- are PRIVATE to this
module. A surface calls :func:`run_badge` (or :func:`effect_record` for the
per-effect ledger) and renders the VALUE it gets back through :func:`view` /
:func:`badge_text`; it never reads a receipt's confirmation bits and never spells
a state token itself. ``tools/lint_verification_badge.py`` fails a surface file
that does; ``tests/test_dec13_badge_surface_agreement.py`` walks every surface in
:data:`SURFACES` over the fixture matrix.

**The render fence (DEC-36 / DEC-37).** :func:`view` checks ``type(x) is
RunBadge`` / ``type(x) is EffectBadge`` before touching the value; anything else
renders ``not_recorded``. A surface handed a proxy -- a dict, a string, ``None``
-- cannot render the filled check.

Display text that carries persisted data (a receipt's note or method) is masked
through ``durable_redact.mask_text``, which composes the evidence mask and the
outbound mask and so reaches the shared span primitive
(``gateway.mask_secret_spans``). Operator copy here is ASCII-only (DEC-32c).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Tuple

# ── the vocabulary ───────────────────────────────────────────────────────────

VERIFIED = "verified"
CLAIMED = "claimed"
MEASURED = "measured"
FAILED = "failed"
HANDOFF = "handoff"
NOT_RECORDED = "not_recorded"
NO_OUTCOME = "no_outcome"

#: Every state a badge can render (``no_outcome`` renders nothing).
BADGE_STATES = (VERIFIED, CLAIMED, MEASURED, FAILED, HANDOFF, NOT_RECORDED)
STATES = BADGE_STATES + (NO_OUTCOME,)

#: The spellings that belong to this badge alone. ``failed`` is also a run status
#: and ``not_recorded`` is also the ledger's DEC-27 cell, so the lint cannot tell
#: a re-derivation from a status check by spelling for those two; the agreement
#: test covers them instead.
VERDICT_TOKENS = frozenset({VERIFIED, CLAIMED, MEASURED, HANDOFF})

#: The receipt keys only this mint may read (its private inputs).
INPUT_KEYS = frozenset({"confirmed", "shadow"})

#: The filled check. DEC-13: it is the verified badge's icon and nothing else's.
VERIFIED_ICON = "check_circle"

# where a run's receipt record stands
RECORD_PRESENT = "present"
RECORD_ABSENT = "absent"
RECORD_UNREADABLE = "unreadable"
RECORD_NO_RUN = "no_run"

# ── run status classes (an open set across the chat log, scrolls, activities) ──

_DONE_STATUSES = frozenset({"success", "completed", "done", "executed"})
_FAILED_STATUSES = frozenset({"failed", "failure", "error", "extraction_failed",
                              "skipped_no_shadow"})
_HANDOFF_STATUSES = frozenset({"partial", "pending_decision", "needs_input",
                               "waiting_on_tools", "spend_cap_reached", "cancelled"})
_HANDOFF_PREFIX = "suspended"
_DONE = "done"


def _status_class(status: Any) -> str:
    """The outcome a status string claims. Not a string: not recorded."""
    if type(status) is not str:
        return NOT_RECORDED
    s = status.strip().lower()
    if s in _DONE_STATUSES:
        return _DONE             # the evidence decides how far it is confirmed
    if s in _FAILED_STATUSES:
        return FAILED
    if s in _HANDOFF_STATUSES or s.startswith(_HANDOFF_PREFIX):
        return HANDOFF
    return NO_OUTCOME


# ── the values ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class EffectBadge:
    """One recorded external effect (one objective's receipt), minted.

    ``method`` / ``detail`` / ``stamped_at`` are the persisted values as read --
    raw, for the ledger's fingerprint and its masked raw-beside column. Never
    render them directly: :func:`view`, :func:`effect_lines` and the receipt panel
    mask them first."""
    state: str
    objective_id: str
    method: Any = None
    detail: Any = None
    stamped_at: Any = None
    #: the writer said, explicitly, that this was a live verification (not the
    #: shadow meter). A receipt written before that flag existed says neither.
    live_marked: bool = False
    #: for a measurement: whether it WOULD have credited.
    would_credit: bool = False
    reason: str = ""


@dataclass(frozen=True)
class EffectRecord:
    """A run's durable receipt record, read once and minted per effect."""
    record: str
    reason: str = ""
    effects: Tuple[EffectBadge, ...] = ()

    def by_objective(self) -> Dict[str, EffectBadge]:
        return {e.objective_id: e for e in self.effects}


@dataclass(frozen=True)
class RunBadge:
    """The verification badge of one run's outcome."""
    state: str
    status: str = ""
    execution_id: str = ""
    record: str = RECORD_NO_RUN
    reason: str = ""
    effects: Tuple[EffectBadge, ...] = ()

    def count(self, state: str) -> int:
        return sum(1 for e in self.effects if e.state == state)


# ── the mint ─────────────────────────────────────────────────────────────────

def mint_effect(objective_id: Any, receipt: Any) -> EffectBadge:
    """Mint ONE effect's badge from its persisted receipt (the durable store or
    the live in-run store -- both carry the same keys).

    The receipt is data off disk: its type is pinned before any use (DEC-36), and
    only the real bool ``True`` counts (``1`` / ``"yes"`` do not)."""
    oid = objective_id if type(objective_id) is str else str(objective_id)
    if type(receipt) is not dict:
        return EffectBadge(state=NOT_RECORDED, objective_id=oid,
                           reason="this receipt entry is not a record")
    shadow = receipt.get("shadow")
    confirmed = receipt.get("confirmed")
    common = dict(objective_id=oid, method=receipt.get("method"),
                  detail=receipt.get("detail"), stamped_at=receipt.get("stamped_at"),
                  live_marked=shadow is False)
    if shadow is True:
        return EffectBadge(state=MEASURED, would_credit=confirmed is True, **common)
    if confirmed is True:
        return EffectBadge(state=VERIFIED, **common)
    return EffectBadge(state=CLAIMED, **common)


def mint_unreceipted(objective_id: Any) -> EffectBadge:
    """Mint the badge of an effect that has NO receipt on record (DEC-13).

    Missing evidence is always ``claimed``: the agent or the tool said it
    happened and nothing independent confirmed it. Never ``verified`` and never a
    blank -- a surface that holds an effect but no receipt for it asks the mint;
    it does not leave the cell empty and does not spell the state itself."""
    oid = objective_id if type(objective_id) is str else str(objective_id)
    return EffectBadge(state=CLAIMED, objective_id=oid)


def _oid_rank(oid: str) -> tuple:
    try:
        return (0, int(oid), "")
    except (TypeError, ValueError):
        return (1, 0, oid)


def mint_effects(receipts: Any) -> Tuple[EffectBadge, ...]:
    """Every entry of a ``{objective_id: receipt}`` store, minted, sorted by
    objective id (ints first). A non-dict store mints nothing."""
    if type(receipts) is not dict:
        return ()
    out = [mint_effect(k, v) for k, v in receipts.items()]
    out.sort(key=lambda e: _oid_rank(e.objective_id))
    return tuple(out)


def effect_record(execution_id: Any, *, data_dir=None) -> EffectRecord:
    """Read a run's durable receipts ONCE and mint every effect. Absent and
    unreadable are distinct: only a missing file is absent."""
    if type(execution_id) is not str or not execution_id:
        return EffectRecord(record=RECORD_NO_RUN,
                            reason="no execution id is recorded for this run")
    from systemu.runtime import receipts_store
    got = receipts_store.read_receipts_witnessed(execution_id, data_dir=data_dir)
    if got.status == receipts_store.READ_ABSENT:
        return EffectRecord(record=RECORD_ABSENT)
    if got.status != receipts_store.READ_PRESENT:
        return EffectRecord(record=RECORD_UNREADABLE,
                            reason=got.reason or "the receipt record could not be read")
    return EffectRecord(record=RECORD_PRESENT, effects=mint_effects(got.rows))


def _evidence_state(rec: EffectRecord) -> Tuple[str, str]:
    """How far a DONE outcome is confirmed, from its effect record. The weakest
    effect decides; no effect at all is claimed (missing evidence is never
    verified)."""
    if rec.record == RECORD_UNREADABLE:
        return NOT_RECORDED, rec.reason
    effects = rec.effects
    bad = sum(1 for e in effects if e.state == NOT_RECORDED)
    if bad:
        return NOT_RECORDED, (f"{bad} receipt entr{'y' if bad == 1 else 'ies'} "
                              "could not be read")
    if not effects:
        return CLAIMED, ""
    states = {e.state for e in effects}
    if states == {VERIFIED}:
        return VERIFIED, ""
    if CLAIMED in states:
        return CLAIMED, ""
    return MEASURED, ""


def badge_from_record(status: Any, rec: EffectRecord, *,
                      execution_id: str = "") -> RunBadge:
    """Pure: the run badge for a status and an already-read effect record."""
    st = status if type(status) is str else ""
    cls = _status_class(status)
    if cls == NO_OUTCOME:
        return RunBadge(state=NO_OUTCOME, status=st, execution_id=execution_id,
                        record=rec.record, reason=rec.reason, effects=rec.effects)
    if cls == NOT_RECORDED:
        return RunBadge(state=NOT_RECORDED, status=st, execution_id=execution_id,
                        record=rec.record, reason="the run's status is not recorded",
                        effects=rec.effects)
    if cls in (FAILED, HANDOFF):
        return RunBadge(state=cls, status=st, execution_id=execution_id,
                        record=rec.record, reason=rec.reason, effects=rec.effects)
    state, reason = _evidence_state(rec)
    return RunBadge(state=state, status=st, execution_id=execution_id,
                    record=rec.record, reason=reason or rec.reason, effects=rec.effects)


def run_badge(status: Any, execution_id: Any, *, data_dir=None) -> RunBadge:
    """THE mint: the verification badge of one run's outcome.

    ``status`` is the run's own recorded status; ``execution_id`` names its
    durable receipts. A status that claims no outcome yet reads nothing."""
    eid = execution_id if type(execution_id) is str else ""
    if _status_class(status) == NO_OUTCOME:
        return RunBadge(state=NO_OUTCOME, status=status, execution_id=eid)
    return badge_from_record(status, effect_record(execution_id, data_dir=data_dir),
                             execution_id=eid)


# ── presentation (the only place a state becomes copy) ───────────────────────

_LABEL = {VERIFIED: "Verified", CLAIMED: "Claimed", MEASURED: "Measured",
          FAILED: "Failed", HANDOFF: "Handoff", NOT_RECORDED: "Not recorded"}
_ICON = {VERIFIED: VERIFIED_ICON, CLAIMED: "radio_button_unchecked",
         MEASURED: "speed", FAILED: "cancel", HANDOFF: "pan_tool",
         NOT_RECORDED: "help_outline"}
_PILL = {VERIFIED: "s-pill s-pill--success", CLAIMED: "s-pill s-pill--muted",
         MEASURED: "s-pill s-pill--accent", FAILED: "s-pill s-pill--danger",
         HANDOFF: "s-pill s-pill--warn", NOT_RECORDED: "s-pill s-pill--muted"}

_METHOD_PLAIN = {
    "api_readback": "re-read from the service's API and matched the submission",
    "email_confirm": "matched a confirmation email",
    "web_assertion": "re-checked on the web page",
    "operator_attest": "attested by you",
}

_MEASURED_TIP = ("Record-only measurement by the S4 shadow meter; it did not credit "
                 "this run")


def _mask_for_display(value: Any) -> str:
    """Persisted text, masked through the shared durable mask (which reaches
    ``gateway.mask_secret_spans``). Never raises; a value that could not be
    masked comes back as the durable mask's own marker, never in clear."""
    if value is None:
        return ""
    from systemu.runtime.durable_redact import mask_text
    return mask_text(value if type(value) is str else str(value))


def _ascii(text: str) -> str:
    return text.encode("ascii", "backslashreplace").decode("ascii")


def method_text(method: Any) -> str:
    """How a receipt was checked, in plain language (masked, ASCII)."""
    if type(method) is not str or not method.strip():
        return "not recorded by this receipt"
    raw = method.strip()
    shown = _ascii(_mask_for_display(raw))
    plain = _METHOD_PLAIN.get(raw)
    return f"{plain} ({shown})" if plain else shown


def detail_text(detail: Any) -> str:
    """What a receipt checked (its persisted note), masked. ``""`` if none."""
    if type(detail) is not str or not detail.strip():
        return ""
    return _mask_for_display(detail.strip())


def objective_text(objective_id: Any) -> str:
    """An objective id for display (it came off disk: masked, ASCII)."""
    return _ascii(_mask_for_display(objective_id)) or "?"


@dataclass(frozen=True)
class BadgeView:
    """What a surface renders. Built only by :func:`view`."""
    state: str
    show: bool
    label: str
    icon: str
    pill_class: str
    tooltip: str
    text: str
    detail: str

    def as_json(self) -> Dict[str, Any]:
        return {"state": self.state, "label": self.label, "detail": self.detail,
                "tooltip": self.tooltip}


def _counts_line(b: RunBadge) -> str:
    n = len(b.effects)
    if not n:
        return ""
    return f"{b.count(VERIFIED)} of {n} recorded effect{'s' if n != 1 else ''} verified"


def _run_tooltip(b: RunBadge) -> str:
    s = b.state
    if s == VERIFIED:
        return ("Every recorded external effect of this run was independently "
                "read back - receipts, not self-report.")
    if s == CLAIMED:
        if not b.effects:
            return ("No independent read-back is on record for this run; its "
                    "outcome is the agent's own report.")
        return (f"{b.count(CLAIMED)} of {len(b.effects)} recorded effects were not "
                "independently confirmed; they are the tool's own report.")
    if s == MEASURED:
        return _MEASURED_TIP + "."
    if s == FAILED:
        return ("The run did not reach its goal. Effects it committed before it "
                "stopped are listed in the receipt.")
    if s == HANDOFF:
        return ("The run stopped and handed back to you. What it already committed "
                "is listed in the receipt, each effect badged.")
    return ("The verification record for this outcome could not be read ("
            + _ascii(b.reason or "no reason recorded")
            + "). Nothing here is shown as verified.")


def _effect_tooltip(e: EffectBadge) -> str:
    if e.state == VERIFIED:
        return (f"Independently machine-verified via {method_text(e.method)} - "
                "receipts, not self-report")
    if e.state == CLAIMED:
        return "Reported by the tool, not independently verified"
    if e.state == MEASURED:
        return _MEASURED_TIP
    return "This receipt entry could not be read; nothing is shown as verified"


_NOT_A_BADGE = "no badge was minted for this outcome"
_NONE_VIEW = BadgeView(state=NO_OUTCOME, show=False, label="", icon="",
                       pill_class="", tooltip="", text="", detail="")


def _built(state: str, tooltip: str, detail: str = "") -> BadgeView:
    tip = _ascii(tooltip)
    return BadgeView(state=state, show=True, label=_LABEL[state], icon=_ICON[state],
                     pill_class=_PILL[state], tooltip=tip,
                     text=f"{_LABEL[state]}: {tip}", detail=detail)


def view(badge: Any) -> BadgeView:
    """THE renderer's input. Only a minted value renders a state; anything else
    renders ``not_recorded`` (DEC-37) -- a proxy can never show the filled check."""
    if type(badge) is RunBadge:
        if badge.state == NO_OUTCOME:
            return _NONE_VIEW
        if badge.state not in BADGE_STATES:
            return _built(NOT_RECORDED, _run_tooltip(
                RunBadge(state=NOT_RECORDED, reason="the badge state is unknown")))
        return _built(badge.state, _run_tooltip(badge), _counts_line(badge))
    if type(badge) is EffectBadge:
        if badge.state not in (VERIFIED, CLAIMED, MEASURED):
            return _built(NOT_RECORDED, _effect_tooltip(
                EffectBadge(state=NOT_RECORDED, objective_id="")))
        return _built(badge.state, _effect_tooltip(badge))
    return _built(NOT_RECORDED, "The verification record for this outcome could not "
                                f"be read ({_NOT_A_BADGE}).")


def badge_text(badge: Any) -> str:
    """One ASCII line for a text channel (CLI, Telegram). ``""`` for no outcome."""
    v = view(badge)
    if not v.show:
        return ""
    return f"[{v.label}] {v.detail + '. ' if v.detail else ''}{v.tooltip}"


def effect_lines(badge: Any) -> Tuple[str, ...]:
    """The committed-effects list (IMPL-7) for a text channel: one masked line per
    recorded effect, each with its own badge. A record that could not be read is
    one line naming that, never an empty list that reads as "nothing was done"."""
    if type(badge) is not RunBadge or badge.state == NO_OUTCOME:
        return ()
    if badge.record == RECORD_UNREADABLE:
        return ("  committed effects: not recorded - "
                + _ascii(badge.reason or "the receipt record could not be read"),)
    out = []
    for e in badge.effects:
        what = _ascii(detail_text(e.detail)) or "no note recorded"
        out.append(f"  [{view(e).label}] objective {objective_text(e.objective_id)}: "
                   f"{what}")
    return tuple(out)


# ── the committed surface list (DEC-43: one list, walked by the agreement test) ──

#: Every operator-facing surface that renders an outcome's verification, as
#: ``(id, "path")`` or ``(id, "path::function")`` when the surface is one function
#: of a larger module. The lint scans each scope; the agreement test must hold an
#: adapter for each id.
SURFACES: Tuple[Tuple[str, str], ...] = (
    ("work_row", "systemu/interface/pages/work.py"),
    ("workflow_detail", "systemu/interface/pages/workflow_detail.py"),
    ("receipt_panel", "systemu/interface/components/receipt_panel.py"),
    ("chat_card", "systemu/interface/pages/chat_page.py"),
    ("home_recent", "systemu/interface/pages/console.py"),
    ("status_menu", "systemu/interface/components/status_menu.py"),
    ("telegram_status", "systemu/messaging/handlers.py"),
    ("telegram_push", "systemu/messaging/event_pusher.py"),
    ("cli_chat_submit", "systemu/interface/cli_commands.py::chat_submit"),
    ("cli_chat_history", "systemu/interface/cli_commands.py::chat_history"),
    ("cli_army_execute", "systemu/interface/cli_commands.py::army_execute"),
    ("task_api", "systemu/interface/task_api.py"),
    ("ledger_export", "systemu/runtime/ledger.py"),
    ("committed_effects", "systemu/runtime/committed_effects.py"),
    ("receipt_badges", "systemu/runtime/receipts_store.py::receipt_badges_for"),
)

#: Surfaces that render outcomes but are not badged by this slice, and why.
DEFERRED_SURFACES: Tuple[Tuple[str, str], ...] = (
    ("inbox_gate_outcome", "owned by a concurrent slice "
     "(systemu/interface/command/gate.py, inbox.py): badge on merge"),
    ("briefs_digests", "not built: no brief composer and no daily_digest watcher exist"),
    ("run_timeline", "not built (UX-11); the workflow detail page has a stage "
     "timeline only, which no longer uses the filled check"),
    ("live_events_feed", "renders event messages, not an outcome verdict; the "
     "task_outcome push it mirrors is badged"),
)
