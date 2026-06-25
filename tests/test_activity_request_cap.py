# tests/test_activity_request_cap.py
from systemu.runtime.harness_policy import HarnessPolicy


def test_policy_has_activity_cap_default():
    p = HarnessPolicy.from_config(None)
    assert getattr(p, "max_requests_per_activity", None) is not None
    assert p.max_requests_per_activity >= p.max_requests_per_run  # never tighter than per-run


def test_arbiter_denies_when_activity_cap_exceeded():
    from systemu.runtime.harness_arbiter import arbitrate
    from systemu.core.models import HarnessRequest, HarnessKind, HarnessDecision
    p = HarnessPolicy.from_config(None)
    req = HarnessRequest(kind=HarnessKind.TOOL, spec={"name": "x"})
    ctx = {"requests_this_run": 0,
           "requests_this_activity": p.max_requests_per_activity}  # at the activity ceiling
    v = arbitrate(req, p, ctx)["verdict"]
    assert v.decision == HarnessDecision.DENY
    assert getattr(v, "cap_exceeded", False) is True
