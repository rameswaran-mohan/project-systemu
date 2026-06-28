"""v0.9.49 F1 — shared finalizer for a PARTIAL activity parked on a tool that can
never become available (operator declined its forge, or it failed validation, or
its record is missing). The ONE idempotent finalize path F2/F3/F4 share.

Rule under test: finalize iff the activity is PARTIAL AND **ANY** required tool is
permanently unavailable (not ALL — the repro has one satisfiable tool + one
rejected). A `proposed/not_run` tool whose forge is merely *pending* (forge_rejected
False) is satisfiable and must NOT trigger finalize.
"""
from __future__ import annotations

import pytest

from systemu.core.models import (
    Activity, ActivityStatus, Tool, ToolStatus, ToolType,
)


@pytest.fixture
def vault(tmp_path):
    from systemu.vault.vault import Vault
    for sub in ("scrolls", "activities", "shadow_army", "skills", "tools", "evolutions"):
        (tmp_path / sub).mkdir()
        (tmp_path / sub / "index.json").write_text("[]")
    return Vault(str(tmp_path))


def _tool(vault, tid, *, status=ToolStatus.PROPOSED, dry_run_status="not_run",
          forge_rejected=False, error=None):
    t = Tool(id=tid, name=tid, description="d", tool_type=ToolType.PYTHON_FUNCTION,
             status=status, enabled=False,
             implementation_path=f"vault/tools/impl/{tid}.py",
             parameters_schema={}, dry_run_status=dry_run_status,
             dry_run_evidence=({"error": error} if error else {}),
             forge_rejected=forge_rejected)
    vault.save_tool(t)
    return t


def _activity(vault, aid, required, *, status=ActivityStatus.PARTIAL):
    a = Activity(id=aid, name="task", scroll_id="s", required_tool_ids=list(required),
                 status=status)
    vault.save_activity(a)
    return a


# ── Tool.forge_rejected field ────────────────────────────────────────────────

def test_tool_has_forge_rejected_default_false():
    t = Tool(id="x", name="x", description="d", tool_type=ToolType.PYTHON_FUNCTION,
             status=ToolStatus.PROPOSED, implementation_path="p", parameters_schema={})
    assert t.forge_rejected is False


# ── _tool_is_permanently_unavailable ─────────────────────────────────────────

def test_predicate_rejected_tool_is_unavailable(vault):
    from systemu.runtime.activity_completion import _tool_is_permanently_unavailable
    _tool(vault, "t_rej", forge_rejected=True)
    assert _tool_is_permanently_unavailable(vault, "t_rej") is True


def test_predicate_failed_dryrun_is_unavailable(vault):
    from systemu.runtime.activity_completion import _tool_is_permanently_unavailable
    _tool(vault, "t_fail", status=ToolStatus.FORGED, dry_run_status="failed", error="boom")
    assert _tool_is_permanently_unavailable(vault, "t_fail") is True


def test_predicate_missing_tool_is_unavailable(vault):
    from systemu.runtime.activity_completion import _tool_is_permanently_unavailable
    assert _tool_is_permanently_unavailable(vault, "t_nope") is True


def test_predicate_pending_proposed_tool_is_satisfiable(vault):
    # forge gate still pending (not rejected) → NOT permanently unavailable
    from systemu.runtime.activity_completion import _tool_is_permanently_unavailable
    _tool(vault, "t_pending", status=ToolStatus.PROPOSED, dry_run_status="not_run")
    assert _tool_is_permanently_unavailable(vault, "t_pending") is False


def test_predicate_deployed_tool_is_satisfiable(vault):
    from systemu.runtime.activity_completion import _tool_is_permanently_unavailable
    _tool(vault, "t_ok", status=ToolStatus.DEPLOYED, dry_run_status="passed")
    assert _tool_is_permanently_unavailable(vault, "t_ok") is False


# ── finalize_unsatisfiable_activity ──────────────────────────────────────────

