"""P9 -- the operator-facing card corpus in `gate.py` was not ASCII.

THE DEFECT
    v0.10.29 and v0.10.30 made every other verdict surface ASCII (DEC-32c): a
    console that cannot encode a line is a console the operator reads NOTHING
    on, and on Windows the daemon's console is cp1252. `gate.py` was missed.
    Its card strings -- titles, `what_approve_does`, the DENY refusal copy, the
    bulk-review listing -- carried roughly forty em dashes and two real
    ellipses, and those strings reach ConPTY and the CLI decision table.

    An em dash IS encodable in cp1252, which is why this survived; the two
    U+2026 ellipses and any future non-latin-1 character are not, and the rule
    that keeps the corpus safe cannot be "only use the non-ASCII characters
    that happen to round-trip in one legacy codepage".

THE PROPERTY
    Every operator-facing string in `gate.py` is ASCII: `--` for an em dash,
    `...` for an ellipsis. Pinned on BOTH halves -- the module-level constants,
    and the strings the card builders actually PRODUCE for each gate kind,
    because most of this copy is f-string fragments that no constant scan sees.

    Comments and docstrings are exempt and stay as they are: nothing reads them
    on a console.

TWO EXEMPTIONS, and why neither is this commit's to make
    (BOTH ARE NOW CLOSED -- see the note at the end of this docstring.)

    1. ``RECLASSIFY_OPTION`` ends in a real ellipsis (U+2026). It is not just
       copy: it is a decision OPTION LABEL that `decision_queue.resolve`
       validates choice-in-options against, the literal is duplicated into
       ``cli_commands.py``'s refusal copy (a file this change may not touch),
       and ``tests/test_impl2_reclassify_gate.py:781`` pins the exact string.
       Changing it here alone would leave the CLI naming a button that no
       longer exists.

    2. The BULK FIRST-GATE CARD (`from_first_gate_bulk`). Its exact bytes are
       frozen by two golden SHAs in ``tests/test_impl4_bulk_first_gate.py``, on
       the operator's ruling that the card's text does not move as a side
       effect. Cleaning it up is a legitimate change AND a deliberate,
       visible re-freeze -- not a typography pass quietly editing the pin that
       exists to catch exactly that.

    Both are named as data below, both exemption sets are themselves asserted,
    and the characters the second one covers are bounded -- so nothing worse
    can arrive under cover of either carve-out.

BOTH EXEMPTIONS ARE CLOSED (v0.10.30, P9)
    They are left described above because the REASONING is the record of why
    neither could be taken as a side effect, and because the tests that
    enforced their bounds are still here doing so.

      * P9(a) removed the first: ``cli_commands.py`` now IMPORTS the constant
        instead of re-typing it, so the label could move, and it did --
        ``"Reclassify effect..."``. ``_EXEMPT_CONSTANTS`` is empty and
        `test_no_module_constant_is_exempt_any_more` asserts the stronger
        property.
      * P9(b) removed the second, as the DELIBERATE, VISIBLE re-freeze this
        docstring asked for: the bulk card is ASCII and both golden SHAs in
        ``tests/test_impl4_bulk_first_gate.py`` were recomputed in the same
        commit, with the old and new digests stated in its message.
        ``_FROZEN_BULK_KINDS`` is kept as-is here -- its bound
        (`found <= allowed`) simply holds vacuously now -- and the positive
        property is pinned in
        ``tests/test_dogfood28_p9b_bulk_first_gate_card_is_ascii.py``.

NO CARD IS ENQUEUED AND NO GATE IS RESOLVED. Every builder is called directly
on synthesized inputs.
"""
from __future__ import annotations

import ast
import io
from types import SimpleNamespace

import pytest

from systemu.interface.command import gate as gate_mod
from systemu.interface.command.gate import GateDescriptor


