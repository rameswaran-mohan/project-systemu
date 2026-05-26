"""v0.8.0 Pattern 1: shadow_decision _prompt_create_new uses dedup_key."""
from unittest.mock import MagicMock, patch
import pytest


def test_prompt_create_new_uses_shadow_decision_dedup_key(monkeypatch, tmp_path):
    """When queue-mode is active and no prior decision exists, _prompt_create_new
    should propagate PendingOperatorDecision carrying the shadow_decision
    dedup_key."""
    monkeypatch.setenv("SYSTEMU_DECISION_QUEUE", "true")
    monkeypatch.delenv("SYSTEMU_HEADLESS", raising=False)
    monkeypatch.delenv("SYSTEMU_NON_INTERACTIVE", raising=False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    from systemu.vault.vault import Vault
    from systemu.approval.exceptions import PendingOperatorDecision
    from systemu.interface import notifications as N

    vault = Vault(str(tmp_path))
    monkeypatch.setattr(N, "_vault", vault)
    monkeypatch.setattr(N, "_decision_queue_instance", None, raising=False)

    fake_activity = MagicMock()
    fake_activity.id = "act_xyz"
    fake_activity.name = "test activity"

    from systemu.pipelines.shadow_decision import _prompt_create_new

    with pytest.raises(PendingOperatorDecision) as exc_info:
        _prompt_create_new(
            activity=fake_activity,
            name_hint="test_shadow",
            reasoning="because tests",
            new_skill_ids=[],
            new_tool_ids=[],
            config=MagicMock(),
            vault=vault,
            skip_supervisor=True,
        )

    assert exc_info.value.dedup_key == "shadow_decision:act_xyz"

    # The decision should also be persisted in the queue
    from systemu.approval.decision_queue import OperatorDecisionQueue
    queue = OperatorDecisionQueue(vault)
    pending = queue.list_pending()
    assert any(d.dedup_key == "shadow_decision:act_xyz" for d in pending)
