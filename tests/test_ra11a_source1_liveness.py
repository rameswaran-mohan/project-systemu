"""R-A11a source #1 (granted-root FileHandle) LIVENESS + BOTH IMPL-5 taint pins.

Source #1 resolves a required path leaf to a file inside an operator-granted root.
It was DORMANT in production for exactly the reason ``_value_digest`` was: the binder
resolved its ``GrantedRootsStore`` off ``ctx._granted_roots`` / ``ctx.vault``, and a
real ``ExecutionContext`` carries NEITHER attribute (``_granted_roots`` is READ in one
place in all of ``systemu/`` and WRITTEN in none). ``bc.granted`` was therefore ``None``
on every production bind, and ``reference_resolver`` FAIL-CLOSES on a ``None`` store —
it drops every candidate — so source #1 never once fired in a real run. Every binder
test nonetheless passed, because each hands the binder a hand-built fixture that sets
``_granted_roots`` by hand: a shape the producer never emits.

What the operator lost, executed: the required path leaf fell through to a bare
``state="missing"`` gap with ``bound_value_ref=None``, so ``requirement_to_field``
rendered an EMPTY text box ("no source bound this required leaf") instead of a
pre-filled one-click confirm — ``default`` is populated FROM ``bound_value_ref``. The
keyed ``bound_value_digest`` was absent for the same reason.

The repair mirrors the ``_value_digest`` fix EXACTLY: the vault is threaded EXPLICITLY
from the call site. It is deliberately NOT hung on ``ExecutionContext`` (that object is
serialized and snapshotted, so a live handle on it is a snapshot-shape hazard). Pin (a)
fails the day someone takes that shortcut.

BOTH taint directions are pinned, because waking a dormant source is precisely the
change that can regress IMPL-5 invisibly:
  * (c) the newly-live source #1 STILL clamps to ``content_derived`` and STILL routes
    to the operator confirm even at confidence 1.0 — it must never silent-bind, and
  * (d) a legitimately TRUSTED bind in the SAME report STILL binds SILENTLY. Without
    this second direction a "clamp everything" repair would satisfy (c) perfectly
    while re-introducing the R-A12c over-ask defect.
"""
from __future__ import annotations

import pathlib

import pytest

from systemu.runtime import requirement_binder as rb
from systemu.runtime.context_builder import ExecutionContext
from systemu.runtime.elicitation import requirement_to_field
from systemu.runtime.granted_roots import GrantedRootsStore
from systemu.runtime.situational_inventory import (
    FileHandleLite,
    SituationReport,
    build_roots,
)
from systemu.vault.vault import Vault


# ── fixtures derived from the REAL producers (never hand-built) ─────────────
def _real_ctx(intent: str = "summarize my resume") -> ExecutionContext:
    """THE PRODUCTION OBJECT, through its real constructor — not a stand-in."""
    return ExecutionContext(
        execution_id="exec-src1",
        system_prompt="sp",
        scroll_json=[],
        tool_index=[],
        use_objectives=True,
        scroll_intent=intent,
    )


def _granted_world(tmp_path, filename: str = "resume.pdf"):
    """A real Vault + a real granted root holding a real file, surveyed by the REAL
    producer. Returns ``(vault, store, situation, target)``.

    ``situation`` is ``SituationReport.model_dump()`` over ``build_roots(store)`` —
    identical in shape to what ``survey_situation`` stashes on
    ``context._situation_report``. NOTHING here is hand-written, so the fixture cannot
    drift from the producer (the dominant defect class this bug belongs to)."""
    vault = Vault(root=tmp_path / "vault")
    work = tmp_path / "work"
    work.mkdir(parents=True, exist_ok=True)
    target = work / filename
    target.write_bytes(b"%PDF-1.4 content")

    store = GrantedRootsStore(base_dir=vault.root)
    store.grant(str(work))

    situation = SituationReport(roots=build_roots(store)).model_dump()
    return vault, store, situation, target