#: EMPTY as of P9(a) (v0.10.30). ``RECLASSIFY_OPTION`` was the one exemption:
#: its literal was duplicated into ``cli_commands.py``, so this file could not
#: change it alone without leaving the CLI naming a button that no longer
#: existed. That duplicate is now an IMPORT of the constant, the label is
#: ``"Reclassify effect..."``, and the carve-out has nothing left to cover.
#:
#: The tuple is kept rather than deleted so the assertion below still says what
#: it says: not "the exemption is the one we documented" but "there is no
#: exemption at all", which is strictly stronger and is what should now hold.
_EXEMPT_CONSTANTS = ()


def _non_ascii(text):
    return sorted({c for c in text if ord(c) > 127})


def _assert_ascii(label, text):
    bad = _non_ascii(text)
    assert not bad, (
        "{}: operator-facing copy carries non-ASCII {} -- this string reaches a "
        "cp1252 console, where an unencodable character is a UnicodeEncodeError "
        "in place of the card (DEC-32c). Use '--' for an em dash and '...' for "
        "an ellipsis.\n  {!r}".format(label, [hex(ord(c)) for c in bad], text))


# --------------------------------------------------------------------------- #
# 1. the module-level constants
# --------------------------------------------------------------------------- #

def _module_string_constants():
    """Every module-level assignment whose value is a string literal.

    Read from the SOURCE rather than from `vars(gate_mod)`, so a constant built
    by concatenation is seen as the fragments the author wrote (and each is
    checked), and so a docstring is never mistaken for a constant.
    """
    source = io.open(gate_mod.__file__, encoding="utf-8").read()
    tree = ast.parse(source)
    out = []
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        names = [t.id for t in targets if isinstance(t, ast.Name)]
        if not names:
            continue
        value = node.value
        if value is None:
            continue
        for sub in ast.walk(value):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                out.append((names[0], sub.value))
    return out


def test_the_module_level_card_constants_are_ascii():
    """`_BULK_CARD_PLAIN_LEAD` is the FIRST line a new operator ever reads on a
    security decision. It, and every sibling constant, is checked here."""
    constants = _module_string_constants()
    assert constants, "the constant scan found nothing; it has stopped pinning"

    for name, value in constants:
        if name in _EXEMPT_CONSTANTS:
            continue
        _assert_ascii("gate.{}".format(name), value)


def test_no_module_constant_is_exempt_any_more():
    """A carve-out that grows silently is not a carve-out. This used to read
    ``== ["RECLASSIFY_OPTION"]``; P9(a) removed that exemption's reason for
    existing (the duplicated literal in `cli_commands.py` is now an import), so
    the assertion is the stronger one: NO module constant in `gate.py` is
    non-ASCII, and adding one means writing its reason down first."""
    assert _EXEMPT_CONSTANTS == ()
    non_ascii_constants = sorted({
        name for name, value in _module_string_constants() if _non_ascii(value)})
    assert non_ascii_constants == [], (
        "gate.py has grown a non-ASCII module constant, and there is no longer "
        "any exemption for one: " + repr(non_ascii_constants))


# --------------------------------------------------------------------------- #
# 2. the strings the builders PRODUCE, per gate kind
# --------------------------------------------------------------------------- #

def _bulk_partition():
    from systemu.runtime.first_gate_review import build_entry, partition_entries

    entries = [
        build_entry(tool_id="t_ok", name="read_a_file",
                    effect_tags=["local_read"], signature="sig::ok"),
        build_entry(tool_id="t_bad", name="run_command",
                    effect_tags=["shell_exec"], signature="sig::bad"),
        build_entry(tool_id="t_none", name="mystery_tool",
                    effect_tags=[], signature="sig::none"),
    ]
    return partition_entries(entries)


def _excluded_only_partition():
    from systemu.runtime.first_gate_review import build_entry, partition_entries

    return partition_entries([
        build_entry(tool_id="t_bad", name="run_command",
                    effect_tags=["shell_exec"], signature="sig::bad")])


