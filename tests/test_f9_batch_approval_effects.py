"""F9 — the first-run bulk approval card may not batch-approve a high-authority effect.

THE DEFECT (found by a human walkthrough of the dashboard on a fresh vault):

The one-time first-gate review card posted "Review 30 tool(s) before the action gate
turns on" and offered "Always allow the approvable ones". Measured against the REAL
shipped seed catalog, that batch contained::

    run_command          shell_exec
    run_cli_command      shell_exec
    launch_application   shell_exec
    close_application    shell_exec
    file_delete          local_delete

and reported ``0 excluded``, while the card's own text asserted

    "an unclassifiable high-severity effect can never be batch-approved, and each
     still needs an individual reclassification."

That promise was TECHNICALLY true and MATERIALLY false: nothing was ever judged
high-severity, because the migration-time DENY floor is ``UNKNOWN ∩ high-severity`` and
a tool declaring ``shell_exec`` is not UNKNOWN. So one click granted standing permission
for arbitrary shell execution.

THE PROPERTY (quantified over the whole tool catalog, and over any tool added later):

    A tool is offered for batch approval ONLY IF its EFFECTIVE effect classification
    (what the governor actually scored, not what the tool declared) is fully classified
    AND is a subset of the explicit low-authority allowlist
    ``effect_tags.BATCH_APPROVABLE``. Shell execution, deletion, network mutation,
    messaging, money, OAuth, network egress, an open-vocabulary extension class, and an
    UNCLASSIFIED effect are all excluded — and excluded by ALLOWLIST, so a tool or an
    effect class invented tomorrow is excluded without anyone updating a denylist.

    AND the card describes the rule it actually applies: the sentence it renders is
    DERIVED from the same allowlist, so the text cannot drift from the set.

THE FENCE has three committed parts, and each is pinned below:

  1. ``effect_tags.BATCH_APPROVABLE`` — an allowlist (default-exclude), constrained by a
     disjointness test against every danger set the codebase already reasons with.
  2. ``action_governance.batch_approvable(ctx)`` — applies it to ``effective_tags``, the
     ESCALATED set, so a benign declaration cannot buy a blanket allow.
  3. both the DECISION side (``partition_entries``) and the RECORD side
     (``apply_bulk_always_allow``) consult it, and the card's copy is generated from it.

WHY BOTH SIDES: the review entries round-trip through the decision store as JSON between
enqueue and resolve. A card posted by an older build — or a mutated stored context — must
not be able to sweep a shell tool when the operator finally clicks.
"""
from __future__ import annotations

import json

import pytest


# ── helpers ──────────────────────────────────────────────────────────────────

class _Store:
    def __init__(self):
        self.standing = []
        self.single_use = []

    def approve(self, sig):
        self.standing.append(sig)

    def mark_resume_approved(self, sig, *, for_reclassification=None):
        self.single_use.append(sig)


def _entry(name, tags, *, tool_id="t1", signature=None):
    from systemu.runtime.first_gate_review import build_entry
    return build_entry(tool_id=tool_id, name=name, effect_tags=list(tags),
                       signature=signature or f"sig::{name}")


# The exact (name, declared effect_tags) rows the shipped seed catalog produces after
# ``vault_migrator.backfill_effect_tags`` — measured, not guessed (see the real-catalog
# integration test at the bottom, which re-derives them from the packaged vault).
_SEED_DANGEROUS = [
    ("run_command", ["shell_exec"]),
    ("run_cli_command", ["shell_exec"]),
    ("launch_application", ["shell_exec"]),
    ("close_application", ["shell_exec"]),
    ("file_delete", ["local_delete"]),
    ("download_file", ["local_write", "net_read"]),
]
# The state these five WERE in when F9 shipped: real desktop/browser actuators that
# the classifier could not describe, so they carried nothing at all.
#
# F14 has since given them honest classes (`screen_capture`, `clipboard_read`,
# `input_synthesis`, `browser_actuate`) and an operator ruling of 2026-08-07 admits
# those classes to the batch — so the SHIPPED tools of these names are now eligible,
# and `tests/test_f14_effect_tag_completeness.py` pins that. These rows are kept
# verbatim because the property they exercise is unchanged and is the one that
# matters: an UNCLASSIFIED tool is excluded, whatever it is called. If a future
# regression empties their tags again, they must go back to being refused.
_SEED_UNCLASSIFIED = [
    ("web_act", []),
    ("type_text", []),
    ("keyboard_shortcut", []),
    ("clipboard_read", []),
    ("take_screenshot", []),
]


