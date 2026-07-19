"""R-A16 / G-LEARN slice 1 — TAINT CARRIAGE in the operator profile (IMPL-5).

§5.9 will promote answered operator asks into the operator profile so the next
identical situation RESOLVES instead of asking. IMPL-5 requires that a promotion
PRESERVES taint: the promoted entry carries the answer's ORIGINAL origin class, and a
``content_derived``-sourced value stays confirm-gated at bind even when later supplied
from the profile ("taint never launders through the profile slot").

Before this slice that was structurally impossible:
  * ``requirement_binder._bind_profile`` hard-coded ``operator`` for EVERY profile
    spine field and EVERY ``user_facts`` entry;
  * ``UserFact`` had no ``origin_class`` field at all, and ``model_config`` sets
    ``extra="forbid"`` — so nothing could carry taint through;
  * spine confidence is 1.0 and ``UserFact.confidence`` defaults to 1.0 ≥ ``T_HIGH``
    ⇒ a trusted, SILENT bind.

So the first ``content_derived`` promotion would have laundered to trusted on the
next run. This file pins the carriage + BOTH directions of the gate.

NOTE ON THE GATE (grounded, and it differs from a naive reading): in this codebase
``state`` is governed by CONFIDENCE alone (``requirement_binder`` line ~483), while the
taint gate is ``_needs_ask`` → ``ask_bundle`` → the elicitation rail
(``shadow_runtime`` ~1515-1527). ``tests/test_ac1_silent_bind_invariant.py`` pins that
split DELIBERATELY: "``state='have'`` alone can never make it silent". So the honest
safety property for a tainted value is "FORCED INTO THE ask_bundle / ``_needs_ask`` is
True", NOT "state == 'resolvable'". These tests pin the operative property.
"""
from __future__ import annotations

import ast
import json
import os
import pathlib

import pytest

from systemu.core.models import Objective, Tool, UserFact
from systemu.runtime import requirement_binder as rb
from systemu.runtime.requirement_binder import (
    build_requirement_report,
    compute_requirements,
)


# ── fixtures (mirror tests/test_ra10_binder.py) ─────────────────────────────
class _FakeGrantedRoots:
    def __init__(self, roots):
        self._roots = [os.path.normcase(os.path.abspath(r)) for r in roots]

    def is_within_granted(self, candidate: str) -> bool:
        c = os.path.normcase(os.path.abspath(str(candidate or "")))
        return any(c == r or c.startswith(r + os.sep) for r in self._roots)


class _FakeCtx:
    def __init__(self, *, situation=None, granted_roots=None):
        self._situation_report = situation
        self._granted_roots = granted_roots
        self.files_produced = []
        self.vault = None


def _tool(name, schema):
    return Tool(id="tool_" + name, name=name, description="test tool",
                tool_type="python_function", parameters_schema=schema,
                effect_tags=[], external_verification_channel=None)


def _obj(oid=1, goal="do the thing"):
    return Objective(id=oid, goal=goal, success_criteria="it is done")


def _situation(**over):
    base = {"services": [], "capabilities": [], "roots": [], "credentials": [],
            "profile": {}, "declared_intents": []}
    base.update(over)
    return base


def _profile_with_fact(**fact_over):
    """A profile whose single user_fact matches an ``account_id`` leaf."""
    fact = {"id": "fact_1", "ts": "2020", "fact": "account_id is acct-42",
            "tags": ["account_id"], "source": "operator", "confidence": 1.0}
    fact.update(fact_over)
    return {"name": "Op", "location_text": "NYC", "timezone": "UTC",
            "default_output_dir": "/out", "user_facts": [fact]}


def _bind_account_id(profile):
    """Bind an ``account_id`` leaf against ``profile``; return (requirement, report)."""
    situation = _situation(profile=profile)
    ctx = _FakeCtx(situation=situation, granted_roots=_FakeGrantedRoots([]))
    cap = _tool("api_call", {"account_id": {"type": "string",
                                            "description": "the account id"}})
    reqs = compute_requirements(_obj(), cap, situation, ctx)
    matches = [r for r in reqs if r.schema_path.endswith("account_id")]
    assert matches, "the required account_id leaf should produce a Requirement"
    report = build_requirement_report([_obj()], cap, situation, ctx)
    return matches[0], report