def test_finalize_partial_activity_with_rejected_tool(vault):
    from systemu.runtime.activity_completion import finalize_unsatisfiable_activity
    _tool(vault, "t_rej", forge_rejected=True)
    _activity(vault, "a1", ["t_rej"])
    assert finalize_unsatisfiable_activity(vault, "a1", context="Declined.")
    assert vault.get_activity("a1").status == ActivityStatus.FAILED


def test_finalize_any_rule_one_deployed_one_rejected(vault):
    # THE REPRO SHAPE: hash deployed + archive rejected → finalize (ANY, not ALL)
    from systemu.runtime.activity_completion import finalize_unsatisfiable_activity
    _tool(vault, "hash", status=ToolStatus.DEPLOYED, dry_run_status="passed")
    _tool(vault, "archive", forge_rejected=True)
    _activity(vault, "a2", ["hash", "archive"])
    assert finalize_unsatisfiable_activity(vault, "a2")
    assert vault.get_activity("a2").status == ActivityStatus.FAILED


def test_finalize_noops_when_all_tools_satisfiable(vault):
    from systemu.runtime.activity_completion import finalize_unsatisfiable_activity
    _tool(vault, "ok1", status=ToolStatus.DEPLOYED, dry_run_status="passed")
    _tool(vault, "pending2", status=ToolStatus.PROPOSED, dry_run_status="not_run")
    _activity(vault, "a3", ["ok1", "pending2"])
    assert not finalize_unsatisfiable_activity(vault, "a3")
    assert vault.get_activity("a3").status == ActivityStatus.PARTIAL


def test_finalize_is_idempotent(vault):
    from systemu.runtime.activity_completion import finalize_unsatisfiable_activity
    _tool(vault, "t_rej", forge_rejected=True)
    _activity(vault, "a4", ["t_rej"])
    assert finalize_unsatisfiable_activity(vault, "a4")
    # second call: activity is no longer PARTIAL → no-op
    assert not finalize_unsatisfiable_activity(vault, "a4")


# ── v0.9.50: a dep-pending dry-run failure is TRANSIENT, not permanent ─────────
# RCA: a tool whose dry-run "failed" only because a dependency awaited operator
# approval was being finalized FAILED seconds before the dep finished installing.

_DEP_MSG = "Tool 'create_pptx' needs operator approval to install: python-pptx"
_BUG_MSG = "TypeError: run() missing 1 required positional argument: 'x'"


def test_classifier_recognizes_dep_pending_approval():
    from systemu.recovery.classifier import classify_dry_run_error
    assert classify_dry_run_error(_DEP_MSG).kind == "DEP_PENDING"
    assert classify_dry_run_error(_BUG_MSG).kind == "DRY_RUN_FAILED_BUG"


def test_dep_pending_failed_tool_is_not_permanently_unavailable(vault):
    from systemu.runtime.activity_completion import _tool_is_permanently_unavailable
    _tool(vault, "t_dep", status=ToolStatus.FORGED, dry_run_status="failed", error=_DEP_MSG)
    assert _tool_is_permanently_unavailable(vault, "t_dep") is False


def test_code_bug_failed_tool_is_permanently_unavailable(vault):
    from systemu.runtime.activity_completion import _tool_is_permanently_unavailable
    _tool(vault, "t_bug", status=ToolStatus.FORGED, dry_run_status="failed", error=_BUG_MSG)
    assert _tool_is_permanently_unavailable(vault, "t_bug") is True


def test_finalize_leaves_dep_pending_task_parked(vault):
    # the exact repro: one tool failing dry-run only because its dep awaits approval
    from systemu.runtime.activity_completion import finalize_unsatisfiable_activity
    _tool(vault, "t_dep", status=ToolStatus.FORGED, dry_run_status="failed", error=_DEP_MSG)
    _activity(vault, "a_dep", ["t_dep"])
    assert not finalize_unsatisfiable_activity(vault, "a_dep")
    assert vault.get_activity("a_dep").status == ActivityStatus.PARTIAL
