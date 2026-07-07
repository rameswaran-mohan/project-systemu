"""R-A9 §5.1 S2 — build_capabilities(vault): the operator's usable Tool catalog
surfaced as CapabilityRef rows (effect_tags [] = UNKNOWN, never invented).

Seeded against a REAL FileVault (systemu.vault.vault.Vault): effect_tags are only
faithfully persisted on the file backend (a G0 SQLite-backfill gap, out of R-A9
scope), so these tests exercise the honest path.
"""
from __future__ import annotations

import pytest

from systemu.core.models import Tool, ToolStatus, ToolType
from systemu.runtime.situational_inventory import build_capabilities, CapabilityRef


@pytest.fixture
def vault(tmp_path):
    from systemu.vault.vault import Vault
    for sub in ("scrolls", "activities", "shadow_army", "skills", "tools", "evolutions"):
        (tmp_path / sub).mkdir()
        (tmp_path / sub / "index.json").write_text("[]")
    return Vault(str(tmp_path))


def _save(vault, tid, **overrides):
    kwargs = dict(
        id=tid, name=tid, description="d", tool_type=ToolType.PYTHON_FUNCTION,
        status=ToolStatus.DEPLOYED, enabled=True,
    )
    kwargs.update(overrides)
    vault.save_tool(Tool(**kwargs))


def test_empty_catalog_returns_empty(vault):
    assert build_capabilities(vault) == []


def test_deployed_tool_surfaces_effect_tags_and_schema(vault):
    _save(
        vault, "fetch_page",
        effect_tags=["net_read"],
        parameters_schema={"type": "object", "properties": {"url": {"type": "string"}}},
        forged_by_systemu=True,
    )
    caps = build_capabilities(vault)
    assert len(caps) == 1
    cap = caps[0]
    assert isinstance(cap, CapabilityRef)
    assert cap.tool_id == "fetch_page"
    assert cap.effect_tags == ["net_read"]        # real tag surfaces, not invented
    assert cap.schema_ref is not None             # a schema exists -> a pointer/marker
    assert cap.forgeable is True                  # self-forged, not forge_rejected
    assert cap.source_kind == "capability"
    assert cap.origin_class == "systemu_authored"


def test_empty_effect_tags_pass_through_as_unknown_not_crash(vault):
    # A tool with no readable source has effect_tags=[] = UNKNOWN-until-classified.
    _save(vault, "mystery", effect_tags=[], parameters_schema={})
    caps = build_capabilities(vault)
    assert len(caps) == 1
    assert caps[0].tool_id == "mystery"
    assert caps[0].effect_tags == []              # [] passes through, does NOT crash
    assert caps[0].schema_ref is None             # no schema -> no pointer


def test_only_deployed_tools_are_usable(vault):
    _save(vault, "live_tool", status=ToolStatus.DEPLOYED, effect_tags=["local_read"])
    _save(vault, "latent_tool", status=ToolStatus.FORGED, effect_tags=["local_read"])
    _save(vault, "draft_tool", status=ToolStatus.PROPOSED)
    ids = {c.tool_id for c in build_capabilities(vault)}
    assert ids == {"live_tool"}                   # latent/proposed are not "HAVE"


def test_deployed_but_disabled_tool_is_excluded(vault):
    # A DEPLOYED tool with enabled=False is Gate-3 blocked (tool_registry raises
    # ToolNotEnabledError) — NOT a usable capability. Forged tools deploy
    # enabled=False by default; a failed recalibrate leaves DEPLOYED+disabled.
    _save(vault, "usable", status=ToolStatus.DEPLOYED, enabled=True)
    _save(vault, "disabled", status=ToolStatus.DEPLOYED, enabled=False)
    ids = {c.tool_id for c in build_capabilities(vault)}
    assert ids == {"usable"}                      # disabled leaks in today -> must not


def test_forgeable_derivation(vault):
    # forgeable = self-forged provenance AND not operator-declined (forge_rejected).
    _save(vault, "self_forged", forged_by_systemu=True, forge_rejected=False)
    _save(vault, "builtin", forged_by_systemu=False)
    _save(vault, "declined", forged_by_systemu=True, forge_rejected=True)
    by_id = {c.tool_id: c for c in build_capabilities(vault)}
    assert by_id["self_forged"].forgeable is True
    assert by_id["builtin"].forgeable is False
    assert by_id["declined"].forgeable is False


def test_broken_vault_returns_empty():
    class _Broken:
        def list_tools(self, status=None):
            raise RuntimeError("vault exploded")
    assert build_capabilities(_Broken()) == []


def test_single_bad_get_tool_is_skipped_not_fatal(vault):
    # N+1 robustness: one tool that fails get_tool is skipped, survey continues.
    _save(vault, "good", effect_tags=["net_read"])
    _save(vault, "bad", effect_tags=["net_read"])
    real_get = vault.get_tool

    def flaky_get(tool_id):
        if tool_id == "bad":
            raise KeyError("corrupt tool json")
        return real_get(tool_id)

    vault.get_tool = flaky_get
    ids = {c.tool_id for c in build_capabilities(vault)}
    assert ids == {"good"}                         # bad skipped, good survives
