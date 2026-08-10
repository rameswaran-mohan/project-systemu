"""F14 — the effect-tag data is INCOMPLETE and the backfill reports otherwise.

THE DEFECT (read off a REAL vault seeded by a real daemon boot):

    file_read      effect_tags=['local_read']   correct
    run_command    effect_tags=['shell_exec']   correct
    web_search     effect_tags=[]               WRONG - it reaches the network
    file_list_dir  effect_tags=[]               WRONG - it reads the filesystem
    format_date    effect_tags=[]               genuinely effect-free - but see below

while the SAME boot logged::

    [EffectTagBackfill] marker=0.10.22+g2 stamped=41 skipped_impl_path=0 errors=0
    [EffectTagIndexConverge] converged=41 cleared=0 unclassified_bodies=0 ... errors=0

``stamped=41`` counted BODIES WRITTEN, not tools classified — 24 of the 41 were
written with an EMPTY list.  ``unclassified_bodies=0`` counted bodies MISSING THE
KEY, and every body had the key (holding ``[]``).  Both numbers are read by an
operator as "41 tools are classified".  Completeness INFERRED, not WITNESSED
(project ruling DEC-27).

AND the model conflated two different facts: ``[]`` meant BOTH "we looked and this
tool has no effects" (``format_date``, a pure function) AND "nobody classified this"
(``web_search``).  The gate cannot tell them apart, so it refuses both — which is why
the first-run review card offered 0 of 30 tools.

THE PROPERTY (quantified over the whole shipped catalog, and over tools added later):

    Every tool in the shipped catalog carries a classification that distinguishes
    "no effects, verified" from "not classified"; no operator-facing counter reports a
    tool as classified unless its effects were actually determined; and batch-
    approvability is decided solely by an explicit allowlist over those classes.

THE FENCE: :func:`test_every_tool_in_the_shipped_catalog_is_classified` walks the
ENTIRE packaged starter pack through the REAL migrator and asserts each tool ends
either with real effect classes or with the explicit ``no_effect`` witness.  A new
starter tool landing unclassified fails it; it cannot slip through as ``[]``.
"""
from __future__ import annotations

import json

import pytest


# ── the catalog under test: the packaged seed vault, migrated for real ───────

@pytest.fixture(scope="module")
def migrated_catalog(tmp_path_factory):
    """The REAL packaged vault, migrated exactly the way the daemon migrates it.

    Not a fixture catalog: the whole point is that the product's own shipped tools
    are the population.  The vault dir MUST be named ``vault`` — a relative
    ``implementation_path`` anchors at the vault root's PARENT, which is how the
    runtime resolves it and how the backfill reads the body source.
    """
    from systemu.runtime.vault_migrator import run as migrate

    vault_dir = tmp_path_factory.mktemp("f14") / "vault"
    vault_dir.mkdir()
    migrate(vault_dir)

    index = json.loads((vault_dir / "tools" / "index.json").read_text(encoding="utf-8"))
    rows = {}
    for row in index:
        tid = row.get("id")
        body_path = vault_dir / "tools" / f"tool_{tid}.json"
        if not body_path.exists():
            continue
        body = json.loads(body_path.read_text(encoding="utf-8"))
        name = body.get("name") or row.get("name") or tid
        rows[name] = {
            "id": tid,
            "body_tags": body.get("effect_tags", None),
            "header_tags": row.get("effect_tags", None),
        }
    return {"vault_dir": vault_dir, "rows": rows}


# ── A. THE FENCE: completeness over the whole shipped catalog ────────────────

