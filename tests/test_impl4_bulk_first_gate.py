"""IMPL-4 — the one-time bulk FIRST-GATE review card, and the DENY band it may never sweep.

Spec ("First-gate migration UX (IMPL-4, v2.1)"):

    When the live gate first ships (S1b), backfilled legacy tools carrying net/UNKNOWN
    tags get a **one-time bulk review card** (batch classify / Always-allow /
    leave-gated) ... **The card PARTITIONS by band:** only ``REQUIRE_APPROVAL``-band
    tools are eligible for batch Always-allow; any **DENY-band (UNKNOWN ∩
    high-severity) tool is EXCLUDED from the bulk action** and resolvable only
    individually via the IMPL-2 reclassify flow + high-friction typed-confirm — never
    swept in by a bulk Always-allow.

THE FAILURE MODE THIS FILE EXISTS FOR (and why the record side alone is not enough):

``tool_signature`` is params-INDEPENDENT (name + body hash + effect tags + host class)
while the DENY verdict is params-DEPENDENT (``is_destructive_param``). A backfilled tool
whose source could not be classified gets ``effect_tags=[]`` — the COMMONEST backfill
outcome — which scores REQUIRE_APPROVAL at migration time (no params in hand) and is
therefore legitimately bulk-eligible. The very same signature scores **DENY** the moment
a destructive argument arrives. A swept-in standing allow would then be consulted before
any band check and run the destructive call ungated, forever.

That is the shape of the CRITICAL defect commit ``2da5547c`` fixed for the single-tool
gate, and the lesson recorded there is the reason this file pins BOTH sides:

    "Refusing to RECORD a standing allow is not enough — only the CONSUMPTION side can
    express 'no stored approval satisfies this band'."

A sweep over the scorer's input cross-product (rather than hand-picked cases) is what
establishes the exposure is real and not theoretical: 14 (name, tag-set) combinations are
bulk-eligible at migration and DENY on a destructive call.
"""
from __future__ import annotations

from itertools import combinations

import pytest


# ── helpers ──────────────────────────────────────────────────────────────────

class _Store:
    """Records which persistence path a bulk resolution took."""

    def __init__(self, approved=()):
        self.standing = []
        self.single_use = []
        self._approved = set(approved)

    def approve(self, sig):
        self.standing.append(sig)
        self._approved.add(sig)

    def mark_resume_approved(self, sig, *, for_reclassification=None):
        self.single_use.append(sig)

    def is_approved(self, sig):
        return sig in self._approved

    # the gate reads these on every call; a bulk review never writes one
    def peek_reclassified(self, sig, *, args_fingerprint=None):
        return None

    def consume_reclassified(self, sig, *, args_fingerprint=None):
        return None

    def consume_resume_approved(self, sig, *, for_reclassification=None):
        return False


def _entry(name, tags, *, tool_id="t1", signature=None):
    from systemu.runtime.first_gate_review import build_entry
    return build_entry(tool_id=tool_id, name=name, effect_tags=list(tags),
                       signature=signature or f"sig::{name}")


@pytest.fixture
def suppose_the_effect_is_batch_approvable(monkeypatch):
    """Isolate the BAND conjunct from the F9 EFFECT-ALLOWLIST conjunct.

    Batch eligibility now needs BOTH: the REQUIRE_APPROVAL band (this file's subject —
    "would this tool gate at all, and is it DENY?") and a batch-approvable effect class
    (``tests/test_f9_batch_approval_effects.py``'s subject — "may this effect class be
    granted blanket, unattended, argument-free permission?").

    The two conjuncts are currently DISJOINT at migration time by construction: the only
    thing that puts a parameter-less action in the REQUIRE_APPROVAL band is
    ``action_governance._APPROVAL_TAGS``, and ``effect_tags.BATCH_APPROVABLE`` is pinned
    disjoint from it. So no real entry satisfies both today — which is the CORRECT F9
    outcome (nothing in this catalog is safe to blanket-allow), but it would silently
    reduce every band pin below to a vacuous assertion about an empty set.

    This fixture stubs the effect half satisfied so the band half stays under test. It
    never weakens the shipped rule: the F9 file owns that half, including the record-side
    proof that a shell tool handed in as eligible is still refused.
    """
    from systemu.runtime import first_gate_review as fgr
    monkeypatch.setattr(fgr, "batch_exclusion_reason", lambda _e: "", raising=True)


# ── A. the partition: which band may be swept ────────────────────────────────

def test_a_deny_band_tool_is_excluded_from_the_bulk_action():
    """THE hard constraint. UNKNOWN ∩ high-severity is individually remediable only
    (IMPL-2 reclassify + typed confirm) and must never appear in the batch."""
    from systemu.runtime.first_gate_review import partition_entries

    # an unmappable tag alongside a high-severity one => UNKNOWN ∩ high-severity
    e = _entry("ledger_sync", ["money_move", "some_unmapped_tag"])
    assert e.verdict == "deny", "fixture drifted — this must score DENY"

    part = partition_entries([e])
    assert e not in part.eligible, "a DENY-band tool must NEVER be batch-approvable"
    assert e in part.excluded


def test_a_require_approval_tool_is_eligible(suppose_the_effect_is_batch_approvable):
    """The DENY carve-out must be surgical: the ordinary band still batches.

    F9 added a SECOND conjunct (the effect allowlist) which an empty-tag tool fails on
    its own account — see ``test_an_unclassified_tool_is_excluded_but_for_its_OWN_reason``
    below. The fixture holds that half satisfied so this stays a pin on the BAND.
    """
    from systemu.runtime.first_gate_review import partition_entries
    e = _entry("process_data", [])          # unclassifiable source => empty tags
    assert e.verdict == "require_approval"
    part = partition_entries([e])
    assert e in part.eligible and e not in part.excluded


def test_an_unclassified_tool_is_excluded_but_for_its_OWN_reason():
    """Without the fixture, the same entry is excluded — and NOT because of the DENY
    floor. Keeping the two reasons distinct is what stops a later "simplification" from
    collapsing them and re-opening one."""
    from systemu.runtime.first_gate_review import (batch_exclusion_reason,
                                                   partition_entries)
    e = _entry("process_data", [])
    assert e.verdict == "require_approval", "still not DENY-band"
    assert e in partition_entries([e]).excluded
    assert "classif" in batch_exclusion_reason(e).lower()


def test_an_allow_band_tool_is_frictionless_not_swept():
    """An ALLOW-band tool never gates, so it needs no standing allow — minting one
    would be noise on a safety surface."""
    from systemu.runtime.first_gate_review import partition_entries
    e = _entry("fetch_weather", ["net_read"])
    assert e.verdict == "allow"
    part = partition_entries([e])
    assert e in part.frictionless
    assert e not in part.eligible and e not in part.excluded


def test_sweep_no_deny_scoring_combination_is_ever_eligible():
    """Sweep the scorer's input cross-product rather than trusting hand-picked cases.

    A prior fix in this codebase was proved "safe but useless" only by sweeping, so the
    partition is checked against ``evaluate_action`` itself over every (name-category x
    tag-set) pair: whatever the governor calls DENY must land in ``excluded``, and
    nothing else may.
    """
    from systemu.runtime.action_governance import ActionContext, evaluate_action
    from systemu.runtime.first_gate_review import partition_entries
    from systemu.runtime.effect_tags import EffectTag

    canon = [t.value for t in EffectTag]
    names = ["neutral_helper", "delete_records", "send_note", "charge_card",
             "submit_form", "ledger_tool", "wire_payout"]

    entries, seen_deny = [], 0
    for name in names:
        for r in range(0, 3):
            for combo in combinations(canon, r):
                entries.append(_entry(name, combo, signature=f"sig::{name}::{combo}"))

    part = partition_entries(entries)
    eligible = set(e.signature for e in part.eligible)

    for e in entries:
        truth, _ = evaluate_action(ActionContext(
            tool=e.name, effect_tags=set(e.effect_tags), is_destructive_param=False))
        if truth.value == "deny":
            seen_deny += 1
            assert e.signature not in eligible, (
                f"{e.name} {e.effect_tags} scores DENY but was batch-eligible")

    assert seen_deny >= 8, ("the sweep must actually EXERCISE the DENY band — if this "
                           "trips, the fixture space stopped producing DENY and the "
                           "assertion above proves nothing")


def test_a_missing_verdict_fails_closed():
    """Two readers once had opposite defaults on the same key. Absence is not consent:
    an entry with no verdict is not batch-eligible."""
    from systemu.runtime.first_gate_review import is_bulk_eligible
    for missing in ("", None, "   "):
        assert is_bulk_eligible(missing) is False, repr(missing)