# ── A. the vocabulary: an ALLOWLIST, default-exclude ─────────────────────────

def test_the_allowlist_is_disjoint_from_every_danger_set_the_codebase_knows():
    """THE constraint on the allowlist itself.

    ``BATCH_APPROVABLE`` is an explicit set, so the only way it can go wrong is someone
    ADDING to it. This pins it against the four independently-reasoned danger sets that
    already exist, so a widening that re-admits shell/delete/net/money goes red here —
    including the exact widening that produced this defect.

    F14 — THE ONE CARVE-OUT, AND WHY THE PIN STILL HAS TEETH. The operator ruling of
    2026-08-07 deliberately admits four capture/actuation classes, and one of them
    (``browser_actuate``) genuinely egresses and so is in ``NET_EFFECTS``, and all four
    are in ``_APPROVAL_TAGS`` (an individual call still gates by default). Rather than
    falsify either set to keep a blanket disjointness assertion green — which would be
    the F9 defect in a new costume — the assertions are re-quantified over
    ``BATCH_APPROVABLE - OPERATOR_RULED_...``: any class NOT in that dated, frozen,
    commented constant is still fully disjoint from all four danger sets. The ruled set
    is then constrained in its own right (below), so the carve-out cannot be used as a
    door for shell/delete/money/message/OAuth.
    """
    from systemu.runtime.effect_tags import (BATCH_APPROVABLE, EffectTag,
                                             HIGH_SEVERITY,
                                             OPERATOR_RULED_BATCH_APPROVABLE,
                                             OPERATOR_RULED_BATCH_APPROVABLE_2026_08_07,
                                             OPERATOR_RULED_BATCH_APPROVABLE_2026_08_09)
    from systemu.runtime.action_governance import (MUST_ISOLATE, NET_EFFECTS,
                                                   _APPROVAL_TAGS)

    allowed = {t.value if isinstance(t, EffectTag) else str(t)
               for t in BATCH_APPROVABLE}
    ruled = {t.value for t in OPERATOR_RULED_BATCH_APPROVABLE}
    high = {t.value for t in HIGH_SEVERITY}

    assert ruled <= allowed, "the ruled set must actually be in the allowlist"
    unruled = allowed - ruled

    # (1) everything admitted WITHOUT an operator ruling is disjoint from all four.
    assert unruled & high == set(), "a high-severity effect may never be batch-approved"
    assert unruled & set(_APPROVAL_TAGS) == set(), (
        "_APPROVAL_TAGS is 'must the operator SEE this?' — anything in it is by "
        "definition not blanket-approvable without an explicit ruling")
    assert unruled & set(NET_EFFECTS) == set(), (
        "network egress (net_read included — it exfiltrates) is not blanket-approvable")
    assert unruled & set(MUST_ISOLATE) == set()

    # (2) the ruling itself is BOUNDED. It may waive the per-call card for a
    # capture/actuation class; it may not reach a high-severity or isolation-requiring
    # one, and it may not grow silently — the exact membership is pinned.
    assert ruled & high == set(), (
        "no operator ruling may admit a high-severity effect")
    assert ruled & set(MUST_ISOLATE) == set(), (
        "no ruling may admit a class that must run isolated")

    # Each ruling is bounded SEPARATELY and by date, so a later one cannot be used to
    # quietly widen an earlier one, and every member traces to a decision on a day.
    assert {t.value for t in OPERATOR_RULED_BATCH_APPROVABLE_2026_08_07} == {
        "screen_capture", "clipboard_read", "input_synthesis", "browser_actuate"}, (
        "the 2026-08-07 ruling covered exactly take_screenshot, clipboard_read, "
        "type_text, keyboard_shortcut and web_act — widening it needs a new ruling, "
        "a new dated constant and a new line here")
    assert {t.value for t in OPERATOR_RULED_BATCH_APPROVABLE_2026_08_09} == {
        "net_read"}, (
        "the 2026-08-09 ruling (F16) covered exactly net_read — reading the network. "
        "It resolved an incoherence: net_read was not in _APPROVAL_TAGS, so seven "
        "shipped tools already ran ungated, yet it blocked a standing allow. Writing "
        "to the network was NOT part of it")

    # (3) the classes no ruling reached are still out, by name.
    assert EffectTag.UNKNOWN.value not in allowed
    assert EffectTag.SHELL_EXEC.value not in allowed, (
        "shell_exec is absent from HIGH_SEVERITY, which is precisely how it reached the "
        "batch; it must be named explicitly here")
    for value in ("local_delete", "net_mutate", "send_message",
                  "money_move", "oauth_call", "clipboard_write", "desktop_notify"):
        assert value not in allowed, f"{value} was never ruled batch-approvable"


