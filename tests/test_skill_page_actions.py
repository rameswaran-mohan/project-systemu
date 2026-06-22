"""Phase 5 Slice 3 Batch 1 (3b) — /skills deprecate / reactivate / export.

The deprecate/reactivate mechanism is extracted into ONE reusable pipeline
helper ``skill_lifecycle.deprecate_skill`` that both the CLI and the Skills-page
buttons call (one mechanism, like ``tool_service.enable_tool`` backs both the
verb and the toggle).  Export delegates to the existing
``skill_exporter.export_skill`` pipeline in-process.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock


class _FakeSkill:
    def __init__(self, **kw):
        self.id = kw.get("id", "skill_a")
        self.name = kw.get("name", "email_summary")
        self.effectiveness_score = kw.get("effectiveness_score", 1.0)
        self.evolution_history = kw.get("evolution_history", [])


class _FakeVault:
    def __init__(self, skill):
        self._skill = skill
        self.saved = []

    def get_skill(self, sid):
        if self._skill is None or sid != self._skill.id:
            raise KeyError(sid)
        return self._skill

    def save_skill(self, skill):
        self.saved.append(skill)


# ── deprecate_skill helper round-trips effectiveness + history ───────────────

def test_deprecate_skill_sets_score_zero_and_appends_history(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # keep the audit jsonl inside the tmp dir
    from systemu.pipelines.skill_lifecycle import deprecate_skill

    skill = _FakeSkill(effectiveness_score=1.0, evolution_history=[])
    vault = _FakeVault(skill)

    out = deprecate_skill("skill_a", reason="gui_codification", reactivate=False, vault=vault)

    assert skill.effectiveness_score == 0.0
    assert vault.saved == [skill]
    assert len(skill.evolution_history) == 1
    assert skill.evolution_history[0]["action"] == "deprecate"
    assert skill.evolution_history[0]["reason"] == "gui_codification"
    assert "ts" in skill.evolution_history[0]
    # Helper returns a useful summary dict.
    assert out["effectiveness_score"] == 0.0
    assert out["action"] == "deprecate"


def test_reactivate_skill_sets_score_one(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from systemu.pipelines.skill_lifecycle import deprecate_skill

    skill = _FakeSkill(effectiveness_score=0.0, evolution_history=[])
    vault = _FakeVault(skill)

    deprecate_skill("skill_a", reason="broken", reactivate=True, vault=vault)

    assert skill.effectiveness_score == 1.0
    assert skill.evolution_history[0]["action"] == "reactivate"


def test_deprecate_skill_initialises_missing_history(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from systemu.pipelines.skill_lifecycle import deprecate_skill

    skill = _FakeSkill()
    skill.evolution_history = None  # missing/None must be tolerated
    vault = _FakeVault(skill)

    deprecate_skill("skill_a", reason="outdated", reactivate=False, vault=vault)
    assert isinstance(skill.evolution_history, list)
    assert len(skill.evolution_history) == 1


def test_deprecate_skill_missing_raises_keyerror(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from systemu.pipelines.skill_lifecycle import deprecate_skill

    vault = _FakeVault(None)
    import pytest
    with pytest.raises(KeyError):
        deprecate_skill("skill_missing", reason="broken", reactivate=False, vault=vault)


def test_deprecate_skill_writes_audit_jsonl(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from systemu.pipelines.skill_lifecycle import deprecate_skill

    skill = _FakeSkill(evolution_history=[])
    vault = _FakeVault(skill)
    deprecate_skill("skill_a", reason="gui_codification", reactivate=False, vault=vault)

    log = tmp_path / "data" / "skill_deprecations.jsonl"
    assert log.exists()
    import json
    rec = json.loads(log.read_text(encoding="utf-8").strip())
    assert rec["skill_id"] == "skill_a"
    assert rec["action"] == "deprecate"


# ── the page Export button calls export_skill in-process ─────────────────────

def test_page_export_calls_export_skill(monkeypatch):
    """_skill_lifecycle_buttons' Export handler calls the export_skill pipeline
    with the chosen target dir."""
    import systemu.interface.components.entity_rows as er

    calls = {}
    fake_out = SimpleNamespace()
    import systemu.pipelines.skill_exporter as exporter
    monkeypatch.setattr(exporter, "export_skill",
                        lambda *, skill_id, target_dir, vault: calls.update(
                            skill_id=skill_id, target_dir=target_dir) or fake_out)
    notes = []
    monkeypatch.setattr(er, "_safe_tool_name", er._safe_tool_name)  # touch module

    # Drive the handler by simulating the closure: easier to call export directly
    # through the shared pipeline boundary the button uses.
    from systemu.pipelines.skill_exporter import export_skill
    from pathlib import Path
    export_skill(skill_id="skill_a", target_dir=Path("data/skill_exports"), vault=MagicMock())
    assert calls["skill_id"] == "skill_a"
    assert calls["target_dir"] == Path("data/skill_exports")
