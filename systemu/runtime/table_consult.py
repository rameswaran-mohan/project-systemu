"""T3 — "Set the table", the guided consult + the bounded ``table_propose``
(spec UNIFIED-v2 §5.10.1, bounds §5.10.b#2/#5/#6).

WHAT §5.10.1 ACTUALLY SPECIFIES
-------------------------------
"The consult is a **synchronous UI-guided flow on `/table`**, NOT a ReAct chat
task: one multi-field elicitation form per coverage area (~6 cards, reusing the
existing ``requested_schema`` form path) ... The LLM is called **synchronously and
only to parse free-text answers** into ``TableItemProposal``s."

Four load-bearing consequences, each held by this module:

* **Zero harness budget.** The consult "does not ride the ReAct/harness-request
  path at all — it burns none of the ``max_requests_per_run``/per-activity caps
  and cannot be force-terminated by them." So nothing here constructs a
  ``HarnessRequest``, and nothing here imports the shadow runtime. The LLM hop is
  a direct ``llm_router.llm_call_json`` — the same non-action pattern §5.5.1's
  ``find_tools`` cites back to this section.
* **Declare-now-configure-later is the DEFAULT.** Every committed item lands
  ``declared``. Nothing here configures, connects, or grants.
* **Pending → review → commit.** Staged items are session-local GHOSTS. They are
  inline-editable and deletable, they are not in any store, the projector cannot
  see them, and they reach the vault only through :func:`commit`, which REFUSES
  without a passed review. That is BLOCKER-2's no-uncommitted-authority.
* **Provider gating.** No verified-live provider ⇒ no consult; the empty state
  leads with the deterministic "+ Put on the table" palette instead.

WHY THE LLM CANNOT CHOOSE A KIND
--------------------------------
The coverage AREA fixes the ``kind``; the parse only extracts names and notes from
the operator's free text. This is not tidiness — it is the fence. A parse that
picked its own kind could mint a ``tool`` card (whose ``ref_key`` prefers
``tool_id``, so an operator's removal tombstones ``tool:<tool_id>`` while a
name-derived card keys ``tool:<name>`` — the keys never meet and the deletion is
silently defeated) or a posture ``preference`` (§5.10.b#5, which must never arrive
except through the explicit Governor surface).

An LLM parse can still hallucinate a NAME the operator never typed. That is what
the mandatory one-screen review is for: every ghost is shown, editable and
deletable, before anything lands. The review is the fence on the parse, not a
formality.

TRUST
-----
Consult answers are operator-typed text, which §5.10.b#7 names explicitly as
trusted operator input ("same as a §5.6 elicitation answer") — so a committed
consult item is ``origin_class="operator"``, provenance ``consulted``. A
``table_propose`` call from any other context is untrusted by construction and
lands ``suggested`` + ``content_derived`` in a separate sidecar whose loader
clamps unconditionally (see ``table_store``). The two paths never share a file and
never share a stamp.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from systemu.runtime import table_store as ts
from systemu.runtime.table_store import TableItem

logger = logging.getLogger(__name__)


class ConsultNotReviewed(RuntimeError):
    """Raised when :func:`commit` is called before the operator passed the
    one-screen review.

    Deliberately an EXCEPTION rather than a ``0`` return: a caller can mistake
    "wrote nothing" for "nothing to write", and the no-uncommitted-authority rule
    is not a bound worth losing to an unchecked return value."""


#: Session ceiling on staged ghosts. The review screen is one screen; a parse that
#: yields hundreds of names makes it unreadable, and an unreadable review is not a
#: review. Complements ``table_store.MAX_PROPOSED_ITEMS`` (the task-side cap).
MAX_PENDING_PER_SESSION = 40

#: Kinds ``table_propose`` may create. ``tool`` is absent for the ``ref_key`` trap
#: described in the module docstring, and must not be added back without a
#: name→tool_id resolver.
PROPOSABLE_KINDS = ("service", "mcp_server", "data_root", "credential_ref",
                    "preference", "device")


# ── coverage areas (§5.10.1 "~6 cards") ───────────────────────────────────────
#: Each area fixes the KIND, so neither the LLM nor a caller picks it.
#: ``posture`` is the §5.10.b#5 propose-only area: the operator may state a
#: posture, it lands as a DECLARED preference, and applying it routes through the
#: Governor surface the card deep-links to. It confers nothing (§5.10.b#3).
AREAS: Tuple[Dict[str, Any], ...] = (
    {"id": "services", "kind": "service",
     "label": "Services & accounts",
     "question": "Which services and accounts do you use for work? "
                 "(e.g. Gmail, Notion, Salesforce)"},
    {"id": "mcp_servers", "kind": "mcp_server",
     "label": "MCP servers",
     "question": "Any MCP servers you want systemu to know about? "
                 "(paste the URLs — one per line)"},
    {"id": "data", "kind": "data_root",
     "label": "Files & data",
     "question": "Where do the files you work with live? "
                 "(folders — one per line)"},
    {"id": "credentials", "kind": "credential_ref",
     "label": "Keys",
     "question": "What credentials will systemu need, BY NAME only? "
                 "Never paste a key here."},
    {"id": "preferences", "kind": "preference",
     "label": "Preferences & defaults",
     "question": "Any defaults systemu should assume? "
                 "(e.g. reports as PDF, times in IST)"},
    {"id": "posture", "kind": "preference", "posture": True,
     "label": "Autonomy posture",
     "question": "When should systemu check with you before acting? "
                 "(this is recorded as intent — you apply it in Settings)"},
)

_AREA_BY_ID = {a["id"]: a for a in AREAS}

#: Where a posture card sends the operator. The table never authorizes
#: (§5.10.b#3), so a posture item is a POINTER to the Governor surface.
_POSTURE_ROUTE = "/settings"


def area_ids() -> List[str]:
    return [a["id"] for a in AREAS]


def area(area_id: str) -> Dict[str, Any]:
    return _AREA_BY_ID.get(area_id) or {}


def posture_deep_link() -> str:
    return _POSTURE_ROUTE


def area_schema(area_id: str) -> Dict[str, Any]:
    """The ``requested_schema`` for one coverage area — built with the SHIPPED
    builder (``elicitation.elicitation_schema_from_fields``) rather than a second
    hand-rolled one, so the /insights + Console form renderer draws it unchanged.

    The builder marks every field required, because its callers pass detected
    parameter GAPS. A coverage area is not a gap: §5.10.e requires the consult be
    skippable, and a required field would make "I don't use any" impossible to
    say. So ``required`` is emptied afterwards — the one documented divergence."""
    from systemu.runtime.elicitation import elicitation_schema_from_fields

    spec = area(area_id)
    if not spec:
        return {"type": "object", "properties": {}, "required": []}
    schema = elicitation_schema_from_fields([
        {"name": "items", "type": "string", "description": spec["question"]},
        {"name": "note", "type": "string",
         "description": "Anything worth remembering about these (optional)."},
    ])
    schema["required"] = []
    return schema


# ── provider gating (§5.10.1) ─────────────────────────────────────────────────

def consult_available(provider_configured: Optional[bool] = None) -> bool:
    """True when the guided consult may run at all.

    Reuses the shipped probe (``platform_profile._provider_configured``, what
    `sharing-on doctor` and the health page already report) instead of a second
    notion of "is there a model". Probe failure ⇒ False: leading the operator into
    a consult that cannot parse is worse than leading with the palette."""
    if provider_configured is None:
        try:
            from systemu.runtime import platform_profile
            provider_configured = platform_profile._provider_configured()
        except Exception:
            logger.debug("[T3] provider probe failed — gating the consult off",
                         exc_info=True)
            return False
    return bool(provider_configured)


def empty_state_cta(provider_configured: Optional[bool] = None) -> Dict[str, str]:
    """What `/table`'s empty state leads with (§5.10.c). Without a provider the
    DETERMINISTIC palette leads and the consult is explained, not hidden — an
    invisible feature reads as a broken one."""
    if consult_available(provider_configured):
        return {"primary": "set_the_table",
                "label": "Set the table — guided setup (~10 min)",
                "note": "Or add things one at a time with “+ Put on the table”."}
    return {"primary": "put_on_the_table",
            "label": "+ Put on the table",
            "note": "Connect a model to unlock the guided consult (“Set the "
                    "table”). Everything else works without one."}


# ── the first-run chat banner (§5.10.c "First-run lands on CHAT") ─────────────
#: Dismissal is a one-line marker file rather than a row in any table sidecar:
#: those four files are the operator's INVENTORY, and a UI preference in one of
#: them would project onto the board as a card.
_BANNER_FLAG = "consult_banner_dismissed"


def _banner_flag_path(vault):
    from pathlib import Path
    return Path(vault.root) / "table" / f"{_BANNER_FLAG}.json"


def dismiss_first_run_banner(vault) -> None:
    """Record the dismissal. Never raises — a banner that will not go away is a
    nuisance, but a crash on the chat page is a broken product."""
    try:
        p = _banner_flag_path(vault)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"dismissed": True}), encoding="utf-8")
    except Exception:
        logger.debug("[T3] could not persist the banner dismissal", exc_info=True)


def should_show_first_run_banner(vault, provider_configured: Optional[bool] = None
                                 ) -> bool:
    """True only for a genuinely cold, provider-having, undismissed install.

    "Cold" means the operator has neither declared nor answered anything — a table
    populated purely by PROJECTION (migrated cards from stores they already had)
    is still cold, because they have told systemu nothing yet."""
    try:
        if not consult_available(provider_configured):
            return False
        if _banner_flag_path(vault).exists():
            return False
        return not (ts.load_consulted_items(vault) or ts.load_operator_items(vault))
    except Exception:
        return False


# ── the free-text parse (synchronous, LLM-optional) ───────────────────────────

_PARSE_SYSTEM = (
    "You extract a list of named things from an operator's free-text answer.\n"
    "Return STRICT JSON: {\"items\": [{\"name\": str, \"detail\": str}]}\n"
    "Rules: use the operator's own words for `name`; do not invent entries that "
    "are not in the text; do not expand abbreviations you are unsure of; `detail` "
    "is a SHORT note or an empty string. Never include passwords, tokens or keys "
    "— if the text contains one, omit that entry entirely."
)


def _deterministic_names(text: str) -> List[str]:
    """Split free text into candidate names without a model. This is the
    provider-absent and parse-failed path — the consult degrades to a plain list
    instead of dying, so a half-configured install is never a dead end."""
    out: List[str] = []
    for line in str(text or "").splitlines():
        for chunk in line.split(","):
            name = chunk.strip().strip("-•*").strip()
            if name and name not in out:
                out.append(name)
    return out


def parse_area_answers(area_id: str, answers: Dict[str, Any], *,
                       llm_fn: Optional[Callable[..., Any]] = None,
                       config: Any = None) -> List[Dict[str, str]]:
    """Free text → proposal dicts ``{kind, name, detail}`` for ONE area.

    ``kind`` comes from the AREA, never from the model (see the module docstring).
    ``llm_fn``/``config`` are injected so the flow is drivable with no provider and
    testable with no network; absent either, the deterministic split is used.
    Any parse failure falls back the same way — never raises."""
    spec = area(area_id)
    if not spec:
        return []
    kind = spec["kind"]
    raw = str((answers or {}).get("items") or "")
    shared_note = str((answers or {}).get("note") or "").strip()
    if not raw.strip():
        return []

    parsed: List[Dict[str, str]] = []
    if llm_fn is not None and config is not None:
        try:
            # DEC-12: parse-class stage — the tier comes from the operator's
            # `parser_tier` knob via the MODEL-MATRIX, not a literal 3 here.
            result = llm_fn(
                stage="consult_parse", system=_PARSE_SYSTEM,
                user=json.dumps({"question": spec["question"], "answer": raw}),
                config=config,
            )
            for entry in (result or {}).get("items") or []:
                if not isinstance(entry, dict):
                    continue
                name = str(entry.get("name") or "").strip()
                if not name:
                    continue
                # `kind` in the model's output is IGNORED, deliberately.
                parsed.append({"kind": kind, "name": name,
                               "detail": str(entry.get("detail") or "").strip()})
        except Exception:
            logger.debug("[T3] LLM parse failed — falling back to the "
                         "deterministic split", exc_info=True)
            parsed = []
    if not parsed:
        parsed = [{"kind": kind, "name": n, "detail": ""}
                  for n in _deterministic_names(raw)]
    if shared_note:
        for p in parsed:
            p["detail"] = p["detail"] or shared_note
    return parsed


def default_llm_fn():
    """The synchronous parse hop. Separate so a UI caller wires it in one place
    and a test never has to reach the network."""
    from systemu.core.llm_router import llm_call_json
    return llm_call_json


# ── the session: pending ghosts, review, commit ───────────────────────────────

@dataclass
class ConsultSession:
    """One run of the consult. Ghosts live HERE and nowhere else until
    :func:`commit`, which is what makes "nothing lands unreviewed" structural
    rather than a check someone can forget."""
    pending: List[TableItem] = field(default_factory=list)
    areas_done: List[str] = field(default_factory=list)
    reviewed: bool = False


def _stage_one(session: ConsultSession, kind: str, name: str, detail: str) -> bool:
    if len(session.pending) >= MAX_PENDING_PER_SESSION:
        return False
    name = str(name or "").strip()
    if not name:
        return False
    item = ts.make_consulted_item(kind, name, str(detail or "").strip())
    key = ts.ref_key(item.kind, item.ref)
    if any(ts.ref_key(p.kind, p.ref) == key for p in session.pending):
        return False
    session.pending.append(item)
    return True


def stage_parsed(session: ConsultSession, area_id: str,
                 parsed: List[Dict[str, str]]) -> int:
    """Stage the output of :func:`parse_area_answers` as pending ghosts. Marks the
    area covered EVEN IF it yielded nothing — "I don't use any" is an answer, and
    an area that never clears would make the checklist unfinishable."""
    spec = area(area_id)
    if not spec:
        return 0
    n = 0
    for p in (parsed or []):
        if _stage_one(session, spec["kind"], p.get("name", ""), p.get("detail", "")):
            n += 1
    if area_id not in session.areas_done:
        session.areas_done.append(area_id)
    return n


def stage(session: ConsultSession, area_id: str, names: List[str],
          detail: str = "") -> int:
    """Stage names directly (the deterministic path — no parse involved)."""
    spec = area(area_id)
    if not spec:
        return 0
    return stage_parsed(session, area_id,
                        [{"name": n, "detail": detail} for n in (names or [])])


def edit_pending(session: ConsultSession, index: int, *, name: Optional[str] = None,
                 detail: Optional[str] = None) -> bool:
    """Inline-edit a ghost mid-consult (§5.10.1). Rebuilt through the constructor
    rather than mutated, so the id/ref stay derived from the (new) name — an
    edited ghost that kept its old ref would tombstone and heal as the wrong thing."""
    if not (0 <= index < len(session.pending)):
        return False
    cur = session.pending[index]
    session.pending[index] = ts.make_consulted_item(
        cur.kind,
        cur.name if name is None else str(name).strip(),
        cur.detail if detail is None else str(detail).strip(),
    )
    return True


def drop_pending(session: ConsultSession, index: int) -> bool:
    if not (0 <= index < len(session.pending)):
        return False
    session.pending.pop(index)
    return True


def review_lines(session: ConsultSession) -> List[str]:
    """The one-screen review (§5.10.1 "Here's your table — anything wrong?").
    One line per ghost, so the operator sees EVERYTHING that is about to land —
    including anything the parse invented."""
    out = []
    for it in session.pending:
        label = area_label_for_kind(it.kind)
        out.append(f"{label}: {it.name}" + (f" — {it.detail}" if it.detail else ""))
    return out


def area_label_for_kind(kind: str) -> str:
    for a in AREAS:
        if a["kind"] == kind:
            return a["label"]
    return kind


def commit(vault, session: ConsultSession) -> int:
    """Write the reviewed ghosts to ``<vault>/table/consulted_items.json``.

    REFUSES unless the operator passed the review. Consumes the flag and clears
    the ghosts on success, so a double-click cannot re-commit and a later stage
    cannot ride a stale approval — the review covers the items it was shown, not
    whatever the session holds next.

    Refuses individual items whose name or note looks like a credential, at the
    value level, via the shipped detector (see :func:`_is_secret_value`)."""
    if not session.reviewed:
        raise ConsultNotReviewed(
            "the consult review has not been passed — nothing may land")
    written = 0
    for item in list(session.pending):
        if _is_secret_value(item.name) or _is_secret_value(item.detail):
            logger.info("[T3] consult item withheld — value looks like a secret "
                        "(§5.10.b#6)")
            continue
        try:
            if ts.add_consulted_item(vault, item):
                written += 1
        except Exception:
            logger.debug("[T3] could not persist a consult item", exc_info=True)
    session.pending = []
    session.reviewed = False
    return written


def progress(session: ConsultSession) -> Tuple[int, int]:
    """(areas covered, total) — the visible "2 of 6" checklist."""
    return (len(session.areas_done), len(AREAS))


def next_area(session: ConsultSession) -> str:
    """The next uncovered area — what the "Resume setting the table" chip returns
    to. Empty string when the checklist is complete."""
    for a in AREAS:
        if a["id"] not in session.areas_done:
            return a["id"]
    return ""


def uncovered_areas(vault, session: ConsultSession) -> List[str]:
    """Re-run diff (§5.10.1 "re-running diffs against the current table and only
    asks about gaps"): an area already represented on the table is not asked
    again. Marks those areas covered on the session so the progress checklist and
    the resume chip agree with the diff."""
    try:
        from systemu.runtime import table_reconciler
        present = {i.kind for i in table_reconciler.project(vault)}
    except Exception:
        present = set()
    out = []
    for a in AREAS:
        if a["id"] in session.areas_done:
            continue
        if a["kind"] in present and not a.get("posture"):
            session.areas_done.append(a["id"])
            continue
        out.append(a["id"])
    return out


# ── the bounded `table_propose` (§5.10.b#2) ───────────────────────────────────

def _is_secret_value(value: Any) -> bool:
    """Value-level secret check. REUSES ``ask_promotion._value_is_secret``, which
    itself reuses ``messaging.gateway.mask_outbound`` (the codebase's outbound
    secret chokepoint) as a detector, plus the two shapes it provably misses. A
    third mechanism would be a third vocabulary to keep in sync. Import failure ⇒
    treat as secret, matching that module's fail-closed posture."""
    try:
        from systemu.runtime.ask_promotion import _value_is_secret
        return bool(_value_is_secret(value))
    except Exception:
        logger.debug("[T3] secret check unavailable — refusing", exc_info=True)
        return True


def _is_posture(name: str) -> bool:
    """§5.10.b#5 — an approval/autonomy posture may only ever be proposed through
    the explicit Governor surface. Reuses ``ask_promotion``'s token set, which
    already encodes this exact refusal for §5.9 learned cards; the rule is the
    same rule and must not drift between the two producers."""
    try:
        from systemu.runtime.ask_promotion import _POSTURE_TOKENS, _tokens
        return bool(_tokens(str(name or "").lower()) & _POSTURE_TOKENS)
    except Exception:
        logger.debug("[T3] posture check unavailable — refusing", exc_info=True)
        return True


def propose(vault, *, kind: str, name: str, detail: str = "") -> Dict[str, Any]:
    """CREATE a task-proposed TableItem, bounded server-side (§5.10.b#2).

    Returns ``{"accepted": bool, "reason": str, "ref_key": str}``. The bounds:

    * **CREATE-only.** There is no id parameter and no update/delete path; a
      repeat lands ``duplicate`` and the first row is left exactly as it was.
    * **Provenance forced.** This function has no consult channel of any kind, so
      everything it writes is ``suggested`` + ``content_derived`` + ``proposed``
      — the forced-provenance bound is structural, not a string comparison a
      caller could spoof. The consult writes through :func:`commit` instead, which
      no agent-callable surface reaches.
    * **Never touches an ``operator_added`` item.** A ref_key collision with the
      operator's own declaration is refused outright rather than shadowed.
    * **Never ``tool``** — the ref_key trap (module docstring).
    * **Never posture** (§5.10.b#5) and **never a secret value** (§5.10.b#6).
    * **Deduped against dismissals** and **capped** — both in
      ``table_store.add_proposed_item``, where they protect every caller.

    Never raises: this is reachable from a tool handler inside a run."""
    def _no(reason: str, key: str = "") -> Dict[str, Any]:
        return {"accepted": False, "reason": reason, "ref_key": key}

    try:
        kind = str(kind or "").strip()
        name = str(name or "").strip()
        detail = str(detail or "").strip()
        if kind not in PROPOSABLE_KINDS:
            return _no("kind_not_allowed")
        if not name:
            return _no("no_name")
        if kind == "preference" and _is_posture(name):
            return _no("posture")
        if _is_secret_value(name) or _is_secret_value(detail):
            return _no("secret")

        item = ts.make_proposed_item(kind, name, detail)
        key = ts.ref_key(item.kind, item.ref)

        # the operator's own declarations are untouchable (§5.10.b#2). Checked
        # HERE rather than in the store because it is a policy about who may
        # propose, not an invariant of the proposal file.
        try:
            operator_keys = {ts.ref_key(i.kind, i.ref)
                             for i in ts.load_operator_items(vault)}
        except Exception:
            operator_keys = set()
        if key in operator_keys:
            return _no("operator_added", key)

        reason = ts.add_proposed_item(vault, item)
        if reason:
            return _no(reason, key)
        return {"accepted": True, "reason": "", "ref_key": key}
    except Exception:
        logger.debug("[T3] propose failed (non-fatal)", exc_info=True)
        return _no("error")
