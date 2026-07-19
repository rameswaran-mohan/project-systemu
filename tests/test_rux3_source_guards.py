"""R-UX3 — the pins that read SOURCE (structural guards + wiring).

Split out from ``test_rux3_command_palette.py`` / ``test_rux3_why_panel.py`` on
purpose. ``tests/conftest.py`` auto-marks an ENTIRE test module
``source_sensitive`` when it contains ``getsource(``, and the edit-safe gate
(``pytest -m "not source_sensitive"``) deselects the whole module. Keeping these
few source-reading assertions here means the behavioural pins next door — the
palette's safety line, the why panel's honesty rules — still run in the fast,
routinely-used gate instead of only in the full tier.

These, by contrast, genuinely are source-snapshot assertions: they compare
against file text and are fragile while the file under test is being edited,
which is exactly what the marker exists for.
"""
from __future__ import annotations

import inspect


# ── palette: the safety line ────────────────────────────────────────────────

def test_palette_source_has_no_execution_seam():
    """CI-asserted (spec): the palette can never directly run an action.

    The ban targets the verbs that ACT. Read-only construction of a queue
    object (``InboxQueue(vault).list_descriptors()``) is deliberately NOT
    banned -- listing open gates is exactly what the Asks group needs, and
    banning the constructor would only push that read somewhere less visible.
    What must never appear is anything that resolves, dispatches, enqueues, or
    spawns.
    """
    from systemu.interface.components import command_palette
    src = inspect.getsource(command_palette)
    forbidden = (
        "resolve_gate", "decision_dispatcher", "dispatch(",
        "direct_task", "submit_task", "run_tool", "subprocess",
        ".enqueue(", ".resolve(", "os.system", "popen",
    )
    hits = [f for f in forbidden if f in src]
    assert not hits, f"palette gained an execution seam: {hits}"


def test_palette_matching_uses_no_model_call():
    from systemu.interface.components import command_palette
    src = inspect.getsource(command_palette).lower()
    for bad in ("llm", "openrouter", "completion", "embedding"):
        assert bad not in src, f"palette reached for {bad!r}"


def test_every_static_action_points_at_a_real_route():
    """The static registry is hand-written, so pin it against the routes the
    dashboard actually serves -- otherwise it rots into dead links."""
    from systemu.interface import dashboard
    from systemu.interface.components.command_palette import _STATIC_ACTIONS

    src = inspect.getsource(dashboard)
    for _label, path, _detail in _STATIC_ACTIONS:
        assert f'@ui.page("{path}")' in src, f"{path} is not a registered route"


# ── palette: wiring ─────────────────────────────────────────────────────────

def test_palette_is_mounted_from_the_shared_layout():
    """Mounted in _build_layout => present on every route, per UX-13."""
    from systemu.interface import dashboard
    layout = inspect.getsource(dashboard._build_layout)
    # The trailing "(" matters: assert the CALL, not just the import. A bare
    # name check stayed green when the call was replaced by `pass` and only the
    # import remained (caught by mutation testing).
    assert "build_command_palette(" in layout


def test_adding_the_palette_did_not_add_an_unguarded_route():
    """The palette is layout chrome, not a route -- so the onboarding-gate
    invariant (test_wave11_onboarding_gate) must be untouched."""
    from systemu.interface import dashboard
    src = inspect.getsource(dashboard)
    assert src.count("@ui.page(") - 1 <= src.count(
        "_redirect_to_welcome_if_needed()")


# ── why: the read-only hard rule ────────────────────────────────────────────

def test_why_panel_does_not_import_or_call_the_evaluator():
    """AST-level, not prose-level.

    A substring scan would flag the module docstring, which legitimately NAMES
    ``evaluate_action`` to explain that it is never called. What must be absent
    is an import of it or a call to it.
    """
    import ast
    from systemu.interface.components import why_panel

    tree = ast.parse(inspect.getsource(why_panel))

    imported, called = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if "action_governance" in (node.module or ""):
                imported.add(node.module)
            for alias in node.names:
                if alias.name in ("evaluate_action", "ActionContext"):
                    imported.add(alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if "action_governance" in alias.name:
                    imported.add(alias.name)
        elif isinstance(node, ast.Call):
            fn = node.func
            nm = getattr(fn, "id", None) or getattr(fn, "attr", None)
            if nm in ("evaluate_action", "ActionContext", "resolve",
                      "resolve_gate"):
                called.add(nm)

    assert not imported, f"why_panel imported the evaluator: {imported}"
    assert not called, f"why_panel called {called} -- it must be read-only"


# ── why: wiring ─────────────────────────────────────────────────────────────

def test_shared_decision_card_renders_why():
    """render_decision_card is the ONE shared card renderer (chat thread,
    insights, inbox asks) -- attaching there covers every ask card."""
    from systemu.interface.pages import insights
    assert "build_why_panel" in inspect.getsource(insights.render_decision_card)


def test_inbox_gate_card_renders_why_from_the_persisted_record():
    """NOT from the GateDescriptor: it carries no verdict/tags/signature, so
    explaining from it would mean inventing those fields."""
    from systemu.interface.pages import inbox_page
    src = inspect.getsource(inbox_page._render_unified_card)
    assert "build_why_panel" in src
    assert "get_decision" in src


def test_gate_persists_its_reason_for_the_panel():
    """The evaluate_action reason must survive structurally. Before R-UX3 it
    existed only inside the free-text `inspect` blob, and parsing prose back out
    would be a proxy, not a record."""
    from systemu.runtime import tool_sandbox
    src = inspect.getsource(tool_sandbox)
    assert '"gate_reason": reason' in src
