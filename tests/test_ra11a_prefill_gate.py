"""A file-valued bind source must not pre-fill a leaf that is not a PATH leaf.

THE REGRESSION. Source #1 (``_bind_filehandle``, the granted-root FileHandle) was
inert in production for its whole life — it read a ``GrantedRootsStore`` off a ctx
that never carried one, so ``bc.granted`` was ``None`` and the resolver fail-closed on
every candidate. Threading the vault from the call site made it LIVE. Live, its shape
turned into an exposure: ``_bind_one_leaf`` runs the WHOLE ``_SOURCES`` chain on EVERY
leaf with no path gate, and ``_bind_filehandle`` scores the OBJECTIVE'S GOAL TEXT
(``bc.reference_text``) rather than the leaf key. ``reference_resolver`` folds the leaf
key into the token set with a UNION (``_tokens(text) | _tokens(key or "")``), so the key
can only ever WIDEN a match — it can never constrain one. One goal naming a
granted-root file therefore resolved for every leaf in the schema, and the operator was
shown a confident pre-filled path in ``password``, ``query``, ``count``, ``verbose``,
``process_id`` …

Measured over the repo's harvested tool schemas before the gate: 83/142 requirements
pre-filled with a path, 50 of them (35.2%) on leaves that are not paths, across 30
distinct keys. After the gate: 33 pre-fills, 0 of them wrong.

WHY THE GATE IS THE LEVER AND NOT THE ORACLE. Narrowing the resolver's scoring so a
path-SHAPED value is required was investigated and rejected: it is attributable for
almost nothing (the goal text names a real file, so the match is genuine — it is the
LEAF that is wrong, not the file). The defect is that the source is consulted at all
for a leaf that cannot hold a path.

THE NAMED TRADEOFF. Gating on the oracle makes pre-fill depend on the oracle's RECALL.
Two properties bound the cost. (1) ``looks_like_path`` is deliberately high-recall — it
unions ``format``, ``contentMediaType``, key-name patterns AND the description, and its
own docstring records that it "leans toward classifying a leaf as a path". (2) The only
leaf it never sees is one with no ``type`` at all, which ``_walk`` routes straight to
``leaf_fn`` with ``kind=""``; a union type like ``["string", "null"]`` is resolved by
``_first_type`` and still reaches the oracle. In the harvested corpus that is 12 leaves,
exactly ONE of which is path-named. Such a leaf now degrades to an honest ``missing``
ask instead of a confidently wrong pre-fill, which is the correct direction: an empty
box the operator fills beats a filled box the operator must notice is wrong.

SCOPE. The gate now covers source #1 AND ``_bind_run_context`` (source #2), which was
ungated in exactly the same way — with one produced file it bound 6/6 leaves at 0.5,
5 of them non-path, including ``password`` and ``verbose``.

SOURCE #2's ARGUMENT IS STRONGER THAN SYMMETRY. Measured over the same harvested-tool
corpus: 29 of 104 requirements pre-filled by source #2 and 29/29 — 100% — landed on
non-path leaves (15 distinct keys: ``pid``, ``lat``, ``lon``, ``query``, ``command``,
``url``, ``message`` …). Source #1 runs first and takes the genuine path leaves, so
source #2's entire contribution to that corpus was wrong pre-fills. After the gate: 0.

And it was not merely cosmetic. ``_SOURCES`` orders source #2 BEFORE inventory, profile
and schema-default, so a junk 0.5 ``content_derived`` bind MASKED the source-#4 profile
bind — and ``content_derived`` never silent-binds. A G-LEARN-promoted ``operator`` fact
therefore stopped paying off the moment a run produced any file: the operator was
re-asked, with a wrong path pre-filled. ``test_a_produced_file_no_longer_masks_a_silent_
profile_bind`` below is that regression, and it is the pin that matters most here.

The G-LEARN S3 suite used source #2 as its ``content_derived`` channel on non-path
leaves. It was RE-CHANNELLED (not weakened) onto source #0's content-seeded
provided-params clamp and, where a vault write would perturb the pin, onto the inventory
entry source — both production-realistic. See ``tests/test_glearn_s3_promotion.py``.

Both IMPL-5 taint directions stay pinned in ``test_ra11a_source1_liveness.py``; this
module must not disturb either, so the path leaf below is asserted to still bind,
still clamp to ``content_derived`` and still reach the ask bundle.
"""
from __future__ import annotations

import pytest