def _in_ask_bundle(report):
    return any(a.schema_path.endswith("account_id") for a in report.ask_bundle)


# ── PIN 1: a content_derived-stamped fact is NEVER silent-bound ─────────────
def test_content_derived_stamped_fact_is_confirm_gated_never_silent():
    """THE pin. A profile fact stamped ``content_derived`` — at confidence 1.0, the
    value that today would bind SILENTLY — must carry its taint to the Requirement and
    be FORCED into the ask_bundle (one-click operator confirm).

    This is the laundering IMPL-5 forbids: run 1 answers an ask from page content, the
    §5.9 promotion writes it to the profile; run 2 must still confirm it, not trust it.
    """
    req, report = _bind_account_id(_profile_with_fact(origin_class="content_derived"))

    assert req.source == "operator_profile"
    assert req.value_origin == "content_derived", (
        "the fact's ORIGINAL taint must survive the profile slot — a hard-coded "
        "operator origin here IS the laundering bug"
    )
    assert rb._needs_ask(req) is True, "a content_derived bind can never be silent"
    assert _in_ask_bundle(report), (
        "the tainted profile bind must reach the operator's one-click confirm bundle"
    )


def test_content_derived_stamp_survives_even_at_full_confidence():
    """Confidence must not rescue taint: 1.0 ≥ T_HIGH, yet the bind still asks."""
    req, report = _bind_account_id(
        _profile_with_fact(origin_class="content_derived", confidence=1.0))
    assert req.confidence >= rb.T_HIGH
    assert req.value_origin == "content_derived"
    assert _in_ask_bundle(report)


# ── PIN 2 (the inverse): trusted + legacy facts still bind SILENTLY ─────────
def test_legacy_unstamped_fact_still_binds_operator_and_silently():
    """GRANDFATHERING — the compatibility claim of this slice. A fact with NO
    ``origin_class`` (every fact written before this slice) keeps binding exactly as
    before: operator origin, state='have', NOT asked.

    Without this pin a future "just clamp everything to content_derived" fix would
    silently destroy the whole payoff of §5.9 and still look green.
    """
    req, report = _bind_account_id(_profile_with_fact())

    assert req.value_origin == "operator"
    assert req.state == "have"
    assert rb._needs_ask(req) is False
    assert not _in_ask_bundle(report), "a legacy profile fact must stay silent"


def test_operator_stamped_fact_binds_silently():
    """An explicitly ``operator``-stamped fact behaves identically to a legacy one."""
    req, report = _bind_account_id(_profile_with_fact(origin_class="operator"))
    assert req.value_origin == "operator"
    assert req.state == "have"
    assert not _in_ask_bundle(report)


def test_systemu_authored_stamped_fact_binds_silently():
    """``systemu_authored`` is the other TRUSTED axis — also silent."""
    req, report = _bind_account_id(_profile_with_fact(origin_class="systemu_authored"))
    assert req.value_origin == "systemu_authored"
    assert not _in_ask_bundle(report)


def test_profile_spine_field_still_binds_operator():
    """The 4-field UserProfile spine is operator-authored (onboarding wizard) and has
    no per-field taint — it must keep binding operator/silent."""
    situation = _situation(profile={"name": "Op", "location_text": "NYC",
                                    "timezone": "UTC", "default_output_dir": "/out"})
    ctx = _FakeCtx(situation=situation, granted_roots=_FakeGrantedRoots([]))
    cap = _tool("writer", {"output_dir": {"type": "string",
                                          "description": "where to write"}})
    reqs = compute_requirements(_obj(), cap, situation, ctx)
    hits = [r for r in reqs if r.schema_path.endswith("output_dir")]
    assert hits and hits[0].value_origin == "operator"
    assert hits[0].source == "operator_profile"


# ── PIN 3: a non-canonical stamp FAILS UNTRUSTED at the bind ───────────────
@pytest.mark.parametrize("bogus", ["operator ", "OPERATOR", "trusted", "", 42, None,
                                   "content-derived"])
