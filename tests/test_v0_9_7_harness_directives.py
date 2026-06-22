"""v0.9.7 Phase 3 (loop plumbing) — the REQUEST_HARNESS GRANT branch must apply
ALL Governor materialise KINDs (not just TOOL), and execution-adherence must be
resolved into the loop and gate the lenient goal-level acceptance shortcut.

These are getsource wiring guards (the resolver + Governor are behaviourally
tested elsewhere) plus a couple of behavioural checks on resolve_adherence.
All loop behaviour stays behind SYSTEMU_INTENT_ENGINE (default off)."""
import inspect


# ── GRANT branch applies every materialise KIND ────────────────────────────────

def test_grant_branch_handles_all_harness_kinds():
    from systemu.runtime.shadow_runtime import ShadowRuntime
    # The per-kind apply-branches live in the shared _apply_materialised_grant
    # helper (extracted from the autonomous GRANT block so the deferred harness
    # grant-resume replays the exact same code). The execute() GRANT branch calls
    # it after _gov.materialise(...).
    exec_src = inspect.getsource(ShadowRuntime.execute)
    assert ".materialise(" in exec_src
    assert "self._apply_materialised_grant(" in exec_src

    src = inspect.getsource(ShadowRuntime._apply_materialised_grant)
    # TOOL path still deploys + offers back
    assert 'if mat.get("tool") is not None:' in src
    assert "deploy_forged_tool" in src
    # the other four kinds each have an explicit branch
    assert 'elif mat.get("compute_grant"):' in src
    assert 'elif mat.get("skill"):' in src
    assert 'elif mat.get("access"):' in src
    assert 'elif mat.get("subagent"):' in src
    # every branch surfaces a grant observation back to the executor
    assert src.count('"type": "harness_granted"') >= 5


def test_compute_grant_extends_iteration_budget():
    """COMPUTE must bump the run's *mutable* iteration budget — not the module
    constant — and the loop must iterate against that mutable budget."""
    from systemu.runtime.shadow_runtime import ShadowRuntime
    exec_src = inspect.getsource(ShadowRuntime.execute)
    # the mutable budget is owned by execute() and threaded through the helper
    assert "_iter_budget = MAX_ITERATIONS" in exec_src
    assert "while iteration < _iter_budget:" in exec_src
    assert "_iter_budget = self._apply_materialised_grant(" in exec_src
    # the COMPUTE bump itself lives in the shared helper, returned to execute()
    apply_src = inspect.getsource(ShadowRuntime._apply_materialised_grant)
    assert "iter_budget += _extra_it" in apply_src
    # bump is bounded (no unbounded budget grant)
    assert "min(int(_cg.get(\"extra_iterations\", 0) or 0), 100)" in apply_src


# ── adherence resolved into the loop + gates the goal-level shortcut ────────────

def test_execute_resolves_adherence():
    from systemu.runtime.shadow_runtime import ShadowRuntime
    src = inspect.getsource(ShadowRuntime.execute)
    assert "from systemu.runtime.adherence import resolve_adherence" in src
    assert "_adherence =" in src
    # request-kind is derived (records honor per-SOP adherence; chat → free)
    assert 'getattr(scroll, "adherence"' in src


def test_strict_adherence_suppresses_goal_level_shortcut():
    """Under strict adherence the per-objective / SOP contract is honored: the
    lenient goal-level acceptance must NOT fire at either seam."""
    from systemu.runtime.shadow_runtime import ShadowRuntime
    src = inspect.getsource(ShadowRuntime.execute)
    # both the COMPLETE gate and the stuck-park bypass guard on non-strict
    assert src.count('_adherence != "strict"') >= 2


# ── behavioural: resolver honors the operator pin and the auto defaults ─────────

def test_resolve_adherence_pin_and_defaults():
    from systemu.runtime.adherence import resolve_adherence

    class _Cfg:
        execution_adherence = "auto"

    # auto + chat → free; auto + record (no sop) → guided
    assert resolve_adherence(_Cfg(), request_kind="chat") == "free"
    assert resolve_adherence(_Cfg(), request_kind="record") == "guided"
    # auto + record + per-SOP strict → strict
    assert resolve_adherence(_Cfg(), request_kind="record", sop_adherence="strict") == "strict"

    class _Pin:
        execution_adherence = "strict"

    # explicit operator pin wins over everything (even a chat request)
    assert resolve_adherence(_Pin(), request_kind="chat", sop_adherence="free") == "strict"