@pytest.mark.parametrize("tag", [t for t in __import__(
    "systemu.runtime.effect_tags", fromlist=["EffectTag"]).EffectTag])
def test_every_canonical_tag_outside_the_allowlist_is_not_batch_approvable(tag):
    """Quantified over the WHOLE vocabulary, not over hand-picked cases."""
    from systemu.runtime.effect_tags import BATCH_APPROVABLE, is_batch_approvable_tag
    expected = tag in BATCH_APPROVABLE
    assert is_batch_approvable_tag(tag) is expected, tag
    assert is_batch_approvable_tag(tag.value) is expected, tag.value


def test_an_open_vocabulary_extension_tag_is_never_batch_approvable():
    """Callout 2 lets a modality register a brand-new effect class at runtime. It must
    default to EXCLUDED — a denylist would silently admit it."""
    from systemu.runtime.effect_tags import (is_batch_approvable_tag,
                                             register_effect_tag)
    for value, high in (("desktop_actuate", True), ("some_new_benign_thing", False)):
        register_effect_tag(value, high_severity=high)
        assert is_batch_approvable_tag(value) is False, value


def test_an_unrecognised_value_is_not_batch_approvable():
    from systemu.runtime.effect_tags import is_batch_approvable_tag
    for junk in ("", None, "   ", "nonsense", 42, object()):
        assert is_batch_approvable_tag(junk) is False, repr(junk)


# ── B. the predicate over an ACTION, on the ESCALATED tag set ────────────────

def test_batch_approvability_is_scored_on_the_EFFECTIVE_tags_not_the_declared_ones():
    """A tool that DECLARES something benign but that the governor ESCALATES must be
    judged on what the governor scored.

    This is the documented shape of a real defect in this codebase (a money-move gate
    reached one-tap approval because a consumer read ``ctx.effect_tags`` instead of
    ``effective_tags``). ``wire_payout`` declares nothing; the name verb map scores it
    ``money_move``."""
    from systemu.runtime.action_governance import (ActionContext, batch_approvable,
                                                   effective_tags)

    ctx = ActionContext(tool="wire_payout", effect_tags=set())
    assert "money_move" in effective_tags(ctx), "precondition: the governor escalates"
    ok, reason = batch_approvable(ctx)
    assert ok is False and "money_move" in reason


def test_an_empty_declaration_is_unclassified_and_never_batch_approvable():
    from systemu.runtime.action_governance import ActionContext, batch_approvable
    ok, reason = batch_approvable(ActionContext(tool="mystery_tool", effect_tags=set()))
    assert ok is False
    assert "classif" in reason.lower(), reason


def test_a_purely_local_read_action_IS_batch_approvable():
    """The allowlist must not be vacuous — if nothing could ever qualify, the tests
    above would pass against a predicate hardcoded to False."""
    from systemu.runtime.action_governance import ActionContext, batch_approvable
    ok, _ = batch_approvable(ActionContext(tool="read_notes",
                                           effect_tags={"local_read"}))
    assert ok is True


# ── C. the DECISION side: partition_entries ──────────────────────────────────

@pytest.mark.parametrize("name,tags", _SEED_DANGEROUS)
def test_no_shipped_high_authority_seed_tool_is_ever_batch_approvable(name, tags):
    """The five tools the operator actually saw offered, plus the net-egress one."""
    from systemu.runtime.first_gate_review import partition_entries
    e = _entry(name, tags)
    part = partition_entries([e])
    assert e not in part.eligible, (
        f"{name} {tags} was offered for one-click standing approval")
    assert e in part.excluded, f"{name} must be listed as EXCLUDED, not hidden"


@pytest.mark.parametrize("name,tags", _SEED_UNCLASSIFIED)
def test_an_unclassified_tool_is_excluded_not_swept(name, tags):
    """These actuate the desktop or a browser and declare NOTHING. An effect we could
    not classify is the last thing that may receive a standing blanket allow."""
    from systemu.runtime.first_gate_review import partition_entries
    e = _entry(name, tags)
    part = partition_entries([e])
    assert e not in part.eligible and e in part.excluded