def test_non_canonical_origin_class_clamps_to_content_derived(bogus):
    """A poisoned / typo'd / hand-edited stamp must clamp to the DANGEROUS axis, never
    be accepted as taint-clear. (A raw dict fact bypasses model validation — exactly
    the hand-edited-JSONL vector, so the bind layer must defend itself.)

    ``None`` is the legacy/absent case and is the ONE value that grandfathers to
    operator; every other non-canonical value fails untrusted.
    """
    req, report = _bind_account_id(_profile_with_fact(origin_class=bogus))
    if bogus is None:
        assert req.value_origin == "operator"      # absent ⇒ grandfathered
        assert not _in_ask_bundle(report)
    else:
        assert req.value_origin == "content_derived", (
            f"non-canonical stamp {bogus!r} must fail UNTRUSTED"
        )
        assert _in_ask_bundle(report)


# ── PIN 3b: ``_fact_origin`` itself, pinned DIRECTLY ───────────────────────
#
# Why this exists (a mutation caught it): the end-to-end clamp test above cannot see
# ``_fact_origin``'s clamp, because ``_emit_requirement`` ALSO clamps every
# ``value_origin`` before constructing the Requirement. Deleting the clamp inside
# ``_fact_origin`` therefore left the whole file green. That redundancy is legitimate
# defense-in-depth, but each layer needs its OWN pin or either can rot unnoticed.

def test_fact_origin_grandfathers_absent_to_operator():
    """Absent stamp ⇒ operator (the compatibility default), pinned at the unit."""
    assert rb._fact_origin({"id": "f"}) == "operator"
    assert rb._fact_origin({"id": "f", "origin_class": None}) == "operator"


@pytest.mark.parametrize("canonical", ["operator", "systemu_authored", "content_derived"])
def test_fact_origin_passes_canonical_through(canonical):
    assert rb._fact_origin({"origin_class": canonical}) == canonical


@pytest.mark.parametrize("bogus", ["operator ", "OPERATOR", "trusted", "", 42,
                                   "content-derived", [], {}])
def test_fact_origin_clamps_non_canonical_to_content_derived(bogus):
    """The clamp lives in ``_fact_origin`` too — not only downstream in
    ``_emit_requirement``. A present-but-non-canonical stamp fails UNTRUSTED here."""
    assert rb._fact_origin({"origin_class": bogus}) == "content_derived"


# ── the model + the writer API ─────────────────────────────────────────────
def test_the_taint_vocabulary_agrees_across_every_module_that_declares_it():
    """ONE discipline, not two. ``core.models`` declares ``ORIGIN_CLASSES`` in the base
    layer rather than importing it from the runtime — the runtime fence
    (``test_world_model.test_only_the_allowed_modules_reference_the_world_model_anywhere``)
    keeps the world model off every decision path, and ``core.models`` is imported by
    essentially everything. This pins the copies EQUAL so they cannot drift apart.
    """
    from systemu.core.models import ORIGIN_CLASSES as core_vocab
    from systemu.runtime.world_model import ORIGIN_CLASSES as wm_vocab

    assert set(core_vocab) == {"operator", "systemu_authored", "content_derived"}
    assert set(core_vocab) == set(wm_vocab), "taint vocabulary drifted"
    assert set(core_vocab) == set(rb._CANONICAL_ORIGINS), "binder vocabulary drifted"


def test_userfact_accepts_canonical_origin_class():
    for oc in ("operator", "systemu_authored", "content_derived"):
        f = UserFact(id="f", ts="2020", fact="x", source="s", origin_class=oc)
        assert f.origin_class == oc


def test_userfact_defaults_origin_class_absent():
    """Absent by default — a legacy fact round-trips unchanged."""
    f = UserFact(id="f", ts="2020", fact="x", source="s")
    assert f.origin_class is None


def test_userfact_rejects_non_canonical_origin_class():
    """CLOSED vocabulary, matching ``world_model.Fact._origin_class_in_vocab``: a
    mis-tagged provenance fails LOUD at construction rather than being written to the
    log as taint-clear."""
    with pytest.raises(Exception):
        UserFact(id="f", ts="2020", fact="x", source="s", origin_class="trusted")


