"""R-B4 / T4 — the §5.9/§5.10 learning WRITE-BACK surfaces.

The events already exist (G-LEARN S3 promotes an answered ask into the profile and
materializes a `learned` TableItem; R-B3's `table_propose` writes `proposed` ones).
R-B4 renders them and gives the operator control: the tray, the provenance badge,
the answer-card ✓/undo chip, and "Needs you".

This module deliberately contains NO ``getsource(`` — conftest auto-tags a whole
module ``source_sensitive`` on that literal, which would deselect these behavioural
pins from the edit-safe tier. The one source-scan pin lives in its own module
(``test_rb4_source_pins.py``) so it drags only itself.
"""
from __future__ import annotations

import json
from pathlib import Path

from systemu.runtime import table_store as ts
from systemu.runtime import table_reconciler as tr
from systemu.runtime.table_provenance import (
    must_not_prefill,
    provenance_banner,
    SECURITY_CRITICAL_PARAM_TOKENS,
)


class _Vault:
    """Minimal vault: a .root dir + an empty tool list (mirrors list_tools())."""

    def __init__(self, root: Path, tools=None):
        self.root = str(root)
        self._tools = tools or []

    def list_tools(self, status=None):
        return list(self._tools)


def _suggested(kind="service", name="acme", origin="content_derived"):
    """A learned suggestion exactly as `make_learned_item` builds one."""
    return ts.make_learned_item(kind, name, origin_class=origin)


# --------------------------------------------------------------------------- #
# 1. acceptance — status moves, taint does not (§5.10.b#1, AC5)
# --------------------------------------------------------------------------- #

def test_accepting_a_suggestion_changes_status_never_origin(tmp_path):
    """AC5: accepting yields `declared` with origin_class UNCHANGED.

    The whole taint model rests on this. If acceptance rewrote origin_class to
    `operator`, every content_derived value would launder itself through one
    operator click and silently bind for ever after (§5.3).
    """
    v = _Vault(tmp_path)
    item = _suggested()
    key = ts.ref_key(item.kind, item.ref)
    assert ts.add_learned_item(v, item) is True

    before = {i.id: i for i in tr.project(v)}[item.id]
    assert before.status == "suggested"

    ts.add_accepted(v, key)
    after = {i.id: i for i in tr.project(v)}[item.id]

    assert after.status == "declared", "acceptance must move status"
    assert after.origin_class == "content_derived", "acceptance must NOT launder taint"
    assert after.provenance == "learned", "acceptance must NOT rewrite provenance"


def test_a_proposal_keeps_its_provenance_after_acceptance(tmp_path):
    """The §5.10.b#2 residual: an accepted proposal still reads as a proposal, so
    the banner can still name a task as its source on every later render."""
    v = _Vault(tmp_path)
    item = ts.make_proposed_item("service", "from-a-webpage")
    assert ts.add_proposed_item(v, item) == ""
    key = ts.ref_key(item.kind, item.ref)

    ts.add_accepted(v, key)
    got = {i.id: i for i in tr.project(v)}[item.id]

    assert got.status == "declared"
    assert got.provenance == "proposed"
    assert got.origin_class == "content_derived"


def test_acceptance_only_ever_promotes_suggested(tmp_path):
    """A stale acceptance must be INERT against a non-suggested item, not a status
    rewrite. Without the `status == "suggested"` guard, an accepted key whose item
    later projected as `broken`/`ready` from the live store would be flipped back to
    `declared` — an acceptance silently downgrading a real health state."""
    v = _Vault(tmp_path)
    item = _suggested()
    key = ts.ref_key(item.kind, item.ref)
    ts.add_learned_item(v, item)
    ts.add_accepted(v, key)

    # force the sidecar row to a health state the projector would preserve
    rows = ts.load_learned_items(v)
    rows[0].status = "broken"
    ts.save_learned_items(v, rows)

    got = {i.id: i for i in tr.project(v)}[item.id]
    assert got.status == "broken", "acceptance must not overwrite a health status"


