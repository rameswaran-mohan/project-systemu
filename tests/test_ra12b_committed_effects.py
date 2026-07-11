"""IMPL-7 / §5.6 — the committed-effects ledger.

Every PRECISE-HANDOFF card and every terminal BLOCKED/stuck/partial summary MUST
enumerate the external effects ALREADY COMMITTED this run, rendered
DETERMINISTICALLY from persisted ``ExternalEvidence`` (never from model prose).

Part A: a pure render helper (``render_committed_effects`` /
``committed_effect_details``) — confirmed-only, sorted, defensive, no LLM.
Part B: the wiring seam in shadow_runtime.py (a handoff/stuck finalizer's
``final_summary`` carries the ledger when there are confirmed effects).
"""
import pytest


# ── Part A — the pure, deterministic render helper ──────────────────────────
class TestRenderCommittedEffects:
    def test_empty_when_no_confirmed(self):
        from systemu.runtime.committed_effects import render_committed_effects
        assert render_committed_effects({}) == ""
        # an UNCONFIRMED (advisory) effect is NOT "already done" — no credit.
        assert render_committed_effects(
            {"1": {"confirmed": False, "detail": "not done"}}) == ""

    def test_lists_only_confirmed_details_sorted(self):
        from systemu.runtime.committed_effects import (
            render_committed_effects, committed_effect_details)
        ev = {"3": {"confirmed": True, "detail": "imported invoice C"},
              "1": {"confirmed": True, "detail": "created issue #412"},
              "2": {"confirmed": False, "detail": "attempted X"}}
        out = render_committed_effects(ev)
        assert ("created issue #412" in out
                and "imported invoice C" in out
                and "attempted X" not in out)
        # deterministic order — sorted by int(objective_id), not dict order.
        assert out.index("created issue #412") < out.index("imported invoice C")
        assert committed_effect_details(ev) == [
            "created issue #412", "imported invoice C"]

    def test_defensive_on_malformed(self):
        from systemu.runtime.committed_effects import render_committed_effects
        assert render_committed_effects(None) == ""
        # a non-dict entry is ignored; a confirmed entry with NO detail is skipped
        # (never raises).
        assert render_committed_effects(
            {"x": "notadict", "1": {"confirmed": True}}) == ""

    def test_skips_empty_detail(self):
        from systemu.runtime.committed_effects import render_committed_effects
        assert render_committed_effects(
            {"1": {"confirmed": True, "detail": ""}}) == ""

    def test_truthy_nonbool_confirmed_is_not_credited(self):
        """Fail-closed like S4's _read_external_ok — only the real bool True credits;
        a truthy 1 / "yes" is a MALFORMED entry that must NOT be listed."""
        from systemu.runtime.committed_effects import render_committed_effects
        assert render_committed_effects(
            {"1": {"confirmed": 1, "detail": "created issue #412"}}) == ""
        assert render_committed_effects(
            {"1": {"confirmed": "yes", "detail": "created issue #412"}}) == ""

    def test_int_keyed_store_also_works(self):
        """The in-memory store may be int-keyed before a snapshot round-trips it
        to str keys; the render must still sort/emit it."""
        from systemu.runtime.committed_effects import committed_effect_details
        ev = {2: {"confirmed": True, "detail": "second"},
              1: {"confirmed": True, "detail": "first"}}
        assert committed_effect_details(ev) == ["first", "second"]

    def test_render_block_shape(self):
        from systemu.runtime.committed_effects import render_committed_effects
        out = render_committed_effects(
            {"1": {"confirmed": True, "detail": "created issue #412"}})
        assert out.startswith("Already committed this run:")
        assert "created issue #412" in out

    def test_shadow_meter_entry_is_skipped(self):
        """R-A13b-1 FIX A — a RECORD-ONLY shadow-meter measurement (shadow=True,
        even when it carries confirmed=True as its would-credit measurement) is an
        instrumentation artifact and MUST NOT render in the operator committed-
        effects ledger — symmetric with _read_external_ok, which also refuses a
        shadow entry. A LIVE (shadow-absent) confirmed entry STILL renders."""
        from systemu.runtime.committed_effects import (
            render_committed_effects, committed_effect_details)
        ev = {
            # a LIVE confirmed effect (ENFORCE path) — MUST still render.
            "1": {"confirmed": True, "detail": "created issue #412"},
            # a shadow-meter would-credit measurement — MUST be suppressed.
            "2": {"confirmed": True, "shadow": True, "would_credit": True,
                  "detail": "token echo matched (host-pinned https, fresh)"},
        }
        details = committed_effect_details(ev)
        assert details == ["created issue #412"], details
        out = render_committed_effects(ev)
        assert "created issue #412" in out
        assert "token echo matched" not in out

    def test_shadow_only_store_renders_nothing(self):
        """A store containing ONLY shadow-meter entries renders an EMPTY ledger — a
        SHADOW run's summary is identical to a no-meter SHADOW run (no leak)."""
        from systemu.runtime.committed_effects import render_committed_effects
        assert render_committed_effects(
            {"1": {"confirmed": True, "shadow": True, "would_credit": True,
                   "detail": "token echo matched (fresh)"}}) == ""