def _many_excluded_partition():
    """Enough excluded tools to force BOTH bounded renders: the per-tool reason
    clip and the "...and N more" disclosure."""
    from systemu.runtime.first_gate_review import build_entry, partition_entries

    return partition_entries([
        build_entry(tool_id="t{}".format(i),
                    name="file_scan_directory_variant_{:03d}".format(i),
                    effect_tags=[], signature="s{}".format(i))
        for i in range(120)])


def _card_builders():
    """One card of every kind this module builds, with the inputs that reach the
    copy under test. Named so a failure says which gate kind is wrong."""
    action = SimpleNamespace(kind="reinstall", scope_kind="tool", scope_id="t1",
                             reason="a dry-run failed", fix_url="",
                             fix_command="", severity="blocker")
    proposal = SimpleNamespace(evolution_type="refine", priority="high",
                               id="evo1", target_entity_type="tool",
                               description="tighten the prompt",
                               rationale="it drifts")
    blocked = [SimpleNamespace(name="write_report", status="proposed",
                               dry_run_status="not_run", enabled=False)]
    harness_request = SimpleNamespace(
        kind=SimpleNamespace(value="capability"), request_id="req1",
        rationale="needs the network", spec={})
    harness_verdict = SimpleNamespace(risk_band=SimpleNamespace(value="high"))

    return {
        "scroll": lambda: GateDescriptor.from_scroll(
            SimpleNamespace(name="a scroll", id="s1"), summary="what it does"),
        "harness": lambda: GateDescriptor.from_harness(
            harness_request, harness_verdict, execution_id="e1"),
        "oauth": lambda: GateDescriptor.from_oauth_url(
            server_id="srv", authorize_url="https://example.invalid/auth",
            execution_id="e1"),
        "dep": lambda: GateDescriptor.from_dep(
            {"package": "requests", "first_seen_tool": "fetch",
             "first_seen_tool_id": "t1", "request_count": 2}),
        "command": lambda: GateDescriptor.from_command(
            tool_name="run_command", command="rm -rf build", cwd="/tmp",
            reason="destructive"),
        "tool": lambda: GateDescriptor.from_tool(
            tool_name="write_file", sig="sig1", effect_tags=["local_write"]),
        "tool_deny": lambda: GateDescriptor.from_tool(
            tool_name="mystery", sig="sig2", verdict="deny",
            reason="unclassifiable effect", effect_tags=[]),
        "tool_reclassified": lambda: GateDescriptor.from_tool(
            tool_name="mystery", sig="sig3", reclassified=True,
            assigned_class="local_write", effect_tags=[]),
        "bulk": lambda: GateDescriptor.from_first_gate_bulk(
            _bulk_partition(), version="0.10.30"),
        "bulk_nothing_eligible": lambda: GateDescriptor.from_first_gate_bulk(
            _excluded_only_partition(), version="0.10.30"),
        "bulk_truncated": lambda: GateDescriptor.from_first_gate_bulk(
            _many_excluded_partition(), version="0.10.30"),
        "mcp": lambda: GateDescriptor.from_mcp_call(
            server="srv", tool="write", params={"path": "x"}),
        "forge": lambda: GateDescriptor.from_forge(
            {"id": "t1", "name": "write_report", "description": "writes it"}),
        "blocked_tools": lambda: GateDescriptor.from_blocked_tools(
            SimpleNamespace(id="a1", name="the activity"), blocked),
        "evolution": lambda: GateDescriptor.from_evolution(proposal),
        "recovery": lambda: GateDescriptor.from_recovery_action(action),
    }


#: The descriptor fields an operator actually reads.
_OPERATOR_FIELDS = ("title", "inspect", "what_approve_does", "safe_default")

#: THE SECOND EXEMPTION. The bulk first-gate card's exact bytes are frozen by two
#: golden SHAs in ``tests/test_impl4_bulk_first_gate.py``, on the operator's own
#: ruling that its TEXT does not move as a side effect of anything. Making it
#: ASCII is a legitimate change and a DELIBERATE, VISIBLE re-freeze -- not a
#: typography commit quietly editing the pin that exists to stop exactly that.
#: See `test_the_frozen_bulk_card_is_the_second_exemption_and_its_pin_is_real`.
_FROZEN_BULK_KINDS = ("bulk", "bulk_nothing_eligible", "bulk_truncated")