def test_sweep_every_tag_combination_the_vocabulary_can_produce():
    """The class-quantified pin: over every (name-category x tag-set) pair the scorer
    can see, ANY entry the partition calls eligible must satisfy ``batch_approvable``.

    A hand-picked list cannot establish this; a new EffectTag, a new verb, or a widened
    ``_APPROVAL_TAGS`` all change the space, and this test re-derives it.
    """
    from itertools import combinations

    from systemu.runtime.action_governance import ActionContext, batch_approvable
    from systemu.runtime.effect_tags import EffectTag
    from systemu.runtime.first_gate_review import partition_entries

    canon = [t.value for t in EffectTag]
    names = ["neutral_helper", "run_command", "file_delete", "wire_payout",
             "send_note", "submit_form", "launch_application"]

    entries = []
    for name in names:
        for r in range(0, 3):
            for combo in combinations(canon, r):
                entries.append(_entry(name, combo,
                                      signature=f"sig::{name}::{combo}"))

    part = partition_entries(entries)
    for e in part.eligible:
        ok, reason = batch_approvable(ActionContext(
            tool=e.name, effect_tags=set(e.effect_tags)))
        assert ok is True, (
            f"{e.name} {list(e.effect_tags)} is batch-eligible but not "
            f"batch-approvable: {reason}")

    # and the sweep must actually EXERCISE the exclusion, or it proves nothing
    assert len(part.excluded) >= 50, len(part.excluded)


def test_a_scoring_failure_fails_closed_out_of_the_batch(monkeypatch):
    """"we could not check" is not "we checked and it was fine"."""
    from systemu.runtime import first_gate_review as fgr

    e = _entry("read_notes", ["local_read"], signature="s")

    def _boom(*a, **k):
        raise RuntimeError("scorer down")

    monkeypatch.setattr(fgr, "migration_batch_approvable", _boom, raising=True)
    part = fgr.partition_entries([e])
    assert e not in part.eligible


# ── D. the RECORD side: a stale/tampered card may not sweep either ───────────

def test_the_recorder_refuses_a_shell_tool_handed_in_as_eligible():
    """A card posted by an OLDER build lists shell tools in its stored context. When the
    operator finally clicks, the recorder must refuse them — the record side re-derives
    from raw signals and never trusts the round-tripped row."""
    from systemu.runtime.first_gate_review import apply_bulk_always_allow

    shell = _entry("run_command", ["shell_exec"], signature="sig-shell")
    lying = shell.model_copy(update={"verdict": "require_approval"})
    store = _Store()

    written = apply_bulk_always_allow([lying], store=store)
    assert written == [] and store.standing == [], (
        "a stale bulk card swept shell_exec into a standing allow")


def test_the_recorder_still_records_a_genuinely_approvable_entry():
    """Surgical: the refusal above must not be a recorder that records nothing."""
    from systemu.runtime import first_gate_review as fgr

    ok = _entry("discovered_reader", ["local_read"], signature="sig-ok")
    store = _Store()
    # force the entry into the REQUIRE_APPROVAL band the way a discovered/registry tool
    # reaches it, so this exercises the real eligible path rather than an ALLOW tool
    monkey = fgr.is_bulk_eligible
    try:
        fgr.is_bulk_eligible = lambda v: True          # noqa: E731 - local monkeypatch
        assert fgr.apply_bulk_always_allow([ok], store=store) == ["sig-ok"]
    finally:
        fgr.is_bulk_eligible = monkey
    assert store.standing == ["sig-ok"]


def test_resolve_gate_end_to_end_refuses_the_shell_tool(monkeypatch):
    """Through the REAL dispatcher, from a stored decision context."""
    from systemu.interface.command.inbox import resolve_gate
    from systemu.runtime.first_gate_review import BULK_GATE_TYPE, OPT_BULK_ALLOW
    import systemu.runtime.command_approvals as _ca

    store = _Store()
    monkeypatch.setattr(_ca, "init_default_store", lambda _p: store, raising=False)

    entries = [_entry(n, t, signature=f"sig::{n}") for n, t in _SEED_DANGEROUS]

    class _Decision:
        choice = OPT_BULK_ALLOW
        dedup_key = "tool_bulk:0.10.22"
        id = "dec-1"
        context = {"kind": "gate", "gate_type": BULK_GATE_TYPE,
                   "bulk_entries": [e.model_dump(mode="json") for e in entries]}

    resolve_gate(_Decision(), vault=object())
    assert store.standing == [], (
        f"the real executor swept {store.standing} — shell/delete tools reached a "
        f"standing allow")


