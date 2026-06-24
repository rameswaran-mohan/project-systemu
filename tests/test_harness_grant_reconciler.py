"""Tasks 5-6: the in-daemon harness-grant reconciler.

``scheduler.jobs.reconcile_resolved_harness_grants`` is the EXECUTOR for an
operator-resolved harness ESCALATE gate. resolve_gate keeps the harness
decision QUEUED (it does NOT materialise) — this daemon-tick reconciler
picks up the resolved gate, materialises the grant exactly ONCE via the
Governor (on Approve / Edit spec), maps the materialise dict into the
per-kind ``grant_payload`` that shadow_runtime's ``_apply_harness_grant``
consumes, and calls ``Supervisor.resume_after_grant``.

Mirrors ``tests/test_v0_8_22_1_cross_process_resume.py`` (real Vault +
real OperatorDecisionQueue, fake Supervisor, idempotency via a persisted
flag) — here the flag is ``decision.context["harness_grant_dispatched"]``,
DISTINCT from the stuck reconciler's ``resume_dispatched`` so the two
reconcilers never interfere.
"""
from __future__ import annotations

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures / helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_vault(tmp_path):
    """Build a filesystem Vault with the dir layout the resume tests use."""
    from systemu.vault.vault import Vault
    for sub in [
        "scrolls", "activities", "shadow_army", "skills",
        "tools/implementations", "evolutions", "notifications",
        "executions", "decisions",
    ]:
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    for idx in [
        "scrolls", "activities", "shadow_army", "skills", "tools",
        "evolutions", "decisions",
    ]:
        (tmp_path / idx / "index.json").write_text("[]", encoding="utf-8")
    return Vault(str(tmp_path))


def _seed_snapshot(tmp_path, *, execution_id, shadow_id, scroll_id, activity_id):
    """Seed an ExecutionSnapshot at the data_dir the reconciler reads."""
    from systemu.runtime.execution_snapshot import ExecutionSnapshot, write_snapshot
    data_dir = tmp_path / "data"
    (data_dir / "audit").mkdir(parents=True, exist_ok=True)
    write_snapshot(
        ExecutionSnapshot(
            execution_id=execution_id, shadow_id=shadow_id,
            scroll_id=scroll_id, activity_id=activity_id,
            completed_objective_ids=[0],
        ),
        data_dir=data_dir,
    )
    return data_dir


def _post_resolve_harness_gate(
    vault, *, choice, harness_kind="tool", spec=None,
    execution_id="exec_x", activity_id="act_x", shadow_id="sh_x",
    request_id="hreq_1", dedup_key=None,
):
    """Post + resolve a kind=='gate'/gate_type=='harness' OperatorDecision.

    Mirrors what harness_review.surface_harness_request stamps via the Inbox
    facade: kind=="gate", gate_type=="harness", plus resume coords + spec.
    """
    from systemu.approval.decision_queue import OperatorDecisionQueue
    queue = OperatorDecisionQueue(vault)
    did = queue.post(
        title=f"Harness request: {harness_kind} [{request_id}]",
        body="?",
        options=["Deny", "Approve", "Edit spec"],
        context={
            "kind": "gate",
            "gate_type": "harness",
            "execution_id": execution_id,
            "activity_id": activity_id,
            "shadow_id": shadow_id,
            "request_id": request_id,
            "harness_kind": harness_kind,
            "spec": spec if spec is not None else {"name": "geocode_place"},
            "risk_band": "medium",
            "chat_submission_id": "ts-h",
        },
        dedup_key=dedup_key or f"harness:{execution_id}:{request_id}",
    )
    queue.resolve(did, choice=choice)
    return did


class _FakeSupervisor:
    """Records resume_after_grant(**kw) calls."""
    def __init__(self):
        self.calls = []

    def resume_after_grant(self, **kw):
        self.calls.append(kw)
        return f"sub_{len(self.calls)}"