def test_verdict_is_normalised_via_value_not_str():
    """``str(Verdict.DENY)`` is ``"Verdict.DENY"`` — which does not equal "deny" and
    silently re-opened this exact class of guard once before. The ENUM MEMBER must be
    recognised as the DENY band, not treated as an unknown-but-not-deny string."""
    from systemu.runtime.action_governance import Verdict
    from systemu.runtime.first_gate_review import is_bulk_eligible

    assert is_bulk_eligible(Verdict.DENY) is False
    assert is_bulk_eligible(Verdict.REQUIRE_APPROVAL) is True
    # and the string form of the member must not sneak through as "not deny"
    assert is_bulk_eligible("Verdict.DENY") is False
    assert is_bulk_eligible("Verdict.REQUIRE_APPROVAL") is False


def test_deny_is_matched_case_insensitively():
    from systemu.runtime.first_gate_review import is_bulk_eligible
    for v in ("DENY", "Deny", " deny ", "dEnY"):
        assert is_bulk_eligible(v) is False, v


# ── B. the RECORD side ───────────────────────────────────────────────────────

def test_bulk_allow_records_a_standing_allow_only_for_eligible_entries(
        suppose_the_effect_is_batch_approvable):
    from systemu.runtime.first_gate_review import apply_bulk_always_allow

    ok = _entry("process_data", [], signature="sig-ok")
    denied = _entry("ledger_sync", ["money_move", "unmapped"], signature="sig-deny")
    store = _Store()

    recorded = apply_bulk_always_allow([ok, denied], store=store)

    assert store.standing == ["sig-ok"]
    assert recorded == ["sig-ok"]
    assert "sig-deny" not in store.standing


def test_bulk_allow_refuses_a_deny_entry_even_when_it_is_handed_in_as_eligible():
    """Defence in depth: the recorder RE-DERIVES the band from the entry's own signals
    instead of trusting a caller-supplied verdict field. A context payload is
    round-tripped through the decision store between enqueue and resolve, so a stale or
    tampered ``verdict`` must not be able to launder a DENY into the batch."""
    from systemu.runtime.first_gate_review import apply_bulk_always_allow

    denied = _entry("ledger_sync", ["money_move", "unmapped"], signature="sig-deny")
    # lie about the band, exactly as a mutated/stale stored context would
    lying = denied.model_copy(update={"verdict": "require_approval"})
    store = _Store()

    recorded = apply_bulk_always_allow([lying], store=store)

    assert store.standing == [] and recorded == [], (
        "the recorder must re-score from (name, effect_tags), not believe the stamped "
        "verdict — otherwise a stale context sweeps a DENY into a standing allow")


def test_bulk_allow_never_mints_a_single_use_bridge():
    """A migration card has no parked run to resume, so a one-shot would sit unconsumed
    on a params-INDEPENDENT signature until some later, unrelated call spent it — the
    dangling-bridge hazard v0.10.21 documents."""
    from systemu.runtime.first_gate_review import apply_bulk_always_allow
    store = _Store()
    apply_bulk_always_allow([_entry("process_data", [], signature="s")], store=store)
    assert store.single_use == []


def test_a_rescore_failure_fails_closed(monkeypatch):
    """If the band cannot be re-derived, the tool is NOT swept.

    Found by mutation testing: flipping the exception fallback from DENY to
    REQUIRE_APPROVAL survived the whole suite. A classification hiccup must never widen
    the batch — that is the difference between "we could not check" and "we checked and
    it was fine", and this codebase has paid for that distinction before.
    """
    from systemu.runtime import first_gate_review as fgr

    entry = _entry("process_data", [], signature="s")
    assert entry.verdict == "require_approval", "precondition: normally sweepable"

    def _boom(*a, **k):
        raise RuntimeError("classifier down")

    monkeypatch.setattr(fgr, "migration_verdict", _boom, raising=True)
    store = _Store()
    assert fgr.apply_bulk_always_allow([entry], store=store) == []
    assert store.standing == []


def test_no_card_is_posted_when_nothing_needs_review(monkeypatch):
    """An entirely frictionless inventory must not manufacture a prompt — the card
    exists to PREVENT gate fatigue, so posting an empty one is self-defeating.

    Found by mutation testing: removing the ``needs_review`` early-return survived.
    """
    from systemu.runtime import first_gate_review as fgr
    import systemu.interface.command.inbox as _inbox_mod

    enqueued = []

    class _Inbox:
        def __init__(self, _vault):
            pass

        def enqueue(self, descriptor, **kw):
            enqueued.append(descriptor)
            return "dec-1"

    monkeypatch.setattr(_inbox_mod, "InboxQueue", _Inbox, raising=False)

    only_frictionless = [_entry("fetch_weather", ["net_read"], signature="s1"),
                         _entry("read_notes", ["local_read"], signature="s2")]
    part = fgr.partition_entries(only_frictionless)
    assert part.eligible == () and part.excluded == ()

    assert fgr.post_bulk_review_card(only_frictionless, vault=object(),
                                     version="0.9.58") == ""
    assert enqueued == [], "no card may be posted when there is nothing to review"


def test_a_card_IS_posted_when_there_is_something_to_review(monkeypatch):
    """The negative above must not be satisfied by a surface that never posts at all."""
    from systemu.runtime import first_gate_review as fgr
    import systemu.interface.command.inbox as _inbox_mod

    enqueued = []

    class _Inbox:
        def __init__(self, _vault):
            pass

        def enqueue(self, descriptor, *, gate_type=None, policy="unset",
                    context_extras=None):
            enqueued.append((descriptor, gate_type, policy, context_extras))
            return "dec-1"

    monkeypatch.setattr(_inbox_mod, "InboxQueue", _Inbox, raising=False)

    dec = fgr.post_bulk_review_card([_entry("process_data", [], signature="s1")],
                                    vault=object(), version="0.9.58")
    assert dec == "dec-1"
    descriptor, gate_type, policy, extras = enqueued[0]
    assert gate_type == fgr.BULK_GATE_TYPE
    assert policy is None, "a floor gate must be enqueued with policy=None"
    assert extras["bulk_entries"], "the entries must round-trip for the executor"


def test_leave_gated_records_nothing():
    from systemu.runtime.first_gate_review import apply_bulk_decision, OPT_LEAVE_GATED
    store = _Store()
    entries = [_entry("process_data", [], signature="s")]
    recorded = apply_bulk_decision(entries, choice=OPT_LEAVE_GATED, store=store)
    assert recorded == [] and store.standing == [] and store.single_use == []


def test_an_unrecognised_choice_records_nothing():
    """Fail closed on a choice this surface does not own."""
    from systemu.runtime.first_gate_review import apply_bulk_decision
    store = _Store()
    entries = [_entry("process_data", [], signature="s")]
    for choice in ("", None, "yes", "approve", "always allow", "Approve & Apply"):
        assert apply_bulk_decision(entries, choice=choice, store=store) == [], choice
    assert store.standing == []


# ── C. the CONSUME side — the half that actually expresses the band ──────────

def test_a_bulk_minted_standing_allow_cannot_satisfy_a_later_DENY_call(
        monkeypatch, suppose_the_effect_is_batch_approvable):
    """THE regression pin.

    Sweep a legitimately-eligible tool into a bulk Always-allow at migration time, then
    call that same signature with a DESTRUCTIVE argument so the governor scores DENY.
    The stored approval must NOT short-circuit the gate: a card must still post.

    This is the params-independence hole in its migration form. The record side cannot
    see it — at migration there are no params to score, and the tool is genuinely
    REQUIRE_APPROVAL — so only the consumption side can refuse.
    """
    from systemu.approval.exceptions import PendingOperatorDecision
    from systemu.runtime.first_gate_review import apply_bulk_always_allow
    from systemu.runtime.action_governance import ActionContext, evaluate_action

    entry = _entry("process_data", [], signature="sig-swept")
    store = _Store()
    apply_bulk_always_allow([entry], store=store)
    assert store.standing == ["sig-swept"], "precondition: the sweep really happened"
    assert store.is_approved("sig-swept")

    # the same signature, now with a destructive argument in hand
    v, _ = evaluate_action(ActionContext(
        tool="process_data", effect_tags=set(), is_destructive_param=True))
    assert v.value == "deny", "precondition: this call really is DENY-band"

    posted = _drive_tool_gate(monkeypatch, store=store, tool_name="process_data",
                              effect_tags=[], parameters={"cmd": "rm -rf /data"},
                              signature="sig-swept")
    assert posted is not None, (
        "a stored (bulk-minted) approval satisfied the DENY band — the swept allow ran "
        "an unclassifiable destructive call with no card")


def test_the_same_swept_allow_still_covers_the_benign_call(
        monkeypatch, suppose_the_effect_is_batch_approvable):
    """The carve-out must be surgical, or the feature is useless: the whole point of the
    sweep is that ORDINARY calls stop prompting."""
    from systemu.runtime.first_gate_review import apply_bulk_always_allow

    entry = _entry("process_data", [], signature="sig-swept")
    store = _Store()
    apply_bulk_always_allow([entry], store=store)

    posted = _drive_tool_gate(monkeypatch, store=store, tool_name="process_data",
                             effect_tags=[], parameters={"path": "report.txt"},
                             signature="sig-swept")
    assert posted is None, "the benign call should run under the swept allow"