from systemu.runtime import requirement_binder as rb
from systemu.runtime.context_builder import ExecutionContext
from systemu.runtime.granted_roots import GrantedRootsStore
from systemu.runtime.situational_inventory import SituationReport, build_roots
from systemu.vault.vault import Vault


# ── the real world, through the real producers (never hand-built) ───────────
def _granted_world(tmp_path, filename: str = "resume.pdf"):
    vault = Vault(root=tmp_path / "vault")
    work = tmp_path / "work"
    work.mkdir(parents=True, exist_ok=True)
    (work / filename).write_bytes(b"%PDF-1.4 content")
    store = GrantedRootsStore(base_dir=vault.root)
    store.grant(str(work))
    return vault, SituationReport(roots=build_roots(store)).model_dump()


def _real_ctx(intent: str = "summarize my resume.pdf") -> ExecutionContext:
    return ExecutionContext(
        execution_id="exec-prefill-gate", system_prompt="sp", scroll_json=[],
        tool_index=[], use_objectives=True, scroll_intent=intent,
    )


class _Obj:
    id = 1
    goal = "summarize my resume.pdf"
    success_criteria = "a summary of the resume file"


class _Cap:
    """Five leaves no oracle calls a path, one that is a path by KEY, and one that is
    a path only by DESCRIPTION (``manifest`` → ``.csv`` on disk)."""

    name = "mixed_tool"
    parameters_schema = {
        "type": "object",
        "properties": {
            "password":   {"type": "string", "description": "the account secret"},
            "query":      {"type": "string", "description": "search text to run"},
            "count":      {"type": "integer", "description": "how many results"},
            "verbose":    {"type": "boolean", "description": "chatty output"},
            "process_id": {"type": "string", "description": "the pid to signal"},
            "input_path": {"type": "string", "description": "path to the file to read"},
            "manifest":   {"type": "string", "description": "the .csv to load from disk"},
        },
        "required": ["password", "query", "count", "verbose", "process_id",
                     "input_path", "manifest"],
    }


_NON_PATH = ("password", "query", "count", "verbose", "process_id")
_PATH = ("input_path", "manifest")


def _by_path(reqs, schema_path):
    for r in reqs:
        if r.schema_path == schema_path:
            return r
    return None


@pytest.fixture()
def reqs(tmp_path):
    vault, situation = _granted_world(tmp_path)
    return rb.compute_requirements(_Obj(), _Cap(), situation, _real_ctx(), vault=vault)


# ── the regression pin ──────────────────────────────────────────────────────
def test_a_non_path_leaf_is_never_prefilled_with_a_granted_root_file(reqs):
    """THE PIN. The goal names a real granted-root file, so source #1 resolves it at a
    high score — and must still be refused for every leaf that cannot hold a path."""
    offenders = {
        k: r.bound_value_ref
        for k in _NON_PATH
        if (r := _by_path(reqs, k)) is not None
        and str(r.bound_value_ref or "").startswith("file:")
    }
    assert not offenders, (
        "a file-valued source pre-filled leaves that are not paths — the operator is "
        f"shown a confident wrong default: {offenders}")


def test_the_oracle_precondition_actually_holds(reqs):
    """Precondition guard: this module is only meaningful while the oracle really does
    classify these five leaves as non-paths. If someone widens the oracle so
    ``password`` becomes a path leaf, the pin above would pass vacuously."""
    for k in _NON_PATH:
        r = _by_path(reqs, k)
        assert r is not None, f"{k} produced no requirement at all"
        assert r.kind != "input", (
            f"precondition broken: the oracle now calls {k!r} a path leaf")


# ── the gate must not disable the source it gates ──────────────────────────
def test_the_path_leaf_in_the_same_report_is_still_prefilled(reqs):
    """The gate narrows source #1, it does not switch it off. In the SAME report the
    real path leaf must still resolve to the granted-root file."""
    r = _by_path(reqs, "input_path")
    assert r is not None
    assert str(r.bound_value_ref or "").startswith("file:"), (
        f"source #1 stopped firing on a genuine path leaf: {r.bound_value_ref!r} "
        f"(state={r.state}) — the gate over-reached")
    assert r.source == "situation"


def test_the_gate_keys_off_the_oracle_not_off_a_key_denylist(reqs):
    """``manifest`` is not a path by NAME — the oracle classifies it from its
    DESCRIPTION (".csv to load from disk"). It must still pre-fill. This fails the day
    someone reimplements the gate as a hardcoded list of path-looking key names."""
    r = _by_path(reqs, "manifest")
    assert r is not None
    assert r.kind == "input", "precondition: the oracle types this leaf from its description"
    assert str(r.bound_value_ref or "").startswith("file:"), (
        "a description-typed path leaf lost its pre-fill — the gate is keyed to key "
        f"NAMES rather than to the oracle: {r.bound_value_ref!r}")


