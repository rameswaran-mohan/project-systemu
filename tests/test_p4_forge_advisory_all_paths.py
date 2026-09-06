"""Phase 4 / slice A3 (R-CAP1 smallest honest slice) - the CAP-6 near-duplicate
advisory must be computed on EVERY forge path, not only the CLI one.

Before this slice the advisory (`capability_index.slot_collisions` +
`forge_dedup_advisory`) was computed at exactly ONE call site: inside
``forge_tool``, which only the CLI reaches. The Governor/daemon provisioner
(``governor._provision_tool`` -> ``forge_proposed_tools`` ->
``_generate_and_save_code``), the dashboard /tools gate (``forge_tool_from_spec``),
the self-heal reforge (``reforge_failed_tool_code``) and the validator auto-forge
bridge ALL bypassed it, so a daemon-forged duplicate was never surfaced anywhere.

``_generate_and_save_code`` is the one function those code-generating paths share,
so the computation lives there and rides out on the operator-visible channel that
function ALREADY emits: the forge ``log_event("SUCCESS", "tool", ...)`` record
(message + a ``dedup_advisory`` context key). No new surface is invented.

``save_approved_code`` (the /tools Gate-2 "Approve & Sign Off" dialog) is the ONE
forge path that does not route through that spine -- it persists code the operator
already read -- so it computes the same line itself, onto the approval event it
already emits. Both are covered below; the claim is "every forge path", so every
one of them is exercised here by name.

NON-BLOCKING everywhere: this is visibility, not the admission gate. A collision
never stops a forge, and a failure to compute the advisory never affects one.

BYTE-IDENTICAL FLOOR: with no collision (the common small-catalog case) every
path's output is byte-for-byte what it was before this slice.
"""
from unittest.mock import MagicMock, patch

import pytest

from systemu.core.models import (
    Activity,
    Scroll,
    Tool,
    ToolStatus,
    ToolType,
)
from systemu.core.utils import generate_id
from systemu.vault.vault import Vault

# The exact advisory text `forge_dedup_advisory` produces for the fixture below.
# Hand-authored (not re-derived from the producer) so this is a real byte pin.
ADVISORY = (
    "Heads up: this shares the create:issue capability with existing tool(s): "
    "create_issue. Consider extending one of those instead of forging a "
    "duplicate."
)

GOOD_CODE = "def run(**kwargs):\n    return {'ok': True}\n"


# --------------------------------------------------------------------------- #
# fixtures / helpers
# --------------------------------------------------------------------------- #

@pytest.fixture
def vault(tmp_path):
    for sub in ["scrolls", "activities", "shadow_army", "skills",
                "tools/implementations", "evolutions", "notifications",
                "executions"]:
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    for idx_dir in ["scrolls", "activities", "shadow_army", "skills", "tools",
                    "evolutions"]:
        (tmp_path / idx_dir / "index.json").write_text("[]", encoding="utf-8")
    return Vault(str(tmp_path))


@pytest.fixture
def config(tmp_path):
    cfg = MagicMock()
    cfg.vault_dir = str(tmp_path)
    cfg.tier2_model = "test-model"
    return cfg


def _occupy_slot(vault, tmp_path):
    """Save a REAL enabled+deployed tool holding the ``create:issue`` slot, via the
    real save path, so ``derive_index`` produces a row for it (an unenabled or
    bodiless tool is not indexed and would make every assertion below vacuous)."""
    impl = tmp_path / "tools" / "implementations" / "create_issue.py"
    impl.parent.mkdir(parents=True, exist_ok=True)
    impl.write_text(GOOD_CODE, encoding="utf-8")
    vault.save_tool(Tool(
        id="tool_existing", name="create_issue",
        description="create an issue", tool_type=ToolType.PYTHON_FUNCTION,
        implementation_path=str(impl), enabled=True,
        status=ToolStatus.DEPLOYED,
    ))
    # precondition: the collision is real, else these tests prove nothing
    from systemu.runtime import capability_index as ci
    assert ci.slot_collisions(vault, "open_issue"), (
        "fixture precondition failed: create_issue is not in the capability index")


def _proposed(vault, name="open_issue", description="opens an issue"):
    tool = Tool(
        id=generate_id("tool"), name=name, description=description,
        tool_type=ToolType.PYTHON_FUNCTION, status=ToolStatus.PROPOSED,
    )
    vault.save_tool(tool)
    return tool


def _scroll(vault, name="s1"):
    scroll = Scroll(
        id=generate_id("scroll"), name=name, source_session_id="t",
        raw_instructions_path="", narrative_md="context",
    )
    vault.save_scroll(scroll)
    return scroll


