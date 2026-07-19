"""R-CAP1 slice 1 — Capability Slots index + deterministic selection view
(spec §5.5.1 CAP-1/CAP-2/CAP-4, 4-lens'd BUILD-READY 2026-07-13).

Covers the pure, load-bearing core: the slot canonicalizer (CAP-1 — synonymous
proposals collapse to one slot BEFORE any occupancy check), the derived index
(CAP-2 — reconciler-sole-writer, derive-only from {Tool catalog ∪ MCP enabled},
usage READ from capability_ledger, rebuildable, MCP rows preserved), and the
deterministic selection view (CAP-4 — a total tuple key with a tool_id terminal
tiebreak so ordering is replay-stable; find_tools is never-subtract over the
COMPLETE store). Honors the CAP-0 build-preconditions 1/6.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from systemu.runtime import capability_slots as cs
from systemu.runtime import capability_index as ci


# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #

class _Tool:
    def __init__(self, id, name, description="", enabled=True, status="deployed",
                 effect_tags=None, parameters_schema=None, forged_by_systemu=False,
                 implementation_path="vault/tools/x.py"):
        self.id = id; self.name = name; self.description = description
        self.enabled = enabled; self.status = status
        self.effect_tags = effect_tags or []
        self.parameters_schema = parameters_schema or {}
        self.forged_by_systemu = forged_by_systemu
        self.implementation_path = implementation_path


class _Vault:
    def __init__(self, root: Path, tools=None):
        self.root = str(root)
        self._tools = tools or []

    def list_tools(self, status=None):
        return list(self._tools)


# --------------------------------------------------------------------------- #
# CAP-1 — the slot canonicalizer (synonyms collapse BEFORE the occupancy gate)
# --------------------------------------------------------------------------- #

def test_canonical_verb_folds_synonyms_and_case():
    assert cs.canonical_verb("Create") == cs.canonical_verb("make") == "create"
    assert cs.canonical_verb("open") == "create"          # open a ticket = create
    assert cs.canonical_verb("POST") == cs.canonical_verb("submit") == "send"
    assert cs.canonical_verb("get") == cs.canonical_verb("fetch") == "read"
    assert cs.canonical_verb("remove") == "delete"
    assert cs.canonical_verb("search") == cs.canonical_verb("list") == "list"


def test_canonical_target_folds_plural_and_case():
    assert cs.canonical_target("Issues") == cs.canonical_target("issue") == "issue"
    assert cs.canonical_target("payments") == "payment"


def test_canonical_slot_collapses_synonymous_proposals():
    # "create issue" and "open ticket" must NOT be two slots if they mean the same
    a = cs.canonical_slot("create", "issue")
    b = cs.canonical_slot("open", "issues")
    assert a == b == ("create", "issue")


def test_slot_str_roundtrip():
    assert cs.slot_str(("create", "issue")) == "create:issue"


# --------------------------------------------------------------------------- #
# CAP-2 — the derived index (reconciler-sole-writer, derive-only)
# --------------------------------------------------------------------------- #

def test_derive_index_from_tool_catalog(tmp_path):
    v = _Vault(tmp_path, tools=[
        _Tool("t1", "create_issue", "open a github issue", effect_tags=["net_call"]),
        _Tool("t2", "read_file", "read a local file", effect_tags=["local_read"]),
    ])
    rows = ci.derive_index(v)
    by_id = {r.tool_id: r for r in rows}
    assert by_id["t1"].origin == "forged" or by_id["t1"].origin == "builtin"
    assert "create:issue" in by_id["t1"].slots        # name-derived slot, canonical
    assert by_id["t1"].effect_tags == ["net_call"]
    assert by_id["t2"].io_shape_hash == by_id["t2"].io_shape_hash  # stable


def test_derive_index_is_deterministic(tmp_path):
    v = _Vault(tmp_path, tools=[_Tool("t1", "create_issue"), _Tool("t2", "send_email")])
    a = [r.model_dump() for r in ci.derive_index(v)]
    b = [r.model_dump() for r in ci.derive_index(v)]
    assert a == b                                      # replay-stable derivation


def test_reconcile_once_is_sole_writer_and_rebuildable(tmp_path):
    v = _Vault(tmp_path, tools=[_Tool("t1", "create_issue")])
    n = ci.reconcile_index(v)
    assert n == 1
    p = tmp_path / "capabilities" / "capability_index.json"
    assert p.exists()
    # delete + rebuild from empty → identical (CAP-0.6 rebuild boundary)
    p.unlink()
    ci.reconcile_index(v)
    assert len(ci.load_index(v)) == 1


def test_index_preserves_mcp_rows(tmp_path, monkeypatch):
    # CAP-0.6: MCP rows live in mcp/connections.enabled_tools, NOT the vault catalog
    v = _Vault(tmp_path, tools=[_Tool("t1", "read_file")])
    monkeypatch.setattr(
        ci, "_mcp_enabled_tools",
        lambda vault: [{"server": "https://gh", "name": "create_issue",
                        "description": "open an issue", "schema": {}}])
    rows = ci.derive_index(v)
    origins = {r.origin for r in rows}
    assert any(o.startswith("mcp:") for o in origins)   # MCP row present
    assert any(r.origin.startswith("builtin") or r.origin == "forged" for r in rows)


def test_load_index_defensive_on_broken_file(tmp_path):
    v = _Vault(tmp_path)
    (tmp_path / "capabilities").mkdir(parents=True)
    (tmp_path / "capabilities" / "capability_index.json").write_text("nope {", encoding="utf-8")
    assert ci.load_index(v) == []                       # never raises


# --------------------------------------------------------------------------- #
# CAP-4 — deterministic selection view (tuple key, tool_id terminal tiebreak)
# --------------------------------------------------------------------------- #

def _rows_for(vault):
    ci.reconcile_index(vault)
    return ci.load_index(vault)


def test_select_top_k_is_replay_stable(tmp_path):
    v = _Vault(tmp_path, tools=[_Tool(f"t{i}", f"create_issue_{i}") for i in range(20)])
    rows = _rows_for(v)
    a = [r.tool_id for r in ci.select_top_k(rows, "create an issue", k=12)]
    b = [r.tool_id for r in ci.select_top_k(rows, "create an issue", k=12)]
    assert a == b and len(a) == 12                     # deterministic, capped


def test_ranking_prefers_slot_match(tmp_path):
    v = _Vault(tmp_path, tools=[
        _Tool("match", "create_issue", "open a github issue"),
        _Tool("noise", "read_file", "read a local file"),
    ])
    rows = _rows_for(v)
    top = ci.select_top_k(rows, "create an issue", k=2)
    assert top[0].tool_id == "match"                   # slot match ranks first


def test_tool_id_is_the_terminal_tiebreak(tmp_path):
    # two tools identical on every ranking signal → deterministic order by tool_id
    v = _Vault(tmp_path, tools=[_Tool("zzz", "create_issue"), _Tool("aaa", "create_issue")])
    rows = _rows_for(v)
    top = ci.select_top_k(rows, "create an issue", k=2)
    assert [r.tool_id for r in top] == ["aaa", "zzz"]  # tie broken by tool_id asc


def test_find_tools_is_never_subtract_over_complete_store(tmp_path):
    # every tool in the store must be returnable via find_tools (CAP-4 floor),
    # including a non-matching one — the view ranks, never hides.
    v = _Vault(tmp_path, tools=[
        _Tool("t1", "create_issue"), _Tool("t2", "read_file"), _Tool("t3", "delete_thing"),
    ])
    _rows_for(v)
    got = {r["tool_id"] for r in ci.find_tools(v, "anything at all")}
    assert got == {"t1", "t2", "t3"}                   # complete store, none hidden


def test_find_tools_live_reads_fresh_without_writing_the_index(tmp_path):
    # live=True derives in memory (fresh) and must NOT create/write the index file
    # (the daemon reconciler stays sole writer — CAP-0.1).
    v = _Vault(tmp_path, tools=[_Tool("t1", "create_issue")])
    rows = ci.find_tools(v, "create an issue", live=True)
    assert {r["tool_id"] for r in rows} == {"t1"}
    assert not (tmp_path / "capabilities" / "capability_index.json").exists()  # no write


# --------------------------------------------------------------------------- #
# CAP-4c — the `sharing-on find-tools` CLI consumer (the live, safe reader)
# --------------------------------------------------------------------------- #

def test_run_find_tools_lists_ranked_and_never_subtracts(tmp_path, capsys):
    from systemu.interface.cli_commands import run_find_tools
    v = _Vault(tmp_path, tools=[
        _Tool("t1", "create_issue", "open a github issue"),
        _Tool("t2", "read_file", "read a local file"),
    ])
    rc = run_find_tools(v, "create an issue", limit=15)
    out = capsys.readouterr().out
    assert rc == 0
    # the matching tool AND the non-matching one both listed (never-subtract),
    # match ranked first.
    assert "create_issue" in out and "read_file" in out
    assert out.index("create_issue") < out.index("read_file")


def test_run_find_tools_empty_catalog_is_a_clean_zero(tmp_path, capsys):
    from systemu.interface.cli_commands import run_find_tools
    rc = run_find_tools(_Vault(tmp_path), "anything")
    assert rc == 0                                     # no matches is not an error
    assert "No tools" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# adversarial-review regression fixes (R-CAP1 slice-1+2)
# --------------------------------------------------------------------------- #

def test_derive_index_reads_DICT_tool_headers(tmp_path):
    # HIGH: vault.list_tools() returns DICTS (not objects) — the derive must read
    # dict headers or the whole vault catalog vanishes from the index.
    #
    # The header is built by the REAL producer rather than hand-authored: the
    # original hand-written fixture supplied implementation_path / effect_tags /
    # parameters_schema, three keys `_tool_header` did not emit, so this test
    # passed while the live index was empty. Building it from `_tool_header`
    # means this can only pass against a shape production really produces.
    from systemu.core.models import Tool
    from systemu.vault.vault import _tool_header

    header = _tool_header(Tool(
        id="t1", name="create_issue", description="open an issue",
        tool_type="python", implementation_path="vault/x.py",
        forged_by_systemu=True, enabled=True, status="deployed"))
    assert isinstance(header, dict)

    class _DictVault:
        def __init__(self, root): self.root = str(root)
        def list_tools(self, status=None):
            return [dict(header)]
    rows = ci.derive_index(_DictVault(tmp_path))
    assert len(rows) == 1
    assert rows[0].tool_id == "t1" and rows[0].origin == "forged"
    assert "create:issue" in rows[0].slots


def test_effectful_query_ranks_trusted_origin_above_keyword_stuffed_mcp(tmp_path):
    # MEDIUM: for an effectful slot, an mcp row can't outrank a builtin by stuffing
    # its description/name with query keywords — origin trust wins.
    builtin = ci.IndexRow(tool_id="b", name="create_issue", slots=["create:issue"],
                          origin="builtin")
    stuffed = ci.IndexRow(tool_id="m", name="create_issue_create_issue_issue",
                          detail="create issue create issue create an issue",
                          slots=["create:issue"], origin="mcp:evil")
    top = ci.rank([stuffed, builtin], "create an issue")
    assert top[0].tool_id == "b"                       # builtin outranks stuffed mcp


# --------------------------------------------------------------------------- #
# CAP-3 — the effectful-slot trust weighting ranks on the QUERY VERB.
#
# `capability_index` used to also carry a module-level `_EFFECTFUL_TAGS` set of
# effect classes, commented as the CAP-3 trust weighting. It had ZERO readers (an
# AST sweep over the repo found only its own assignment) — the shipped mechanism
# is `_EFFECTFUL_VERBS`, keyed off the QUERY, per the §5.5.1 BUILD STATUS line
# ("effectful-query → origin-trust > lexical"). It was deleted as misleading dead
# code, and these two pins are the evidence for that deletion: #1 proves the live
# query-verb path is reached and materially decides the ranking WITHOUT the
# deleted constant, and #2 proves a row's OWN effect_tags must never drive it.
#
# The pin directly above cannot carry that weight: `_tokens()` returns a SET, so
# its repeated-word "stuffed" name tokenizes to the same {create, issue} as the
# builtin's. Lexical score TIES, and `trust` then decides in BOTH tuple branches —
# so that test passes whether or not the effectful branch exists. Verified: forcing
# `effectful = False` in `score_key` leaves it green. The fixtures below give the
# untrusted row STRICTLY BETTER lexical overlap, so the two orderings diverge.
# --------------------------------------------------------------------------- #

def test_effectful_query_trust_outranks_a_STRICTLY_BETTER_lexical_match():
    """#1 — the live `_EFFECTFUL_VERBS` (query-verb) path, mutation-detectable.

    The mcp row wins on lexical overlap outright (3 tokens vs 2). Only the
    effectful branch — which sorts `trust` AHEAD of `-lex` — can keep the builtin
    on top. Disabling that branch flips the winner to the untrusted row, so this
    fails if CAP-3's weighting regresses."""
    builtin = ci.IndexRow(tool_id="b", name="account_manager",
                          slots=["delete:account"], origin="builtin")
    stuffed = ci.IndexRow(tool_id="m", name="delete_account_widget",
                          slots=["delete:account"], origin="mcp:evil")
    q = "delete account widget"

    # precondition: the untrusted row really does have the BETTER lexical score,
    # so a pass cannot be an artifact of a tie (the flaw in the pin above).
    lex = lambda r: len(ci._tokens(q) & ci._tokens(r.name, " ".join(r.slots)))
    assert lex(stuffed) > lex(builtin), "fixture must not tie on lexical overlap"

    assert ci.rank([stuffed, builtin], q)[0].tool_id == "b"


