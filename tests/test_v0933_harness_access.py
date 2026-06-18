"""Bug 5 (D) — honest ACCESS grant on the local single-owner backend.

D.1: the ACCESS grant observation must NOT tell the agent that a sandbox
boundary authorizes the operation (none is enforced — by design); it records
an ADVISORY lease.

D.2: the dead ``apply`` sandbox-policy patch (governor → jobs → runtime) is
removed — nothing ever consumed it.
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace
from typing import Any, Dict, List

import systemu.runtime.governor as governor_mod
import systemu.scheduler.jobs as jobs_mod
from systemu.runtime.shadow_runtime import ShadowRuntime


# ─────────────────────────────────────────────────────────────────────────────
# D.1 — honest ACCESS observation
# ─────────────────────────────────────────────────────────────────────────────

class _FakeContext:
    def __init__(self) -> None:
        self.observations: List[Dict[str, Any]] = []

    def add_observation(self, result: Dict[str, Any], action_block_num: int) -> None:
        self.observations.append(result)


def _runtime_stub() -> ShadowRuntime:
    rt = ShadowRuntime.__new__(ShadowRuntime)
    rt.vault = SimpleNamespace()
    rt.config = SimpleNamespace()
    return rt


def test_access_grant_observation_is_advisory_not_enforced():
    rt = _runtime_stub()
    ctx = _FakeContext()
    mat = {"materialised": True,
           "access": {"resource": "example.com", "access_type": "network_host"}}

    new_budget = rt._apply_materialised_grant(
        mat, context=ctx, tools=[], tool_index=[], current_ab=1, iter_budget=10,
    )

    # Budget unchanged — ACCESS is observation-only.
    assert new_budget == 10
    assert len(ctx.observations) == 1
    obs = ctx.observations[0]
    assert obs["type"] == "harness_granted"
    msg = obs["message"]

    # Honest wording present: advisory + single-owner + no boundary enforced.
    assert "advisory" in msg.lower()
    assert "single-owner" in msg.lower()
    assert "no sandbox boundary is enforced" in msg.lower()
    # Still names the access spec so the agent knows what was leased.
    assert "example.com" in msg or "network_host" in msg

    # The OLD dishonest phrasing must be gone.
    assert "Access granted (scoped lease)" not in msg
    assert "the operation it authorizes" not in msg


# ─────────────────────────────────────────────────────────────────────────────
# D.2 — dead apply-patch plumbing removed (governor → jobs → runtime)
# ─────────────────────────────────────────────────────────────────────────────

class _FakeRequest:
    def __init__(self, spec):
        self.spec = spec


def test_provision_access_does_not_emit_apply_patch():
    """Governor records an advisory lease but returns NO sandbox-policy patch —
    nothing consumes it (single-owner, by design)."""
    gov = governor_mod.Governor.__new__(governor_mod.Governor)

    # Stub the lease registration the real method calls.
    registered = {}

    def _fake_register(lease_id, request, execution_id):
        registered["lease_id"] = lease_id

    gov._register_lease = _fake_register  # type: ignore[attr-defined]

    request = _FakeRequest({"network_host": "example.com", "access_type": "read"})
    verdict = SimpleNamespace(lease_id="lease-xyz")

    out = gov._provision_access(
        request, verdict, vault=SimpleNamespace(), config=SimpleNamespace(),
        execution_id="exec-1",
    )

    assert out["materialised"] is True
    assert out["lease_id"] == "lease-xyz"
    assert out["access"] == {"network_host": "example.com", "access_type": "read"}
    # The dead patch must be gone — no `apply` key.
    assert "apply" not in out


def test_map_grant_payload_access_carries_no_apply():
    payload = jobs_mod._map_grant_payload(
        "access",
        {"access": {"resource": "x"}, "lease_id": "L", "apply": {"network_host": "x"}},
    )
    assert payload["access"] == {"resource": "x"}
    assert payload["lease_id"] == "L"
    # `apply` is no longer forwarded.
    assert "apply" not in payload


def test_no_apply_dead_plumbing_in_source():
    """Static guard: the dead apply-patch plumbing is removed from all three
    sites. No code builds, forwards, or reconstructs the unconsumed patch."""
    # governor._provision_access: no `"apply"` patch return, no patch loop.
    gov_src = inspect.getsource(governor_mod.Governor._provision_access)
    assert '"apply"' not in gov_src
    assert 'network_host", "fs_read", "fs_write", "env_var"' not in gov_src

    # jobs._map_grant_payload: no `payload["apply"]`.
    jobs_src = inspect.getsource(jobs_mod._map_grant_payload)
    assert 'payload["apply"]' not in jobs_src

    # shadow_runtime._apply_harness_grant: no `mat["apply"]`.
    rt_src = inspect.getsource(ShadowRuntime._apply_harness_grant)
    assert 'mat["apply"]' not in rt_src