class _FakeGovernor:
    """Stand-in for Governor whose materialise() returns a canned per-kind
    outcome dict mirroring the real provisioners — NO real forge runs."""
    last_call = None

    def __init__(self, config=None):
        self.config = config

    def materialise(self, request, verdict, *, vault, config, execution_id):
        _FakeGovernor.last_call = {
            "request": request, "verdict": verdict,
            "execution_id": execution_id,
        }
        kind = getattr(request.kind, "value", str(request.kind))
        if kind == "tool":
            return {"materialised": True, "lease_id": "lease_1",
                    "tool": "geocode_place", "tool_id": "tool_abc"}
        if kind == "compute":
            return {"materialised": True, "lease_id": "lease_c",
                    "compute_grant": {"extra_iterations": 5, "extra_think": 0}}
        if kind == "skill":
            return {"materialised": True, "lease_id": "lease_s",
                    "skill": "/skills/foo/SKILL.md"}
        if kind == "access":
            # Bug 5 / D.2: real Governor no longer returns an apply patch.
            return {"materialised": True, "lease_id": "lease_a",
                    "access": {"resource": "db"}}
        if kind == "subagent":
            return {"materialised": True, "lease_id": "lease_sa",
                    "subagent": {"task": "do X", "depth_cap": 1}}
        return {"materialised": False, "reason": "unmaterialised kind"}


@pytest.fixture(autouse=True)
def _patch_governor(monkeypatch):
    """Patch the Governor symbol the reconciler imports so no real forge runs."""
    import systemu.scheduler.jobs as jobs_mod
    monkeypatch.setattr(jobs_mod, "Governor", _FakeGovernor, raising=False)
    # The reconciler imports Governor lazily — also patch at source so a
    # late `from systemu.runtime.governor import Governor` resolves to fake.
    import systemu.runtime.governor as gov_mod
    monkeypatch.setattr(gov_mod, "Governor", _FakeGovernor, raising=False)
    _FakeGovernor.last_call = None
    yield