def test_a_rows_OWN_effect_tags_never_trigger_the_trust_weighting():
    """#2 — the remove-not-wire decision, locked in.

    Wiring the deleted `_EFFECTFUL_TAGS` would have meant a row's self-declared
    effect_tags decide whether it gets trust protection — letting an untrusted
    tool influence its own trust weight with the very content CAP-3 exists to
    discount. Here the mcp row carries money_move/delete/send_message (members of
    the deleted set) on a NON-effectful query and still wins on lexical merit; it
    must NOT be promoted by its own tags."""
    builtin = ci.IndexRow(tool_id="b", name="account_info",
                          slots=["read:account"], origin="builtin")
    tagged = ci.IndexRow(tool_id="m", name="widget_account_widget",
                         slots=["read:account"], origin="mcp:evil",
                         effect_tags=["money_move", "delete", "send_message"])
    assert ci.rank([tagged, builtin], "account widget")[0].tool_id == "m"

    # and effect_tags are inert in the key itself: two otherwise-identical rows
    # differing ONLY in effect_tags score identically.
    plain = ci.IndexRow(tool_id="same", name="send_email",
                        slots=["send:email"], origin="builtin")
    heavy = ci.IndexRow(tool_id="same", name="send_email", slots=["send:email"],
                        origin="builtin", effect_tags=["money_move", "irreversible"])
    assert ci.score_key(plain, "send an email") == ci.score_key(heavy, "send an email")