def test_every_tool_in_the_shipped_catalog_is_classified(migrated_catalog):
    """THE FENCE.  Every shipped tool ends CLASSIFIED — real effect classes, or the
    explicit ``no_effect`` witness meaning "we looked and there are none".

    An empty list is the UNCLASSIFIED state and is not an acceptable outcome for a
    tool the product itself ships: we control those bodies, so "we could not tell"
    is a defect in the classifier or an unhonest tool, never a shipping state.
    """
    from systemu.runtime.effect_tags import EffectTag

    rows = migrated_catalog["rows"]
    assert len(rows) >= 40, f"the seed catalog did not deploy: {len(rows)} tools"

    unclassified = sorted(n for n, r in rows.items() if not r["body_tags"])
    assert unclassified == [], (
        f"{len(unclassified)} of {len(rows)} shipped tools are UNCLASSIFIED "
        f"(effect_tags is empty/absent): {unclassified}")

    # ...and UNKNOWN is not a classification either — it is the sentinel for
    # "the scan could not resolve this body".
    unknown = sorted(n for n, r in rows.items()
                     if EffectTag.UNKNOWN.value in (r["body_tags"] or []))
    assert unknown == [], f"shipped tools the classifier could not resolve: {unknown}"


def test_the_header_agrees_with_the_body_for_every_shipped_tool(migrated_catalog):
    """The index header is what every live reader consumes (``vault.list_tools()``
    → ``capability_index.derive_index`` → the dashboard).  A body-only fix is
    invisible to all of them."""
    rows = migrated_catalog["rows"]
    disagree = {n: (r["body_tags"], r["header_tags"])
                for n, r in rows.items() if r["body_tags"] != r["header_tags"]}
    assert disagree == {}, f"header/body effect_tags disagree: {disagree}"


def test_no_effect_is_EXCLUSIVE_wherever_it_appears(migrated_catalog):
    """``no_effect`` asserts "there are none".  Carrying it alongside a real effect
    class is a self-contradicting record, and a reader that stops at the first tag
    would draw the opposite conclusion from a reader that stops at the second."""
    from systemu.runtime.effect_tags import EffectTag

    for name, row in migrated_catalog["rows"].items():
        tags = row["body_tags"] or []
        if EffectTag.NO_EFFECT.value in tags:
            assert tags == [EffectTag.NO_EFFECT.value], (name, tags)


# ── B. the model distinguishes verified-none from never-classified ───────────

def test_verified_no_effects_and_unclassified_are_DIFFERENT_values():
    """PROBLEM 2, at the vocabulary.  A pure body yields the positive ``no_effect``
    witness; a body whose effects cannot be determined yields NOTHING, and the two
    must not be the same value."""
    from systemu.runtime.effect_tags import EffectTag, classify_source

    pure = "from datetime import datetime\n" \
           "def run(s):\n    return datetime.strptime(s, '%Y-%m-%d').isoformat()\n"
    opaque = "import helpers\ndef run(**kw):\n    return helpers.do_whatever(kw)\n"

    assert classify_source(pure) == {EffectTag.NO_EFFECT}, (
        "a provably pure body must carry the positive verified-none witness")
    assert classify_source(opaque) == set(), (
        "a body whose effects cannot be determined must stay UNCLASSIFIED")


def test_a_pure_tool_is_batch_approvable_and_an_unclassified_one_is_not():
    """The consequence the operator sees: the safest thing in the catalog is freely
    batch-approvable, and an unclassified tool never is.  Today both are ``[]`` and
    the gate refuses both."""
    from systemu.runtime.action_governance import ActionContext, batch_approvable
    from systemu.runtime.effect_tags import EffectTag

    ok, _ = batch_approvable(ActionContext(tool="format_date",
                                           effect_tags={EffectTag.NO_EFFECT.value}))
    assert ok is True, "a verified effect-free tool must be batch-approvable"

    ok, reason = batch_approvable(ActionContext(tool="format_date", effect_tags=set()))
    assert ok is False and "classif" in reason.lower(), reason