# ── E. the CARD TEXT must describe the rule it applies ───────────────────────

def _card(eligible_rows, excluded_rows):
    from systemu.interface.command.gate import GateDescriptor
    from systemu.runtime.first_gate_review import partition_entries
    entries = [_entry(n, t, signature=f"sig::{n}")
               for n, t in (eligible_rows + excluded_rows)]
    part = partition_entries(entries)
    return part, GateDescriptor.from_first_gate_bulk(part, version="0.10.22")


def test_the_card_no_longer_claims_the_narrow_high_severity_rule():
    """The false promise, verbatim. The card said the exclusion rule was
    "an unclassifiable high-severity effect" while it excluded nothing at all."""
    _part, d = _card([], _SEED_DANGEROUS)
    blob = f"{d.what_approve_does}\n{d.inspect}"
    assert "unclassifiable high-severity effect can never be batch-approved" not in blob


def test_the_card_rule_sentence_is_DERIVED_from_the_allowlist():
    """Text cannot drift from the set: the sentence NAMES every allowlist member and no
    non-member, so widening the allowlist rewrites the card automatically (and trips the
    disjointness test in section A)."""
    from systemu.runtime.effect_tags import BATCH_APPROVABLE, EffectTag
    from systemu.runtime.first_gate_review import batch_rule_sentence

    sentence = batch_rule_sentence()
    for t in BATCH_APPROVABLE:
        assert t.value in sentence, t
    for t in EffectTag:
        if t not in BATCH_APPROVABLE:
            assert f"only {t.value}" not in sentence.lower()

    _part, d = _card([], _SEED_DANGEROUS)
    assert sentence in f"{d.what_approve_does}\n{d.inspect}"


def test_the_card_names_each_excluded_tool_AND_why():
    """"0 excluded" with no reasons is what made the promise unfalsifiable on screen."""
    _part, d = _card([], _SEED_DANGEROUS)
    for name, _tags in _SEED_DANGEROUS:
        assert name in d.inspect, name
    assert "shell execution" in d.inspect
    assert "deletion" in d.inspect


def test_the_card_never_describes_an_option_it_does_not_offer():
    """Observed on the LIVE card after the set was fixed: with 0 eligible the copy read
    "'Always allow the approvable ones' remembers 0 tool(s)" while ``options`` was
    exactly ``['Leave gated']``. Same promise/reality gap as F9, one surface over."""
    from systemu.runtime.first_gate_review import OPT_BULK_ALLOW, OPT_LEAVE_GATED

    for rows in ([], _SEED_DANGEROUS, _SEED_DANGEROUS + _SEED_UNCLASSIFIED):
        _part, d = _card([], rows) if rows else _card(
            [], [("file_delete", ["local_delete"])])
        for label in (OPT_BULK_ALLOW, OPT_LEAVE_GATED):
            if f"'{label}'" in d.what_approve_does:
                assert label in d.options, (
                    f"the card explains {label!r} but does not offer it")


def test_an_exclusion_reason_never_states_an_INFERRED_effect_as_a_declared_one():
    """Observed on the LIVE card: ``file_list_dir — unclassified [excluded: network
    mutation (net_mutate)]``. The tool declares nothing; the name verb map fires on the
    token "file". Rendered flat, the line contradicts its own tags column and asserts
    something false about the tool. A reason that overstates is the same defect class as
    a promise that overstates."""
    from systemu.runtime.first_gate_review import batch_exclusion_reason

    for name in ("file_list_dir", "file_append", "create_word_doc", "type_text",
                 "notify_desktop"):
        reason = batch_exclusion_reason(_entry(name, []))
        assert "declares no effects" in reason, (name, reason)
        assert "(net_mutate)" not in reason and "(send_message)" not in reason, (
            f"{name}: an inferred class is presented as a declared one — {reason}")
        if "net_mutate" in reason or "send_message" in reason:
            assert "not declared" in reason, (name, reason)

    # a tool that REALLY declares the class is still named flatly — the qualifier must
    # not be sprayed over honest declarations
    assert "(shell_exec)" in batch_exclusion_reason(_entry("run_command",
                                                           ["shell_exec"]))
    assert "not declared" not in batch_exclusion_reason(_entry("file_delete",
                                                                ["local_delete"]))