def test_add_fact_round_trips_origin_class(tmp_path):
    """``add_fact(origin_class=...)`` persists and reads back through the JSONL."""
    from systemu.runtime import user_profile as up

    class _V:
        root = tmp_path

    up.add_fact(_V(), "the account id is acct-9", source="promotion",
                tags=["account_id"], origin_class="content_derived")
    facts = up.get_facts(_V())
    assert len(facts) == 1
    assert facts[0].origin_class == "content_derived"


def test_add_fact_defaults_to_absent_origin_class(tmp_path):
    """The default is ABSENT — every existing writer keeps its current behavior."""
    from systemu.runtime import user_profile as up

    class _V:
        root = tmp_path

    up.add_fact(_V(), "role is ops", source="onboarding", tags=["office_context"])
    facts = up.get_facts(_V())
    assert len(facts) == 1
    assert facts[0].origin_class is None


# ── PIN 3c: the SANCTIONED API — the four vault wrappers must TRANSPORT ────
#
# ``user_profile`` line 7-8 declares the vault wrapper the sanctioned path ("All
# readers/writers go through this module so consumers don't reach into the vault
# directly"), and every non-onboarding writer in the tree — ``fact_extractor``,
# ``cli_commands.user_remember`` — reaches ``add_fact`` ONLY through it. So a
# parameter that ``add_fact`` accepts but the wrappers drop is UNREACHABLE in
# production: slice 1 shipped the taint mechanism with no way to stamp through it.
# ``Vault`` and ``SqliteVault`` raised ``TypeError: got an unexpected keyword
# argument 'origin_class'``; ``FileVault``/``ParallelVault`` accepted ``**kwargs``
# and forwarded straight into that same TypeError.
#
# These pins assert PERSISTED JSON, not the returned object: a wrapper that
# forwarded to the model but not to the writer would still be broken for the §5.9
# promoter, which cares only about what run 2 reads back off disk.

def _persisted_facts(root):
    """Every fact on disk under ``root``, as RAW dicts (pre-validation)."""
    p = pathlib.Path(root) / "user_facts.jsonl"
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _mk_vault(tmp_path):
    from systemu.vault.vault import Vault
    v = Vault(str(tmp_path / "vault"))
    return v, v.root


def _mk_sqlite_vault(tmp_path):
    """SqliteVault delegates profile storage to the SAME file-side ``add_fact`` (it
    exposes ``.root`` = the memory dir), so it is analytically identical to ``Vault``
    — but no live SQL backend was exercised when the gap was found, so pin it for
    real rather than reasoning about it."""
    from systemu.storage.sqlite.vault import SqliteVault
    db = tmp_path / "v.db"
    sv = SqliteVault(f"sqlite:///{db.as_posix()}", memory_dir=tmp_path / "memory")
    return sv, sv.root


def _mk_file_vault(tmp_path):
    from systemu.storage.file_vault import FileVault
    from systemu.vault.vault import Vault
    inner = Vault(str(tmp_path / "vault"))
    return FileVault(inner), inner.root


def _mk_parallel_vault(tmp_path):
    from systemu.storage.parallel_vault import ParallelVault
    from systemu.vault.vault import Vault
    primary = Vault(str(tmp_path / "p"))
    secondary = Vault(str(tmp_path / "s"))
    return ParallelVault(primary, secondary), primary.root


_WRAPPERS = [
    ("Vault", _mk_vault),
    ("SqliteVault", _mk_sqlite_vault),
    ("FileVault", _mk_file_vault),
    ("ParallelVault", _mk_parallel_vault),
]