def _drive_tool_gate(monkeypatch, *, store, tool_name, effect_tags, parameters,
                     signature):
    """Run ``_maybe_gate_tool`` against a stub tool; return the posted descriptor
    (or None if the call was allowed through)."""
    from systemu.approval.exceptions import PendingOperatorDecision
    from systemu.runtime import tool_sandbox as ts

    class _Tool:
        id, version, name = "t1", "1", tool_name
        implementation_path = ""
        effect_tags = list()

    tool = _Tool()
    tool.effect_tags = list(effect_tags)
    tool.name = tool_name

    posted = {}

    class _Inbox:
        def __init__(self, _vault):
            pass

        def enqueue(self, descriptor, *, gate_type, policy=None, context_extras=None):
            posted["descriptor"] = descriptor
            posted["gate_type"] = gate_type
            return "dec-1"

    import systemu.interface.command.inbox as _inbox_mod
    monkeypatch.setattr(_inbox_mod, "InboxQueue", _Inbox, raising=False)
    monkeypatch.setattr(ts, "tool_signature",
                        lambda *a, **k: signature, raising=False)
    import systemu.runtime.command_approvals as _ca
    monkeypatch.setattr(_ca, "tool_signature", lambda *a, **k: signature, raising=False)

    sandbox = ts.ToolSandbox.__new__(ts.ToolSandbox)
    sandbox._command_approvals = store
    sandbox._vault = object()
    sandbox.vault_root = __import__("pathlib").Path(".")
    monkeypatch.setattr(ts.ToolSandbox, "_tool_body_hash",
                        lambda self, t: "bodyhash", raising=False)

    try:
        sandbox._maybe_gate_tool(tool, tool_name, parameters)
    except PendingOperatorDecision:
        return posted.get("descriptor") or True
    return None


# -- C2. the migration moment: posted ONCE, at the FIRST TASK SUBMISSION -----
#
# TIMING RULING (operator, 2026-08-12). This card used to post at DAEMON BOOT. A fresh
# operator therefore met a HIGH-risk "Review N tool(s) before the action gate turns on"
# demand at minute zero, before they had run anything - asked to consent to an inventory
# they had no reason to have an opinion about yet. The card now posts at the first TASK
# SUBMISSION, which is the first moment the operator has any stake in the answer.
#
# ONLY THE TIMING MOVED. The marker+version semantics, the partition, the BATCH_APPROVABLE
# fence, the card's bytes, the executor and the inbox dispatch are all unchanged - pinned
# below and in section C2b.

def _vault_with_tools(tmp_path, tools):
    """Build a minimal post-backfill vault: tools/index.json + tool_<id>.json bodies."""
    import json
    (tmp_path / "tools").mkdir(parents=True, exist_ok=True)
    index = []
    for tid, name, tags in tools:
        index.append({"id": tid, "name": name})
        (tmp_path / "tools" / f"tool_{tid}.json").write_text(
            json.dumps({"id": tid, "name": name, "version": "1",
                        "effect_tags": list(tags)}), encoding="utf-8")
    (tmp_path / "tools" / "index.json").write_text(json.dumps(index), encoding="utf-8")
    return tmp_path


def test_the_review_card_is_posted_once_per_version(tmp_path, monkeypatch):
    """One-time, by construction. A card re-posted on every submission is the very
    gate-fatigue IMPL-4 exists to prevent."""
    from systemu.runtime import first_gate_review as fgr
    import systemu.interface.command.inbox as _inbox_mod

    posted = []

    class _Inbox:
        def __init__(self, _vault):
            pass

        def enqueue(self, descriptor, **kw):
            posted.append(descriptor)
            return "dec-1"

    monkeypatch.setattr(_inbox_mod, "InboxQueue", _Inbox, raising=False)
    vault_dir = _vault_with_tools(tmp_path, [("t1", "process_data", [])])

    assert fgr.maybe_post_first_gate_review(
        vault=object(), vault_dir=vault_dir, version="0.9.58") == "dec-1"
    assert len(posted) == 1

    # second submission, same version - no rescan, no second card
    assert fgr.maybe_post_first_gate_review(
        vault=object(), vault_dir=vault_dir, version="0.9.58") == ""
    assert len(posted) == 1, "the card must not re-post on every submission"

    # a version bump re-runs the backfill, so a fresh card is correct
    assert fgr.maybe_post_first_gate_review(
        vault=object(), vault_dir=vault_dir, version="0.9.59") == "dec-1"
    assert len(posted) == 2