# ── both IMPL-5 taint directions survive the gate ──────────────────────────
def test_the_surviving_file_bind_is_still_clamped_and_still_asked(tmp_path):
    """Direction A must be untouched: the bind the gate LETS THROUGH is still
    ``content_derived`` and still reaches the operator's confirm bundle."""
    vault, situation = _granted_world(tmp_path)
    rep = rb.build_requirement_report([_Obj()], _Cap(), situation, _real_ctx(),
                                      vault=vault)
    r = _by_path(rep.per_objective[1], "input_path")
    assert r.value_origin == "content_derived", (
        f"the gate must not launder the taint clamp, got {r.value_origin!r}")
    assert rb._needs_ask(r) is True
    assert any(x.schema_path == "input_path" for x in rep.ask_bundle)


def test_a_trusted_provided_param_still_binds_silently_through_the_gate(tmp_path):
    """Direction B must be untouched: the gate sits on the FILE sources only, so a
    trusted provided param on a NON-path leaf still binds silently. Without this, a
    gate implemented as "skip all sources on a non-path leaf" would pass every pin
    above while re-introducing the R-A12c over-ask defect."""
    vault, situation = _granted_world(tmp_path)
    rep = rb.build_requirement_report(
        [_Obj()], _Cap(), situation, _real_ctx(),
        provided_params={"query": "quarterly numbers"}, vault=vault)
    r = _by_path(rep.per_objective[1], "query")
    assert r is not None
    assert r.source == "provided", f"the gate swallowed source #0: source={r.source}"
    assert r.value_origin == "systemu_authored"
    assert r.state == "have"
    assert rb._needs_ask(r) is False, "a trusted bind must still bind SILENTLY"


# ── the named tradeoff, pinned as intended behaviour ───────────────────────
def test_an_untyped_leaf_degrades_to_an_honest_missing_ask(tmp_path):
    """THE ACCEPTED COST. A leaf with no ``type`` never reaches the oracle, so it is
    not a path leaf as far as the gate can tell and it loses its pre-fill. It must
    degrade to a clean ``missing`` gap — an empty box — rather than to a confidently
    wrong path. Pinned so the tradeoff is a decision on the record, not a surprise."""
    vault, situation = _granted_world(tmp_path)

    class _UntypedCap:
        name = "untyped_tool"
        parameters_schema = {
            "type": "object",
            "properties": {"thing": {"description": "no type declared"}},
            "required": ["thing"],
        }

    reqs = rb.compute_requirements(_Obj(), _UntypedCap(), situation, _real_ctx(),
                                   vault=vault)
    r = _by_path(reqs, "thing")
    assert r is not None
    assert not str(r.bound_value_ref or "").startswith("file:"), (
        f"an untyped leaf was pre-filled with a path: {r.bound_value_ref!r}")
    assert r.state == "missing"
    assert r.bound_value_ref is None


# ── source #2 (_bind_run_context): the same gate, a stronger argument ───────
def _ctx_with_produced(produced, intent: str = "summarize my resume.pdf"):
    ctx = _real_ctx(intent)
    ctx.files_produced = list(produced)
    return ctx


def test_a_non_path_leaf_is_never_prefilled_from_the_RUN_CONTEXT(tmp_path):
    """THE PIN for source #2. One produced file must not pre-fill ``password``,
    ``verbose``, ``count``, ``query`` or ``process_id`` with a FILE PATH."""
    vault, situation = _granted_world(tmp_path)
    ctx = _ctx_with_produced([str(tmp_path / "work" / "resume.pdf")])
    reqs = rb.compute_requirements(_Obj(), _Cap(), situation, ctx, vault=vault)
    offenders = {
        k: r.bound_value_ref
        for k in _NON_PATH
        if (r := _by_path(reqs, k)) is not None
        and str(r.bound_value_ref or "").startswith("run_context:")
    }
    assert not offenders, (
        "the run-context source pre-filled leaves that cannot hold a path: %s"
        % offenders)