def test_acceptance_is_not_auto_written_by_the_projector(tmp_path):
    """The hard invariant: `suggested` NEVER auto-promotes. Projecting repeatedly
    must not accumulate acceptances — only a direct operator action writes them."""
    v = _Vault(tmp_path)
    ts.add_learned_item(v, _suggested())
    for _ in range(3):
        tr.reconcile_once(v)
    assert ts.load_accepted(v) == set(), "a reconcile tick must never accept anything"
    assert all(i.status == "suggested" for i in tr.project(v))


# --------------------------------------------------------------------------- #
# 2. removal sticks (AC8) — and beats acceptance
# --------------------------------------------------------------------------- #

def test_a_dismissed_suggestion_is_not_resuggested(tmp_path):
    """AC8: 'a dismissed suggestion is not re-proposed for the same evidence.'
    Both halves: the write refuses, and a sidecar row written EARLIER still cannot
    project."""
    v = _Vault(tmp_path)
    item = _suggested()
    key = ts.ref_key(item.kind, item.ref)
    ts.add_learned_item(v, item)
    ts.add_tombstone(v, key)

    assert [i for i in tr.project(v) if i.id == item.id] == [], "read-side must skip"
    assert ts.add_learned_item(v, _suggested()) is False, "write-side must refuse"


def test_a_tombstone_beats_an_acceptance(tmp_path):
    """Accept then dismiss ⇒ gone. The acceptance overlay runs over the projection,
    so it can only reach items the tombstone check already let through; a key in
    BOTH files must project nothing rather than project as `declared`."""
    v = _Vault(tmp_path)
    item = _suggested()
    key = ts.ref_key(item.kind, item.ref)
    ts.add_learned_item(v, item)
    ts.add_accepted(v, key)
    ts.add_tombstone(v, key)

    assert [i for i in tr.project(v) if i.id == item.id] == []


def test_undoing_a_dismissal_restores_the_prior_accepted_state(tmp_path):
    """Dismiss does not clear the acceptance, so undo restores what was there —
    `declared`, not a re-suggestion the operator would have to accept twice."""
    v = _Vault(tmp_path)
    item = _suggested()
    key = ts.ref_key(item.kind, item.ref)
    ts.add_learned_item(v, item)
    ts.add_accepted(v, key)
    ts.add_tombstone(v, key)
    ts.remove_tombstone(v, key)

    got = {i.id: i for i in tr.project(v)}[item.id]
    assert got.status == "declared"


def test_accepted_store_is_defensive_and_fails_closed(tmp_path):
    """A corrupt `accepted.json` must mean 'nothing is accepted', never 'everything
    is'. The failure direction matters: the flattering read here would auto-promote
    every suggestion on the table."""
    v = _Vault(tmp_path)
    (Path(tmp_path) / "table").mkdir(parents=True, exist_ok=True)
    (Path(tmp_path) / "table" / "accepted.json").write_text("{not json", encoding="utf-8")
    assert ts.load_accepted(v) == set()

    # non-str entries are dropped, not coerced into keys that could match
    (Path(tmp_path) / "table" / "accepted.json").write_text(
        json.dumps(["service:ok", 7, None, {"a": 1}]), encoding="utf-8")
    assert ts.load_accepted(v) == {"service:ok"}


def test_removing_an_acceptance_returns_the_item_to_suggested(tmp_path):
    v = _Vault(tmp_path)
    item = _suggested()
    key = ts.ref_key(item.kind, item.ref)
    ts.add_learned_item(v, item)
    ts.add_accepted(v, key)
    ts.remove_accepted(v, key)
    assert {i.id: i for i in tr.project(v)}[item.id].status == "suggested"


# --------------------------------------------------------------------------- #
# 3. the provenance banner — it must never flatter (§5.10.b#4)
# --------------------------------------------------------------------------- #

