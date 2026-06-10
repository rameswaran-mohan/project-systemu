"""Phase 5 Slice 3 Batch 1 (3a/3d) — shared canonical entity renderers.

``systemu.interface.components.entity_rows`` owns ONE renderer per entity:
``render_tool_row`` / ``render_skill_row``.  The NiceGUI rendering itself can't
run headless, so we test the pure view-model functions that drive the cells —
the same split-the-data-from-the-paint pattern as ``remediation_card_model``.
"""
from __future__ import annotations

from types import SimpleNamespace


# ── tool row view-model (3a + 3d) ────────────────────────────────────────────

def test_tool_row_model_carries_core_cells():
    from systemu.interface.components.entity_rows import tool_row_model

    header = {
        "id": "tool_a", "name": "fetch_json", "tool_type": "python_function",
        "status": "forged", "enabled": False, "dry_run_status": "passed",
        "description": "Fetch and parse JSON from a URL",
    }
    m = tool_row_model(header)
    assert m["id"] == "tool_a"
    assert m["name"] == "fetch_json"
    assert m["tool_type"] == "python_function"
    assert m["status"] == "forged"
    assert m["dry_run_status"] == "passed"


def test_tool_row_model_enable_gate_matches_legacy_policy():
    """Enable button only when reviewed-but-disabled AND dry_run passed."""
    from systemu.interface.components.entity_rows import tool_row_model

    ok = tool_row_model({"id": "t", "status": "forged", "enabled": False,
                         "dry_run_status": "passed"})
    assert ok["show_enable"] is True

    not_passed = tool_row_model({"id": "t", "status": "forged", "enabled": False,
                                 "dry_run_status": "failed"})
    assert not_passed["show_enable"] is False

    already_on = tool_row_model({"id": "t", "status": "forged", "enabled": True,
                                 "dry_run_status": "passed"})
    assert already_on["show_enable"] is False


def test_tool_row_model_proposed_offers_review_and_forge():
    from systemu.interface.components.entity_rows import tool_row_model
    m = tool_row_model({"id": "t", "status": "proposed", "dry_run_status": "not_run"})
    assert m["show_review_forge"] is True
    # Dry-Run action still surfaces for proposed (mirrors _row_actions_for).
    assert any(a["kind"] == "dryrun" for a in m["actions"])


# ── 3d: tool dependencies inline in the row ──────────────────────────────────

def test_tool_deps_from_header_when_present():
    from systemu.interface.components.entity_rows import tool_row_deps
    header = {"id": "t", "dependencies": ["requests", "pyyaml"]}
    assert tool_row_deps(header, vault=None) == ["requests", "pyyaml"]


def test_tool_deps_fallback_to_vault_when_header_lacks_them():
    from systemu.interface.components.entity_rows import tool_row_deps
    header = {"id": "tool_a"}  # no 'dependencies' key in the index header
    vault = SimpleNamespace(
        get_tool=lambda tid: SimpleNamespace(dependencies=["httpx"]) if tid == "tool_a"
        else (_ for _ in ()).throw(KeyError(tid))
    )
    assert tool_row_deps(header, vault=vault) == ["httpx"]


def test_tool_deps_defensive_empty():
    from systemu.interface.components.entity_rows import tool_row_deps
    # No header deps, vault lookup explodes → empty, never raises.
    vault = SimpleNamespace(get_tool=lambda tid: (_ for _ in ()).throw(KeyError(tid)))
    assert tool_row_deps({"id": "x"}, vault=vault) == []
    assert tool_row_deps({"id": "x"}, vault=None) == []


def test_tool_deps_display_caps_and_overflows():
    from systemu.interface.components.entity_rows import tool_deps_display
    few = tool_deps_display(["a", "b"], cap=4)
    assert few == {"visible": ["a", "b"], "overflow": 0}

    many = tool_deps_display(["a", "b", "c", "d", "e", "f"], cap=4)
    assert many["visible"] == ["a", "b", "c", "d"]
    assert many["overflow"] == 2

    assert tool_deps_display([], cap=4) == {"visible": [], "overflow": 0}


def test_tool_row_model_includes_deps_from_header():
    from systemu.interface.components.entity_rows import tool_row_model
    m = tool_row_model({"id": "t", "status": "forged",
                        "dependencies": ["requests", "pyyaml"]})
    assert m["deps"] == ["requests", "pyyaml"]


# ── skill row view-model (3a + 3b deprecated badge) ──────────────────────────

def test_skill_row_model_basics():
    from systemu.interface.components.entity_rows import skill_row_model
    s = {"id": "skill_a", "name": "email_summary", "category": "communication",
         "description": "Summarize threads", "evidence_scroll_ids": ["scr_1"]}
    m = skill_row_model(s)
    assert m["id"] == "skill_a"
    assert m["name"] == "email_summary"
    assert m["category"] == "communication"
    assert m["evidence_count"] == 1


def test_skill_row_model_deprecated_badge_below_threshold():
    """effectiveness_score < 0.5 → deprecated badge surfaces (3b)."""
    from systemu.interface.components.entity_rows import skill_row_model
    assert skill_row_model({"id": "s", "name": "x", "effectiveness_score": 0.0})["deprecated"] is True
    assert skill_row_model({"id": "s", "name": "x", "effectiveness_score": 0.49})["deprecated"] is True
    assert skill_row_model({"id": "s", "name": "x", "effectiveness_score": 0.5})["deprecated"] is False
    assert skill_row_model({"id": "s", "name": "x", "effectiveness_score": 1.0})["deprecated"] is False
    # Missing score defaults to 1.0 (not deprecated).
    assert skill_row_model({"id": "s", "name": "x"})["deprecated"] is False


# ── 3b: effectiveness resolved from the vault when the header lacks it ────────

def test_skill_effectiveness_prefers_header():
    from systemu.interface.components.entity_rows import skill_effectiveness
    assert skill_effectiveness({"id": "s", "effectiveness_score": 0.0}, vault=None) == 0.0


def test_skill_effectiveness_falls_back_to_vault():
    from systemu.interface.components.entity_rows import skill_effectiveness
    vault = SimpleNamespace(
        get_skill=lambda sid: SimpleNamespace(effectiveness_score=0.0) if sid == "s"
        else (_ for _ in ()).throw(KeyError(sid))
    )
    # No score in the header → vault lookup yields the deprecated score.
    assert skill_effectiveness({"id": "s"}, vault=vault) == 0.0


def test_skill_effectiveness_defensive_default():
    from systemu.interface.components.entity_rows import skill_effectiveness
    vault = SimpleNamespace(get_skill=lambda sid: (_ for _ in ()).throw(KeyError(sid)))
    assert skill_effectiveness({"id": "x"}, vault=vault) == 1.0
    assert skill_effectiveness({"id": "x"}, vault=None) == 1.0


# ── renderers are importable with the documented signatures ──────────────────

def test_renderers_importable():
    import inspect
    from systemu.interface.components import entity_rows

    for fn_name in ("render_tool_row", "render_skill_row"):
        fn = getattr(entity_rows, fn_name)
        sig = inspect.signature(fn)
        # editable is a keyword-only flag defaulting True.
        assert sig.parameters["editable"].default is True