def test_a_failed_post_is_retried_on_the_next_submission(tmp_path, monkeypatch):
    """The marker records that the operator was ASKED. Stamping it when the post failed
    would silently swallow the migration review entirely."""
    from systemu.runtime import first_gate_review as fgr
    import systemu.interface.command.inbox as _inbox_mod

    calls = {"n": 0}

    class _Inbox:
        def __init__(self, _vault):
            pass

        def enqueue(self, descriptor, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("queue down")
            return "dec-1"

    monkeypatch.setattr(_inbox_mod, "InboxQueue", _Inbox, raising=False)
    vault_dir = _vault_with_tools(tmp_path, [("t1", "process_data", [])])

    assert fgr.maybe_post_first_gate_review(
        vault=object(), vault_dir=vault_dir, version="0.9.58") == ""
    assert fgr.maybe_post_first_gate_review(
        vault=object(), vault_dir=vault_dir, version="0.9.58") == "dec-1"


# -- CRITERION 1: nothing is enqueued before the first task submission --------

def test_daemon_boot_enqueues_nothing(tmp_path, monkeypatch):
    """CRITERION 1 (the timing ruling), stated as the BEHAVIOUR the operator sees.

    Not "the daemon does not call ``maybe_post_first_gate_review``" - that is a
    convention the checked party can decline by reaching the queue another way. This
    drives the real boot helper against a real-shaped vault with a spy InboxQueue and
    asserts the INBOX IS UNTOUCHED. Whatever route boot might take, it lands here.

    The old pin asserted the exact opposite ("the daemon must post the first-gate review
    after the backfill"). Its mutation-testing lesson - a surface nobody invokes is not
    built - is not lost: it moves to the two lane pins below, which is where the call now
    has to be.
    """
    from systemu.scheduler import daemon as _daemon
    import systemu.runtime.vault_migrator as _vm
    import systemu.interface.command.inbox as _inbox_mod

    posted = []

    class _Inbox:
        def __init__(self, _vault):
            pass

        def enqueue(self, descriptor, **kw):
            posted.append(descriptor)
            return "dec-1"

    monkeypatch.setattr(_inbox_mod, "InboxQueue", _Inbox, raising=False)
    monkeypatch.setattr(_vm, "run", lambda *a, **k: {"fast_path": True}, raising=False)

    vault_dir = _vault_with_tools(tmp_path, [("t1", "process_data", []),
                                             ("t2", "run_command", ["shell_exec"])])

    class _Vault:
        root = str(vault_dir)

    _daemon._v0822_run_vault_migrator(_Vault())

    assert posted == [], (
        "daemon boot enqueued an approval card. A fresh operator must not be handed a "
        f"HIGH-risk consent demand at minute zero: {[d.title for d in posted]}")
    assert not (vault_dir / ".first_gate_review").exists(), (
        "boot stamped the one-time marker without ever asking - the first task "
        "submission would then find the offer already spent")


def test_the_quick_lane_posts_the_review_at_the_first_task_submission(monkeypatch):
    """CRITERION 1, quick lane. ``submit_quick_task`` is the quick lane's intake point
    (``interface/pages/chat_page.py:421`` and ``pipelines/direct_task.py:708`)."""
    import systemu.pipelines.quick_task as qt
    from systemu.runtime import first_gate_review as fgr

    seen = []
    monkeypatch.setattr(fgr, "maybe_post_on_task_submission",
                        lambda vault: seen.append(vault) or "dec-1", raising=False)
    monkeypatch.setattr(qt, "run_quick_task",
                        lambda *a, **k: qt.QuickResult(status="success", answer_md="ok"))

    class _V:
        root = "vault"

        def append_chat_history(self, e):
            pass

        def update_chat_history_entry(self, ts, f):
            pass

    v = _V()
    qt.submit_quick_task("hi", None, v)

    assert seen and seen[0] is v, (
        "the quick lane must offer the first-gate review at intake - otherwise the "
        "card is reachable only from a test")


def test_the_workflow_lane_posts_the_review_at_the_first_task_submission(monkeypatch):
    """CRITERION 1, workflow lane. ``run_direct_task`` is the workflow lane's intake
    point (``interface/pages/chat_page.py:425``, ``pipelines/direct_task.py:711``,
    ``interface/cli_commands.py:1306``)."""
    import systemu.pipelines.direct_task as dt
    import systemu.pipelines.scroll_refiner as sr
    import systemu.pipelines.activity_extractor as ae
    import systemu.interface.notifications as notif
    from systemu.runtime import first_gate_review as fgr

    seen = []
    monkeypatch.setattr(fgr, "maybe_post_on_task_submission",
                        lambda vault: seen.append(vault) or "dec-1", raising=False)
    monkeypatch.setattr(notif, "set_vault", lambda *a, **k: None)
    monkeypatch.setattr(ae, "init_pipeline", lambda *a, **k: None)

    def _boom(*a, **k):
        raise RuntimeError("stop the pipeline right after intake")

    monkeypatch.setattr(sr, "refine_from_text", _boom)

    class _V:
        root = "vault"

        def append_chat_history(self, e):
            pass

        def update_chat_history_entry(self, ts, f):
            pass

    v = _V()
    assert dt.run_direct_task("x", None, v) is None

    assert seen and seen[0] is v, (
        "the workflow lane must offer the first-gate review at intake")


def test_the_task_intake_hook_keeps_the_marker_and_version_semantics(tmp_path,
                                                                     monkeypatch):
    """CRITERION 1, "post at most once" - and an UPGRADE STILL RE-OFFERS.

    The hook must reuse the existing marker, not invent a second one: a fresh
    classification generation has to be re-offered, which is the whole reason the marker
    carries the version.
    """
    from systemu.runtime import first_gate_review as fgr
    import systemu.interface.command.inbox as _inbox_mod
    import systemu.runtime.vault_migrator as _vm

    posted = []

    class _Inbox:
        def __init__(self, _vault):
            pass

        def enqueue(self, descriptor, **kw):
            posted.append(descriptor)
            return "dec-1"

    monkeypatch.setattr(_inbox_mod, "InboxQueue", _Inbox, raising=False)
    vault_dir = _vault_with_tools(tmp_path, [("t1", "process_data", [])])

    class _V:
        root = str(vault_dir)

    v = _V()
    monkeypatch.setattr(_vm, "_installed_version", lambda: "0.9.58", raising=False)

    assert fgr.maybe_post_on_task_submission(v) == "dec-1"
    assert len(posted) == 1
    # every later submission on this version is a marker read and nothing more
    assert fgr.maybe_post_on_task_submission(v) == ""
    assert fgr.maybe_post_on_task_submission(v) == ""
    assert len(posted) == 1, "the card must not re-post on every task submission"
    assert (vault_dir / ".first_gate_review").read_text(encoding="utf-8").strip() == "0.9.58"

    # an upgrade re-runs the backfill, so a fresh offer is correct
    monkeypatch.setattr(_vm, "_installed_version", lambda: "0.9.59", raising=False)
    assert fgr.maybe_post_on_task_submission(v) == "dec-1"
    assert len(posted) == 2


def test_the_task_intake_hook_never_raises_into_the_lane():
    """A consent nicety must never be able to fail a submission."""
    from systemu.runtime import first_gate_review as fgr

    class _Broken:
        @property
        def root(self):
            raise RuntimeError("vault root exploded")

    assert fgr.maybe_post_on_task_submission(_Broken()) == ""
    assert fgr.maybe_post_on_task_submission(None) == ""
    assert fgr.maybe_post_on_task_submission(object()) == ""


# -- C2b. CRITERIA 2-4: the timing moved, the SCOPE did not ------------------

def test_the_per_tool_first_use_card_is_the_same_mint(monkeypatch):
    """CRITERION 2: the per-tool first-use gate is untouched - zero changes to gate.py.

    Two halves, because either alone is declinable:
      * BEHAVIOURAL - drive the real ``_maybe_gate_tool`` and read the descriptor it
        mints back off the queue: same title, same dedup namespace, same options, same
        fail-closed safe_default at index 0.
      * STRUCTURAL - ``gate.py`` must not have learned anything about task intake. If a
        future edit routes the bulk card's timing through the descriptor module, this
        trips.
    """
    from pathlib import Path
    import systemu.interface.command.gate as _gate

    d = _drive_tool_gate(monkeypatch, store=_Store(), tool_name="process_data",
                         effect_tags=[], parameters={"path": "report.txt"},
                         signature="sig-per-tool")
    assert d is not None and d is not True, "the un-reviewed tool must gate on first use"
    assert d.title == "Run tool: process_data"
    assert d.dedup == "tool:sig-per-tool", (
        "the per-tool card must still dedup on tool:<signature> - that key IS the "
        "'ask the first time it runs' contract")
    assert list(d.options) == ["Deny", "Approve once", "Always allow"]
    assert d.safe_default == "Deny" == d.options[0]
    assert "args: path='report.txt'" in d.inspect, (
        "the per-tool card still shows the ACTUAL arguments - that is what makes it the "
        "right place to decide, and the reason the bulk card need not come first")

    src = Path(_gate.__file__).read_text(encoding="utf-8")
    for forbidden in ("maybe_post_on_task_submission", "post_review_on_demand",
                      "submit_quick_task", "run_direct_task"):
        assert forbidden not in src, (
            f"gate.py learned about task intake ({forbidden!r}). The descriptor module "
            "mints cards; it must never decide WHEN one is posted")


#: CRITERION 3 - the bulk card's bytes, frozen the moment before the timing moved.
#: Rendered from a fixed partition by ``_bulk_card_blob`` below. A change to the lead,
#: the consent record, the rule sentence, the partition copy, the options or the dedup
#: moves this hash. It is SUPPOSED to be annoying: the ruling moved the timing and
#: NOTHING else, and a later dated ruling that legitimately extends the consent record
#: must be a deliberate, visible re-freeze - never a side effect.
#:
#: RE-FROZEN ONCE, v0.10.30 (P9b), deliberately and in its own commit:
#:     _BULK_CARD_SHA_REAL           741a2859... -> bdfdd731...
#:     _BULK_CARD_SHA_WITH_ELIGIBLE  0c431080... -> a402e069...
#: The card is now ASCII. It is posted to a Windows daemon's cp1252 console,
#: where an unencodable character is a UnicodeEncodeError in place of the card
#: (DEC-32c) - i.e. the first security decision an operator ever sees rendering
#: as a traceback. The ONLY change is em dash -> "--" (10 of them in the first
#: golden, 9 in the second; no ellipsis is reachable on a 3-entry partition).
#: WITNESSED, not asserted: applying that one substitution to the pre-change
#: bytes reproduces these digests exactly, and the partition counts are
#: unmoved. The consent record's WORDS, the partition, the options and the
#: dedup are all untouched.
_BULK_CARD_SHA_REAL = "bdfdd731edab5be8f8a483c06f1c04033c5ff1a4b101a6c0f6dc8e69be2e7b00"
_BULK_CARD_SHA_WITH_ELIGIBLE = (
    "a402e069a63dbf7cd13cc806eaa88d8c87a3791dfda33c6b1020b88c2fa266d7")


def _bulk_card_entries():
    from systemu.runtime.first_gate_review import build_entry
    return [
        build_entry(tool_id="t1", name="process_data", effect_tags=[],
                    signature="sig-a"),
        build_entry(tool_id="t2", name="run_command", effect_tags=["shell_exec"],
                    signature="sig-b"),
        build_entry(tool_id="t3", name="ledger_sync",
                    effect_tags=["money_move", "zzz_unmapped"], signature="sig-c"),
    ]


def _bulk_card_blob(partition):
    import hashlib
    import json
    from systemu.interface.command.gate import GateDescriptor

    d = GateDescriptor.from_first_gate_bulk(partition, version="9.9.9")
    blob = json.dumps({"title": d.title, "risk": d.risk, "options": list(d.options),
                       "safe_default": d.safe_default, "inspect": d.inspect,
                       "what_approve_does": d.what_approve_does, "dedup": d.dedup},
                      sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest(), d


def test_the_bulk_card_content_is_byte_identical_after_the_timing_move():
    """CRITERION 3: consent record, partition and options are BYTE-IDENTICAL.

    The operator ruled that the timing moves and the scope does not. "Scope" on a
    consent surface is the exact text the operator consents to, so the pin is the exact
    text - hashed over the whole descriptor, not spot-checked over the phrases someone
    remembered to assert.
    """
    from systemu.runtime.first_gate_review import partition_entries

    partition = partition_entries(_bulk_card_entries())
    assert (len(partition.eligible), len(partition.excluded),
            len(partition.frictionless)) == (0, 3, 0), (
        "the partition itself moved - re-derive the golden deliberately, do not "
        "re-freeze it to make this pass")

    got, d = _bulk_card_blob(partition)
    assert got == _BULK_CARD_SHA_REAL, (
        "the bulk card's content changed. Only the TIMING was ruled; the card the "
        f"operator consents to did not move.\n--- title ---\n{d.title}\n"
        f"--- options ---\n{list(d.options)}\n--- inspect ---\n{d.inspect}\n"
        f"--- what_approve_does ---\n{d.what_approve_does}\n--- dedup ---\n{d.dedup}")


def test_the_bulk_card_with_an_affirmative_option_is_byte_identical(
        suppose_the_effect_is_batch_approvable):
    """CRITERION 3, the branch that actually offers the sweep.

    The real catalog has nothing batch-approvable today (correctly), so the golden above
    only exercises the 0-eligible rendering. This freezes the other branch - the one
    carrying the affirmative option's copy and the "what Approve does" promise.
    """
    from systemu.runtime.first_gate_review import partition_entries

    partition = partition_entries(_bulk_card_entries())
    assert (len(partition.eligible), len(partition.excluded)) == (2, 1)

    got, d = _bulk_card_blob(partition)
    assert got == _BULK_CARD_SHA_WITH_ELIGIBLE, (
        "the bulk card's affirmative branch changed.\n--- options ---\n"
        f"{list(d.options)}\n--- inspect ---\n{d.inspect}\n"
        f"--- what_approve_does ---\n{d.what_approve_does}")


def test_the_batch_approvable_fence_is_unmoved():
    """CRITERION 3: the fence itself. Widening BATCH_APPROVABLE is how a timing change
    turns into a scope change without anyone editing a card."""
    from systemu.runtime import effect_tags as _et

    assert sorted(_et.batch_approvable_tags()) == [
        "browser_actuate", "clipboard_read", "input_synthesis", "local_read",
        "local_write", "net_read", "no_effect", "screen_capture",
    ], ("the batch-approvable allowlist moved. The timing ruling does not authorise "
        "widening what a single click can bless")


def test_the_executor_and_the_inbox_dispatch_are_unmoved(monkeypatch):
    """CRITERION 3: ``apply_bulk_decision`` and the inbox ``tool_bulk`` handling.

    Same affirmative label, same fail-closed default for everything else, and the inbox
    still routes ``tool_bulk`` to the bulk executor rather than NOOPing it.
    """
    from systemu.runtime.first_gate_review import (BULK_GATE_TYPE, OPT_BULK_ALLOW,
                                                   OPT_LEAVE_GATED,
                                                   apply_bulk_decision)
    from systemu.interface.command import inbox as _inbox

    entry = _entry("process_data", [], signature="sig-x")
    monkeypatch.setattr("systemu.runtime.first_gate_review.batch_exclusion_reason",
                        lambda _e: "", raising=True)

    store = _Store()
    assert apply_bulk_decision([entry], choice=OPT_BULK_ALLOW, store=store) == ["sig-x"]
    assert apply_bulk_decision([entry], choice=OPT_LEAVE_GATED, store=_Store()) == []
    assert apply_bulk_decision([entry], choice="Always allow", store=_Store()) == []
    assert apply_bulk_decision([entry], choice=None, store=_Store()) == []

    assert BULK_GATE_TYPE == "tool_bulk"
    src = __import__("inspect").getsource(_inbox.resolve_gate)
    assert "BULK_GATE_TYPE" in src and "_handle_bulk_first_gate_review" in src, (
        "the inbox stopped dispatching the bulk card to its executor - the card would "
        "resolve to a NOOP and the operator's choice would be silently discarded")


def test_re_gate_semantics_are_unchanged_by_the_timing_move(
        monkeypatch, suppose_the_effect_is_batch_approvable):
    """CRITERION 4: a swept allow still binds to the exact body + effect set, and still
    yields to a higher-scoring call.

    Both halves in one place because the timing move is exactly the kind of change that
    could have been implemented by widening the allow (post later, remember harder).
    """
    from systemu.runtime.first_gate_review import apply_bulk_always_allow

    store = _Store()
    apply_bulk_always_allow([_entry("process_data", [], signature="sig-v1")],
                            store=store)
    assert store.standing == ["sig-v1"]

    # RE-FORGE: a new body means a new signature, and the swept allow does not reach it
    posted = _drive_tool_gate(monkeypatch, store=store, tool_name="process_data",
                              effect_tags=[], parameters={"path": "report.txt"},
                              signature="sig-v2-reforged")
    assert posted is not None, "a re-forged body ran under the OLD body's swept allow"

    # HIGHER-SCORING ARGUMENTS: same signature, destructive param => still gates
    posted = _drive_tool_gate(monkeypatch, store=store, tool_name="process_data",
                              effect_tags=[], parameters={"cmd": "rm -rf /data"},
                              signature="sig-v1")
    assert posted is not None, (
        "a swept allow satisfied a call whose arguments score DENY")

    # and the benign call on the reviewed signature still runs unprompted
    assert _drive_tool_gate(monkeypatch, store=store, tool_name="process_data",
                            effect_tags=[], parameters={"path": "report.txt"},
                            signature="sig-v1") is None


# -- C2c. the affordance: "Review all tools" on the Build page ---------------

def test_the_on_demand_review_ignores_the_marker(tmp_path, monkeypatch):
    """An EXPLICIT operator request always gets the card.

    The marker exists to stop an UNASKED-FOR card from repeating. It has no business
    refusing a card the operator just asked for - and it is not stamped either, so the
    one-time offer the operator has not yet answered is not silently spent.
    """
    from systemu.runtime import first_gate_review as fgr
    import systemu.interface.command.inbox as _inbox_mod
    import systemu.runtime.vault_migrator as _vm

    posted = []

    class _Inbox:
        def __init__(self, _vault):
            pass

        def enqueue(self, descriptor, **kw):
            posted.append(descriptor)
            return "dec-1"

    monkeypatch.setattr(_inbox_mod, "InboxQueue", _Inbox, raising=False)
    monkeypatch.setattr(_vm, "_installed_version", lambda: "0.9.58", raising=False)
    vault_dir = _vault_with_tools(tmp_path, [("t1", "process_data", [])])

    class _V:
        root = str(vault_dir)

    v = _V()
    # spend the one-time offer first
    assert fgr.maybe_post_on_task_submission(v) == "dec-1"
    assert fgr.maybe_post_on_task_submission(v) == "", "precondition: marker is stamped"

    assert fgr.post_review_on_demand(v) == "dec-1", (
        "the operator asked for the review and the marker refused it")
    assert len(posted) == 2

    # asking does not consume anything: the marker is untouched, and the card carries
    # the SAME dedup key, so the Inbox collapses the duplicate rather than stacking it
    assert (vault_dir / ".first_gate_review").read_text(
        encoding="utf-8").strip() == "0.9.58"
    assert posted[0].dedup == posted[1].dedup == "tool_bulk:0.9.58"


def test_the_on_demand_review_posts_the_same_card_and_never_raises(tmp_path,
                                                                   monkeypatch):
    """Same mint, same gate type, same floor-gate policy - one card, two triggers."""
    from systemu.runtime import first_gate_review as fgr
    import systemu.interface.command.inbox as _inbox_mod
    import systemu.runtime.vault_migrator as _vm

    seen = {}

    class _Inbox:
        def __init__(self, _vault):
            pass

        def enqueue(self, descriptor, *, gate_type, policy=None, context_extras=None):
            seen["descriptor"] = descriptor
            seen["gate_type"] = gate_type
            seen["policy"] = policy
            seen["ctx"] = context_extras
            return "dec-1"

    monkeypatch.setattr(_inbox_mod, "InboxQueue", _Inbox, raising=False)
    monkeypatch.setattr(_vm, "_installed_version", lambda: "0.9.58", raising=False)
    vault_dir = _vault_with_tools(tmp_path, [("t1", "run_command", ["shell_exec"])])

    class _V:
        root = str(vault_dir)

    assert fgr.post_review_on_demand(_V()) == "dec-1"
    assert seen["gate_type"] == "tool_bulk"
    assert seen["policy"] is None, "the on-demand card must still be a FLOOR gate"
    assert seen["descriptor"].risk == "high"
    assert seen["descriptor"].safe_default == seen["descriptor"].options[0]
    assert seen["ctx"]["first_gate_version"] == "0.9.58"

    class _Broken:
        @property
        def root(self):
            raise RuntimeError("no vault")

    assert fgr.post_review_on_demand(_Broken()) == ""
    assert fgr.post_review_on_demand(None) == ""


def test_the_build_page_offers_the_on_demand_review():
    """The affordance must be WIRED, not merely available.

    This is the mutation-testing lesson the old daemon pin carried: a surface reachable
    only from a test is not built. The Build page is where an operator goes to look at
    their tools, so it is where "review them all" belongs.

    Pinned by AST, not by substring. The first draft of this test asserted
    ``"Review all tools" in src`` and stayed GREEN when the button was deleted - the
    label still appeared in the handler's docstring and in the explanation line beneath
    it. A pin a mutation walks past is a false assertion of enforcement (DEC-34), so this
    one requires the actual ``ui.button(<label>, on_click=<handler>)`` node inside
    ``build_tools_page``.
    """
    import ast
    from pathlib import Path
    import systemu.interface.pages.tools as _tools_page

    assert callable(getattr(_tools_page, "_request_tool_review", None)), (
        "the handler must be a module-level function so it is testable without a "
        "NiceGUI slot stack")

    tree = ast.parse(Path(_tools_page.__file__).read_text(encoding="utf-8"))
    page = next((f for f in ast.walk(tree)
                 if isinstance(f, ast.FunctionDef) and f.name == "build_tools_page"),
                None)
    assert page is not None, "build_tools_page vanished"

    wired = []
    for node in ast.walk(page):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "button"):
            continue
        label = node.args[0].value if (
            node.args and isinstance(node.args[0], ast.Constant)) else None
        handler = next((kw.value.id for kw in node.keywords
                        if kw.arg == "on_click" and isinstance(kw.value, ast.Name)), None)
        if handler == "_request_tool_review":
            wired.append(label)

    assert wired == ["Review all tools"], (
        "the Build page must render exactly one button wired to _request_tool_review, "
        f"labelled honestly in the operator's words; found: {wired}")


def test_the_build_page_action_reports_both_outcomes(monkeypatch):
    """It must say what happened - including the honest "nothing to review" answer.

    A button that silently does nothing when the inventory is already clean reads as
    broken, and an operator who cannot tell the difference between "no card needed" and
    "the post failed" has been told nothing.
    """
    import systemu.interface.pages.tools as _tools_page
    from systemu.runtime import first_gate_review as fgr

    notes = []
    monkeypatch.setattr(_tools_page.ui, "notify",
                        lambda msg, **kw: notes.append((msg, kw.get("type"))))

    class _State:
        vault = object()

    monkeypatch.setattr(_tools_page.AppState, "get", staticmethod(lambda: _State()))

    monkeypatch.setattr(fgr, "post_review_on_demand", lambda _v: "dec-1", raising=False)
    _tools_page._request_tool_review()
    assert notes and notes[-1][1] == "positive"
    assert "Inbox" in notes[-1][0], "tell the operator WHERE the card went"

    notes.clear()
    monkeypatch.setattr(fgr, "post_review_on_demand", lambda _v: "", raising=False)
    _tools_page._request_tool_review()
    assert notes and notes[-1][1] != "positive", (
        "no card posted must not be reported as success")


def test_the_body_hash_anchors_a_relative_impl_path_the_way_THE_GATE_does(tmp_path):
    """The collector must mirror ``ToolSandbox._tool_body_hash``, NOT the backfill.

    ``implementation_path`` is stored relative to the vault root's PARENT (e.g.
    ``vault/tools/implementations/x.py``) — that is how ``tool_forge`` writes it and how
    the sandbox reads it back. Anchoring it at ``tools/implementations`` instead yields a
    path that does not exist, and the read silently falls back to ``<id>:<version>``.

    This is not hypothetical: that exact mis-anchoring is the bug that made every seeded
    tool's effect tags read as empty. Here it would be worse than wrong tags — a
    fallback hash produces a signature the live gate never looks up, so the sweep would
    report the tools as remembered while every call still prompts.
    """
    import hashlib
    from systemu.runtime.command_approvals import tool_signature
    from systemu.runtime.first_gate_review import collect_backfilled_entries

    vault = tmp_path / "vault"
    impl_dir = vault / "tools" / "implementations"
    impl_dir.mkdir(parents=True)
    body_src = b"def run():\n    return 1\n"
    (impl_dir / "process_data.py").write_bytes(body_src)

    (vault / "tools" / "index.json").write_text(
        '[{"id": "t1", "name": "process_data"}]', encoding="utf-8")
    (vault / "tools" / "tool_t1.json").write_text(
        '{"id": "t1", "name": "process_data", "version": "1", "effect_tags": [],'
        ' "implementation_path": "vault/tools/implementations/process_data.py"}',
        encoding="utf-8")

    entries = collect_backfilled_entries(vault)
    assert len(entries) == 1

    real_hash = hashlib.sha1(body_src).hexdigest()
    expected = tool_signature("process_data", real_hash, set(), host_class="")
    assert entries[0].signature == expected, (
        "the collector did not hash the file the gate will hash — a relative "
        "implementation_path must anchor at the vault root's PARENT")

    # and it must NOT have silently degraded to the id:version fallback
    fallback = tool_signature("process_data", "t1:1", set(), host_class="")
    assert entries[0].signature != fallback


def test_eligibility_does_not_key_on_tag_EMPTINESS(tmp_path):
    """A tool carrying REAL tags must partition on those tags, not on "has no tags".

    The seeded inventory's tags changed underneath this feature (a backfill fix moved 17
    tools from empty to real tags like ``shell_exec`` / ``local_delete`` / ``net_read``).
    Nothing here may depend on emptiness: the outcome comes from the governor over
    whatever tags are present. Empty is merely one input that happens to score UNKNOWN.

    F9 REWROTE THE EXPECTED ANSWER, and this assertion was the defect in test form. It
    read "shell_exec and local_delete are approval-band effects" and concluded they were
    batch-eligible. Approval-band is exactly what makes them UN-batchable: the operator
    must see the call. They are still not DENY — the two exclusions are distinct — but
    they are excluded, each naming its own effect.
    """
    from systemu.runtime.first_gate_review import (batch_exclusion_reason,
                                                   partition_entries)

    shell = _entry("run_script", ["shell_exec"], signature="s1")
    delete = _entry("cleanup", ["local_delete"], signature="s2")
    read = _entry("fetch_page", ["net_read"], signature="s3")

    part = partition_entries([shell, delete, read])
    assert part.eligible == (), "no one-click standing allow for a shell or a delete"
    assert shell in part.excluded and delete in part.excluded
    assert read in part.frictionless, "net_read never gates, so there is nothing to batch"

    # NOT the DENY floor — the band and the batch rule are separate facts, and the card
    # must not tell the operator to 'reclassify' a tool that is perfectly well classified
    assert shell.verdict == "require_approval" and delete.verdict == "require_approval"
    assert "shell execution" in batch_exclusion_reason(shell)
    assert "deletion" in batch_exclusion_reason(delete)


def test_the_collected_signature_matches_what_the_live_gate_computes(tmp_path):
    """A swept allow keyed to a signature the gate never looks up is worse than useless:
    it reads as 'remembered' while every call still prompts. The collector must mirror
    ``ToolSandbox._tool_body_hash`` + ``tool_signature`` exactly."""
    from systemu.runtime.command_approvals import tool_signature
    from systemu.runtime.first_gate_review import collect_backfilled_entries

    vault_dir = _vault_with_tools(tmp_path, [("t1", "process_data", [])])
    entries = collect_backfilled_entries(vault_dir)
    assert len(entries) == 1

    # what the live gate computes for a tool with no on-disk implementation
    expected = tool_signature("process_data", "t1:1", set(), host_class="")
    assert entries[0].signature == expected


# ── C3. the real executor branch (resolve_gate dispatch) ────────────────────

class _Decision:
    def __init__(self, choice, context):
        self.choice = choice
        self.context = context
        self.dedup_key = "tool_bulk:0.9.58"
        self.id = "dec-1"


def _bulk_context(entries):
    from systemu.runtime.first_gate_review import BULK_GATE_TYPE
    return {"kind": "gate", "gate_type": BULK_GATE_TYPE,
            "bulk_entries": [e.model_dump(mode="json") for e in entries]}


def test_resolve_gate_sweeps_only_the_eligible_band_end_to_end(
        monkeypatch, suppose_the_effect_is_batch_approvable):
    """Through the REAL dispatcher, not the helper: a bulk card carrying both bands in
    its stored context must write a standing allow for the approvable tool only."""
    from systemu.interface.command.inbox import resolve_gate
    from systemu.runtime.first_gate_review import OPT_BULK_ALLOW
    import systemu.runtime.command_approvals as _ca

    store = _Store()
    monkeypatch.setattr(_ca, "init_default_store", lambda _p: store, raising=False)

    ok = _entry("process_data", [], signature="sig-ok")
    denied = _entry("ledger_sync", ["money_move", "unmapped"], signature="sig-deny")
    decision = _Decision(OPT_BULK_ALLOW, _bulk_context([ok, denied]))

    result = resolve_gate(decision, vault=object())

    assert store.standing == ["sig-ok"]
    assert "sig-deny" not in store.standing
    assert store.single_use == []
    assert "1" in result.summary


def test_resolve_gate_records_nothing_when_the_card_is_left_gated(monkeypatch):
    from systemu.interface.command.inbox import resolve_gate
    from systemu.runtime.first_gate_review import OPT_LEAVE_GATED
    import systemu.runtime.command_approvals as _ca

    store = _Store()
    monkeypatch.setattr(_ca, "init_default_store", lambda _p: store, raising=False)

    decision = _Decision(OPT_LEAVE_GATED,
                         _bulk_context([_entry("process_data", [], signature="s")]))
    resolve_gate(decision, vault=object())
    assert store.standing == [] and store.single_use == []


def test_resolve_gate_refuses_a_deny_entry_whose_stored_verdict_was_tampered(monkeypatch):
    """The stored context is untrusted input by the time the operator clicks — it has
    round-tripped through the decision store as JSON. The executor re-scores."""
    from systemu.interface.command.inbox import resolve_gate
    from systemu.runtime.first_gate_review import OPT_BULK_ALLOW
    import systemu.runtime.command_approvals as _ca

    store = _Store()
    monkeypatch.setattr(_ca, "init_default_store", lambda _p: store, raising=False)

    denied = _entry("ledger_sync", ["money_move", "unmapped"], signature="sig-deny")
    ctx = _bulk_context([denied])
    ctx["bulk_entries"][0]["verdict"] = "require_approval"   # the tamper

    resolve_gate(_Decision(OPT_BULK_ALLOW, ctx), vault=object())
    assert store.standing == [], "a tampered stored verdict must not launder a DENY"


# ── C4. the REMOTE lane must never tap-to-approve this card ─────────────────

def test_the_bulk_card_is_never_remotely_resolvable():
    """A phone tap must not bless an entire tool inventory.

    ``decision_bridge.classify_resolution`` gates the remote lane on an ALLOWLIST
    (``_REMOTE_GATE_TYPES``), so ``tool_bulk`` floors at step 1 — before any of the
    per-axis safety checks run. This pins that, and pins the allowlist membership
    directly, because adding ``tool_bulk`` there would be a one-word change that
    silently makes the widest gate in the system tap-approvable.
    """
    from systemu.messaging.decision_bridge import (
        classify_resolution, RESOLUTION_FLOOR, _REMOTE_GATE_TYPES)
    from systemu.interface.command.gate import GateDescriptor
    from systemu.runtime.first_gate_review import BULK_GATE_TYPE, partition_entries

    assert BULK_GATE_TYPE not in _REMOTE_GATE_TYPES

    part = partition_entries([_entry("process_data", [], signature="s")])
    d = GateDescriptor.from_first_gate_bulk(part, version="0.9.58")
    ctx = d.to_decision_context(gate_type=BULK_GATE_TYPE)
    assert classify_resolution(ctx) == RESOLUTION_FLOOR


def test_no_payload_however_benign_makes_the_bulk_card_remotely_resolvable():
    """The bulk card floors on the ALLOWLIST, independently of the tag policy.

    ``classify_resolution`` rejects an unlisted ``gate_type`` at step 1, before any
    per-axis check runs. So this asserts the strong property directly: no payload —
    whatever it claims on the other axes — makes this card tap-approvable.

    DELIBERATELY NOT COUPLED TO THE TAG AXIS. An earlier revision of this test carried a
    "control" asserting that the SAME payload on ``gate_type="tool"`` was remotely
    resolvable, to prove the allowlist (not the tag check) was doing the work. That
    control encoded a LIVE BUG as its premise — an empty ``effect_tags`` list satisfying
    the floor — and the moment that bug was fixed the control would either start failing
    for an unrelated reason or, worse, silently stop controlling anything. The tag policy
    belongs to ``decision_bridge`` and changes on its own schedule; a bulk-card test must
    not break when it does.

    Detection power is established by MUTATION instead, which does not rot: adding
    ``tool_bulk`` to ``_REMOTE_GATE_TYPES`` fails this test and its sibling above.
    """
    from systemu.messaging.decision_bridge import classify_resolution, RESOLUTION_FLOOR
    from systemu.runtime.first_gate_review import BULK_GATE_TYPE

    base = {
        "kind": "gate",
        "gate_type": BULK_GATE_TYPE,
        "verdict": "require_approval",
        "destructive": False,
    }
    # Every tag shape, including the ones a tag-axis change could flip in either
    # direction: absent, empty, unknown-only, and a positively-benign known tag.
    for tags in (None, [], ["unknown"], ["local_read"], ["net_read", "local_read"]):
        ctx = dict(base) if tags is None else dict(base, effect_tags=tags)
        assert classify_resolution(ctx) == RESOLUTION_FLOOR, (
            f"a bulk card became remotely resolvable with effect_tags={tags!r} — one tap "
            f"would bless an entire backfilled inventory")


# ── D. the card ──────────────────────────────────────────────────────────────

def test_the_card_safe_default_is_leave_gated_at_index_zero():
    """Fail-closed ordering, mirroring every other gate descriptor in this codebase."""
    from systemu.interface.command.gate import GateDescriptor
    from systemu.runtime.first_gate_review import (partition_entries, OPT_LEAVE_GATED)

    part = partition_entries([_entry("process_data", [])])
    d = GateDescriptor.from_first_gate_bulk(part, version="0.9.58")
    assert d.options[0] == OPT_LEAVE_GATED
    assert d.safe_default == OPT_LEAVE_GATED


def test_the_card_does_not_offer_a_bulk_allow_when_every_tool_is_deny_band():
    """The UX half of the carve-out: with nothing eligible there is nothing to sweep,
    so the affirmative option must not be rendered at all. (The rail resolves
    ``options[-1]`` — an offered-but-inert option is exactly the shape of the IMPL-2
    'Approve once on a DENY card' defect.)"""
    from systemu.interface.command.gate import GateDescriptor
    from systemu.runtime.first_gate_review import partition_entries, OPT_BULK_ALLOW

    part = partition_entries([_entry("ledger_sync", ["money_move", "unmapped"])])
    assert part.eligible == ()
    d = GateDescriptor.from_first_gate_bulk(part, version="0.9.58")
    assert OPT_BULK_ALLOW not in d.options


def test_no_offered_option_is_inert(suppose_the_effect_is_batch_approvable):
    """Every option on this card must DO something when resolved.

    An option that cannot act is the shape of the IMPL-2 adversarial finding ("Approve
    once" on a DENY card was a documented no-op that still minted a redeemable token).
    Here that means exactly two: the safe default, and the sweep — and the sweep only
    when there is something to sweep.
    """
    from systemu.interface.command.gate import GateDescriptor
    from systemu.runtime.first_gate_review import (partition_entries, OPT_BULK_ALLOW,
                                                   OPT_LEAVE_GATED)

    part = partition_entries([_entry("process_data", [])])
    d = GateDescriptor.from_first_gate_bulk(part, version="0.9.58")
    assert d.options == [OPT_LEAVE_GATED, OPT_BULK_ALLOW]


def test_with_nothing_eligible_the_affirmative_option_degrades_to_the_safe_default():
    """Defence in depth against BOTH zero-click paths at once.

    The rail's quick-approve and the Bypass auto-grant each resolve ``options[-1]``. Both
    are already blocked for this card — but when nothing is sweepable, ``options[-1]``
    is the safe default anyway, so even a future surface that reintroduces a one-click
    path cannot turn a DENY-only inventory into an approval.
    """
    from systemu.interface.command.gate import GateDescriptor
    from systemu.runtime.first_gate_review import partition_entries

    part = partition_entries([_entry("ledger_sync", ["money_move", "unmapped"])])
    d = GateDescriptor.from_first_gate_bulk(part, version="0.9.58")
    assert d.options[-1] == d.safe_default


def test_no_batch_classify_option_is_offered():
    """A deliberate spec deviation, pinned so it cannot be added back casually.

    The spec lists "batch classify" among the card's actions. Assigning an effect class
    is precisely what defeats the UNKNOWN conjunct (see ``action_governance
    ._effective_tags``), so a batch assignment would lift the ENTIRE excluded set into
    approvability in one click — the sweep IMPL-4 exists to prevent, and a
    friction-decreasing bulk action that §10 forbids. Classification stays per-tool
    under IMPL-2's typed confirm.
    """
    from systemu.interface.command.gate import GateDescriptor
    from systemu.runtime.first_gate_review import partition_entries

    part = partition_entries([_entry("process_data", []),
                              _entry("ledger_sync", ["money_move", "unmapped"])])
    d = GateDescriptor.from_first_gate_bulk(part, version="0.9.58")
    assert not any("classif" in o.lower() for o in d.options), (
        "no bulk classification action may be offered on this card")


def test_the_label_taken_OFF_THE_CARD_is_the_one_the_executor_acts_on(
        suppose_the_effect_is_batch_approvable):
    """Pins the card and the executor together across the decision store.

    ``OperatorDecisionQueue.resolve`` raises unless the choice is IN ``options``, so the
    operator can only ever send back a string the CARD offered. If the executor matched
    anything else — a one-character drift, a renamed constant on one side only — the
    affirmative click would resolve cleanly and then silently do NOTHING, which on this
    surface reads as "remembered" while every tool keeps prompting.

    So this feeds the executor the exact label off the rendered card, rather than the
    constant, and asserts it acts.
    """
    from systemu.interface.command.gate import GateDescriptor
    from systemu.runtime.first_gate_review import (apply_bulk_decision,
                                                   partition_entries)

    entry = _entry("process_data", [], signature="sig-ok")
    part = partition_entries([entry])
    d = GateDescriptor.from_first_gate_bulk(part, version="0.9.58")

    affirmative = d.options[-1]          # what the operator's click actually sends back
    store = _Store()
    assert apply_bulk_decision([entry], choice=affirmative, store=store) == ["sig-ok"]

    # and the safe default, likewise taken off the card, must do nothing
    store2 = _Store()
    assert apply_bulk_decision([entry], choice=d.safe_default, store=store2) == []


def test_the_card_names_the_excluded_deny_tools_and_their_remedy():
    """A refusal the operator cannot see is a dead end — IMPL-2's whole lesson."""
    from systemu.interface.command.gate import GateDescriptor
    from systemu.runtime.first_gate_review import partition_entries

    part = partition_entries([_entry("process_data", []),
                              _entry("ledger_sync", ["money_move", "unmapped"])])
    d = GateDescriptor.from_first_gate_bulk(part, version="0.9.58")
    assert "ledger_sync" in d.inspect
    assert "reclassif" in (d.inspect + d.what_approve_does).lower()


def test_the_card_says_how_many_are_swept_and_how_many_are_not(
        suppose_the_effect_is_batch_approvable):
    from systemu.interface.command.gate import GateDescriptor
    from systemu.runtime.first_gate_review import partition_entries

    part = partition_entries([_entry("process_data", []),
                              _entry("analyze_report", []),
                              _entry("ledger_sync", ["money_move", "unmapped"])])
    d = GateDescriptor.from_first_gate_bulk(part, version="0.9.58")
    assert "2" in d.what_approve_does and "1" in d.what_approve_does


def test_a_large_inventory_produces_a_BOUNDED_card(
        suppose_the_effect_is_batch_approvable):
    """A vault with hundreds of forged tools must not render an unbounded card.

    The card lists tools by name, so its size scales with the inventory. This payload is
    persisted in the decision context AND rendered in the dashboard, and an oversized
    render has already dropped the dashboard socket once (R-UX2, v0.10.20 — hence
    ``live_events_pane.clip_detail`` / ``_MAX_DETAIL_CHARS``). Every sibling descriptor
    bounds its variable-length field (``from_mcp_call`` clips args to 300 chars;
    ``_small_args_preview`` to 8 keys / 80 chars); this one must too.

    The COUNTS must stay exact and the excluded band must stay fully visible — a
    truncation that hid a DENY-band tool would defeat the card's purpose.
    """
    from systemu.interface.command.gate import GateDescriptor
    from systemu.runtime.first_gate_review import partition_entries, MAX_LISTED_PER_BAND

    many = [_entry(f"tool_{i:03d}", [], signature=f"s{i}") for i in range(400)]
    part = partition_entries(many)
    assert len(part.eligible) == 400

    d = GateDescriptor.from_first_gate_bulk(part, version="0.9.58")

    assert len(d.inspect) < 8000, "the rendered card must stay bounded"
    assert "400" in d.what_approve_does, "the COUNT must remain exact and un-truncated"
    assert "more" in d.inspect.lower(), "truncation must be disclosed, never silent"
    # the listing itself is capped
    listed = sum(1 for ln in d.inspect.splitlines() if ln.startswith("  tool_"))
    assert listed <= MAX_LISTED_PER_BAND


def test_truncation_never_hides_a_DENY_band_tool_when_the_band_is_small(
        suppose_the_effect_is_batch_approvable):
    """The excluded band is the safety-critical half of the card. A handful of DENY-band
    tools must ALL be named even when the eligible band is huge and gets clipped."""
    from systemu.interface.command.gate import GateDescriptor
    from systemu.runtime.first_gate_review import partition_entries

    many = [_entry(f"tool_{i:03d}", [], signature=f"s{i}") for i in range(400)]
    denied = [_entry(f"ledger_{i}", ["money_move", "unmapped"], signature=f"d{i}")
              for i in range(3)]
    part = partition_entries(many + denied)
    assert len(part.excluded) == 3

    d = GateDescriptor.from_first_gate_bulk(part, version="0.9.58")
    for i in range(3):
        assert f"ledger_{i}" in d.inspect, (
            "every DENY-band tool must be named — the operator cannot remediate a "
            "refusal they were never shown")


def test_the_card_is_high_risk_and_one_time_dedup():
    from systemu.interface.command.gate import GateDescriptor
    from systemu.runtime.first_gate_review import (partition_entries,
                                                   BULK_DEDUP_PREFIX)
    part = partition_entries([_entry("process_data", [])])
    d = GateDescriptor.from_first_gate_bulk(part, version="0.9.58")
    assert d.dedup == f"{BULK_DEDUP_PREFIX}0.9.58", "one card per version — idempotent"
    assert d.risk == "high"


# ── E. the auto-policy rails (a Bypass policy / a one-click rail approve) ─────

def test_the_bulk_gate_is_on_the_bypass_floor():
    """A Bypass policy previously auto-granted a gate with ZERO clicks via a
    born-resolved row. A new gate type that can be resolved that way must be on the
    floor, or the whole card is bypassable."""
    from systemu.interface.command.gate_mode import FLOOR_GATE_TYPES
    from systemu.runtime.first_gate_review import BULK_GATE_TYPE
    assert BULK_GATE_TYPE in FLOOR_GATE_TYPES


def test_a_bypass_policy_still_ASKS_for_the_bulk_card(
        suppose_the_effect_is_batch_approvable):
    """Behavioural, not just set-membership.

    The hazard is concrete: ``_synthetic_approved`` born-resolves an auto-granted gate
    with ``descriptor.options[-1]``, and for THIS card ``options[-1]`` IS the batch
    Always-allow. Under Bypass, without the floor, one boot would silently bless the
    entire backfilled inventory with zero operator clicks.
    """
    from systemu.interface.command.gate import GateDescriptor
    from systemu.interface.command.gate_mode import GateMode, GateModePolicy
    from systemu.runtime.first_gate_review import (BULK_GATE_TYPE, OPT_BULK_ALLOW,
                                                   partition_entries)

    part = partition_entries([_entry("process_data", [])])
    d = GateDescriptor.from_first_gate_bulk(part, version="0.9.58")
    assert d.options[-1] == OPT_BULK_ALLOW, (
        "precondition: the affirmative option really is the one auto-grant would pick")

    policy = GateModePolicy(mode=GateMode.BYPASS)
    assert policy.decide(risk=d.risk, gate_type=BULK_GATE_TYPE) == "ask", (
        "a Bypass policy must not auto-grant the bulk first-gate review")


def test_the_bulk_gate_is_not_one_click_approvable_from_the_rail():
    """The rail's quick-approve resolves ``options[-1]`` — here, the bulk Always-allow.
    ``tool_bulk:`` does NOT match the existing ``tool:`` prefix, so it must be added
    explicitly rather than assumed covered."""
    from systemu.interface.components.inbox_rail import (
        _RAIL_RENDER_ONLY_DEDUP_PREFIXES, _is_render_only_gate)
    from systemu.runtime.first_gate_review import BULK_DEDUP_PREFIX

    assert BULK_DEDUP_PREFIX in _RAIL_RENDER_ONLY_DEDUP_PREFIXES
    assert _is_render_only_gate(f"{BULK_DEDUP_PREFIX}0.9.58") is True


def test_the_rail_renders_no_approve_label_for_the_bulk_card():
    """End-to-end through the rail's own row model, not just the constant."""
    from systemu.interface.command.gate import GateDescriptor
    from systemu.interface.components.inbox_rail import _inbox_rail_rows
    from systemu.runtime.first_gate_review import partition_entries

    part = partition_entries([_entry("process_data", [])])
    d = GateDescriptor.from_first_gate_bulk(part, version="0.9.58")
    rows = _inbox_rail_rows([("dec-1", d)])
    assert rows[0]["render_only"] is True
    assert rows[0]["approve_label"] == "", (
        "a one-click rail approve would sweep every eligible tool into a standing "
        "allow with a single click and no partition shown")


# NOTE: ``policy=None`` at enqueue (the third zero-click path) is pinned BEHAVIOURALLY
# by ``test_a_card_IS_posted_when_there_is_something_to_review``, which asserts on the
# argument the queue actually receives.
#
# This file must NOT read source text via the inspect module. ``conftest
# .pytest_collection_modifyitems`` auto-tags any module whose TEXT contains that call
# as ``source_sensitive`` — a substring match, so even a mention of it in a COMMENT
# counts — and that DESELECTS THE WHOLE MODULE from ``pytest -m "not source_sensitive"``,
# the edit-safe gate developers run while working. An earlier revision of this file
# asserted on the source text of ``post_bulk_review_card`` and thereby removed all of
# these DENY-band pins from that gate. A safety pin that does not run in the fast gate
# is a pin nobody sees fail. Assert on BEHAVIOUR here, never on source text.