def test_the_banner_refuses_to_name_a_source_it_cannot_determine():
    """The trust-failure class this project has already shipped twice: reporting a
    reassuring default where the answer could not actually be determined.

    An unrecognised provenance must report `determined=False` and SAY the source is
    unknown — never fall through to the operator-declared branch.
    """
    banner = provenance_banner(
        {"provenance": "totally_new_value", "origin_class": "operator"})

    assert banner["determined"] is False
    assert banner["trusted"] is False
    assert banner["tone"] == "danger"
    assert "unknown" in banner["headline"].lower()
    assert "cannot establish" in banner["detail"]
    # the specific flattering failure: it must not claim the operator declared it
    assert "You put this" not in banner["detail"]
    assert banner["warning"], "an undetermined source must still carry a warning"


def test_a_missing_provenance_is_undetermined_not_trusted():
    """Absent is not the same as operator. A row with no provenance at all is the
    likeliest shape of a partial migration, and it must read as unknown."""
    for item in ({}, {"origin_class": "operator"}, {"provenance": None}):
        banner = provenance_banner(item)
        assert banner["determined"] is False, item
        assert banner["trusted"] is False, item


def test_an_unknown_origin_does_not_inherit_a_known_provenances_trust():
    """Both axes must be known before ANY source is named. A row with a legitimate
    `operator_added` provenance but a garbage origin_class is half-recognised, and
    half-recognised must not resolve to the trusted half."""
    banner = provenance_banner(
        {"provenance": "operator_added", "origin_class": "sudo_trusted"})
    assert banner["determined"] is False
    assert banner["trusted"] is False


def test_a_task_proposal_never_renders_as_operator_declared():
    """§5.10.b#2's residual made concrete: the tray is a cross-run persistence
    channel for content-derived text, so a proposal's badge must name the task."""
    banner = provenance_banner(
        {"provenance": "proposed", "origin_class": "content_derived"})
    assert banner["determined"] is True
    assert banner["trusted"] is False
    assert banner["tone"] == "danger"
    assert "task" in banner["source"]
    assert "not from you" in banner["detail"]
    assert banner["warning"]


def test_content_derived_taint_overrides_a_trusted_provenance():
    """origin_class is the taint axis and outranks provenance. A `migrated` row
    carrying a content_derived origin is NOT trusted, even though `migrated` on its
    own is."""
    trusted = provenance_banner(
        {"provenance": "migrated", "origin_class": "operator"})
    tainted = provenance_banner(
        {"provenance": "migrated", "origin_class": "content_derived"})
    assert trusted["trusted"] is True and trusted["warning"] == ""
    assert tainted["trusted"] is False and tainted["warning"]


def test_a_learned_item_from_an_operator_answer_says_systemu_put_it_there():
    """§5.9 promotes the answer's ORIGINAL origin, so a learned card CAN be
    operator-origin. That is trusted INPUT but not an operator DECLARATION — the
    banner must not imply the operator added it to the table."""
    banner = provenance_banner(
        {"provenance": "learned", "origin_class": "operator"})
    assert banner["determined"] is True
    assert banner["trusted"] is False, "systemu chose to put it there, not the operator"
    assert "systemu put this on your table" in banner["detail"]
    assert "came from you" in banner["detail"]


def test_the_banner_reads_a_real_table_item_not_just_a_dict():
    """§5.10.c's nicegui boundary rule means the UI holds `model_dump()` dicts, but
    the runtime holds models. Both must work identically."""
    item = _suggested()
    assert provenance_banner(item) == provenance_banner(item.model_dump(mode="json"))


def test_every_known_provenance_produces_a_determined_banner():
    """The closed vocabulary and the banner's branch table must not drift apart: a
    sixth provenance added to the store without a branch here would silently start
    rendering as 'Source unknown' on a legitimate row."""
    for prov in ts.ITEM_PROVENANCES:
        banner = provenance_banner(
            {"provenance": prov, "origin_class": "operator"})
        assert banner["determined"] is True, prov
        assert banner["headline"], prov