def test_an_operator_may_not_hand_assign_the_verified_none_class():
    """``no_effect`` is a MACHINE-WITNESSED property (a purity proof over the body),
    not an operator assertion.  Offering it on the IMPL-2 reclassify menu would let a
    typed confirmation manufacture the very witness the fence relies on."""
    from systemu.interface.pages.inbox_page import _reclassify_choices
    from systemu.runtime.effect_tags import EffectTag

    choices = _reclassify_choices()
    assert EffectTag.NO_EFFECT.value not in choices
    assert EffectTag.UNKNOWN.value not in choices
    assert EffectTag.SHELL_EXEC.value in choices, "the menu must not be empty-by-accident"


# ── C. the vocabulary has honest classes for capture / actuation ─────────────

_ACTUATION_CLASSES = ("screen_capture", "clipboard_read", "clipboard_write",
                      "input_synthesis", "browser_actuate")


@pytest.mark.parametrize("value", _ACTUATION_CLASSES)
def test_the_vocabulary_has_a_class_for_each_real_world_capability(value):
    from systemu.runtime.effect_tags import EffectTag
    assert value in {t.value for t in EffectTag}, (
        f"{value} has no honest class; forcing it into local_read/local_write is "
        f"exactly the conflation F9 existed to eliminate")


# What each shipped actuator MUST carry.  Derived from the BODY, not the name:
#   take_screenshot   mss.grab           + writes the png     -> capture + local write
#   clipboard_read    pyperclip.paste                          -> clipboard read
#   clipboard_write   pyperclip.copy                           -> clipboard write
#   type_text         pynput keyboard.type                     -> input synthesis
#   keyboard_shortcut pynput keyboard.press/release            -> input synthesis
#   web_act           browser_pool + act_loop + page.goto/click-> browser actuation
_REQUIRED_TAGS = {
    "take_screenshot": {"screen_capture"},
    "clipboard_read": {"clipboard_read"},
    "clipboard_write": {"clipboard_write"},
    "type_text": {"input_synthesis"},
    "keyboard_shortcut": {"input_synthesis"},
    "web_act": {"browser_actuate"},
    "web_search": {"net_read"},
    "find_places": {"net_read"},
    "file_list_dir": {"local_read"},
    "file_scan_directory": {"local_read"},
    "file_append": {"local_write"},
    "notify_desktop": {"desktop_notify"},
    "read_excel_sheet": {"local_read"},
    "read_word_doc": {"local_read"},
    "create_excel_sheet": {"local_write"},
    "create_word_doc": {"local_write"},
    "image_resize": {"local_read", "local_write"},
    "compress_files": {"local_read", "local_write"},
    "extract_archive": {"local_read", "local_write"},
    "format_date": {"no_effect"},
    "detect_language_from_extension": {"no_effect"},
    "run_command": {"shell_exec"},
    "file_delete": {"local_delete"},
    "file_read": {"local_read"},
}


@pytest.mark.parametrize("name,required", sorted(_REQUIRED_TAGS.items()))
def test_the_shipped_body_classifies_to_what_it_actually_does(name, required,
                                                              migrated_catalog):
    rows = migrated_catalog["rows"]
    assert name in rows, f"{name} is not in the shipped catalog"
    got = set(rows[name]["body_tags"] or [])
    assert required <= got, f"{name}: expected {sorted(required)} ⊆ {sorted(got)}"


# ── D. the operator ruling of 2026-08-07, applied EXACTLY ────────────────────

# The operator was shown the risks IN FULL (screenshots and clipboard reads travel to
# the LLM provider; synthetic input lands in whatever window has focus) and ruled that
# these five ARE batch-approvable once correctly tagged.
_RULED_IN = ("take_screenshot", "clipboard_read", "type_text", "keyboard_shortcut",
             "web_act")
# The ruling did NOT extend to anything else.
_RULED_OUT = ("run_command", "run_cli_command", "launch_application",
              "close_application", "file_delete")


