"""UX-14 -- "why?" on every card (R-UX3).

A one-tap expansion that explains why a card is in front of the operator,
rendered from the PERSISTED decision record and nothing else.

The discipline
--------------
This surface is only worth having if it is TRUE. Two rules follow from that,
and both are pinned in tests/test_rux3_why_panel.py:

1. **Read-only.** ``explain`` never re-runs ``evaluate_action`` or any
   resolution -- it reads what was already written when the card was posted.
   Re-scoring here would report a verdict for a context that no longer exists
   and could differ from the one the operator is actually being asked about.

2. **Silence is reported, never filled.** Most gate types (forge, dep,
   evolution, recovery, harness) never pass through the effect gate at all, so
   their records carry no verdict, no effect tags, no signature. The panel says
   so in plain words and lists the gap under ``unknowns``. It does not infer,
   and it never lets a missing field read as a benign one -- "no verdict
   recorded" is not "allowed", and an empty tag set is "unclassified" (which is
   precisely how ``action_governance`` itself treats it), not "no effects".

   The same rule applies to signature history. The spec asks for a status like
   "prompting because: new signature", but whether this signature was ever
   approved before is NOT on the card. Deriving "new" from the absence of an
   approval would be a proxy dressed as a fact, so it is declared unknown.

Output is plain ASCII so the same render can go to a chat transport and a
Windows console (cp1252) without a UnicodeEncodeError.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

_NOT_RECORDED = "not recorded"

# Gate types that are scored by the effect gate. Everything else is a
# workflow/lifecycle gate that never computes a verdict.
_EFFECT_SCORED_GATES = frozenset({"tool"})


@dataclass(frozen=True)
class WhyLine:
    label: str
    value: str


@dataclass(frozen=True)
class WhyExplanation:
    """The deterministic explanation for one card.

    ``lines`` are facts read from the record. ``unknowns`` are the things this
    card does NOT tell us -- rendered to the operator, never hidden.
    """

    headline: str
    lines: List[WhyLine] = field(default_factory=list)
    unknowns: List[str] = field(default_factory=list)

    def as_text(self) -> str:
        out = [self.headline]
        for ln in self.lines:
            out.append(f"  {ln.label}: {ln.value}")
        if self.unknowns:
            out.append("  Not known from this card:")
            for u in self.unknowns:
                out.append(f"    - {u}")
        return "\n".join(out)


def _ctx(decision: Dict[str, Any]) -> Dict[str, Any]:
    ctx = decision.get("context") if isinstance(decision, dict) else None
    return dict(ctx) if isinstance(ctx, dict) else {}


def _explain_tool_gate(ctx: Dict[str, Any]) -> WhyExplanation:
    lines: List[WhyLine] = []
    unknowns: List[str] = []

    name = str(ctx.get("tool_name") or "this tool")
    headline = f"Asked because a call to {name} was gated before it ran."

    verdict = ctx.get("verdict")
    if verdict:
        lines.append(WhyLine("Verdict", str(verdict)))
    else:
        lines.append(WhyLine("Verdict", _NOT_RECORDED))
        unknowns.append(
            "the verdict was not recorded on this card, so what the gate "
            "decided cannot be shown here")

    reason = ctx.get("gate_reason")
    if reason:
        lines.append(WhyLine("Reason", str(reason)))
    else:
        lines.append(WhyLine("Reason", _NOT_RECORDED))
        unknowns.append(
            "the gate's reason string was not recorded on this card (cards "
            "posted before this field existed do not carry it)")

    tags = ctx.get("effect_tags") or []
    if tags:
        lines.append(WhyLine("Effects", ", ".join(str(t) for t in tags)))
    else:
        # An empty tag set is UNKNOWN to the gate, not "harmless".
        lines.append(WhyLine(
            "Effects",
            "unclassified - the gate treats an empty effect set as unknown, "
            "which is why it asked rather than proceeding"))

    if ctx.get("destructive"):
        lines.append(WhyLine(
            "Escalator",
            "a destructive parameter was detected on this call"))

    sig = ctx.get("tool_signature")
    if sig:
        lines.append(WhyLine("Tool signature", str(sig)))
        unknowns.append(
            "whether this signature was approved before is not recorded on "
            "the card, so this cannot say if it is new or was invalidated by "
            "a re-forge")
    else:
        unknowns.append("the tool signature was not recorded on this card")

    if ctx.get("reclassified"):
        assigned = str(ctx.get("assigned_class") or "unspecified")
        lines.append(WhyLine(
            "Reclassified",
            f"you assigned the effect class {assigned}; that classification is "
            f"single-use and is not remembered for the next call"))

    return WhyExplanation(headline=headline, lines=lines, unknowns=unknowns)


def _explain_unscored_gate(ctx: Dict[str, Any]) -> WhyExplanation:
    gate_type = str(ctx.get("gate_type") or "unknown")
    lines = [WhyLine("Gate type", gate_type)]
    if ctx.get("risk"):
        lines.append(WhyLine("Risk band", str(ctx["risk"])))
    lines.append(WhyLine(
        "Verdict",
        f"{_NOT_RECORDED} - a {gate_type} gate does not run through the "
        f"effect gate, so no verdict or effect tags were computed for it"))
    return WhyExplanation(
        headline=(f"Asked because a {gate_type} step needs your decision "
                  f"before it can continue."),
        lines=lines,
        unknowns=[
            "no verdict was computed for this card - it is a workflow gate, "
            "not an effect-gate decision",
            "no effect tags were computed for this card",
        ],
    )


def _explain_ask(ctx: Dict[str, Any]) -> WhyExplanation:
    req = ctx.get("requirement")
    if not isinstance(req, dict):
        return WhyExplanation(
            headline="Asked because the run needed something from you.",
            lines=[WhyLine(
                "Detail",
                f"{_NOT_RECORDED} - no requirement record is attached to this "
                f"card")],
            unknowns=["which requirement produced this ask was not recorded",
                      "what resolution was attempted was not recorded"],
        )

    lines: List[WhyLine] = []
    unknowns: List[str] = []

    kind = str(req.get("kind") or _NOT_RECORDED)
    lines.append(WhyLine("Needs", kind))
    lines.append(WhyLine("Field", str(req.get("schema_path") or _NOT_RECORDED)))
    lines.append(WhyLine("State", str(req.get("state") or _NOT_RECORDED)))
    lines.append(WhyLine(
        "Found by", str(req.get("source") or _NOT_RECORDED)))
    if req.get("rationale"):
        lines.append(WhyLine("Rationale", str(req["rationale"])))

    attempted = ctx.get("attempted")
    if isinstance(attempted, (list, tuple)) and attempted:
        for a in attempted:
            lines.append(WhyLine("Tried", str(a)))
    else:
        unknowns.append(
            "what resolution was attempted before asking is not recorded on "
            "this card")

    return WhyExplanation(
        headline=f"Asked because a required {kind} could not be resolved.",
        lines=lines, unknowns=unknowns)


def explain(decision: Dict[str, Any]) -> WhyExplanation:
    """Explain one card from its persisted record. Pure and read-only."""
    ctx = _ctx(decision)
    kind = str(ctx.get("kind") or "")
    gate_type = str(ctx.get("gate_type") or "")

    if kind == "gate":
        if gate_type in _EFFECT_SCORED_GATES:
            return _explain_tool_gate(ctx)
        return _explain_unscored_gate(ctx)

    if kind == "ask" or "requirement" in ctx:
        return _explain_ask(ctx)

    title = str((decision or {}).get("title") or "this card")
    return WhyExplanation(
        headline=f"Asked because the run parked on a question: {title}.",
        lines=[WhyLine(
            "Effect gate",
            "not applicable - this card did not pass through the effect gate, "
            "so it carries no verdict or effect tags")],
        unknowns=["no verdict or effect tags were computed for this card"],
    )


# ── the inline expansion (NiceGUI) ──────────────────────────────────────────

def build_why_panel(decision: Dict[str, Any]) -> None:
    """Render the one-tap "why?" expansion for a card. Read-only."""
    try:
        from nicegui import ui
    except Exception:                                    # pragma: no cover
        return

    try:
        ex = explain(decision)
    except Exception:                                    # pragma: no cover
        return

    with ui.expansion("why?").classes("s-why").props("dense"):
        ui.label(ex.headline).classes("s-cell").style(
            "font-size: 12px; margin-bottom: 6px;")
        for ln in ex.lines:
            with ui.row().style("gap: 6px; align-items: baseline;"):
                ui.label(ln.label).classes("s-field-label")
                ui.label(ln.value).classes("s-cell").style("font-size: 12px;")
        if ex.unknowns:
            ui.label("Not known from this card").classes("s-field-label")
            for u in ex.unknowns:
                ui.label(f"- {u}").classes("s-muted").style("font-size: 11px;")