def _spy_events(monkeypatch):
    """Capture the operator-visible event stream `_generate_and_save_code` emits.

    It does ``from systemu.interface.notifications import log_event`` at CALL
    time, so patching the module attribute intercepts the real call site."""
    import systemu.interface.notifications as N
    events = []

    def _spy(level, category, message, context=None, **kw):
        events.append({"level": level, "category": category,
                       "message": message, "context": context or {}})

    monkeypatch.setattr(N, "log_event", _spy)
    return events


def _forge_success_events(events):
    return [e for e in events
            if e["level"] == "SUCCESS" and e["category"] == "tool"
            and "forged successfully" in e["message"]]


# --------------------------------------------------------------------------- #
# 1. THE GOVERNOR/DAEMON PATH - the one this slice exists for
# --------------------------------------------------------------------------- #

def test_governor_path_computes_and_surfaces_the_dedup_advisory(
        vault, config, tmp_path, monkeypatch):
    """The REAL Governor provisioner entry point (``forge_proposed_tools``, built
    exactly as governor._provision_tool builds it: a stub Activity linking one
    PROPOSED tool) must surface the near-duplicate advisory. Pre-slice this path
    computed NOTHING - a daemon forging a duplicate was invisible."""
    _occupy_slot(vault, tmp_path)
    tool = _proposed(vault)
    scroll = _scroll(vault)
    activity = Activity(
        id=generate_id("activity"), name="governor-harness-open_issue",
        scroll_id=scroll.id, required_tool_ids=[tool.id],
    )
    events = _spy_events(monkeypatch)

    with patch("systemu.pipelines.tool_forge.llm_call_json",
               return_value={"implementation": GOOD_CODE}):
        from systemu.pipelines.tool_forge import forge_proposed_tools
        forged = forge_proposed_tools(activity, config, vault)

    assert len(forged) == 1 and forged[0].status == ToolStatus.FORGED, (
        "precondition: the Governor path must still forge (advisory never blocks)")

    succ = _forge_success_events(events)
    assert succ, "the Governor path emitted no forge-success event to attach to"
    assert ADVISORY in succ[0]["message"], (
        "the Governor path forged a same-slot duplicate and said nothing about it")
    assert succ[0]["context"].get("dedup_advisory") == ADVISORY


def test_governor_path_no_collision_output_is_byte_identical(
        vault, config, tmp_path, monkeypatch):
    """AC8 floor: a small catalog with no collision produces byte-for-byte the
    pre-slice event (message AND context), so the common case is untouched."""
    tool = _proposed(vault, name="open_issue")
    scroll = _scroll(vault)
    activity = Activity(
        id=generate_id("activity"), name="a", scroll_id=scroll.id,
        required_tool_ids=[tool.id],
    )
    events = _spy_events(monkeypatch)

    with patch("systemu.pipelines.tool_forge.llm_call_json",
               return_value={"implementation": GOOD_CODE}):
        from systemu.pipelines.tool_forge import forge_proposed_tools
        assert len(forge_proposed_tools(activity, config, vault)) == 1

    succ = _forge_success_events(events)
    assert len(succ) == 1
    impl_path = tmp_path / "tools" / "implementations" / "open_issue.py"
    assert succ[0]["message"] == (
        "Tool 'open_issue' forged successfully \u2192 open_issue.py")
    assert succ[0]["context"] == {"tool_id": tool.id,
                                  "impl_path": str(impl_path)}


# --------------------------------------------------------------------------- #
# 2. THE CLI PATH - pinned byte-identical (its pre-gate line must not move)
# --------------------------------------------------------------------------- #

def _capture_gate(monkeypatch):
    captured = {}

    def _spy_notify(**kwargs):
        captured.update(kwargs)
        return "Skip"          # stop before code generation

    monkeypatch.setattr("systemu.pipelines.tool_forge.notify_user", _spy_notify)
    return captured


def test_cli_gate_message_is_byte_identical_with_a_collision(
        vault, config, tmp_path, monkeypatch):
    """The CLI confirmation body is hand-authored here, character for character,
    including the blank line and the U+26A0 marker that precede the advisory."""
    _occupy_slot(vault, tmp_path)
    tool = _proposed(vault)
    scroll = _scroll(vault)
    captured = _capture_gate(monkeypatch)

    from systemu.pipelines.tool_forge import forge_tool
    assert forge_tool(tool, scroll, config, vault) is None   # "Skip"

    assert captured["title"] == "Forge New Tool?"
    assert captured["actions"] == ["Skip", "Forge"]
    assert captured["dedup_key"] == "tool_forge:" + tool.id
    assert captured["message"] == (
        "Tool: [bold]open_issue[/bold]\n"
        "Type: ToolType.PYTHON_FUNCTION\n"
        "Description: opens an issue\n"
        "Dependencies: none\n\n"
        "Context scroll: s1"
        "\n\n\u26a0 " + ADVISORY
    )