# --------------------------------------------------------------------------- #
# 4. §5.10.b#4 — never pre-fill security-critical params
# --------------------------------------------------------------------------- #

def test_security_critical_params_are_never_prefillable():
    """§5.10.b#4 names endpoint/URL/hash/root path explicitly. Substring matching so
    a prefixed variant cannot slip past."""
    for name in ("url", "base_url", "server_endpoint", "root_path", "sha256",
                 "content_hash", "host", "data_dir", "folder", "port"):
        assert must_not_prefill(name) is True, name


def test_an_unnameable_param_is_treated_as_critical():
    """A field we cannot name is a field we cannot clear — fail-closed."""
    for name in ("", None, 42, {}):
        assert must_not_prefill(name) is True, name


def test_an_ordinary_param_stays_prefillable():
    """The guard must not be vacuous — if everything were critical the rule would
    be unfalsifiable and the one-click confirm would be dead."""
    for name in ("format", "tone", "locale", "units", "language"):
        assert must_not_prefill(name) is False, name
    assert SECURITY_CRITICAL_PARAM_TOKENS, "the token list must not be empty"


# --------------------------------------------------------------------------- #
# 5. the tray / board split (§5.10.c)
# --------------------------------------------------------------------------- #

def test_the_tray_holds_the_suggestions_and_the_board_does_not():
    """A suggestion rendered in its kind zone reads as inventory. The two views
    must partition the items — no double-render, nothing dropped."""
    from systemu.interface.pages.table import board_items, tray_items
    items = [_suggested(name="a"), _suggested(name="b")]
    items[1].status = "ready"
    assert [i.name for i in tray_items(items)] == ["a"]
    assert [i.name for i in board_items(items)] == ["b"]
    assert len(tray_items(items)) + len(board_items(items)) == len(items)


def test_tray_and_board_are_defensive_on_empty():
    from systemu.interface.pages.table import board_items, tray_items
    assert tray_items(None) == [] and board_items(None) == []


def test_no_tray_path_can_produce_a_tool_item(tmp_path):
    """The tombstone trap, pinned rather than left to review.

    ``ref_key("tool", …)`` prefers ``tool_id``, so an operator's Dismiss tombstones
    ``tool:<id>`` while an answer-derived card knows only ``tool:<name>`` — the keys
    never meet and the dismissal is silently defeated. The tray's Accept/Dismiss key
    items by their own kind+ref, so a `tool` reaching the tray would reopen it.

    It cannot today, via all three routes into `suggested`, and each is asserted so
    that re-introducing `tool` to any of them fails HERE rather than shipping a
    dismissal that does not stick:
      1. learned cards  — `_card_kind_for` never returns "tool"
      2. task proposals — "tool" is not in `PROPOSABLE_KINDS`
      3. the projector  — a live tool projects ready/broken/declared, never suggested
    """
    from systemu.runtime.ask_promotion import _CARD_KIND_TOKENS, _card_kind_for
    from systemu.runtime.table_consult import PROPOSABLE_KINDS
    from systemu.interface.pages.table import tray_items

    assert "tool" not in {kind for kind, _ in _CARD_KIND_TOKENS}
    for _kind, markers in _CARD_KIND_TOKENS:
        for marker in markers:
            assert _card_kind_for(marker) != "tool", marker
    assert "tool" not in PROPOSABLE_KINDS

    v = _Vault(tmp_path, tools=[
        {"id": "t1", "name": "zipper", "status": "enabled", "dry_run_status": "passed"},
        {"id": "t2", "name": "broken-one", "status": "enabled"},
    ])
    assert [i for i in tr.project(v) if i.kind == "tool"], "tools must still project"
    assert tray_items(tr.project(v)) == [], "no tool may reach the tray"