def _strip_foreign(text):
    """`text` minus the fragments another module wrote into it.

    THE SCOPE OF THIS FINDING IS `gate.py`. Two of its cards quote text built
    elsewhere and gate.py only interpolates it:

      * ``first_gate_review.batch_rule_sentence()`` -- the CONSENT RECORD on
        the bulk card, quoted whole into both `inspect` and `what_approve_does`.
      * ``first_gate_review.batch_exclusion_reason(entry)`` -- the per-tool
        "why it was excluded" clause, which gate.py clips and appends after
        ``[excluded:``.
      * ``RECLASSIFY_OPTION`` -- this module's documented exemption, quoted into
        the DENY card's copy.

    Those are subtracted so this file pins what it is entitled to pin: an em
    dash gate.py writes itself survives the subtraction and fails. The residue
    is asserted separately, as a SUBSET, so fixing the other module later does
    not turn one of these tests red.
    """
    from systemu.runtime.first_gate_review import batch_rule_sentence

    out = text.replace(batch_rule_sentence(), " ")
    out = out.replace(gate_mod.RECLASSIFY_OPTION, " ")
    kept = []
    for line in out.splitlines():
        # The reason is clipped by gate.py, so the foreign half cannot be
        # matched by value -- everything from the marker on is theirs.
        kept.append(line.split("[excluded:", 1)[0])
    return "\n".join(kept)


@pytest.mark.parametrize("kind", sorted(set(_card_builders()) - set(_FROZEN_BULK_KINDS)))
def test_every_gate_kinds_card_is_ascii(kind):
    """THE PIN. Most of this copy is f-string fragments, so a constant scan
    alone would have passed over the em dashes in `from_blocked_tools` and the
    three branches of `from_tool`."""
    card = _card_builders()[kind]()

    for field in _OPERATOR_FIELDS:
        _assert_ascii("{}.{}".format(kind, field),
                      _strip_foreign(getattr(card, field)))
    for i, option in enumerate(card.options):
        # P9(a): the skip that used to sit here ("the documented exemption")
        # is gone with the exemption. Every option an operator can click is
        # checked, RECLASSIFY_OPTION included.
        _assert_ascii("{}.options[{}]".format(kind, i), option)


def test_the_frozen_bulk_card_is_the_second_exemption_and_its_pin_is_real():
    """The bulk first-gate card is NOT cleaned up here, and the reason is
    checkable rather than a story.

    ``tests/test_impl4_bulk_first_gate.py`` freezes that card's exact bytes
    under two golden SHAs, deliberately: "the ruling moved the timing and
    NOTHING else", so any change to the text the operator consents to must be a
    visible, separate re-freeze rather than a side effect of a typography pass.
    Editing an existing pin to let this commit through is exactly what that pin
    exists to prevent.

    So: the exemption is named, its justification is asserted to still exist,
    and the characters it covers are BOUNDED -- an em dash and an ellipsis and
    nothing else, so no worse character can arrive under cover of the carve-out.
    """
    import io as _io
    import pathlib

    assert _FROZEN_BULK_KINDS == ("bulk", "bulk_nothing_eligible",
                                  "bulk_truncated")

    pin = (pathlib.Path(__file__).parent / "test_impl4_bulk_first_gate.py")
    text = _io.open(pin, encoding="utf-8").read()
    for golden in ("_BULK_CARD_SHA_REAL", "_BULK_CARD_SHA_WITH_ELIGIBLE"):
        assert golden in text, (
            "the golden that justifies this exemption is gone from "
            + pin.name + "; the bulk card can and should be made ASCII now")

    allowed = {"—", "…"}          # em dash, horizontal ellipsis
    for kind in _FROZEN_BULK_KINDS:
        card = _card_builders()[kind]()
        found = {c for field in _OPERATOR_FIELDS
                 for c in getattr(card, field) if ord(c) > 127}
        assert found <= allowed, (
            kind + ": the frozen bulk card grew a non-ASCII character that is "
            "not one of the two the exemption covers: " + repr(sorted(found)))