@pytest.mark.parametrize("name", _RULED_IN)
def test_the_five_ruled_tools_ARE_batch_approvable(name, migrated_catalog):
    from systemu.runtime.first_gate_review import (build_entry, partition_entries)

    row = migrated_catalog["rows"][name]
    e = build_entry(tool_id=row["id"], name=name, effect_tags=row["body_tags"] or [],
                    signature=f"sig::{name}")
    part = partition_entries([e])
    assert e in part.eligible, (
        f"{name} {row['body_tags']} is NOT batch-approvable — operator ruling "
        f"2026-08-07 says it must be")


@pytest.mark.parametrize("name", _RULED_OUT)
def test_the_ruling_did_not_widen_to_shell_or_delete(name, migrated_catalog):
    from systemu.runtime.first_gate_review import (batch_exclusion_reason, build_entry,
                                                   partition_entries)

    row = migrated_catalog["rows"][name]
    e = build_entry(tool_id=row["id"], name=name, effect_tags=row["body_tags"] or [],
                    signature=f"sig::{name}")
    part = partition_entries([e])
    assert e in part.excluded, f"{name} reached the batch"
    assert batch_exclusion_reason(e), f"{name} is excluded with no reason given"


def test_the_batch_allowlist_admits_the_ruled_classes_BY_DECISION():
    """An EXPLICIT, AUDITABLE allowlist entry — a reader must be able to see that
    capture and actuation are batch-approvable BY DECISION, and which decision."""
    from systemu.runtime.effect_tags import (BATCH_APPROVABLE,
                                             OPERATOR_RULED_BATCH_APPROVABLE_2026_08_07,
                                             EffectTag)

    ruled = OPERATOR_RULED_BATCH_APPROVABLE_2026_08_07
    assert ruled <= BATCH_APPROVABLE
    assert {t.value for t in ruled} == {"screen_capture", "clipboard_read",
                                        "input_synthesis", "browser_actuate"}
    # the ruling is a set of MEMBERS, not a weakening of the rule: everything else
    # is still refused by default.
    for t in EffectTag:
        if t not in BATCH_APPROVABLE:
            from systemu.runtime.effect_tags import is_batch_approvable_tag
            assert is_batch_approvable_tag(t.value) is False, t


def test_an_unruled_new_actuation_class_is_still_refused():
    """The allowlist semantics the docstring promises must survive the addition:
    a novel actuation class invented tomorrow is refused without a denylist."""
    from systemu.runtime.effect_tags import (is_batch_approvable_tag,
                                             register_effect_tag)
    register_effect_tag("holographic_actuate", high_severity=False)
    assert is_batch_approvable_tag("holographic_actuate") is False
    # and the two capture/actuation classes the ruling did NOT name stay out
    assert is_batch_approvable_tag("clipboard_write") is False
    assert is_batch_approvable_tag("desktop_notify") is False


def test_a_capture_tool_that_also_egresses_is_caught_by_the_SCAN_not_the_name():
    """THE RESIDUAL of ``_NAME_ESCALATION_EXEMPT``, pinned.

    Exempting the desktop capture/actuation classes from the name verb map means a
    ``screen_capture`` tool called ``upload_screen`` no longer picks up NET_MUTATE
    from the token "upload".  What must still catch it is the STRUCTURAL SCAN of the
    body — and it does, because a tool that really uploads has an upload sink.  If
    that ever stops being true this test says so, rather than the exemption quietly
    becoming a hole.
    """
    from systemu.runtime.action_governance import ActionContext, batch_approvable
    from systemu.runtime.effect_tags import classify_source

    body = ("import mss\nimport requests\n"
            "def run(**kw):\n"
            "    with mss.mss() as s:\n        img = s.grab(s.monitors[1])\n"
            "    requests.post('https://x.example/u', data=img)\n")
    tags = {t.value for t in classify_source(body)}
    assert "screen_capture" in tags, tags
    assert "net_mutate" in tags, (
        "the scan must see the upload — the name map no longer will")

    ok, reason = batch_approvable(ActionContext(tool="upload_screen",
                                                effect_tags=tags))
    assert ok is False and "net_mutate" in reason, reason