def test_cli_gate_message_is_byte_identical_without_a_collision(
        vault, config, monkeypatch):
    """AC8 floor on the CLI path: a free slot adds nothing at all."""
    tool = _proposed(vault)
    scroll = _scroll(vault)
    captured = _capture_gate(monkeypatch)

    from systemu.pipelines.tool_forge import forge_tool
    assert forge_tool(tool, scroll, config, vault) is None

    assert captured["message"] == (
        "Tool: [bold]open_issue[/bold]\n"
        "Type: ToolType.PYTHON_FUNCTION\n"
        "Description: opens an issue\n"
        "Dependencies: none\n\n"
        "Context scroll: s1"
    )


# --------------------------------------------------------------------------- #
# 3. THE DASHBOARD /tools GATE PATH (forge_tool_from_spec)
# --------------------------------------------------------------------------- #

def test_dashboard_gate_path_surfaces_the_dedup_advisory(
        vault, config, tmp_path, monkeypatch):
    """``forge_tool_from_spec`` is what the /tools + Inbox forge gate runs on
    Approve. It skips ``forge_tool`` entirely, so pre-slice it was silent."""
    import json as _json
    _occupy_slot(vault, tmp_path)
    tool = _proposed(vault)
    events = _spy_events(monkeypatch)

    with patch("systemu.pipelines.tool_forge.llm_call_json",
               return_value={"implementation": GOOD_CODE}):
        from systemu.pipelines.tool_forge import forge_tool_from_spec
        out = forge_tool_from_spec(
            tool.id, _json.dumps({"name": "open_issue"}), config, vault)

    assert out is not None and out.status == ToolStatus.FORGED
    succ = _forge_success_events(events)
    assert succ and ADVISORY in succ[0]["message"]
    assert succ[0]["context"].get("dedup_advisory") == ADVISORY


def test_dashboard_gate_path_no_collision_is_byte_identical(
        vault, config, tmp_path, monkeypatch):
    import json as _json
    tool = _proposed(vault)
    events = _spy_events(monkeypatch)

    with patch("systemu.pipelines.tool_forge.llm_call_json",
               return_value={"implementation": GOOD_CODE}):
        from systemu.pipelines.tool_forge import forge_tool_from_spec
        assert forge_tool_from_spec(
            tool.id, _json.dumps({"name": "open_issue"}), config, vault) is not None

    succ = _forge_success_events(events)
    assert len(succ) == 1
    impl_path = tmp_path / "tools" / "implementations" / "open_issue.py"
    assert succ[0]["message"] == (
        "Tool 'open_issue' forged successfully \u2192 open_issue.py")
    assert succ[0]["context"] == {"tool_id": tool.id,
                                  "impl_path": str(impl_path)}


# --------------------------------------------------------------------------- #
# 4. THE SELF-HEAL REFORGE PATH (reforge_failed_tool_code)
# --------------------------------------------------------------------------- #

def test_selfheal_reforge_path_surfaces_the_dedup_advisory(
        vault, config, tmp_path, monkeypatch):
    _occupy_slot(vault, tmp_path)
    tool = _proposed(vault)
    events = _spy_events(monkeypatch)

    with patch("systemu.pipelines.tool_forge.llm_call_json",
               return_value={"implementation": GOOD_CODE}):
        from systemu.pipelines.tool_forge import reforge_failed_tool_code
        out = reforge_failed_tool_code(tool, config, vault,
                                       prior_failure="boom")

    assert out is not None
    succ = _forge_success_events(events)
    assert succ and ADVISORY in succ[0]["message"]


# --------------------------------------------------------------------------- #
# 5. THE GATE-2 SIGN-OFF PATH (save_approved_code)
#
# The one forge path that does NOT route through _generate_and_save_code: the
# /tools rich dialog persists code the operator already read (tools.py:927), so
# it needs the advisory computed on its own. Without this it would be the single
# remaining silent surface, and "every forge path" would be a false claim.
# --------------------------------------------------------------------------- #

def _approved_events(events):
    return [e for e in events
            if e["level"] == "SUCCESS" and e["category"] == "tool"
            and "approved by user" in e["message"]]