@pytest.mark.parametrize("wrapper_name,make", _WRAPPERS, ids=[n for n, _ in _WRAPPERS])
def test_vault_wrapper_round_trips_origin_class_to_persisted_json(
        wrapper_name, make, tmp_path):
    """THE reachability pin. Stamping through the sanctioned wrapper must land the
    taint on disk — otherwise the whole slice-1 mechanism is dead code."""
    vault, root = make(tmp_path)

    vault.append_user_fact(fact="the account id is acct-9", source="promotion",
                           tags=["account_id"], origin_class="content_derived")

    rows = _persisted_facts(root)
    assert len(rows) == 1, f"{wrapper_name}.append_user_fact persisted {len(rows)} facts"
    assert rows[0].get("origin_class") == "content_derived", (
        f"{wrapper_name}.append_user_fact DROPPED origin_class on the way to "
        f"user_facts.jsonl — the taint stamp is unreachable through the sanctioned "
        f"vault API. persisted={rows[0]!r}"
    )


@pytest.mark.parametrize("wrapper_name,make", _WRAPPERS, ids=[n for n, _ in _WRAPPERS])
def test_vault_wrapper_omitting_origin_class_persists_absent(
        wrapper_name, make, tmp_path):
    """GRANDFATHERING through the wrapper: omitting the stamp must persist ABSENT, not
    a wrapper-invented default. Every writer that exists today omits it, and
    ``_fact_origin`` grandfathers absent → operator; a wrapper that hard-coded a value
    here would silently re-classify every existing operator surface."""
    vault, root = make(tmp_path)

    vault.append_user_fact(fact="role is ops", source="onboarding",
                           tags=["office_context"])

    rows = _persisted_facts(root)
    assert len(rows) == 1
    assert rows[0].get("origin_class") is None, (
        f"{wrapper_name}.append_user_fact invented an origin_class default: "
        f"{rows[0].get('origin_class')!r}"
    )


def test_parallel_vault_carries_origin_class_to_the_SECONDARY_store_too(tmp_path):
    """ParallelVault's shadow write goes through ``_write_secondary``, which SWALLOWS
    every exception (logs a warning, never raises). So a dropped stamp on the secondary
    leg is invisible to the caller — during a file→SQLite migration the secondary would
    silently accumulate taint-CLEARED copies, and the cutover would launder them all.
    Pin both legs."""
    from systemu.storage.parallel_vault import ParallelVault
    from systemu.vault.vault import Vault

    primary = Vault(str(tmp_path / "p"))
    secondary = Vault(str(tmp_path / "s"))
    pv = ParallelVault(primary, secondary)

    pv.append_user_fact(fact="the account id is acct-9", source="promotion",
                        tags=["account_id"], origin_class="content_derived")

    for leg, root in (("primary", primary.root), ("secondary", secondary.root)):
        rows = _persisted_facts(root)
        assert len(rows) == 1, f"ParallelVault wrote {len(rows)} facts to {leg}"
        assert rows[0].get("origin_class") == "content_derived", (
            f"ParallelVault dropped origin_class on the {leg} leg"
        )


# ── PIN 4: the WRITER INVENTORY — the guard on the grandfathered default ───
#
# Grandfathering ``absent -> operator`` is safe ONLY while every in-repo writer is a
# genuine operator surface. This test is what stops the NEXT producer (the §5.9
# promoter) from silently riding that default. It is AST-based, follows the
# forwarding-wrapper hop (``vault.append_user_fact`` -> ``user_profile.add_fact``),
# and FAILS when an unstamped caller appears that is not explicitly allowlisted.
#
# TIERING: these three read PRODUCTION SOURCE, so they carry exactly the hazard the
# ``source_sensitive`` marker exists for — a file left transiently unparseable by a
# concurrent edit is skipped by the walker, which can make the stale-entry check
# false-FAIL. conftest's auto-tagger only detects ``inspect.getsource``, so they are
# marked EXPLICITLY. This mirrors ``test_ac1_silent_bind_invariant``: the behavioural
# safety pins above stay in the edit-safe tier; the source-reading structural
# companions run in the full tier.

_FACT_WRITE_NAMES = {"add_fact", "append_user_fact"}