# --------------------------------------------------------------------------- #
# 6. the §5.6 answer-card ✓/undo chip
# --------------------------------------------------------------------------- #

def test_a_promotion_records_what_it_put_on_the_table(tmp_path):
    """The chip names what was added, so the promoter must write a receipt naming
    it. Keyed by the ask's `request_id` — the same id the decision context carries."""
    v = _Vault(tmp_path)
    ts.record_answer_receipt(v, "ask-1", ref_key_="service:acme", name="acme",
                             kind="service")
    got = ts.load_answer_receipts(v)
    assert got == {"ask-1": [{"key": "service:acme", "name": "acme",
                              "kind": "service"}]}


def test_a_receipt_does_not_stack_on_a_re_answer(tmp_path):
    """Re-answering the same ask heals the card in place; the acknowledgement must
    not accumulate a duplicate row each time."""
    v = _Vault(tmp_path)
    for _ in range(3):
        ts.record_answer_receipt(v, "ask-1", ref_key_="service:acme", name="acme",
                                 kind="service")
    assert len(ts.load_answer_receipts(v)["ask-1"]) == 1


def test_the_undo_chip_tombstones_so_the_card_cannot_come_back(tmp_path):
    """The undo must STICK. Deleting the sidecar row would not: the profile fact
    survives the undo, so the next promotion for the same leaf would re-add the
    card. Only a tombstone is honoured by `add_learned_item`."""
    v = _Vault(tmp_path)
    item = _suggested()
    key = ts.ref_key(item.kind, item.ref)
    ts.add_learned_item(v, item)
    ts.record_answer_receipt(v, "ask-1", ref_key_=key, name=item.name, kind=item.kind)

    removed = ts.undo_answer_receipt(v, "ask-1")

    assert removed == [key]
    assert key in ts.load_tombstones(v)
    assert ts.load_answer_receipts(v) == {}
    assert [i for i in tr.project(v) if i.id == item.id] == [], "must not project"
    assert ts.add_learned_item(v, _suggested()) is False, "must not be re-added"


def test_the_ack_model_never_claims_something_that_was_not_written(tmp_path):
    """A fabricated acknowledgement is systemu telling the operator it did
    something it did not. Absent receipt / no vault / no id ⇒ not visible."""
    from systemu.interface.pages.insights import answer_ack_model
    v = _Vault(tmp_path)
    assert answer_ack_model(v, "never-happened")["visible"] is False
    assert answer_ack_model(None, "ask-1")["visible"] is False
    assert answer_ack_model(v, "")["visible"] is False


def test_the_ack_model_names_the_items_it_added(tmp_path):
    from systemu.interface.pages.insights import answer_ack_model
    v = _Vault(tmp_path)
    ts.record_answer_receipt(v, "ask-1", ref_key_="service:acme", name="acme",
                             kind="service")
    model = answer_ack_model(v, "ask-1")
    assert model["visible"] is True and "acme" in model["text"]


def test_receipts_are_bounded(tmp_path):
    """An acknowledgement is worthless once the card is gone — the file must not
    grow without limit on a long-lived vault."""
    v = _Vault(tmp_path)
    for i in range(ts.MAX_ANSWER_RECEIPTS + 15):
        ts.record_answer_receipt(v, f"ask-{i}", ref_key_=f"service:s{i}",
                                 name=f"s{i}", kind="service")
    assert len(ts.load_answer_receipts(v)) == ts.MAX_ANSWER_RECEIPTS


# --------------------------------------------------------------------------- #
# 7. F3 — a real pick, and the marker's containment
# --------------------------------------------------------------------------- #

def test_the_pick_marker_never_reaches_a_tool_parameter():
    """`__picked__` is provenance metadata. If it survived into the coerced params
    it would be injected as an argument into the re-dispatched tool call."""
    from systemu.runtime.elicitation import PICK_MARKER_KEY, param_answers_from_choice
    schema = {"type": "object", "properties": {"out": {"type": "string"}}}
    out = param_answers_from_choice(schema, {"out": "x.md", PICK_MARKER_KEY: ["out"]})
    assert out == {"out": "x.md"}


