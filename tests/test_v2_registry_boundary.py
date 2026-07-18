"""The v2 tool-registry boundary — ENFORCED, not assumed.

``ToolSandbox.execute()``'s v2 fast path calls a registered handler DIRECTLY
(tool_sandbox.py ~:601-656). It does not route through ``execute_tool``, so
``_maybe_gate_command`` / ``_maybe_gate_tool`` never see the call. That is only
safe because the v2 registry is a CURATED surface: entries come from repo code
imported at boot (the ``discover_modules`` AST scan over
``systemu/runtime/tools/``) plus the operator-connected MCP bridge — never from
a forged tool, a plugin, or config.

The codebase relies on that property but nothing checked it. These are the cheap
pins: if a future change ever registers a handler defined outside the ``systemu``
package (a plugin loader, an ``exec``'d forged body, a third-party import), this
test fails instead of the ungated fast path silently accepting it.
"""
from __future__ import annotations

import pytest


# The MCP bridge registers namespaced tools whose handler is a closure defined in
# ``systemu.runtime.mcp.sdk.registry_bridge`` — inside the package, so it needs no
# special-casing. Kept explicit so the allowance is visible if that ever moves.
ALLOWED_MODULE_PREFIXES = ("systemu.",)


def _boot_registry():
    """Run the boot-time discovery the daemon runs, then return the singleton."""
    from systemu.runtime.tool_registry_v2 import registry
    registry.discover_modules("systemu.runtime.tools")
    return registry


class TestV2RegistryHandlerProvenance:
    def test_discovery_actually_registers_tools(self):
        """Precondition. Without this, the invariant test below would pass
        vacuously over an empty registry — the failure mode that shipped an
        EMPTY prod capability index once already."""
        registry = _boot_registry()
        assert len(registry.list()) > 0, (
            "boot-time discovery registered NOTHING — the provenance invariant "
            "below would be vacuous")

    def test_every_handler_is_defined_in_repo_code(self):
        """Every v2 handler must resolve to a ``systemu.`` module.

        This is what makes the ungated fast path defensible: the code being
        called is curated repo code (or the MCP bridge's closure over the
        ``call_mcp_tool`` chokepoint), not arbitrary generated or third-party
        code.
        """
        registry = _boot_registry()

        offenders = []
        for entry in registry.list():
            handler = entry.handler
            # Unwrap functools.partial / decorated handlers so the check reads
            # the REAL defining module rather than a wrapper's.
            target = getattr(handler, "func", handler)
            module = getattr(target, "__module__", None)
            if module is None or not module.startswith(ALLOWED_MODULE_PREFIXES):
                offenders.append((entry.name, module))

        assert not offenders, (
            "v2 registry handlers defined outside the systemu package — the "
            "ungated v2 fast path in ToolSandbox.execute() would call these "
            f"directly: {offenders}")

    def test_every_handler_is_callable(self):
        """A non-callable entry would surface as an opaque TypeError deep inside
        the fast path rather than at registration."""
        registry = _boot_registry()
        bad = [e.name for e in registry.list() if not callable(e.handler)]
        assert not bad, f"non-callable v2 handlers: {bad}"


class TestPendingOperatorDecisionChokepointIsPreserved:
    def test_execute_reraises_pending_operator_decision_from_a_v2_handler(self):
        """The gating obligation documented on ``ToolRegistry.register``: an
        effectful v2 handler gates by raising ``PendingOperatorDecision``, and
        ``ToolSandbox.execute`` must let it PROPAGATE to the park/resume
        machinery — never swallow it into a failure dict (which would orphan the
        Inbox decision and continue the run with a denied tool).
        """
        import asyncio

        from systemu.approval.exceptions import PendingOperatorDecision
        from systemu.runtime.tool_registry_v2 import registry
        from systemu.runtime.tool_sandbox import ToolSandbox

        name = "_test_gated_probe_tool"

        def _gating_handler(**kwargs):
            raise PendingOperatorDecision(
                decision_id="dec_probe", dedup_key="tool:probe",
                options=["Deny", "Approve once"], message="probe",
            )

        registry.register(
            name=name, toolset="_test", schema={"type": "object"},
            handler=_gating_handler, is_action_tool=True,
        )
        try:
            sandbox = ToolSandbox()
            with pytest.raises(PendingOperatorDecision):
                asyncio.run(sandbox.execute(name, {}))
        finally:
            registry.unregister(name)