@pytest.fixture(autouse=True)
def _config_from_env(monkeypatch):
    """Stub Config.from_env so the reconciler's config acquisition is hermetic."""
    from types import SimpleNamespace
    import sharing_on.config as cfg_mod
    monkeypatch.setattr(
        cfg_mod.Config, "from_env",
        classmethod(lambda cls: SimpleNamespace(skills_user_dir=None)),
        raising=False,
    )
    yield


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestHarnessGrantReconciler:
    def test_approve_tool_materialises_and_resumes(self, tmp_path):
        """Approve a TOOL gate → materialise once → resume_after_grant with a
        grant_payload carrying tool_id/tool/lease_id (the keys
        _apply_harness_grant consumes for the tool branch)."""
        from systemu.scheduler.jobs import reconcile_resolved_harness_grants

        vlt = _make_vault(tmp_path)
        data_dir = _seed_snapshot(
            tmp_path, execution_id="exec_x", shadow_id="sh_x",
            scroll_id="sc_x", activity_id="act_x",
        )
        did = _post_resolve_harness_gate(vlt, choice="Approve", harness_kind="tool")

        sup = _FakeSupervisor()
        n = reconcile_resolved_harness_grants(
            vault=vlt, supervisor=sup, data_dir=data_dir,
        )

        assert n == 1
        assert len(sup.calls) == 1
        kw = sup.calls[0]
        assert kw["execution_id"] == "exec_x"
        assert kw["activity_id"] == "act_x"
        assert kw["shadow_id"] == "sh_x"
        gp = kw["grant_payload"]
        assert gp["kind"] == "tool"
        assert gp["granted"] is True
        assert gp["tool_id"] == "tool_abc"
        assert gp["tool"] == "geocode_place"
        assert gp["lease_id"] == "lease_1"
        # materialise was called exactly once
        assert _FakeGovernor.last_call is not None

        # The persisted flag is stamped — DISTINCT from resume_dispatched.
        after = vlt.get_decision(did)
        assert after.context.get("harness_grant_dispatched") is True
        assert after.context.get("resume_dispatched") is not True

    def test_idempotent_second_pass_is_noop(self, tmp_path):
        """A SECOND reconcile pass must NOT re-materialise or re-resume."""
        from systemu.scheduler.jobs import reconcile_resolved_harness_grants

        vlt = _make_vault(tmp_path)
        data_dir = _seed_snapshot(
            tmp_path, execution_id="exec_x", shadow_id="sh_x",
            scroll_id="sc_x", activity_id="act_x",
        )
        _post_resolve_harness_gate(vlt, choice="Approve", harness_kind="tool")

        sup = _FakeSupervisor()
        n1 = reconcile_resolved_harness_grants(vault=vlt, supervisor=sup, data_dir=data_dir)
        assert n1 == 1
        assert len(sup.calls) == 1

        _FakeGovernor.last_call = None
        n2 = reconcile_resolved_harness_grants(vault=vlt, supervisor=sup, data_dir=data_dir)
        assert n2 == 0
        assert len(sup.calls) == 1          # no second resume
        assert _FakeGovernor.last_call is None   # no second materialise

    def test_deny_resumes_with_denied_payload_and_skips_governor(self, tmp_path):
        """A 'Deny' choice → resume with {denied:True}; Governor NOT called."""
        from systemu.scheduler.jobs import reconcile_resolved_harness_grants

        vlt = _make_vault(tmp_path)
        data_dir = _seed_snapshot(
            tmp_path, execution_id="exec_d", shadow_id="sh_d",
            scroll_id="sc_d", activity_id="act_d",
        )
        _post_resolve_harness_gate(
            vlt, choice="Deny", harness_kind="tool",
            execution_id="exec_d", activity_id="act_d", shadow_id="sh_d",
            request_id="hreq_d",
        )

        sup = _FakeSupervisor()
        n = reconcile_resolved_harness_grants(vault=vlt, supervisor=sup, data_dir=data_dir)

        assert n == 1
        assert len(sup.calls) == 1
        gp = sup.calls[0]["grant_payload"]
        assert gp["denied"] is True
        assert gp["kind"] == "tool"
        assert "rationale" in gp
        # Deny must not materialise.
        assert _FakeGovernor.last_call is None

    def test_non_harness_decision_is_skipped(self, tmp_path):
        """A resolved decision that is NOT a harness gate must be ignored."""
        from systemu.approval.decision_queue import OperatorDecisionQueue
        from systemu.scheduler.jobs import reconcile_resolved_harness_grants

        vlt = _make_vault(tmp_path)
        data_dir = tmp_path / "data"
        (data_dir / "audit").mkdir(parents=True, exist_ok=True)

        q = OperatorDecisionQueue(vlt)
        # A structured_question (the stuck-reconciler's row) — must be skipped here.
        did = q.post(
            title="Stuck", body="?", options=["A", "B"],
            context={
                "kind": "structured_question",
                "chat_submission_id": "ts", "execution_id": "ex",
                "activity_id": "act", "shadow_id": "sh",
            },
            dedup_key="stuck:x",
        )
        q.resolve(did, choice="A")
        # A non-harness gate (recovery) — also skipped.
        did2 = q.post(
            title="Recovery", body="?", options=["Skip", "Apply"],
            context={"kind": "gate", "gate_type": "recovery"},
            dedup_key="recovery:x",
        )
        q.resolve(did2, choice="Apply")

        sup = _FakeSupervisor()
        n = reconcile_resolved_harness_grants(vault=vlt, supervisor=sup, data_dir=data_dir)
        assert n == 0
        assert sup.calls == []

    def test_pending_harness_gate_is_skipped(self, tmp_path):
        """A harness gate still PENDING (unresolved) must not be dispatched."""
        from systemu.approval.decision_queue import OperatorDecisionQueue
        from systemu.scheduler.jobs import reconcile_resolved_harness_grants

        vlt = _make_vault(tmp_path)
        data_dir = _seed_snapshot(
            tmp_path, execution_id="exec_p", shadow_id="sh_p",
            scroll_id="sc_p", activity_id="act_p",
        )
        q = OperatorDecisionQueue(vlt)
        q.post(
            title="Harness request: tool", body="?",
            options=["Deny", "Approve", "Edit spec"],
            context={
                "kind": "gate", "gate_type": "harness",
                "execution_id": "exec_p", "activity_id": "act_p",
                "shadow_id": "sh_p", "request_id": "hreq_p",
                "harness_kind": "tool", "spec": {"name": "x"},
            },
            dedup_key="harness:exec_p:hreq_p",
        )  # NOT resolved

        sup = _FakeSupervisor()
        n = reconcile_resolved_harness_grants(vault=vlt, supervisor=sup, data_dir=data_dir)
        assert n == 0
        assert sup.calls == []

    def test_missing_coords_is_skipped(self, tmp_path):
        """A resolved harness gate lacking execution/activity/shadow ids is skipped."""
        from systemu.approval.decision_queue import OperatorDecisionQueue
        from systemu.scheduler.jobs import reconcile_resolved_harness_grants

        vlt = _make_vault(tmp_path)
        data_dir = tmp_path / "data"
        (data_dir / "audit").mkdir(parents=True, exist_ok=True)

        q = OperatorDecisionQueue(vlt)
        did = q.post(
            title="Harness request: tool", body="?",
            options=["Deny", "Approve", "Edit spec"],
            context={
                "kind": "gate", "gate_type": "harness",
                "harness_kind": "tool", "spec": {"name": "x"},
                # execution_id / activity_id / shadow_id MISSING
            },
            dedup_key="harness:nocoords",
        )
        q.resolve(did, choice="Approve")

        sup = _FakeSupervisor()
        n = reconcile_resolved_harness_grants(vault=vlt, supervisor=sup, data_dir=data_dir)
        assert n == 0
        assert sup.calls == []

    def test_compute_kind_maps_compute_grant(self, tmp_path):
        """Approve a COMPUTE gate → grant_payload carries compute_grant."""
        from systemu.scheduler.jobs import reconcile_resolved_harness_grants

        vlt = _make_vault(tmp_path)
        data_dir = _seed_snapshot(
            tmp_path, execution_id="exec_c", shadow_id="sh_c",
            scroll_id="sc_c", activity_id="act_c",
        )
        _post_resolve_harness_gate(
            vlt, choice="Approve", harness_kind="compute",
            spec={"extra_iterations": 5},
            execution_id="exec_c", activity_id="act_c", shadow_id="sh_c",
            request_id="hreq_c",
        )
        sup = _FakeSupervisor()
        n = reconcile_resolved_harness_grants(vault=vlt, supervisor=sup, data_dir=data_dir)
        assert n == 1
        gp = sup.calls[0]["grant_payload"]
        assert gp["kind"] == "compute"
        assert gp["compute_grant"] == {"extra_iterations": 5, "extra_think": 0}
        assert gp["lease_id"] == "lease_c"

    def test_skill_access_subagent_kinds_map(self, tmp_path):
        """SKILL/ACCESS/SUBAGENT grants carry the keys _apply_harness_grant reads."""
        from systemu.scheduler.jobs import reconcile_resolved_harness_grants

        for kind, dataid, asserts in [
            ("skill", "k", lambda gp: gp["skill"] == "/skills/foo/SKILL.md"),
            ("access", "a", lambda gp: gp["access"] == {"resource": "db"}
                                       and "apply" not in gp),  # D.2: no apply patch
            ("subagent", "g", lambda gp: gp["subagent"]["task"] == "do X"),
        ]:
            vlt = _make_vault(tmp_path / kind)
            data_dir = _seed_snapshot(
                tmp_path / kind, execution_id=f"ex_{dataid}", shadow_id=f"sh_{dataid}",
                scroll_id=f"sc_{dataid}", activity_id=f"ac_{dataid}",
            )
            _post_resolve_harness_gate(
                vlt, choice="Approve", harness_kind=kind,
                execution_id=f"ex_{dataid}", activity_id=f"ac_{dataid}",
                shadow_id=f"sh_{dataid}", request_id=f"hreq_{dataid}",
            )
            sup = _FakeSupervisor()
            n = reconcile_resolved_harness_grants(vault=vlt, supervisor=sup, data_dir=data_dir)
            assert n == 1, kind
            gp = sup.calls[0]["grant_payload"]
            assert gp["kind"] == kind
            assert asserts(gp), (kind, gp)

    def test_governor_failure_skips_row_without_crashing(self, tmp_path, monkeypatch):
        """A Governor/materialise exception logs + skips that row — no crash,
        no resume, and the flag is NOT stamped (so a later tick can retry)."""
        from systemu.scheduler.jobs import reconcile_resolved_harness_grants
        import systemu.scheduler.jobs as jobs_mod

        class _BoomGovernor(_FakeGovernor):
            def materialise(self, *a, **kw):
                raise RuntimeError("forge exploded")

        monkeypatch.setattr(jobs_mod, "Governor", _BoomGovernor, raising=False)
        import systemu.runtime.governor as gov_mod
        monkeypatch.setattr(gov_mod, "Governor", _BoomGovernor, raising=False)

        vlt = _make_vault(tmp_path)
        data_dir = _seed_snapshot(
            tmp_path, execution_id="exec_b", shadow_id="sh_b",
            scroll_id="sc_b", activity_id="act_b",
        )
        did = _post_resolve_harness_gate(
            vlt, choice="Approve", harness_kind="tool",
            execution_id="exec_b", activity_id="act_b", shadow_id="sh_b",
            request_id="hreq_b",
        )
        sup = _FakeSupervisor()
        # Must not raise.
        n = reconcile_resolved_harness_grants(vault=vlt, supervisor=sup, data_dir=data_dir)
        assert n == 0
        assert sup.calls == []
        after = vlt.get_decision(did)
        assert after.context.get("harness_grant_dispatched") is not True