def test_the_pick_marker_is_dropped_even_if_the_schema_declares_it():
    """Dropped BEFORE the schema lookup — otherwise a schema declaring a property
    of this name would smuggle the marker through as a real argument."""
    from systemu.runtime.elicitation import PICK_MARKER_KEY, param_answers_from_choice
    schema = {"type": "object",
              "properties": {PICK_MARKER_KEY: {"type": "string"},
                             "out": {"type": "string"}}}
    out = param_answers_from_choice(schema, {"out": "x.md", PICK_MARKER_KEY: "sneaky"})
    assert PICK_MARKER_KEY not in out


def test_the_answer_carries_the_pick_and_omits_it_when_nothing_was_picked():
    from systemu.interface.pages.insights import build_elicitation_answer
    from systemu.runtime.elicitation import PICK_MARKER_KEY
    schema = {"type": "object", "properties": {"out": {"type": "string"}}}

    picked = json.loads(build_elicitation_answer(schema, {"out": "x.md"}, ["out"]))
    assert picked[PICK_MARKER_KEY] == ["out"]

    # nothing picked ⇒ serializes exactly as before (no new key on every answer)
    plain = json.loads(build_elicitation_answer(schema, {"out": "x.md"}, []))
    assert PICK_MARKER_KEY not in plain
    assert plain == {"out": "x.md"}


def test_the_pick_marker_can_only_name_a_declared_field():
    """Intersected with the schema properties, so the marker cannot name a field
    that does not exist and be believed downstream."""
    from systemu.interface.pages.insights import build_elicitation_answer
    from systemu.runtime.elicitation import PICK_MARKER_KEY
    schema = {"type": "object", "properties": {"out": {"type": "string"}}}
    got = json.loads(
        build_elicitation_answer(schema, {"out": "x"}, ["out", "not_a_field"]))
    assert got[PICK_MARKER_KEY] == ["out"]


def test_an_explicit_pick_keeps_the_taint_a_digest_mismatch_would_have_dropped(tmp_path):
    """THE F3 payoff, and the reason it is a security fix rather than UI polish.

    A path-shaped candidate (`out/x.md` vs `out\\x.md`) can fail the byte-equality
    comparison even when the operator DID take the suggestion. The old code read
    that mismatch as "the operator typed this" ⇒ `operator`, the TRUSTED axis —
    laundering a content_derived value. An explicit pick must keep the taint.
    """
    from systemu.runtime import ask_promotion as ap
    from systemu.runtime import replay_metrics as rm

    v = _Vault(tmp_path)
    answered = "OUT/X.MD"
    # a candidate digest that will NOT compare equal to the answer's digest, but is
    # signed under the same vault key so it is a COMPARABLE candidate
    cand = rm.value_ref("a-different-shape", v)
    dctx = {"request_id": "ask-1", "spec": {"requirement_snapshot": [{
        "schema_path": "vendor_platform", "class": "input", "state": "resolvable",
        "value_origin": "content_derived", "candidate_ref": cand,
    }]}}

    assert cand is not None and rm.value_ref(answered, v) != cand

    n = ap.promote_answered_asks(v, dctx, {"vendor_platform": answered},
                                 picked=["vendor_platform"])
    assert n == 1, "the promotion itself must still happen"

    from systemu.runtime.user_profile import get_facts
    facts = [f for f in get_facts(v) if f.source == ap.PROMOTION_SOURCE]
    assert facts, "a fact must have been promoted"
    assert facts[-1].origin_class == "content_derived", \
        "an explicit pick must carry the candidate's taint, not default to operator"