def test_the_name_map_still_escalates_a_BROWSER_tool(migrated_catalog):
    """``browser_actuate`` is deliberately NOT exempt from the name verb map: it is a
    network class, and the name is the only signal separating a page fetch from a
    payment or a message.  A browser tool whose name says "submit" must leave the
    batch even though its bare class is in the allowlist."""
    from systemu.runtime.action_governance import (ActionContext, batch_approvable,
                                                   effective_tags)

    ctx = ActionContext(tool="submit_expense_via_browser",
                        effect_tags={"browser_actuate"})
    assert "net_mutate" in effective_tags(ctx)
    ok, _ = batch_approvable(ctx)
    assert ok is False


def test_the_first_gate_card_offers_a_NON_ZERO_batch_from_the_real_catalog(
        migrated_catalog):
    """The user-visible payoff: the card offered 0 of 30 tools because every desktop
    actuator was ``[]``.  It must now offer the ruled ones, name the excluded ones,
    and still exclude shell/delete."""
    from systemu.interface.command.gate import GateDescriptor
    from systemu.runtime.first_gate_review import (collect_backfilled_entries,
                                                   partition_entries)

    entries = collect_backfilled_entries(migrated_catalog["vault_dir"])
    part = partition_entries(entries)
    offered = {e.name for e in part.eligible}

    assert len(part.eligible) > 0, "the card still offers 0 tools"
    for name in _RULED_IN:
        assert name in offered, f"{name} missing from the batch"
    for name in _RULED_OUT:
        assert name not in offered, f"{name} reached the batch"

    d = GateDescriptor.from_first_gate_bulk(part, version="0.10.22")
    for name in _RULED_IN:
        assert name in d.inspect, name
    assert "unclassified" not in d.inspect, (
        "a shipped tool is still rendered as unclassified on the card")


# ── E. the counters must mean what a reader takes them to mean ───────────────

def test_the_backfill_reports_CLASSIFIED_not_just_BODIES_WRITTEN(tmp_path):
    """PROBLEM 1.  ``stamped=41`` counted bodies WRITTEN.  The reported number must
    now be the number whose effects were actually DETERMINED, and it must equal what
    is on disk."""
    from systemu.runtime.vault_migrator import backfill_effect_tags
    from systemu.runtime.vault_migrator import run as migrate

    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    migrate(vault_dir)

    out = backfill_effect_tags(vault_dir, force=True)
    assert "classified" in out and "unclassified" in out, out
    assert out["bodies_written"] == out["classified"] + out["unclassified"], out

    on_disk = 0
    for p in (vault_dir / "tools").glob("tool_*.json"):
        body = json.loads(p.read_text(encoding="utf-8"))
        if body.get("effect_tags"):
            on_disk += 1
    assert out["classified"] == on_disk, (out, on_disk)
    assert out["unclassified"] == 0, (
        f"{out['unclassified']} shipped bodies ended unclassified")


def test_the_converge_pass_counts_EMPTY_tag_lists_not_only_missing_keys(tmp_path):
    """``unclassified_bodies=0`` was true and useless: every body HAD the key, holding
    ``[]``.  The pass must report the empty ones separately, under a name that cannot
    be read as "0 tools are unclassified"."""
    from systemu.runtime.vault_migrator import converge_index_effect_tags
    from systemu.runtime.vault_migrator import run as migrate

    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    migrate(vault_dir)

    # damage one body the way the pre-fix migrator did: key present, value empty
    idx = json.loads((vault_dir / "tools" / "index.json").read_text(encoding="utf-8"))
    tid = idx[0]["id"]
    body_path = vault_dir / "tools" / f"tool_{tid}.json"
    body = json.loads(body_path.read_text(encoding="utf-8"))
    body["effect_tags"] = []
    body_path.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")

    out = converge_index_effect_tags(vault_dir)
    assert out["bodies_with_empty_tags"] == 1, out
    assert out["bodies_missing_tag_key"] == 0, out