class _Obj:
    id = 1
    goal = "summarize my resume"
    success_criteria = "a summary of the resume file"


class _Cap:
    """One PATH leaf (source #1's territory) + one leaf a TRUSTED source fills."""

    name = "summarize_file"
    parameters_schema = {
        "type": "object",
        "properties": {
            "input_path": {"type": "string", "description": "path to the file to read"},
            "output_format": {"type": "string", "description": "rendering style"},
        },
        "required": ["input_path", "output_format"],
    }


def _by_path(reqs, schema_path):
    for r in reqs:
        if r.schema_path == schema_path:
            return r
    return None


# ── (a) the ctx-SHAPE fixture-realism pin ───────────────────────────────────
def test_a_real_execution_context_carries_neither_vault_nor_granted_roots():
    """THE SHAPE PIN — the direct sibling of the ``_value_digest`` ctx-shape pin.

    Both attributes the binder consults for source #1 are ABSENT from the real
    production object. This fails the day someone "fixes" the threading by hanging a
    live vault (or a live GrantedRootsStore) on the context — the wrong repair, since
    the context is serialized and snapshotted."""
    ctx = _real_ctx()
    assert not hasattr(ctx, "vault"), (
        "a real ExecutionContext must NOT carry a vault — it is serialized and "
        "snapshotted. Thread the vault through build_requirement_report instead.")
    assert not hasattr(ctx, "_granted_roots"), (
        "a real ExecutionContext must NOT carry a GrantedRootsStore either. Any test "
        "fixture that sets _granted_roots is using a shape the producer never emits — "
        "exactly why source #1 looked healthy while it was dead in production.")


@pytest.mark.source_sensitive
def test_granted_roots_is_never_assigned_anywhere_in_the_package():
    """The fixture-realism pin WITH TEETH: a fresh-object ``hasattr`` check cannot see
    an attribute some later code path sets mid-run. This asserts the stronger fact the
    whole diagnosis rests on — nothing in ``systemu/`` ever ASSIGNS ``_granted_roots``,
    so the ctx seam is a test-only injection point and the threaded vault is the ONLY
    production route. If a real producer ever starts setting it, this fails and the
    story above must be re-derived rather than quietly rotting into a second dormant
    source.

    Marked source_sensitive deliberately: it reads package source, so it belongs OUT
    of the edit-safe tier even though it uses no snapshot comparison."""
    import systemu

    root = pathlib.Path(systemu.__file__).parent
    writers = []
    for py in root.rglob("*.py"):
        try:
            text = py.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if "._granted_roots" not in stripped or "==" in stripped:
                continue
            lhs = stripped.split("=", 1)[0] if "=" in stripped else ""
            if "._granted_roots" in lhs:
                writers.append(f"{py.relative_to(root)}:{lineno}: {stripped}")
    assert writers == [], (
        "something now ASSIGNS ctx._granted_roots in production:\n  "
        + "\n  ".join(writers)
        + "\nRe-derive the source-#1 threading story before trusting these pins.")


def test_the_salient_handle_fixture_shape_is_exactly_what_the_producer_emits(tmp_path):
    """FIXTURE-REALISM, the data side: the dict the resolver reads must be the dict
    ``build_roots`` actually produces. A hand-written salient handle that invents or
    omits a key would diverge here."""
    _vault, _store, situation, _target = _granted_world(tmp_path)

    handles = [fh for root in situation["roots"] for fh in (root.get("salient") or [])]
    assert len(handles) == 1, f"the real producer emitted {len(handles)} handle(s)"
    produced_keys = set(handles[0])
    assert produced_keys == set(FileHandleLite.model_fields), (
        "the surveyed handle's keys drifted from FileHandleLite — any fixture built to "
        f"the old shape is now unreal. produced={sorted(produced_keys)}")
    for key in ("path", "name", "ext", "mtime"):
        assert key in produced_keys, f"resolver reads {key!r}; the producer omits it"
    assert handles[0]["origin_class"] == "content_derived"


