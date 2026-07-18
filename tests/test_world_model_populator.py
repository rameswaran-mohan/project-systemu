"""R-W1 (W-A slice-2a) — the SituationReport → FactStore populator.

Pins the entry→Fact mapping, idempotence across runs (no dupes, no chain growth),
end-to-end queryability, the no-secret credential rule, fail-safe defensiveness, and
the carried-through trust property (a content_derived file fact is not silent-bind
permitted). The write-only / no-planner-change / no-binder-read boundary is pinned by
tests/test_world_model.py::test_store_is_not_read_by_any_bind_or_plan_module.
"""
from __future__ import annotations

from types import SimpleNamespace

from systemu.runtime import world_model as wm
from systemu.runtime.world_model_populator import populate_from_situation, _facts_from_report
from systemu.runtime.situational_inventory import (
    SituationReport, ConnectedService, CapabilityRef, RootSurvey, FileHandleLite,
)


def _store(tmp_path):
    return wm.FactStore(SimpleNamespace(root=tmp_path))


def _report():
    return SituationReport(
        services=[ConnectedService(name="https://api.github.com", auth_kind="oauth", has_live_token=True)],
        capabilities=[CapabilityRef(tool_id="mcp__github__create_issue")],
        roots=[RootSurvey(path="C:/Users/me/Invoices", salient=[
            FileHandleLite(path="C:/Users/me/Invoices/jan.pdf", name="jan.pdf", ext=".pdf", size=1000, mtime=1.0)])],
        credentials=["openrouter", "github"],
    )


def test_populate_maps_each_entry_to_the_right_kind_and_origin(tmp_path):
    n = populate_from_situation(_report(), SimpleNamespace(root=tmp_path))
    assert n == 5                                              # 1 svc + 1 cap + 1 file + 2 creds
    s = _store(tmp_path)
    kinds = {f.kind for f in s.all_facts()}
    assert kinds == {"service", "capability", "data_location", "credential_ref"}
    svc = wm.find_services(s, "github")[0]
    assert svc.value == "https://api.github.com" and svc.origin_class == "operator"
    cap = wm.what_can(s, "create", "issue")[0]
    assert cap.value == "mcp__github__create_issue" and cap.origin_class == "systemu_authored"
    data = [f for f in s.all_facts() if f.kind == "data_location"][0]
    assert data.value.endswith("jan.pdf") and data.origin_class == "content_derived"


def test_populate_is_idempotent_across_runs(tmp_path):
    v = SimpleNamespace(root=tmp_path)
    assert populate_from_situation(_report(), v) == 5
    assert populate_from_situation(_report(), v) == 5         # second run confirms in place
    s = _store(tmp_path)
    assert len(s.all_facts()) == 5                            # no duplicates (stable fact_id)
    svc = wm.find_services(s, "github")[0]
    assert len(svc.source_chain) == 1                         # re-observed, chain not grown (F4)


def test_credential_facts_carry_the_name_not_a_secret(tmp_path):
    populate_from_situation(_report(), SimpleNamespace(root=tmp_path))
    creds = [f for f in _store(tmp_path).all_facts() if f.kind == "credential_ref"]
    assert {f.value for f in creds} == {"openrouter", "github"}   # names only (E6)
    assert all(f.origin_class == "operator" for f in creds)


def test_content_derived_file_fact_is_not_silent_bind_permitted(tmp_path):
    populate_from_situation(_report(), SimpleNamespace(root=tmp_path))
    data = [f for f in _store(tmp_path).all_facts() if f.kind == "data_location"][0]
    assert data.taint_permits_silent_bind is False           # trust property carried through


def test_populate_is_fail_safe_on_bad_input(tmp_path):
    v = SimpleNamespace(root=tmp_path)
    assert populate_from_situation(None, v) == 0              # no report → 0, no raise
    assert populate_from_situation(SituationReport(), v) == 0 # empty report → 0
    # a report whose entry has an out-of-vocab origin_class: that entry is SKIPPED
    bad = SituationReport(services=[
        ConnectedService(name="x", auth_kind="k", has_live_token=False, origin_class="bogus")])
    facts = _facts_from_report(bad)
    assert facts == []                                        # validator rejects → skipped, not raised


def test_populate_skips_empty_values(tmp_path):
    rep = SituationReport(credentials=["", "real"])           # an empty name is skipped
    assert [f.value for f in _facts_from_report(rep)] == ["real"]


def test_one_run_cannot_mint_unbounded_data_location_facts():
    # F2: `data_location` is the churny kind (a busy root re-mints path facts). Slice-2a
    # has no removal (belief-revision/gardener is W-D), so ONE run's contribution is
    # capped rather than silently unbounded.
    import systemu.runtime.world_model_populator as pop
    many = [FileHandleLite(path=f"C:/r/f{i}.txt", name=f"f{i}.txt", ext=".txt", size=1, mtime=1.0)
            for i in range(pop._MAX_DATA_LOCATION_PER_RUN + 50)]
    facts = _facts_from_report(SituationReport(roots=[RootSurvey(path="C:/r", salient=many)]))
    assert len(facts) == pop._MAX_DATA_LOCATION_PER_RUN
