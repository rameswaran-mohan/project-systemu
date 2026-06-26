"""Phase 7 — governed bounded self-heal re-forge (2026-06-26 addendum plan).

A forged tool that fails its dry-run with a CODE bug (e.g. the LLM wrote
``encrypt(output_file)`` missing the required ``outfile`` positional) gets ONE
Governor-gated re-forge whose code prompt is fed the dry-run error as an
AUTHORITATIVE course-correction. Constraints under test:
  * cap = 1 (no loop), gated to code-bugs only (dep/permission route to operator);
  * the Governor (``review_reforge``) is the check + course-correction author;
  * the reconciler self-heals IN-TICK so ``dry_run_status='failed'`` is persisted
    only after the single attempt is spent — the reaper stays correct, untouched.
"""
from __future__ import annotations

import json as _json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from systemu.core.models import Tool, ToolStatus, ToolType


def _tool(**over) -> Tool:
    base = dict(
        id="tool_ph7", name="password_protect_docx", description="protect a docx",
        tool_type=ToolType.PYTHON_FUNCTION, status=ToolStatus.FORGED, enabled=False,
        implementation_path="vault/tools/implementations/password_protect_docx.py",
        implementation_notes="Use msoffcrypto. office_file.encrypt(output_path, output_file).",
        parameters_schema={
            "type": "object",
            "properties": {"input_path": {"type": "string"},
                           "output_path": {"type": "string"},
                           "password": {"type": "string"}},
            "required": ["input_path", "output_path", "password"],
        },
    )
    base.update(over)
    return Tool(**base)


@pytest.fixture
def vault(tmp_path):
    from systemu.vault.vault import Vault
    for sub in ("scrolls", "activities", "shadow_army", "skills", "tools", "evolutions"):
        (tmp_path / sub).mkdir()
        (tmp_path / sub / "index.json").write_text("[]")
    return Vault(str(tmp_path))


_ENCRYPT_ERR = "TypeError: OOXMLFile.encrypt() missing 1 required positional argument: 'outfile'"


# ── Task 7.1 — Tool.forge_reattempts ─────────────────────────────────────────

def test_tool_has_forge_reattempts_default_zero():
    assert _tool().forge_reattempts == 0


def test_forge_reattempts_round_trips(vault):
    t = _tool(forge_reattempts=1)
    vault.save_tool(t)
    assert vault.get_tool("tool_ph7").forge_reattempts == 1


# ── Task 7.2 — Governor.review_reforge (check + course-correction) ────────────

def test_review_reforge_grants_code_bug_under_cap():
    from systemu.runtime.governor import Governor
    d = Governor(None).review_reforge(_tool(forge_reattempts=0), _ENCRYPT_ERR)
    assert d.granted
    assert "AUTHORITATIVE" in d.course_correction
    assert "outfile" in d.course_correction          # the specific error is carried through


def test_review_reforge_denies_when_cap_exhausted():
    from systemu.runtime.governor import Governor
    d = Governor(None).review_reforge(_tool(forge_reattempts=1), _ENCRYPT_ERR)
    assert not d.granted and "exhaust" in d.reason.lower()


def test_review_reforge_denies_dep_pending():
    from systemu.runtime.governor import Governor
    d = Governor(None).review_reforge(_tool(), "ModuleNotFoundError: No module named 'msoffcrypto'")
    assert not d.granted


def test_review_reforge_denies_fs_permission():
    from systemu.runtime.governor import Governor
    d = Governor(None).review_reforge(_tool(), "PermissionError: [Errno 13] Permission denied")
    assert not d.granted


# ── Task 7.3 — reforge_failed_tool_code threads the error into the code prompt ─

_GOOD_CODE = ("def run(input_path, output_path, password):\n"
              "    return {'success': True, 'error': None}\n")


def test_reforge_threads_previous_attempt_error(vault, tmp_path):
    from systemu.pipelines.tool_forge import reforge_failed_tool_code
    config = SimpleNamespace(vault_dir=str(tmp_path))
    captured = {}

    def fake_llm(**kwargs):
        captured["user"] = kwargs["user"]
        return {"implementation": _GOOD_CODE}

    t = _tool()
    with patch("systemu.pipelines.tool_forge.llm_call_json", fake_llm), \
         patch("systemu.interface.notifications.log_event"), \
         patch("systemu.interface.notifications.notify_user"):
        reforge_failed_tool_code(t, config, vault, prior_failure="boom: " + _ENCRYPT_ERR)

    payload = _json.loads(captured["user"])
    assert payload["previous_attempt_error"].startswith("boom:")
    # the impl file was actually (re)written with the new code
    impl = (tmp_path / "tools" / "implementations" / "password_protect_docx.py").read_text()
    assert "def run(" in impl