# ── Part B — the wiring seam ────────────────────────────────────────────────
class TestFinalizerWiring:
    def test_augment_helper_appends_only_when_confirmed(self):
        """The shadow_runtime seam: a no-op when there are zero confirmed effects,
        appends the ledger when there is one, and NEVER raises."""
        from systemu.runtime.shadow_runtime import (
            _augment_summary_with_committed_effects as aug)

        class _Ctx:
            pass

        c = _Ctx()
        # no store at all → unchanged
        assert aug("Parked awaiting operator.", c) == "Parked awaiting operator."
        # store with only an unconfirmed effect → unchanged
        c._external_evidence = {"1": {"confirmed": False, "detail": "tried"}}
        assert aug("Parked awaiting operator.", c) == "Parked awaiting operator."
        # a confirmed effect → appended
        c._external_evidence = {"1": {"confirmed": True, "detail": "created issue #412"}}
        out = aug("Parked awaiting operator.", c)
        assert out.startswith("Parked awaiting operator.")
        assert "created issue #412" in out

    def test_finalize_stuck_summary_carries_ledger(self, monkeypatch):
        """Drive the REAL _finalize_stuck handoff finalizer (mirrors the existing
        TestStuckIntegration fixture) with a seeded confirmed ExternalEvidence and
        assert the committed-effects ledger lands in result['summary']."""
        from systemu.runtime.shadow_runtime import ShadowRuntime
        from systemu.runtime.context_builder import ExecutionContext

        ctx = ExecutionContext.__new__(ExecutionContext)
        ctx.execution_id = "exec_L"
        ctx._snapshots = []
        ctx._history = []
        # a deterministic-matcher-confirmed external effect from earlier this run.
        ctx._external_evidence = {
            "1": {"confirmed": True, "detail": "created issue #412"}}

        rt = ShadowRuntime.__new__(ShadowRuntime)
        rt._stuck_round_for_obj = {}
        rt._iters_since_obj_credit = 0
        rt._same_tool_fail_streak = {}
        rt._tools_since_credit = set()
        rt._operator_hint = None
        monkeypatch.setattr(rt, "_append_to_shadow_log", lambda *a, **k: None,
                            raising=False)

        res = rt._finalize_stuck(
            context=ctx, status="partial",
            reason="no objective credit for 5 iterations",
            stuck_on=2, completed=[1], iteration=5, tool_calls_made=4,
            scroll=None, shadow=None, execution_id="exec_L",
            exec_start=0.0, total_objectives=3)

        assert res["status"] == "partial"
        summary = res.get("summary") or ""
        # the ORIGINAL stuck reason is preserved …
        assert "no objective credit" in summary
        # … AND the deterministic committed-effects ledger is appended.
        assert "created issue #412" in summary
        assert "Already committed this run:" in summary

    def test_clean_finalize_without_evidence_is_unchanged(self, monkeypatch):
        """A stuck finalize on a context with NO external evidence gets NO ledger
        block (nothing appended) — the seam is silent when there's nothing to say."""
        from systemu.runtime.shadow_runtime import ShadowRuntime
        from systemu.runtime.context_builder import ExecutionContext

        ctx = ExecutionContext.__new__(ExecutionContext)
        ctx.execution_id = "exec_M"
        ctx._snapshots = []
        ctx._history = []
        # note: NO _external_evidence attribute at all — must be getattr-safe.

        rt = ShadowRuntime.__new__(ShadowRuntime)
        rt._stuck_round_for_obj = {}
        rt._iters_since_obj_credit = 0
        rt._same_tool_fail_streak = {}
        rt._tools_since_credit = set()
        rt._operator_hint = None
        monkeypatch.setattr(rt, "_append_to_shadow_log", lambda *a, **k: None,
                            raising=False)

        res = rt._finalize_stuck(
            context=ctx, status="partial", reason="no progress",
            stuck_on=2, completed=[1], iteration=5, tool_calls_made=4,
            scroll=None, shadow=None, execution_id="exec_M",
            exec_start=0.0, total_objectives=3)

        assert "Already committed this run:" not in (res.get("summary") or "")