def test_description_is_not_a_ranking_signal(tmp_path):
    # a tool whose only overlap is in its (tool-controlled) description must not
    # outrank one matching on name/slot.
    desc_only = ci.IndexRow(tool_id="d", name="zzz", detail="create issue create issue",
                            slots=[], origin="builtin")
    name_match = ci.IndexRow(tool_id="n", name="create_issue", slots=["create:issue"],
                             origin="builtin")
    top = ci.rank([desc_only, name_match], "create an issue")
    assert top[0].tool_id == "n"


def test_slots_from_name_strips_mcp_prefix_any_server_width():
    assert cs.slots_from_name("mcp__gh__create_issue") == [("create", "issue")]
    assert cs.slots_from_name("mcp__my_long_server__send_email") == [("send", "email")]


def test_io_shape_hash_survives_boolean_subschema():
    # a legal JSON-Schema boolean subschema must not crash (which would drop the
    # tool from the index — a never-subtract violation).
    h = ci.io_shape_hash({"properties": {"x": True, "y": {"type": "string"}}})
    assert isinstance(h, str) and h


def test_boolean_subschema_tool_still_indexed(tmp_path):
    v = _Vault(tmp_path, tools=[
        _Tool("t1", "read_file", parameters_schema={"properties": {"flag": True}})])
    rows = ci.derive_index(v)
    assert any(r.tool_id == "t1" for r in rows)         # not silently dropped