class TestWrapperAndRegistration:
    def test_wrapper_importable_and_callable(self):
        """The APScheduler wrapper exists and is callable (no-op without vault)."""
        from systemu.scheduler.jobs import _harness_grant_reconciler_job
        # _vault is None in a fresh import → wrapper returns without crashing.
        _harness_grant_reconciler_job()

    def test_daemon_module_imports(self):
        """daemon.py imports cleanly with the new registration in place."""
        import importlib
        import systemu.scheduler.daemon as daemon_mod
        importlib.reload(daemon_mod)
        # The wrapper symbol the daemon registers is importable from jobs.
        from systemu.scheduler.jobs import _harness_grant_reconciler_job
        assert callable(_harness_grant_reconciler_job)

    def test_daemon_registers_harness_grant_reconciler(self):
        """The daemon's scheduler setup registers a job with the expected id +
        the harness-grant wrapper (verified at the source level, since the real
        registration lives inside the live _run_daemon_loop)."""
        import inspect
        import systemu.scheduler.daemon as daemon_mod
        src = inspect.getsource(daemon_mod)
        assert "_harness_grant_reconciler_job" in src
        assert 'id="harness_grant_reconciler"' in src
        assert 'name="Harness Grant Reconciler"' in src


class TestFreeTextInputAnswer:
    """v0.9.45: a free-text ASK_OPERATOR (synthesized one-field schema) must inject
    the operator's CLEAN typed value — not the raw form JSON or a button label —
    so the agent gets a usable answer and the workflow-lane re-ask loop ends."""

    def _post_resolve_free_text(self, vault, *, choice):
        from systemu.approval.decision_queue import OperatorDecisionQueue
        from systemu.runtime.elicitation import free_text_input_schema
        queue = OperatorDecisionQueue(vault)
        did = queue.post(
            title="Harness request: input [hreq_i]", body="?",
            options=["Deny", "Approve", "Edit spec"],
            context={
                "kind": "gate", "gate_type": "harness",
                "execution_id": "exec_i", "activity_id": "act_i",
                "shadow_id": "sh_i", "request_id": "hreq_i",
                "harness_kind": "input",
                "requested_schema": free_text_input_schema("What number?"),
                "pending_tool": {}, "param_substitution": False,
                "spec": {"question": "What number?"}, "risk_band": "medium",
            },
            dedup_key="harness:exec_i:hreq_i",
        )
        queue.resolve(did, choice=choice)
        return did

    def test_clean_value_injected_not_json_or_label(self, tmp_path):
        import json
        from systemu.scheduler.jobs import reconcile_resolved_harness_grants
        vlt = _make_vault(tmp_path)
        data_dir = _seed_snapshot(
            tmp_path, execution_id="exec_i", shadow_id="sh_i",
            scroll_id="sc_i", activity_id="act_i")
        self._post_resolve_free_text(vlt, choice=json.dumps({"answer": "42"}))
        sup = _FakeSupervisor()
        n = reconcile_resolved_harness_grants(
            vault=vlt, supervisor=sup, data_dir=data_dir)
        assert n == 1 and len(sup.calls) == 1
        gp = sup.calls[0]["grant_payload"]
        assert gp["kind"] == "input"
        assert gp["operator_answer"] == "42"          # the CLEAN value
        assert "{" not in gp["operator_answer"]        # not raw form JSON
        assert gp["operator_answer"] not in ("Approve", "Edit spec", "Deny")

    def test_deny_takes_denial_path_not_label_answer(self, tmp_path):
        from systemu.scheduler.jobs import reconcile_resolved_harness_grants
        vlt = _make_vault(tmp_path)
        data_dir = _seed_snapshot(
            tmp_path, execution_id="exec_i", shadow_id="sh_i",
            scroll_id="sc_i", activity_id="act_i")
        self._post_resolve_free_text(vlt, choice="Deny")
        sup = _FakeSupervisor()
        reconcile_resolved_harness_grants(
            vault=vlt, supervisor=sup, data_dir=data_dir)
        gp = sup.calls[0]["grant_payload"]
        assert gp.get("denied") is True
        assert gp.get("operator_answer") != "Deny"