def test_gate2_signoff_path_surfaces_the_dedup_advisory(
        vault, config, tmp_path, monkeypatch):
    _occupy_slot(vault, tmp_path)
    tool = _proposed(vault)
    events = _spy_events(monkeypatch)

    from systemu.pipelines.tool_forge import save_approved_code
    out = save_approved_code(tool, GOOD_CODE, config, vault)

    assert out.status == ToolStatus.FORGED
    appr = _approved_events(events)
    assert appr, "the Gate-2 sign-off path emitted no approval event"
    assert ADVISORY in appr[0]["message"]
    assert appr[0]["context"].get("dedup_advisory") == ADVISORY


def test_gate2_signoff_path_no_collision_is_byte_identical(
        vault, config, tmp_path, monkeypatch):
    tool = _proposed(vault)
    events = _spy_events(monkeypatch)

    from systemu.pipelines.tool_forge import save_approved_code
    save_approved_code(tool, GOOD_CODE, config, vault)

    appr = _approved_events(events)
    assert len(appr) == 1
    impl_path = tmp_path / "tools" / "implementations" / "open_issue.py"
    assert appr[0]["message"] == (
        "Tool 'open_issue' approved by user \u2192 FORGED "
        "(disabled until toggled ON)")
    assert appr[0]["context"] == {"tool_id": tool.id,
                                  "impl_path": str(impl_path)}


# --------------------------------------------------------------------------- #
# 6. REACHABILITY PIN on the _generate_and_save_code call site (mutation-checked)
# --------------------------------------------------------------------------- #

def test_generate_and_save_code_calls_the_capability_index_advisory(
        vault, config, tmp_path, monkeypatch):
    """MUTATION-CHECKED REACHABILITY PIN: delete the advisory call site from
    ``_generate_and_save_code`` and THIS NAMED TEST goes red. It spies on the real
    producer functions in ``systemu.runtime.capability_index``, so no amount of
    re-wording the surface can keep it green without a live call."""
    from systemu.runtime import capability_index as ci
    _occupy_slot(vault, tmp_path)
    tool = _proposed(vault)
    scroll = _scroll(vault)

    seen = {"collisions": [], "advisory": []}
    real_collisions, real_advisory = ci.slot_collisions, ci.forge_dedup_advisory

    def _spy_collisions(v, name, **kw):
        seen["collisions"].append((name, kw.get("exclude_id")))
        return real_collisions(v, name, **kw)

    def _spy_advisory(name, cols):
        seen["advisory"].append(name)
        return real_advisory(name, cols)

    monkeypatch.setattr(ci, "slot_collisions", _spy_collisions)
    monkeypatch.setattr(ci, "forge_dedup_advisory", _spy_advisory)

    with patch("systemu.pipelines.tool_forge.llm_call_json",
               return_value={"implementation": GOOD_CODE}):
        from systemu.pipelines.tool_forge import _generate_and_save_code
        assert _generate_and_save_code(tool, scroll, config, vault) is not None

    assert ("open_issue", tool.id) in seen["collisions"], (
        "_generate_and_save_code never asked capability_index for same-slot "
        "collisions - the shared forge spine computes no advisory")
    assert "open_issue" in seen["advisory"]


# --------------------------------------------------------------------------- #
# 7. NON-BLOCKING: a broken advisory degrades to "" and is LOGGED, never silent
# --------------------------------------------------------------------------- #

def test_advisory_failure_never_blocks_the_forge_and_is_logged_at_debug(
        vault, config, tmp_path, monkeypatch, caplog):
    """A courtesy line may swallow its own failure; it may not do so invisibly."""
    import logging
    from systemu.runtime import capability_index as ci

    _occupy_slot(vault, tmp_path)
    tool = _proposed(vault)
    scroll = _scroll(vault)

    def _boom(*a, **kw):
        raise RuntimeError("index exploded")

    monkeypatch.setattr(ci, "slot_collisions", _boom)
    events = _spy_events(monkeypatch)
    caplog.set_level(logging.DEBUG, logger="systemu.pipelines.tool_forge")

    with patch("systemu.pipelines.tool_forge.llm_call_json",
               return_value={"implementation": GOOD_CODE}):
        from systemu.pipelines.tool_forge import _generate_and_save_code
        out = _generate_and_save_code(tool, scroll, config, vault)

    assert out is not None and out.status == ToolStatus.FORGED, (
        "a failed advisory blocked the forge - it is advisory, never a gate")
    succ = _forge_success_events(events)
    assert len(succ) == 1
    assert "dedup_advisory" not in succ[0]["context"]     # degraded to ""
    assert any(r.levelno == logging.DEBUG and "advisory" in r.getMessage().lower()
               for r in caplog.records), (
        "the advisory failed with no debug record - an invisible failure")
