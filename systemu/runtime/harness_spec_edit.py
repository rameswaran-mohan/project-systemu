"""Pure helpers for the operator amend-then-approve flow on harness capability
gates. No NiceGUI / no I/O — all logic here is unit-testable.

The per-kind whitelists mirror each kind's provisioner read-keys in
``systemu/runtime/governor.py`` (the source of truth). Keep them in sync if a
provisioner starts reading a new spec key.
"""
from __future__ import annotations

from typing import Any, Dict, List

from systemu.core.models import RiskBand

# kind → (required spec keys, full editable allow-list)
_EDIT_FIELDS: Dict[str, Dict[str, set]] = {
    "tool": {
        "required": {"name"},
        "allowed": {"name", "description", "tool_type", "parameters_schema",
                    "return_schema", "implementation_notes", "dependencies"},
    },
    "skill": {
        "required": {"name"},
        "allowed": {"name", "description", "procedure", "pitfalls", "confidence"},
    },
    "access": {
        "required": set(),  # at least one resource key (checked below)
        "allowed": {"access_type", "network_host", "fs_read", "fs_write",
                    "env_var", "resource"},
    },
    "compute": {
        "required": set(),
        "allowed": {"budget_fraction", "extra_iterations", "extra_think", "tokens"},
    },
    "subagent": {
        "required": {"task"},
        "allowed": {"task", "depth", "budget_fraction", "tasks"},
    },
    "mcp": {
        # NOTE: pin the exact MCP keys against the MCP provisioner
        # (governor.py ~827-1005) before relying on this in production.
        "required": set(),
        "allowed": {"server", "url", "command", "args", "scopes", "host",
                    "transport", "env"},
    },
}

_BAND_ORDER = {RiskBand.LOW: 0, RiskBand.MEDIUM: 1, RiskBand.HIGH: 2}


def band_rank(band: Any) -> int:
    """Map a RiskBand (or its string value) to 0/1/2 for comparison."""
    if isinstance(band, RiskBand):
        return _BAND_ORDER[band]
    return _BAND_ORDER.get(RiskBand(str(band)), 0)


def spec_edit_view(kind: str, spec: Dict[str, Any]) -> Dict[str, Any]:
    """Return {editable, allowed_keys, required_keys} for an operator spec edit.

    Only spec-dict keys are editable; request_id/kind/rationale/fallback are
    immutable and never surfaced. Unknown kinds yield an empty allow-list (the
    caller treats that as "not editable").
    """
    fields = _EDIT_FIELDS.get((kind or "").lower(), {"required": set(), "allowed": set()})
    return {
        "editable": dict(spec or {}),
        "allowed_keys": set(fields["allowed"]),
        "required_keys": set(fields["required"]),
    }


def validate_amended_spec(kind: str, edited: Dict[str, Any],
                          *, original_spec: Dict[str, Any]) -> List[str]:
    """Return a list of human-readable validation errors (empty == OK).

    Rules: every edited key must be in the kind's allow-list; every required key
    must be present; ACCESS must keep at least one resource key.
    """
    view = spec_edit_view(kind, original_spec)
    allowed, required = view["allowed_keys"], view["required_keys"]
    errs: List[str] = []
    if not allowed:
        return [f"{kind!r} specs are not editable"]
    unknown = [k for k in (edited or {}) if k not in allowed]
    if unknown:
        errs.append(f"Not editable for {kind}: {', '.join(sorted(unknown))}")
    missing = [k for k in required if k not in (edited or {})]
    if missing:
        errs.append(f"Missing required: {', '.join(sorted(missing))}")
    if (kind or "").lower() == "access":
        _resource_keys = {"access_type", "network_host", "fs_read", "fs_write",
                          "env_var", "resource"}
        if not (set(edited or {}) & _resource_keys):
            errs.append("ACCESS must keep at least one resource key")
    return errs


def evaluate_amendment(*, kind: str, original_spec: Dict[str, Any],
                       edited_spec: Dict[str, Any],
                       arb_context: Dict[str, Any] | None,
                       config: Any) -> Dict[str, Any]:
    """Deterministically evaluate an operator spec edit.

    Arbitrates BOTH the original and edited specs with the per-run cap neutralized
    (requests_this_run=0) so the cap can't mask the per-kind band. Returns
    ``{blocked, reason, band_increase, from_band, to_band}``. ``blocked`` is True
    only on a hard-safety DENY of the edited spec (e.g. MCP SSRF) — never on a mere
    ESCALATE. Uses the PURE arbiter (no LLM), so it is safe in a sync UI handler.
    """
    from systemu.core.models import HarnessRequest, HarnessKind, HarnessDecision
    from systemu.runtime.harness_arbiter import arbitrate
    from systemu.runtime.harness_policy import HarnessPolicy

    policy = HarnessPolicy.from_config(config)
    ctx0 = {**(dict(arb_context) if arb_context else {}), "requests_this_run": 0}
    hk = HarnessKind((kind or "").lower())

    def _band(spec):
        req = HarnessRequest(kind=hk, spec=dict(spec or {}))
        return arbitrate(req, policy, ctx0)["verdict"]

    v_orig = _band(original_spec)
    v_edit = _band(edited_spec)
    if v_edit.decision == HarnessDecision.DENY:
        return {"blocked": True, "reason": v_edit.rationale or "edit denied by policy",
                "band_increase": False,
                "from_band": v_orig.risk_band.value, "to_band": v_edit.risk_band.value}
    return {
        "blocked": False, "reason": "",
        "band_increase": band_rank(v_edit.risk_band) > band_rank(v_orig.risk_band),
        "from_band": v_orig.risk_band.value, "to_band": v_edit.risk_band.value,
    }