def test_without_an_explicit_pick_a_digest_mismatch_still_reads_as_operator(tmp_path):
    """The negative control: the marker must be what changes the outcome. Without
    it the pre-existing behaviour is unchanged, so this pins the DELTA rather than
    a property the code had anyway."""
    from systemu.runtime import ask_promotion as ap
    from systemu.runtime import replay_metrics as rm

    v = _Vault(tmp_path)
    cand = rm.value_ref("a-different-shape", v)
    dctx = {"request_id": "ask-2", "spec": {"requirement_snapshot": [{
        "schema_path": "vendor_platform", "class": "input", "state": "resolvable",
        "value_origin": "content_derived", "candidate_ref": cand,
    }]}}
    ap.promote_answered_asks(v, dctx, {"vendor_platform": "OUT/X.MD"})

    from systemu.runtime.user_profile import get_facts
    facts = [f for f in get_facts(v) if f.source == ap.PROMOTION_SOURCE]
    assert facts and facts[-1].origin_class == "operator"


def test_a_forged_pick_marker_can_only_ever_add_taint(tmp_path):
    """The marker rides a persisted decision across a suspend, so it is
    attacker-shaped. Forging it must not be able to make anything MORE trusted —
    it only ever moves the origin toward the candidate's (tainted) axis."""
    from systemu.runtime import ask_promotion as ap
    from systemu.runtime import replay_metrics as rm

    v = _Vault(tmp_path)
    answered = "acme"
    cand = rm.value_ref(answered, v)          # a MATCHING operator-origin candidate
    dctx = {"request_id": "ask-3", "spec": {"requirement_snapshot": [{
        "schema_path": "vendor_platform", "class": "input", "state": "resolvable",
        "value_origin": "content_derived", "candidate_ref": cand,
    }]}}
    # garbage/forged entries must not crash it or flip anything to trusted
    ap.promote_answered_asks(v, dctx, {"vendor_platform": answered},
                             picked=[None, 7, {"a": 1}, "service_name"])

    from systemu.runtime.user_profile import get_facts
    facts = [f for f in get_facts(v) if f.source == ap.PROMOTION_SOURCE]
    assert facts and facts[-1].origin_class == "content_derived"


# --------------------------------------------------------------------------- #
# 8. "Needs you" (§5.10 honest-risks: Needs-you surfacing is load-bearing)
# --------------------------------------------------------------------------- #

def test_tray_suggestions_count_toward_needs_you(tmp_path):
    from systemu.interface.components.attention import (
        needs_you_breakdown, table_suggestion_count,
    )
    v = _Vault(tmp_path)
    ts.add_learned_item(v, _suggested())
    tr.reconcile_once(v)                      # the count reads the projection

    assert table_suggestion_count(v) == 1
    b = needs_you_breakdown(v)
    assert b["table_suggestions"] == 1 and b["total"] >= 1


def test_the_badge_targets_the_table_when_the_tray_is_all_that_waits(tmp_path):
    """A badge that counts the tray but always links to /inbox lands the operator
    on an empty page telling them nothing needs them."""
    from systemu.interface.components.attention import needs_you_breakdown
    v = _Vault(tmp_path)
    ts.add_learned_item(v, _suggested())
    tr.reconcile_once(v)
    assert needs_you_breakdown(v)["target"] == "/table"


def test_an_accepted_suggestion_stops_asking_for_attention(tmp_path):
    """Accepting resolves the attention — otherwise the badge never clears and the
    operator learns to ignore it."""
    from systemu.interface.components.attention import table_suggestion_count
    v = _Vault(tmp_path)
    item = _suggested()
    ts.add_learned_item(v, item)
    ts.add_accepted(v, ts.ref_key(item.kind, item.ref))
    tr.reconcile_once(v)
    assert table_suggestion_count(v) == 0


def test_needs_you_counting_is_defensive(tmp_path):
    """A broken vault must not break the shell — and must count 0, not crash."""
    from systemu.interface.components.attention import table_suggestion_count
    assert table_suggestion_count(None) == 0