# Pure FORWARDERS: they carry no origin of their own, they pass the caller's through —
# which is why they are exempt from the "must stamp" rule below.
#
# That exemption is EARNED, not assumed. When slice 1 shipped it was FALSE: ``Vault``
# and ``SqliteVault`` did not accept ``origin_class`` at all, so far from passing the
# caller's origin through they REJECTED it with TypeError — and since the vault wrapper
# is the sanctioned write path, the taint mechanism was unreachable in production. The
# transport is now real and is pinned behaviourally by PIN 3c above
# (``test_vault_wrapper_round_trips_origin_class_to_persisted_json``), which asserts
# each of these four actually lands the stamp in ``user_facts.jsonl``. If that pin ever
# goes red, these entries stop being forwarders and must come off this allowlist.
_FORWARDERS = {
    ("systemu/vault/vault.py", "append_user_fact"),
    ("systemu/storage/sqlite/vault.py", "append_user_fact"),
    ("systemu/storage/file_vault.py", "append_user_fact"),
    ("systemu/storage/parallel_vault.py", "append_user_fact"),
}

# Genuine OPERATOR SURFACES — each verified to originate from operator-authored input:
#   welcome.save_onboarding   — operator-typed wizard fields (persona/role/org)
#   welcome.mark_skipped      — systemu sentinel written on an operator click
#   tour.mark_tour_completed  — systemu sentinel written on an operator action
#   cli_commands.user_remember— operator types the fact verbatim (`user remember`)
#
# RETRACTED (R-A16): ``fact_extractor.extract_from_chat`` was allowlisted here on the
# justification "LLM extraction whose SOLE input is the operator's own typed chat
# prompt — LLM-MEDIATED but not content-sourced". That claim was FALSE and this
# allowlist turned an undocumented exposure into a written safety warrant.
# ``chat_entry["prompt"]`` is operator-DELIVERED, not operator-AUTHORED: paste an
# email, a log or a scraped page into chat and the EXTRACTOR picks which of its
# sentences become durable facts, unreviewed, at the >= 0.9 confidence the extraction
# prompt asks for — a silent bind of an LLM paraphrase of pasted content. That caller
# now stamps ``origin_class="content_derived"`` explicitly, so it passes the guard
# below on its ``stamps_origin_class`` check and needs no entry here. Pinned by
# ``tests/test_ra16_auto_extract_silent_bind.py``.
_OPERATOR_SURFACES = {
    ("systemu/interface/pages/welcome.py", "save_onboarding"),
    ("systemu/interface/pages/welcome.py", "mark_skipped"),
    ("systemu/interface/tour.py", "mark_tour_completed"),
    ("systemu/interface/cli_commands.py", "user_remember"),
}


def _repo_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parent.parent


def _iter_prod_sources():
    for p in (_repo_root() / "systemu").rglob("*.py"):
        if "__pycache__" in str(p):
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        rel = p.resolve().relative_to(_repo_root()).as_posix()
        yield rel, tree


def _fact_write_call_sites():
    """Every production call site of a fact-write name, as
    (relpath, lineno, callee, enclosing_function, stamps_origin_class)."""
    sites = []
    for rel, tree in _iter_prod_sources():
        parent = {}
        for n in ast.walk(tree):
            for c in ast.iter_child_nodes(n):
                parent[c] = n
        for n in ast.walk(tree):
            if not isinstance(n, ast.Call):
                continue
            f = n.func
            callee = (f.attr if isinstance(f, ast.Attribute)
                      else f.id if isinstance(f, ast.Name) else None)
            if callee not in _FACT_WRITE_NAMES:
                continue
            cur, encl = n, "<module>"
            while cur in parent:
                cur = parent[cur]
                if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    encl = cur.name
                    break
            stamps = any(k.arg == "origin_class" for k in n.keywords)
            sites.append((rel, n.lineno, callee, encl, stamps))
    return sites


