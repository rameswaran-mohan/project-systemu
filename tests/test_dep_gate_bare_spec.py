"""The dependency card names the package -- not a Python list's repr of it.

WITNESSED DEFECT. ``ToolRegistry._attempt_self_heal`` posted the pending-approval
card with ``package=str(result.pending_approval or declared)``. ``pending_approval``
is a ``List[str]``, so a tool needing ``python-docx`` produced:

    title   "Install dependency: ['python-docx']"
    dedup   "dep:['python-docx']"
    resolve approve_and_install(package="['python-docx']")  ->  pip install "['python-docx']"

The install failed, and the approval was recorded under a key the installer never
checks, so the dependency stayed blocked after the operator approved it. The only
test drove ``_maybe_enqueue_dep_gate`` directly with a bare string, so it never saw
the real caller. These tests drive the REAL caller and follow the card all the way
to the install call: one card per pending requirement, each carrying the bare spec.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from systemu.core.models import Tool, ToolType


@pytest.fixture
def posted(monkeypatch):
    import systemu.runtime.tool_registry as tr
    from systemu.runtime.dependency_installer import InstallResult, InstallStatus

    cards = []

    class _FakeInbox:
        def __init__(self, vault):
            pass

        def enqueue(self, descriptor, *, gate_type, **kw):
            cards.append((descriptor, gate_type))
            return f"dec_{len(cards)}"

    monkeypatch.setattr(tr, "InboxQueue", _FakeInbox, raising=False)
    monkeypatch.setattr(
        "systemu.runtime.dependency_installer.ensure_satisfied",
        lambda *a, **k: InstallResult(
            ok=False, status=InstallStatus.BLOCKED_PENDING_APPROVAL,
            pending_approval=["python-docx", "lxml>=5"],
            error="needs operator approval"))
    tool = Tool(id="tool_make_doc", name="make_doc", description="d",
                tool_type=ToolType.PYTHON_FUNCTION,
                dependencies=["python-docx", "lxml>=5"])
    from systemu.runtime.dependency_installer import InstallMode
    registry = SimpleNamespace(_install_mode=InstallMode.PROMPT, _approvals=None,
                               _vault=object())
    result = tr.ToolRegistry._attempt_self_heal(registry, tool, "docx")
    return SimpleNamespace(cards=cards, result=result)


def test_one_card_per_pending_requirement_with_the_bare_spec(posted):
    assert [g for _d, g in posted.cards] == ["dep", "dep"]
    assert [d.title for d, _g in posted.cards] == [
        "Install dependency: python-docx", "Install dependency: lxml>=5"]
    assert [d.dedup for d, _g in posted.cards] == ["dep:python-docx", "dep:lxml>=5"]
    for d, _g in posted.cards:
        assert "[" not in d.title and "'" not in d.dedup
        assert d.tool_id == "tool_make_doc"
    assert posted.result["missing_packages"] == ["python-docx", "lxml>=5"]


@pytest.mark.parametrize("index,spec", [(0, "python-docx"), (1, "lxml>=5")])
def test_approving_the_card_installs_the_bare_spec(posted, index, spec):
    """Follow the card to the executor: Approve & Install hands pip the spec."""
    from systemu.interface.command.inbox import resolve_gate
    descriptor, gate_type = posted.cards[index]
    decision = MagicMock()
    decision.context = descriptor.to_decision_context(gate_type=gate_type)
    decision.dedup_key = descriptor.dedup
    decision.choice = "Approve & Install"
    with patch("systemu.runtime.dep_approvals.approve_and_install") as install:
        resolve_gate(decision, vault=MagicMock())
    install.assert_called_once()
    assert install.call_args.kwargs["package"] == spec
    assert install.call_args.kwargs["tool_id"] == "tool_make_doc"