def test_the_run_context_source_still_fills_a_genuine_PATH_leaf(tmp_path):
    """The gate narrows source #2, it does not switch it off. With NO granted roots
    source #1 cannot resolve, so a path leaf falls through to source #2 — and must
    still be pre-filled from the produced file. Without this the pin above would be
    satisfied by deleting the source outright."""
    vault = Vault(root=tmp_path / "vault")
    produced = str(tmp_path / "out" / "report.csv")
    ctx = _ctx_with_produced([produced])
    reqs = rb.compute_requirements(_Obj(), _Cap(), {}, ctx, vault=vault)
    r = _by_path(reqs, "input_path")
    assert r is not None
    assert str(r.bound_value_ref or "") == f"run_context:{produced}", (
        "source #2 stopped firing on a genuine path leaf: %r (state=%s) — the gate "
        "over-reached" % (r.bound_value_ref, r.state))


def test_the_surviving_run_context_bind_is_still_clamped_to_content_derived(tmp_path):
    """IMPL-5 direction A for source #2: the bind the gate LETS THROUGH keeps its
    ``content_derived`` clamp and still reaches the operator's confirm bundle. The gate
    must not become a laundering path."""
    vault = Vault(root=tmp_path / "vault")
    ctx = _ctx_with_produced([str(tmp_path / "out" / "report.csv")])
    rep = rb.build_requirement_report([_Obj()], _Cap(), {}, ctx, vault=vault)
    r = _by_path(rep.per_objective[1], "input_path")
    assert str(r.bound_value_ref or "").startswith("run_context:")
    assert r.value_origin == "content_derived", (
        "the gate laundered source #2's taint clamp, got %r" % r.value_origin)
    assert rb._needs_ask(r) is True
    assert any(x.schema_path == "input_path" for x in rep.ask_bundle)


def test_a_produced_file_no_longer_masks_a_silent_profile_bind(tmp_path):
    """THE REASON THE GATE IS WORTH ITS BLAST RADIUS — the G-LEARN payoff regression.

    ``_SOURCES`` runs source #2 BEFORE the profile. So an operator-origin profile fact,
    which is supposed to SILENT-BIND its leaf on the next identical goal, was masked by
    a 0.5 ``content_derived`` path bind on any run that had produced a file — and
    ``content_derived`` never silent-binds. The operator was re-asked for a value they
    had already confirmed, with a wrong PATH pre-filled in the box.

    The negative control is explicit: the SAME report, same produced file, asserts the
    profile bind wins. If someone reverts the gate this fails on the mask."""
    from systemu.runtime import situational_inventory as si
    from systemu.runtime import user_profile as up

    vault = Vault(root=tmp_path / "vault")
    up.add_fact(vault, "the recipient is ops@acme.com", source="explicit_user",
                tags=["recipient"])

    class _RecipientCap:
        name = "send_tool"
        parameters_schema = {
            "type": "object",
            "properties": {"recipient": {"type": "string",
                                         "description": "who to send it to"}},
            "required": ["recipient"],
        }

    situation = {"profile": si.build_profile(vault)}
    ctx = _ctx_with_produced([str(tmp_path / "out" / "report.csv")])
    rep = rb.build_requirement_report([_Obj()], _RecipientCap(), situation, ctx,
                                      vault=vault)
    r = _by_path(rep.per_objective[1], "recipient")
    assert r is not None
    assert str(r.bound_value_ref or "").startswith("profile_fact:"), (
        "a produced file MASKED the operator profile bind: bound %r via %r — the "
        "G-LEARN promotion payoff is destroyed" % (r.bound_value_ref, r.source))
    assert r.value_origin == "operator"
    assert rb._needs_ask(r) is False, (
        "the promoted operator fact must SILENT-bind; it is being re-asked")
    assert not any(x.schema_path == "recipient" for x in rep.ask_bundle)


def test_a_union_typed_string_leaf_still_reaches_the_oracle(tmp_path):
    """The untyped cost is bounded to leaves with NO ``type``. A union type still
    resolves through ``_first_type``, so it keeps its pre-fill — this pins the bound
    on the tradeoff above."""
    vault, situation = _granted_world(tmp_path)

    class _UnionCap:
        name = "union_tool"
        parameters_schema = {
            "type": "object",
            "properties": {
                "input_path": {"type": ["string", "null"],
                               "description": "path to the file to read"},
            },
            "required": ["input_path"],
        }

    reqs = rb.compute_requirements(_Obj(), _UnionCap(), situation, _real_ctx(),
                                   vault=vault)
    r = _by_path(reqs, "input_path")
    assert r is not None
    assert r.kind == "input", "a union-typed string leaf must still reach the oracle"
    assert str(r.bound_value_ref or "").startswith("file:"), (
        f"a union-typed path leaf lost its pre-fill: {r.bound_value_ref!r}")