# ── (b) the liveness counterfactual ─────────────────────────────────────────
def test_source1_is_dormant_without_the_threaded_vault(tmp_path):
    """THE BUG, pinned as a counterfactual. Same real ctx, same real situation, NO
    vault threaded ⇒ ``bc.granted`` is None ⇒ the resolver fail-closes ⇒ the required
    path leaf falls all the way through to a bare ``missing`` gap, even though the
    file sits in a granted root and the objective names it."""
    _vault, _store, situation, _target = _granted_world(tmp_path)

    reqs = rb.compute_requirements(_Obj(), _Cap(), situation, _real_ctx())
    req = _by_path(reqs, "input_path")
    assert req is not None
    assert req.state == "missing", (
        f"expected the dormant-source gap, got state={req.state} "
        f"source={req.source} ref={req.bound_value_ref}")
    assert req.source == "schema"
    assert req.bound_value_ref is None


def test_source1_goes_live_once_the_vault_is_threaded(tmp_path):
    """THE FIX. The identical call with ``vault=`` threaded resolves the granted-root
    file and binds it — source #1 finally fires in a run driven by the real context."""
    vault, _store, situation, _target = _granted_world(tmp_path)

    reqs = rb.compute_requirements(_Obj(), _Cap(), situation, _real_ctx(), vault=vault)
    req = _by_path(reqs, "input_path")
    assert req is not None
    assert req.source == "situation", (
        f"source #1 did not fire: source={req.source} state={req.state}")
    assert req.bound_value_ref.startswith("file:")
    assert req.bound_value_ref.lower().endswith("resume.pdf")


def test_the_thread_survives_build_requirement_report(tmp_path):
    """The PRODUCTION seam: both shadow_runtime call sites reach source #1 through
    ``build_requirement_report(..., vault=self.vault)``, so the thread must survive
    that hop — this is the function the runtime actually calls."""
    vault, _store, situation, _target = _granted_world(tmp_path)

    rep = rb.build_requirement_report([_Obj()], _Cap(), situation, _real_ctx(), vault=vault)
    req = _by_path(rep.per_objective[1], "input_path")
    assert req is not None and req.source == "situation"
    assert req.bound_value_digest is not None, (
        "the vault is threaded, so the bind must also carry its keyed digest")


def test_the_operator_gets_a_prefilled_confirm_instead_of_a_blank_box(tmp_path):
    """WHAT WAS LOST, pinned at the operator-facing surface. ``requirement_to_field``
    populates ``default`` FROM ``bound_value_ref``; a dormant source #1 leaves that
    None, so the operator is asked to type a full path by hand for a file that is
    sitting in a root they already granted."""
    vault, _store, situation, _target = _granted_world(tmp_path)

    dormant = _by_path(
        rb.compute_requirements(_Obj(), _Cap(), situation, _real_ctx()), "input_path")
    live = _by_path(
        rb.compute_requirements(_Obj(), _Cap(), situation, _real_ctx(), vault=vault),
        "input_path")

    assert "default" not in requirement_to_field(dormant), (
        "precondition: the dormant gap must render as an empty box")
    live_field = requirement_to_field(live)
    assert live_field.get("default") == live.bound_value_ref
    assert live_field["default"].lower().endswith("resume.pdf")