# --------------------------------------------------------------------------- #
# CAP-4 slice-3a — order_records: reorder the LLM-visible index most-relevant-
# first for a task, WITHOUT dropping any tool (never-subtract).
# --------------------------------------------------------------------------- #

def _rec(name, desc=""):
    return {"name": name, "description": desc, "parameter_names": [], "parameters_schema": {}}


def test_order_records_puts_the_match_first_and_keeps_all():
    recs = [_rec("read_file"), _rec("send_email"), _rec("create_issue")]
    out = ci.order_records(recs, "create an issue")
    assert out[0]["name"] == "create_issue"            # slot match ranked first
    assert {r["name"] for r in out} == {r["name"] for r in recs}   # never-subtract


def test_order_records_ignores_tool_controlled_description():
    # a tool stuffing the query into its description must NOT jump ahead of a real
    # name/slot match (description is not a ranking signal).
    recs = [
        _rec("zzz_unrelated", "create an issue create an issue create an issue"),
        _rec("create_issue"),
    ]
    out = ci.order_records(recs, "create an issue")
    assert out[0]["name"] == "create_issue"


def test_order_records_is_deterministic_and_defensive():
    recs = [_rec("b_tool"), _rec("a_tool")]
    assert [r["name"] for r in ci.order_records(recs, "x")] == \
           [r["name"] for r in ci.order_records(recs, "x")]         # stable
    # a malformed record (missing name) must not crash — order left intact
    weird = [{"nope": 1}, _rec("create_issue")]
    out = ci.order_records(weird, "create an issue")
    assert len(out) == 2                                            # never-subtract
    assert out[0]["name"] == "create_issue"


