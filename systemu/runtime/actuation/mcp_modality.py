"""R-A14a slice 2 — the ``mcp`` ActuationModality (MASTER-SPEC §8.2 / §8.3 tier 2).

Wraps the EXISTING gated MCP chokepoint (``runtime.mcp.dispatch.call_mcp_tool``) as
an :class:`ActuationModality`, so an MCP mutation is:

  * **executed THROUGH the gate** — ``execute()`` delegates to ``call_mcp_tool``,
    which embeds the L1-L4 gate incl. the L3 ``_gate_mcp_call`` (the MCP-class S1
    instance). It adds NO second gate — a second gate would DIVERGE from the live
    path. ``_gate_mcp_call`` vs ``action_governance.evaluate_action``: the universal
    evaluator is NOT yet wired into the MCP path (that wiring is S1b — see
    ``action_governance.py:22-24``); the LIVE MCP-class S1 gate today is
    ``_gate_mcp_call``. When S1b makes it delegate to ``evaluate_action``, ``execute``
    inherits it automatically (it always routes through ``call_mcp_tool``). ONE truth.

  * **made VERIFIABLE by the S3/S4 net** — ``capture_evidence()`` derives a
    per-actuation verification obligation from the effect result and drives it through
    the EXISTING money-move-safe engine
    (``shadow_runtime._run_external_verification`` → ``ExternalVerifier.verify()`` +
    the hardened ``api_readback``). The money-move fail-closed invariant is PRESERVED
    BY REUSE: the modality NEVER adds an MCP-specific confirm path, so an MCP
    money-move is held to the SAME bar (only a hardened independent host-pinned+fresh
    api_readback credits it). The modality only changes the TRIGGER (a per-actuation
    obligation for a declared MCP mutation) — DECOUPLED from the flag-gated
    ``SYSTEMU_S4_STAMP`` binder classifier, so it works with the net OFF/SHADOW.

MCP tools carry NO effect_tags (``registry_bridge`` registers a bare namespaced
entry), so the money-move classification runs over the objective goal/params
(``money_move_net_applies`` — a financial signal on an UNKNOWN effect ⇒ money-move).
"""
from __future__ import annotations

import logging
from typing import Any, List, Optional
from urllib.parse import urlparse

from systemu.core.models import ExternalEvidence
from systemu.runtime.actuation.modality import Action, ActionResult

logger = logging.getLogger(__name__)