# ── (c) taint direction A: the newly-live source STILL clamps ───────────────
def test_newly_live_source1_still_clamps_and_never_silent_binds(tmp_path):
    """IMPL-5, DIRECTION A. Source #1 scores an exact filename match at ~1.0 — well
    above T_HIGH — so the requirement legitimately reaches ``state="have"``. It must
    STILL carry ``content_derived`` and STILL be surfaced in the ask_bundle: a resolved
    FILE is untrusted content no matter how confident the match. Waking the source must
    not turn it into a laundering path."""
    vault, _store, situation, _target = _granted_world(tmp_path)

    rep = rb.build_requirement_report([_Obj()], _Cap(), situation, _real_ctx(), vault=vault)
    req = _by_path(rep.per_objective[1], "input_path")

    assert req.source == "situation", "precondition: source #1 must actually have fired"
    assert req.confidence >= rb.T_HIGH, (
        f"precondition: this pin only bites above T_HIGH (got {req.confidence})")
    assert req.state == "have", "precondition: a >=T_HIGH bind is a 'have'"

    assert req.value_origin == "content_derived", (
        f"source #1 must CLAMP to content_derived, got {req.value_origin!r} — a "
        "granted-root file is untrusted bytes regardless of match score")
    assert rb._needs_ask(req) is True, (
        "a content_derived bind must NEVER silent-bind, even at state='have'")
    assert any(r.schema_path == "input_path" for r in rep.ask_bundle), (
        "the clamped file bind must reach the operator's one-click confirm bundle")


# ── (d) taint direction B: a TRUSTED bind STILL goes silent ─────────────────
def test_a_trusted_bind_still_binds_silently_in_the_same_report(tmp_path):
    """IMPL-5, DIRECTION B — the direction a "clamp everything" repair destroys.

    In the SAME report where source #1 is live and clamped, a genuinely trusted bind
    (source #0, an already-supplied tool-call param ⇒ ``systemu_authored``) must reach
    ``state="have"`` and be ABSENT from the ask_bundle. Without this, over-clamping
    every source would satisfy direction A perfectly while silently re-introducing the
    R-A12c over-ask defect."""
    vault, _store, situation, _target = _granted_world(tmp_path)

    rep = rb.build_requirement_report(
        [_Obj()], _Cap(), situation, _real_ctx(),
        provided_params={"output_format": "markdown"}, vault=vault)

    trusted = _by_path(rep.per_objective[1], "output_format")
    assert trusted is not None
    assert trusted.source == "provided", f"precondition: got source={trusted.source}"
    assert trusted.value_origin == "systemu_authored", (
        f"a trusted provided param must NOT be clamped, got {trusted.value_origin!r}")
    assert trusted.state == "have"
    assert rb._needs_ask(trusted) is False, "a trusted bind must bind SILENTLY"
    assert not any(r.schema_path == "output_format" for r in rep.ask_bundle), (
        "the trusted bind must NOT appear in the operator ask bundle")

    # …and in the VERY SAME report the file bind is still clamped and still asked
    filed = _by_path(rep.per_objective[1], "input_path")
    assert filed.value_origin == "content_derived"
    assert any(r.schema_path == "input_path" for r in rep.ask_bundle)


# ── (e) waking the source must not WIDEN file access ───────────────────────
def test_a_file_outside_every_granted_root_is_still_refused(tmp_path):
    """Waking source #1 makes the resolver's canonical confinement re-gate LOAD-BEARING
    for the first time (it was previously vacuous — every candidate was dropped because
    the store was None). A salient handle pointing OUTSIDE the granted roots must still
    be refused, so the repair tightens rather than widens file access."""
    vault, store, situation, _target = _granted_world(tmp_path)

    outside = tmp_path / "outside" / "resume.pdf"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_bytes(b"%PDF-1.4 elsewhere")
    assert store.is_within_granted(str(outside)) is False

    # the exact producer shape, pointing somewhere ungranted
    situation["roots"][0]["salient"] = [
        FileHandleLite(path=str(outside), name="resume.pdf", ext=".pdf",
                       size=outside.stat().st_size,
                       mtime=outside.stat().st_mtime).model_dump()]

    reqs = rb.compute_requirements(_Obj(), _Cap(), situation, _real_ctx(), vault=vault)
    req = _by_path(reqs, "input_path")
    assert req.state == "missing", (
        f"an out-of-root candidate must never bind; got {req.bound_value_ref!r}")