def test_order_records_preserves_order_when_no_relevance_signal():
    # a zero-signal query must NOT alphabetically reshuffle (which would front-load
    # verb-first names like delete_* ahead of get_*) — original order is kept.
    recs = [_rec("zebra_tool"), _rec("delete_thing"), _rec("apple_tool")]
    out = ci.order_records(recs, "no overlap here")
    assert [r["name"] for r in out] == ["zebra_tool", "delete_thing", "apple_tool"]


# --------------------------------------------------------------------------- #
# CAP-5/CAP-6 slice-4 — same-slot dedup advisory at the forge gate (non-blocking)
# --------------------------------------------------------------------------- #

def test_slot_collisions_finds_same_slot_tool(tmp_path):
    v = _Vault(tmp_path, tools=[
        _Tool("t1", "create_issue"), _Tool("t2", "read_file")])
    cols = ci.slot_collisions(v, "open_issue")           # open→create, so create:issue
    assert [c["tool_id"] for c in cols] == ["t1"]        # collides with create_issue
    assert ci.slot_collisions(v, "delete_widget") == []  # free slot → no collision


def test_slot_collisions_excludes_self(tmp_path):
    v = _Vault(tmp_path, tools=[_Tool("t1", "create_issue")])
    assert ci.slot_collisions(v, "create_issue", exclude_id="t1") == []