def test_the_excluded_listing_with_reasons_stays_inside_the_dashboard_clip():
    """Adding a per-tool reason made the card's size scale with inventory x reason.

    Measured before the bound: a 400-tool excluded band rendered 10,547 chars, past the
    dashboard's 8000-char detail clip (``live_events_pane.clip_detail`` — an oversized
    render dropped the socket once, R-UX2). The renderer clipping the card mid-listing is
    worse than the card truncating on its own terms, because only the card discloses it.
    """
    from systemu.interface.command.gate import GateDescriptor
    from systemu.runtime.first_gate_review import (MAX_LISTED_PER_BAND,
                                                   partition_entries)

    many = [_entry(f"file_scan_directory_variant_{i:03d}", [], signature=f"s{i}")
            for i in range(400)]
    part = partition_entries(many)
    assert len(part.excluded) == 400, "precondition: a large EXCLUDED band"

    d = GateDescriptor.from_first_gate_bulk(part, version="0.10.22")
    assert len(d.inspect) < 8000, f"card is {len(d.inspect)} chars — past the clip"
    assert "400" in d.what_approve_does, "the COUNT must stay exact and un-truncated"
    assert "more" in d.inspect.lower(), "truncation must be disclosed, never silent"
    listed = sum(1 for ln in d.inspect.splitlines()
                 if ln.startswith("  file_scan_directory_variant_"))
    assert listed <= MAX_LISTED_PER_BAND

    # a clip must never remove the part that names WHY
    for ln in d.inspect.splitlines():
        if "[excluded:" in ln:
            assert "declares no effects" in ln, ln


def test_the_card_offers_no_batch_action_when_nothing_qualifies():
    """An offered-but-inert affirmative option is the shape of a prior CRITICAL finding —
    and here the rail's one-click quick-approve resolves ``options[-1]``."""
    from systemu.runtime.first_gate_review import OPT_BULK_ALLOW
    _part, d = _card([], _SEED_DANGEROUS + _SEED_UNCLASSIFIED)
    assert OPT_BULK_ALLOW not in d.options
    assert d.options[0] == d.safe_default


def test_the_card_still_offers_the_batch_when_something_DOES_qualify(monkeypatch):
    """The exclusion must not be a card that can never batch anything."""
    from systemu.interface.command.gate import GateDescriptor
    from systemu.runtime.first_gate_review import (BulkReviewPartition,
                                                   OPT_BULK_ALLOW)
    part = BulkReviewPartition(
        eligible=(_entry("discovered_reader", ["local_read"], signature="s1"),),
        excluded=(_entry("run_command", ["shell_exec"], signature="s2"),))
    d = GateDescriptor.from_first_gate_bulk(part, version="0.10.22")
    assert OPT_BULK_ALLOW in d.options


# ── F. the real shipped catalog, end to end ──────────────────────────────────

def test_the_REAL_seed_catalog_offers_no_high_authority_tool(tmp_path):
    """Not a fixture — the packaged vault, migrated the way the daemon migrates it.

    This is the test that would have caught F9: it reads the tools the product actually
    ships and asserts the batch the operator is offered is free of shell execution and
    deletion, and that the excluded count is non-zero (the ``0 excluded`` on the screen
    was the tell).
    """
    from systemu.runtime.vault_migrator import run as migrate
    from systemu.runtime.first_gate_review import (collect_backfilled_entries,
                                                   partition_entries)

    # the vault dir must be named `vault` — a relative implementation_path anchors at
    # the vault root's PARENT, which is how the runtime resolves it.
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    migrate(vault_dir)

    entries = collect_backfilled_entries(vault_dir)
    assert len(entries) >= 40, f"the seed catalog did not deploy: {len(entries)}"

    part = partition_entries(entries)
    offered = {e.name: list(e.effect_tags) for e in part.eligible}

    for banned in ("run_command", "run_cli_command", "launch_application",
                   "close_application", "file_delete"):
        assert banned not in offered, f"{banned} is batch-approvable: {offered[banned]}"

    for name, tags in offered.items():
        assert tags, f"{name} is offered with NO classification at all"
        assert "shell_exec" not in tags and "local_delete" not in tags, (name, tags)

    assert len(part.excluded) > 0, (
        "'0 excluded' again — the partition is not excluding anything")

    # and the classification really did land (guards against a vault where the backfill
    # silently stamped [] on everything, which would make the assertions above vacuous)
    classified = [e for e in entries if e.effect_tags]
    assert len(classified) >= 15, f"only {len(classified)} tools carry any tags"