class McpActuationModality:
    """Tier-2 MCP actuation over the in-daemon gated client (SPEC §8.3)."""

    name = "mcp"
    reliability_tier = 2

    def __init__(self, runtime: Any = None, *, vault: Any = None, config: Any = None) -> None:
        # ``runtime`` supplies the injected independent readback client
        # (``runtime._external_api_client``) + is the object the reused
        # verification engine reads. vault/config are used by ``execute`` to reach
        # the gated chokepoint; they default off the runtime when present.
        self._runtime = runtime
        self._vault = vault if vault is not None else getattr(runtime, "vault", None)
        self._config = config if config is not None else getattr(runtime, "config", None)

    # ── §8.2 shape ───────────────────────────────────────────────────────────
    def probe(self, target: Any = None) -> bool:
        """Best-effort availability: is any MCP tool operator-enabled (or an env
        server declared)? Reuses the dispatch L1 check. Never raises."""
        try:
            from systemu.runtime.mcp.dispatch import _mcp_any_enabled
            return bool(_mcp_any_enabled())
        except Exception:
            return False

    def discover_affordances(self, target: Any = None) -> List[str]:
        """The enabled MCP affordances = the namespaced ``mcp__…`` entries in the v2
        registry. Never raises."""
        try:
            from systemu.runtime.tool_registry_v2 import registry
            return [e.name for e in registry.list_by_toolset("mcp")]
        except Exception:
            return []

    def propose_action(self, objective: Any, *args: Any, **kwargs: Any) -> Action:
        """Build an INSPECTABLE Action for a (server, tool, params). Resolves
        ``is_mutation`` from the registered tool's ``is_action_tool`` (read-only ⇒ not
        a mutation). Accepts ``server`` / ``name`` / ``params`` kwargs."""
        server = kwargs.get("server") or (args[0] if len(args) > 0 else "")
        name = kwargs.get("name") or (args[1] if len(args) > 1 else "")
        params = kwargs.get("params") or (args[2] if len(args) > 2 else {})
        entry = None
        try:
            from systemu.runtime.tool_registry_v2 import registry
            entry = registry.get(str(name))
        except Exception:
            entry = None
        return Action(
            modality=self.name, target=str(server or ""), name=str(name or ""),
            params=dict(params or {}), objective=objective, tool=entry,
            is_mutation=bool(getattr(entry, "is_action_tool", False)))

    def execute(self, action: Action, *, gate: Any = None) -> ActionResult:
        """Run the MCP call THROUGH the gated chokepoint. NEVER bypasses the gate.

        Delegates to ``call_mcp_tool`` (which embeds L1 availability, L2 allowlist,
        L3 ``_gate_mcp_call`` [the MCP-class S1 instance], the rug-pull re-hash, SDK
        execute, and L4 output guard). An L3 gate DENY raises ``PendingOperatorDecision``
        — we let it PROPAGATE (the loop's resume handlers park+resume it); swallowing
        it would BYPASS the gate. An L2 allowlist refusal returns a failure envelope
        (no raise) → ``ActionResult(success=False)``.

        The ``gate`` param is accepted for §8.2 conformance; the authoritative gate for
        this modality is ``call_mcp_tool``'s embedded gate (ONE truth) — ``execute`` does
        not add a second, divergent gate."""
        from systemu.runtime.mcp.dispatch import call_mcp_tool
        out = call_mcp_tool(
            action.target, action.name, dict(action.params or {}),
            vault=self._vault, config=self._config, session_id=self._session_id())
        if not (isinstance(out, dict) and out.get("success")):
            return ActionResult(
                success=False,
                error=str((out or {}).get("error") or "MCP call failed"),
                raw=out)
        return ActionResult(success=True, response=(out or {}).get("response"), raw=out)

    def probe_presubmit(self, action: Action) -> dict:
        """Run the PRE-SUBMIT freshness probe for a money-move MCP mutation whose
        readback target is knowable BEFORE the mutation (a client-provided
        idempotency token + a curated readback template). MUST be called BEFORE the
        mutation. Returns a freshness snapshot: ``probe_ran`` + ``presubmit_tokens``
        + ``pre_submit_absent`` + ``probed_url``/``probed_tokens``. Returns a
        ``probe_ran=False`` snapshot when no probe could run (no curated template, no
        client, or any error) ⇒ freshness UNPROVABLE ⇒ the reused money-move gate
        keeps the effect fail-closed. Never raises."""
        unproven = {"presubmit_tokens": [], "pre_submit_absent": False,
                    "probe_ran": False, "probed_url": "", "probed_tokens": []}
        try:
            from systemu.runtime.actuation.mcp_readback import presubmit_directive_from_params
            directive = presubmit_directive_from_params(
                getattr(action, "name", None), getattr(action, "params", None) or {})
            if directive is None:
                return dict(unproven)
            from systemu.runtime.shadow_runtime import _probe_presubmit_absence
            probe = _probe_presubmit_absence(self._runtime, directive)
            if not probe:
                return dict(unproven)
            return {"probe_ran": True, **probe}
        except Exception:
            logger.debug("[McpModality] probe_presubmit failed — unprovable (fail-closed)",
                         exc_info=True)
            return dict(unproven)

    def capture_evidence(
        self, action: Action, result: ActionResult, *, presubmit: Optional[dict] = None
    ) -> Optional[ExternalEvidence]:
        """Turn a KNOWN-mutation MCP result into an ``ExternalEvidence`` by REUSING the
        money-move-safe verification engine (never a new confirm path).

        Returns ``None`` for a READ / non-mutation, and for a NON-money mutation that
        exposes NO evidence channel (⇒ the credit stays on today's path — byte-identical).
        A money-move ALWAYS produces evidence (fail-closed): even with no hardened
        readback the reused ``verify()`` demotes it to ``confirmed=False``, so the caller
        gates the credit. Never raises."""
        if not bool(getattr(action, "is_mutation", False)):
            return None
        try:
            directive = self._directive_from_result(result)
            is_money = self._is_money_move(action)
            if directive is None and not is_money:
                return None  # non-money mutation, no evidence channel → today's behavior
            return self._verify(action, directive or {}, presubmit=presubmit)
        except Exception:
            logger.debug("[McpModality] capture_evidence failed — fail-closed", exc_info=True)
            # a money-move must NEVER silently credit: hand back an UNCONFIRMED evidence
            # so the caller's obligation gates (never credits) the effect.
            if self._is_money_move(action):
                return ExternalEvidence(
                    objective_id=self._objective_id(action.objective),
                    confirmed=False, method="mcp_capture_error",
                    detail="capture error — fail-closed")
            return None

    # ── internals ────────────────────────────────────────────────────────────
    def _verify(self, action: Action, directive: dict, *,
                presubmit: Optional[dict] = None) -> ExternalEvidence:
        """Drive the EXISTING credit-seam engine (host-pin + https + freshness +
        money-move gate + branch-2 skip), returning its ExternalEvidence. The directive
        is threaded exactly as a tool's ``parsed['external']`` envelope so
        ``_run_external_verification`` treats an MCP mutation identically to any other
        externally-verified effect.

        ``presubmit`` (from ``probe_presubmit``, run BEFORE the mutation) supplies the
        freshness proof: with ``probe_ran=True`` + the probed url/tokens bound to the
        credited resource, the reused engine credits a money-move; absent a probe
        (the default), a money-move stays fail-closed (freshness unprovable). The
        reused engine — not this method — enforces the money-move gate."""
        import types as _types
        from systemu.runtime.shadow_runtime import _run_external_verification
        shim = _types.SimpleNamespace(parsed={"external": dict(directive)})
        decision = {"parameters": dict(action.params or {})}
        presub = presubmit if presubmit is not None else {
            "presubmit_tokens": [], "pre_submit_absent": False,
            "probe_ran": False, "probed_url": "", "probed_tokens": []}
        return _run_external_verification(
            self._runtime, objective=action.objective, decision=decision,
            tool=action.tool, result=shim, presubmit=presub)

    def _is_money_move(self, action: Action) -> bool:
        try:
            from systemu.runtime.shadow_runtime import _is_money_move_seam
            return bool(_is_money_move_seam(
                action.objective, {"parameters": dict(action.params or {})}, action.tool))
        except Exception:
            # fail-closed: an unclassifiable MCP effect is treated as money-move.
            return True

    def _directive_from_result(self, result: ActionResult) -> Optional[dict]:
        """Recover the external-verification DIRECTIVE from the MCP effect result.

        Priority: an explicit ``external`` sub-dict on the (unwrapped) MCP payload
        (mirrors the existing ``parsed['external']`` contract) → else SYNTHESIZE from a
        created-resource shape (a public https resource URL + its id/number). ``None``
        when the result exposes no usable channel."""
        payload = self._unwrap_payload(result)
        if not isinstance(payload, dict):
            return None
        ext = payload.get("external")
        if isinstance(ext, dict) and ext:
            return dict(ext)
        return self._synthesize_directive(payload)

    @staticmethod
    def _unwrap_payload(result: Any) -> Optional[dict]:
        """Recover the STRUCTURED MCP payload from ``execute``'s guarded envelope.

        L4 (``_guard_mcp_output``) wraps a non-empty payload as
        ``{"untrusted": True, "mcp_untrusted_output": <banner>\\n<json>}``. Strip the
        banner line and re-parse the JSON. A raw structured dict (empty/falsy payload
        that the guard passes through, or a test-supplied dict) is returned as-is.
        Never raises."""
        try:
            resp = getattr(result, "response", None)
            if resp is None and isinstance(result, dict):
                resp = result.get("response")
            if not isinstance(resp, dict):
                return resp if isinstance(resp, dict) else None
            if "mcp_untrusted_output" in resp:
                text = resp.get("mcp_untrusted_output") or ""
                body = text.split("\n", 1)[1] if "\n" in text else text
                try:
                    import json as _json
                    parsed = _json.loads(body)
                    return parsed if isinstance(parsed, dict) else {"response_body": body}
                except Exception:
                    return {"response_body": body}
            return resp
        except Exception:
            return None

    @staticmethod
    def _synthesize_directive(payload: dict) -> Optional[dict]:
        """Best-effort directive from a created-resource shape (e.g. a GitHub issue
        result: ``{"html_url": "...", "number": 42, "id": 123}``). Requires a PUBLIC
        https resource URL + at least one identifying token. ``pre_submit_absent=True``
        marks the create-once nature (trusted for a NON-money effect only; the reused
        money-move branch zeroes it). ``None`` when no channel can be formed."""
        try:
            url = None
            for k in ("readback_url", "html_url", "url", "self", "location"):
                v = payload.get(k)
                if isinstance(v, str) and v.lower().startswith("https://"):
                    url = v
                    break
            if not url:
                return None
            tokens: List[str] = []
            for k in ("expected_tokens", "id", "number", "sha", "name", "key"):
                v = payload.get(k)
                if isinstance(v, (list, tuple)):
                    tokens.extend(str(x) for x in v)
                elif isinstance(v, (str, int)) and str(v):
                    tokens.append(str(v))
            host = (urlparse(url).hostname or "").lower().strip()
            if not tokens or not host:
                return None
            return {"strategy": "api_readback", "readback_url": url,
                    "expected_tokens": tokens, "submit_host": host,
                    "pre_submit_absent": True}
        except Exception:
            return None

    @staticmethod
    def _objective_id(objective: Any) -> int:
        for attr in ("objective_id", "id"):
            v = getattr(objective, attr, None)
            if isinstance(v, int):
                return v
            if isinstance(v, str) and v.isdigit():
                return int(v)
        return 0

    def _session_id(self) -> str:
        try:
            from systemu.runtime.mcp_run_ctx import current_mcp_session_id
            return str(current_mcp_session_id() or "")
        except Exception:
            return ""