def test_slot_collisions_no_slot_name_is_empty(tmp_path):
    v = _Vault(tmp_path, tools=[_Tool("t1", "create_issue")])
    assert ci.slot_collisions(v, "???") == []            # no derivable slot → no false hit


def test_forge_dedup_advisory_wording():
    adv = ci.forge_dedup_advisory("open_issue", [{"tool_id": "t1", "name": "create_issue",
                                                  "slots": ["create:issue"]}])
    assert "create_issue" in adv and "create:issue" in adv and "extend" in adv.lower()
    assert ci.forge_dedup_advisory("x", []) == ""        # free slot → no advisory line


# --------------------------------------------------------------------------- #
# PRODUCER-GROUNDED pins — the "empty index on a real vault" regression
#
# ``test_derive_index_reads_DICT_tool_headers`` above was added by the R-CAP1
# adversarial review to prove "the whole vault catalog doesn't vanish from the
# index". It hand-authored its header dict, and THREE of the keys it supplies
# (implementation_path / effect_tags / parameters_schema) were keys the real
# producer — ``vault._tool_header`` — did not emit. So it passed green while
# ``derive_index`` returned ZERO rows for every real vault: a SqliteVault with 41
# enabled seeded builtins (read_file, web_search, …) indexed nothing, and
# ``sharing-on find-tools "read a file"`` answered "No tools on your table yet."
#
# These pins never hand-author a header. They RUN the producer and assert on
# whatever it actually emits.
# --------------------------------------------------------------------------- #

def _real_file_vault(tmp_path):
    """A REAL Vault with a REAL Tool saved through the REAL save path, so every
    header under test is produced by ``vault._tool_header``, not by hand."""
    from systemu.core.models import Tool
    from systemu.vault.vault import Vault
    v = Vault(vault_dir=tmp_path / "realvault")
    impl = Path(v.root) / "tools" / "implementations" / "create_github_issue.py"
    impl.parent.mkdir(parents=True, exist_ok=True)
    impl.write_text("def run(**kw):\n    return {}\n", encoding="utf-8")
    v.save_tool(Tool(
        id="tool-gh", name="create_github_issue",
        description="open an issue on github", tool_type="python",
        implementation_path=str(impl),
        # bare-props form — the shape every shipped seed tool actually stores
        parameters_schema={"title": {"type": "string"}},
        enabled=True, status="deployed",
    ))
    return v


def test_derive_index_is_NONEMPTY_for_a_real_vault_catalog(tmp_path):
    v = _real_file_vault(tmp_path)
    assert v.list_tools(), "producer precondition: the vault must list the saved tool"
    rows = ci.derive_index(v)
    assert rows, ("derive_index returned NOTHING for a real enabled+deployed tool — "
                  "the entire vault catalog is missing from the capability index")
    assert rows[0].name == "create_github_issue"
    assert "create:issue" in rows[0].slots


@pytest.mark.parametrize("backend", ["file", "sqlite"])
def test_tool_header_emits_every_field_derive_index_gates_on(tmp_path, backend):
    """FIXTURE-REALISM pin: fails if derive_index gates on a key the real producer
    never emits. This is the guard the hand-authored dict fixture could not be.

    Covers BOTH producers — there are exactly two ``_tool_header``
    implementations (systemu/vault/vault.py and systemu/storage/sqlite/vault.py)
    and they must not drift apart, since either can back a live index."""
    if backend == "file":
        header = _real_file_vault(tmp_path).list_tools()[0]
    else:
        from systemu.storage.sqlite.vault import SqliteVault
        header = SqliteVault(f"sqlite:///{tmp_path / 'v.db'}").list_tools()[0]
    for key in ("id", "name", "enabled", "implementation_path"):
        assert key in header, (
            f"the {backend} _tool_header() does not emit {key!r} but "
            f"capability_index gates on it — any fixture supplying {key!r} asserts "
            f"a shape production never produces")