@pytest.mark.source_sensitive
def test_every_fact_writer_stamps_origin_class_or_is_an_allowlisted_operator_surface():
    """The guard on the grandfathered default — an ACCIDENT detector, not a proof.

    A new writer that does NOT stamp ``origin_class`` inherits ``operator`` — i.e. it
    binds SILENTLY. That is correct only for a genuine operator surface. Any other new
    writer (notably the §5.9 promoter, which carries a possibly-tainted answer) MUST
    stamp.

    WHAT IT CATCHES — the plain, overwhelmingly common shape: a syntactically visible
    call to ``add_fact(...)`` / ``vault.append_user_fact(...)`` whose keyword list has no
    ``origin_class``. That is the shape a developer writes when they simply forget, and
    it is the case this pin exists for.

    WHAT IT DOES **NOT** CATCH (probed and CONFIRMED to pass undetected, so do not read
    a green here as "no unstamped writer exists"):
      * aliased imports — ``from systemu.runtime.user_profile import add_fact as
        _remember`` then ``_remember(...)``; the walker matches the callee NAME only;
      * dynamic dispatch — ``getattr(vault, "append_user_fact")(...)``, or any callee
        assembled at runtime; there is no ``ast.Call`` with a matching name;
      * schema bypass — appending a hand-built line straight to ``user_facts.jsonl``,
        which never goes through the writer API at all. (``requirement_binder
        ._fact_origin`` is the backstop for THAT one: it clamps a present-but-
        non-canonical stamp fail-untrusted, pinned in PIN 3b.)

    Defeating those would need a general call-graph / import-alias proof, which is
    deliberately out of scope: this is a guard against accidental omission by an honest
    contributor, not a containment boundary against a motivated bypass. The real
    safety property is enforced at BIND time (PINs 1-3b), not here.

    TIER: marked ``source_sensitive`` — it reads production source, so it is EXCLUDED
    from the edit-safe tier (``pytest -m "not source_sensitive"``) and runs only in the
    full suite. Do not rely on it while editing.
    """
    sites = _fact_write_call_sites()
    assert sites, "AST inventory found no fact-write call sites — the walker is broken"

    offenders = [
        (rel, ln, callee, encl) for rel, ln, callee, encl, stamps in sites
        if not stamps and (rel, encl) not in _FORWARDERS
        and (rel, encl) not in _OPERATOR_SURFACES
    ]
    assert not offenders, (
        "unstamped fact writer(s) not on the operator-surface allowlist — they would "
        "inherit the grandfathered `operator` origin and bind SILENTLY:\n  "
        + "\n  ".join(f"{r}:{l} {c}() in {e}()" for r, l, c, e in offenders)
        + "\nIf this is a genuine operator surface, add it to _OPERATOR_SURFACES with "
          "a justification; otherwise pass origin_class= explicitly."
    )


@pytest.mark.source_sensitive
def test_writer_allowlist_has_no_stale_entries():
    """Keeps the allowlist HONEST: an entry that no longer exists must be removed, so
    the allowlist cannot silently grow into a rubber stamp."""
    live = {(rel, encl) for rel, _ln, _c, encl, _s in _fact_write_call_sites()}
    stale = (_FORWARDERS | _OPERATOR_SURFACES) - live
    assert not stale, f"allowlist entries no longer present in the tree: {sorted(stale)}"


@pytest.mark.source_sensitive
def test_direct_add_fact_callers_are_fully_enumerated():
    """Completeness guard for the wrapper hop. If someone adds a NEW forwarding wrapper
    around ``add_fact``, depth-2 callers of it would otherwise escape the inventory
    entirely. Pin the exact set of functions that call ``add_fact`` DIRECTLY."""
    direct = set()
    for rel, tree in _iter_prod_sources():
        parent = {}
        for n in ast.walk(tree):
            for c in ast.iter_child_nodes(n):
                parent[c] = n
        for n in ast.walk(tree):
            if not isinstance(n, ast.Call):
                continue
            f = n.func
            callee = (f.attr if isinstance(f, ast.Attribute)
                      else f.id if isinstance(f, ast.Name) else None)
            if callee != "add_fact":
                continue
            cur, encl = n, "<module>"
            while cur in parent:
                cur = parent[cur]
                if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    encl = cur.name
                    break
            direct.add((rel, encl))

    known = _FORWARDERS | _OPERATOR_SURFACES
    unknown = direct - known
    assert not unknown, (
        "new direct add_fact caller(s) — if this is a forwarding wrapper, add it to "
        f"_FORWARDERS so its own callers get inventoried too: {sorted(unknown)}"
    )
