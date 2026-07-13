"""R-A13.5 slice-1 — the avoidable-FORGE replay metric (CAP-10).

Deterministic post-hoc replay (IMPL-15 discipline, never an LLM judge): for every
forged tool, re-run the capability-index slot query with hindsight — would an
EXISTING tool already in that slot have bound? The rate is the CAP-10 tripwire that
adjudicates the DEC-18 "no embeddings" (CAP-8) question, reported beside the §10
avoidable-ask rate. Computable over the live vault today (reuses the R-CAP1 index).
"""
from __future__ import annotations

from pathlib import Path

from systemu.runtime import replay_metrics as rm


class _Tool:
    def __init__(self, id, name, forged=False, enabled=True,
                 implementation_path="vault/tools/x.py"):
        self.id = id; self.name = name; self.forged_by_systemu = forged
        self.enabled = enabled; self.implementation_path = implementation_path
        self.status = "deployed"; self.effect_tags = []; self.parameters_schema = {}
        self.description = ""


class _Vault:
    def __init__(self, root: Path, tools=None):
        self.root = str(root)
        self._tools = tools or []

    def list_tools(self, status=None):
        # the real vault returns DICT headers — mirror that (the R-CAP1 contract)
        return [{"id": t.id, "name": t.name, "forged_by_systemu": t.forged_by_systemu,
                 "enabled": t.enabled, "implementation_path": t.implementation_path,
                 "status": t.status, "effect_tags": t.effect_tags,
                 "parameters_schema": t.parameters_schema, "description": t.description}
                for t in self._tools]


def test_forged_tool_alone_in_its_slot_is_not_avoidable(tmp_path):
    v = _Vault(tmp_path, tools=[_Tool("t1", "create_issue", forged=True)])
    rep = rm.avoidable_forge_report(v)
    assert rep["total_forged"] == 1
    assert rep["avoidable_count"] == 0 and rep["rate"] == 0.0


def test_forged_tool_colliding_with_a_builtin_is_avoidable(tmp_path):
    # a builtin already occupies create:issue → forging a second one was avoidable
    v = _Vault(tmp_path, tools=[
        _Tool("builtin1", "create_issue", forged=False),
        _Tool("forged1", "open_issue", forged=True),        # open→create, same slot
    ])
    rep = rm.avoidable_forge_report(v)
    assert rep["total_forged"] == 1                          # only the forged one counts as a forge
    assert rep["avoidable_count"] == 1 and rep["rate"] == 1.0
    item = rep["avoidable"][0]
    assert item["tool_id"] == "forged1"
    assert "create_issue" in item["would_bind"]


def test_two_mutually_forged_in_a_slot_counts_only_the_extras(tmp_path):
    # adversarial-review fix: two forged tools share create:issue, NO builtin. The
    # FIRST forge into the (then-empty) slot was NOT avoidable — so exactly k-1 (=1)
    # count, not both (no symmetric double-count).
    v = _Vault(tmp_path, tools=[
        _Tool("f1", "create_issue", forged=True),
        _Tool("f2", "open_issue", forged=True),         # open→create, same slot
    ])
    rep = rm.avoidable_forge_report(v)
    assert rep["total_forged"] == 2 and rep["assessable"] == 2
    assert rep["avoidable_count"] == 1                   # NOT 2
    assert rep["avoidable"][0]["tool_id"] == "f2"        # the non-first (min id = f1 kept)


def test_three_forged_in_a_slot_counts_k_minus_one(tmp_path):
    v = _Vault(tmp_path, tools=[
        _Tool("a", "create_issue", forged=True),
        _Tool("b", "open_issue", forged=True),
        _Tool("c", "make_issue", forged=True),          # make→create, same slot
    ])
    rep = rm.avoidable_forge_report(v)
    assert rep["avoidable_count"] == 2                   # 3 forged, 1 first kept → 2 extras


def test_forged_with_no_slot_is_unassessable_not_avoidable(tmp_path):
    # a name with no derivable verb → no slot → excluded from BOTH numerator and
    # denominator (never silently deflates the rate).
    v = _Vault(tmp_path, tools=[_Tool("x", "zzz", forged=True)])
    rep = rm.avoidable_forge_report(v)
    assert rep["total_forged"] == 1 and rep["assessable"] == 0
    assert rep["unassessable_no_slot"] == 1 and rep["avoidable_count"] == 0
    assert rep["rate"] == 0.0


def test_report_is_deterministic(tmp_path):
    v = _Vault(tmp_path, tools=[
        _Tool("b", "create_issue"), _Tool("f", "open_issue", forged=True)])
    assert rm.avoidable_forge_report(v) == rm.avoidable_forge_report(v)


def test_empty_vault_is_a_clean_zero(tmp_path):
    rep = rm.avoidable_forge_report(_Vault(tmp_path))
    assert rep["total_forged"] == 0 and rep["rate"] == 0.0 and rep["avoidable"] == []


def test_never_raises_on_a_broken_vault(tmp_path):
    class _Bad:
        root = str(tmp_path)
        def list_tools(self, status=None):
            raise RuntimeError("boom")
    rep = rm.avoidable_forge_report(_Bad())
    assert rep["total_forged"] == 0 and rep["rate"] == 0.0   # defensive


def test_format_report_lines_are_plain_strings(tmp_path):
    v = _Vault(tmp_path, tools=[
        _Tool("b", "create_issue"), _Tool("f", "open_issue", forged=True)])
    lines = rm.format_avoidable_forge(rm.avoidable_forge_report(v))
    assert isinstance(lines, list) and all(isinstance(x, str) for x in lines)
    assert any("open_issue" in x for x in lines) and any("%" in x for x in lines)