def test_sqlite_seeded_catalog_produces_a_nonempty_index(tmp_path):
    """The production backend, over its real seeded builtin tools."""
    from systemu.storage.sqlite.vault import SqliteVault
    v = SqliteVault(f"sqlite:///{tmp_path / 'v.db'}")
    seeded = [h for h in v.list_tools() if h.get("enabled")]
    assert len(seeded) > 5, "precondition: the sqlite vault seeds enabled builtin tools"
    rows = ci.derive_index(v)
    assert len(rows) >= len(seeded), (
        f"{len(seeded)} enabled seeded tools produced only {len(rows)} index rows")
    indexed = {r.name for r in rows}
    # named seeds, so a silently-shrinking catalog is caught as well as an empty one
    assert {"file_read", "web_search", "file_write"} <= indexed


def test_sqlite_seeded_catalog_yields_DISTINCT_io_shapes(tmp_path):
    """io_shape_hash must actually discriminate: reading only the wrapped-schema
    form hashed all 41 seeded tools to ONE digest (they store bare props), which
    would make every tool look interface-identical to same-slot forge dedup."""
    from systemu.storage.sqlite.vault import SqliteVault
    v = SqliteVault(f"sqlite:///{tmp_path / 'v.db'}")
    rows = ci.derive_index(v)
    assert len(rows) > 20, "precondition: the seeded catalog is indexed"
    assert len({r.io_shape_hash for r in rows}) > len(rows) // 2


def test_forge_dedup_sees_an_existing_real_vault_tool(tmp_path):
    """CAP-6: with an empty index the dedup advisory was silently always blank, so
    the forge could never warn it was about to duplicate a shipped tool."""
    v = _real_file_vault(tmp_path)
    cols = ci.slot_collisions(v, "create_issue")
    assert cols, "slot_collisions found nothing though create_github_issue holds create:issue"
    assert ci.forge_dedup_advisory("create_issue", cols)


def test_ready_tolerates_a_LEGACY_header_without_implementation_path(tmp_path):
    """An on-disk tools/index.json written before the field was added carries no
    implementation_path key. ABSENT means unknown, not bodiless — gating on absence
    is what emptied the index for every existing vault."""
    class _LegacyVault:
        def __init__(self, root): self.root = str(root)
        def list_tools(self, status=None):
            return [{"id": "old1", "name": "read_file", "enabled": True,
                     "description": "read a file", "status": "deployed"}]
    assert len(ci.derive_index(_LegacyVault(tmp_path))) == 1


def test_a_present_but_EMPTY_implementation_path_is_still_skipped(tmp_path):
    """The gate keeps its teeth where the producer actually reports a bodiless tool."""
    class _BodilessVault:
        def __init__(self, root): self.root = str(root)
        def list_tools(self, status=None):
            return [{"id": "b1", "name": "read_file", "enabled": True,
                     "implementation_path": "", "status": "proposed"}]
    assert ci.derive_index(_BodilessVault(tmp_path)) == []


def test_io_shape_hash_reads_the_BARE_PROPS_schema_production_stores():
    """The shipped seed tools store parameters_schema as bare {name: {"type": …}},
    NOT wrapped in {"properties": …}. Hashing only the wrapped form collapsed all
    41 real tools onto the empty-schema digest, so io_shape could not tell any two
    tools' interfaces apart."""
    a = ci.io_shape_hash({"query": {"type": "string"}})
    b = ci.io_shape_hash({"path": {"type": "string"}, "mode": {"type": "string"}})
    assert a != b
    assert a != ci.io_shape_hash({})


def test_io_shape_hash_agrees_across_full_schema_and_header_summary():
    """A tool must hash to the same shape whether derived from the full schema or
    from the header's parameters_schema_summary — `_tool_header` carries only the
    summary, so mismatched sources would collide inconsistently."""
    summary = ci.io_shape_hash({"query": "string"})
    assert ci.io_shape_hash({"query": {"type": "string"}}) == summary
    assert ci.io_shape_hash({"type": "object",
                             "properties": {"query": {"type": "string"}}}) == summary