def test_generate_and_save_code_omits_key_without_prior_failure(vault, tmp_path):
    from systemu.pipelines.tool_forge import _generate_and_save_code
    from systemu.core.models import Scroll
    config = SimpleNamespace(vault_dir=str(tmp_path))
    captured = {}

    def fake_llm(**kwargs):
        captured["user"] = kwargs["user"]
        return {"implementation": _GOOD_CODE}

    scroll = Scroll(id="s", name="x", source_session_id="", raw_instructions_path="", narrative_md="n")
    with patch("systemu.pipelines.tool_forge.llm_call_json", fake_llm), \
         patch("systemu.interface.notifications.log_event"), \
         patch("systemu.interface.notifications.notify_user"):
        _generate_and_save_code(_tool(), scroll, config, vault)

    assert "previous_attempt_error" not in _json.loads(captured["user"])


# ── Task 7.4 — the prompt teaches the authoritative override ──────────────────

def test_forge_code_prompt_has_authoritative_override():
    from systemu.core.utils import load_prompt
    body = load_prompt("forge_tool_code.md").lower()
    assert "previous_attempt_error" in body
    assert "authoritative" in body


# ── Task 7.5 — reconciler self-heals in-tick; reaper stays correct ────────────

def _make_pending(vault, t: Tool):
    t.dry_run_status = "not_run"
    vault.save_tool(t)


def test_reconciler_self_heals_then_deploys(vault, tmp_path):
    from systemu.scheduler import tool_reconciler
    from systemu.pipelines.tool_dry_run import DryRunResult
    config = SimpleNamespace(vault_dir=str(tmp_path))
    _make_pending(vault, _tool())

    fail = DryRunResult(success=False, status="failed", error=_ENCRYPT_ERR)
    ok = DryRunResult(success=True, status="passed")

    with patch("systemu.pipelines.tool_dry_run.dry_run_tool", side_effect=[fail, ok]), \
         patch("systemu.pipelines.tool_forge.reforge_failed_tool_code") as reforge, \
         patch("systemu.interface.notifications.log_event"):
        tool_reconciler.reconcile_once(vault, config)

    reforge.assert_called_once()
    healed = vault.get_tool("tool_ph7")
    assert healed.status == ToolStatus.DEPLOYED
    assert healed.dry_run_status == "passed"
    assert healed.forge_reattempts == 1


def test_reconciler_one_attempt_then_terminal_failed(vault, tmp_path):
    from systemu.scheduler import tool_reconciler
    from systemu.pipelines.tool_dry_run import DryRunResult
    config = SimpleNamespace(vault_dir=str(tmp_path))
    _make_pending(vault, _tool())

    fail = DryRunResult(success=False, status="failed", error=_ENCRYPT_ERR)

    with patch("systemu.pipelines.tool_dry_run.dry_run_tool", side_effect=[fail, fail]), \
         patch("systemu.pipelines.tool_forge.reforge_failed_tool_code"), \
         patch("systemu.interface.notifications.log_event"), \
         patch("systemu.pipelines.tool_service.disable_if_dry_run_failed"):
        tool_reconciler.reconcile_once(vault, config)

    t = vault.get_tool("tool_ph7")
    assert t.dry_run_status == "failed"      # terminal, persisted only after the one attempt
    assert t.forge_reattempts == 1           # exactly one re-forge — no loop


def test_reconciler_dep_failure_is_not_reforged(vault, tmp_path):
    from systemu.scheduler import tool_reconciler
    from systemu.pipelines.tool_dry_run import DryRunResult
    config = SimpleNamespace(vault_dir=str(tmp_path))
    _make_pending(vault, _tool())

    dep = DryRunResult(success=False, status="failed",
                       error="ModuleNotFoundError: No module named 'msoffcrypto'")

    with patch("systemu.pipelines.tool_dry_run.dry_run_tool", side_effect=[dep]) as dr, \
         patch("systemu.pipelines.tool_forge.reforge_failed_tool_code") as reforge, \
         patch("systemu.interface.notifications.log_event"), \
         patch("systemu.pipelines.tool_service.disable_if_dry_run_failed"):
        tool_reconciler.reconcile_once(vault, config)

    reforge.assert_not_called()               # dep failures route to operator, never re-forged
    assert dr.call_count == 1                  # no second dry-run
    assert vault.get_tool("tool_ph7").forge_reattempts == 0