def test_whatever_non_ascii_survives_came_from_a_named_source():
    """The subtraction in `_strip_foreign` is only honest if it is BOUNDED.
    Every non-ASCII character still reaching the operator on a NON-frozen card
    must be traceable to `batch_rule_sentence` / `batch_exclusion_reason` (both
    in `systemu/runtime/first_gate_review.py` -- a separate file and a separate
    change) or to `RECLASSIFY_OPTION`.

    Asserted as a SUBSET so this stays green when those are cleaned up too, and
    so a NEW foreign source cannot appear unnoticed.
    """
    from systemu.runtime.first_gate_review import (batch_exclusion_reason,
                                                   batch_rule_sentence)

    partition = _bulk_partition()
    foreign = set(batch_rule_sentence())
    for entry in list(partition.excluded) + list(partition.eligible):
        foreign |= set(batch_exclusion_reason(entry))
    foreign |= set(gate_mod.RECLASSIFY_OPTION)

    leftover = set()
    for kind, build in _card_builders().items():
        if kind in _FROZEN_BULK_KINDS:
            continue
        card = build()
        leftover |= {c for field in _OPERATOR_FIELDS
                     for c in getattr(card, field) if ord(c) > 127}

    assert leftover <= foreign, (
        "a non-ASCII character on a card came from neither "
        "batch_rule_sentence, batch_exclusion_reason nor RECLASSIFY_OPTION: "
        + repr(sorted(leftover - foreign)))


def test_the_builder_sweep_actually_reaches_the_copy_under_test():
    """The sweep above is only worth its name if the cards it builds really do
    carry the sentences this finding is about. Asserted directly, because a
    builder that silently returned an empty card would make every ASCII
    assertion vacuously true."""
    cards = {kind: build() for kind, build in _card_builders().items()}

    assert "Task blocked" in cards["blocked_tools"].title
    assert "write_report" in cards["blocked_tools"].inspect
    assert "dry-run: not_run" in cards["blocked_tools"].inspect
    assert "cannot be remembered" in cards["tool_deny"].what_approve_does
    assert "single-use" in cards["tool_reclassified"].what_approve_does
    assert "picks up on its own" in cards["tool"].what_approve_does
    assert "excluded:" in cards["bulk"].inspect
    assert "acknowledges this list" in \
        cards["bulk_nothing_eligible"].what_approve_does
    assert "more" in cards["bulk_truncated"].inspect.lower()


def test_the_repaired_lines_still_read_as_sentences():
    """`--` where an em dash was, not a deleted clause. A typography pass that
    quietly drops half a sentence is a copy defect, not a fix -- and these three
    sentences are the ones that tell an operator what an approval costs."""
    tool = GateDescriptor.from_tool(tool_name="write_file", sig="s",
                                    effect_tags=["local_write"])
    deny = GateDescriptor.from_tool(tool_name="mystery", sig="s2",
                                    verdict="deny", effect_tags=[])
    reclassified = GateDescriptor.from_tool(
        tool_name="mystery", sig="s3", reclassified=True,
        assigned_class="local_write", effect_tags=[])
    blocked = GateDescriptor.from_blocked_tools(
        SimpleNamespace(id="a1", name="the activity"),
        [SimpleNamespace(name="write_report", status="proposed",
                         dry_run_status="not_run", enabled=False)])

    assert "picks up on its own -- you do not need to start it again" in \
        tool.what_approve_does
    assert "cannot be remembered -- you will be asked again every time" in \
        deny.what_approve_does
    assert "cannot be remembered -- the next identical call is gated again" in \
        reclassified.what_approve_does
    assert blocked.title == "Task blocked -- 1 tool(s) not ready"
    assert "write_report -- proposed, disabled, dry-run: not_run" in \
        blocked.inspect
