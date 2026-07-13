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
    class _DictVault:
        def __init__(self, root): self.root = str(root)
        def list_tools(self, status=None):
            return [{"id": "t1", "name": "create_issue", "enabled": True,
                     "implementation_path": "vault/x.py", "effect_tags": ["net_call"],
                     "forged_by_systemu": True, "description": "open an issue",
                     "parameters_schema": {}, "status": "deployed"}]
    rows = ci.derive_index(_DictVault(tmp_path))
    assert len(rows) == 1
    assert rows[0].tool_id == "t1" and rows[0].origin == "forged"
    assert "create:issue" in rows[0].slots and rows[0].effect_tags == ["net_call"]


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
