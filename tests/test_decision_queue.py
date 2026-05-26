"""Tests for the v0.8.0 OperatorDecisionQueue (Pattern 1)."""
from datetime import datetime, timezone
import pytest
from unittest.mock import MagicMock


def test_decision_dataclass_round_trips_to_dict():
    from systemu.approval.decision_queue import OperatorDecision
    d = OperatorDecision(
        id="dec_abc",
        title="Forge new tool?",
        body="Tool: file_writer",
        options=["Skip", "Forge"],
        context={"tool_id": "tool_x"},
        dedup_key="tool_forge:tool_x",
        status="pending",
        choice=None,
        created_at=datetime(2026, 5, 26, 10, 0, tzinfo=timezone.utc),
        resolved_at=None,
    )
    raw = d.to_dict()
    assert raw["id"] == "dec_abc"
    assert raw["options"] == ["Skip", "Forge"]
    assert raw["context"]["tool_id"] == "tool_x"
    assert raw["status"] == "pending"
    d2 = OperatorDecision.from_dict(raw)
    assert d2.id == d.id
    assert d2.options == d.options
    assert d2.created_at == d.created_at


def test_queue_post_creates_pending_decision():
    from systemu.approval.decision_queue import OperatorDecisionQueue
    fake_vault = MagicMock()
    fake_vault.load_index.return_value = []
    saved = []
    fake_vault.save_decision.side_effect = lambda d: saved.append(d)

    queue = OperatorDecisionQueue(fake_vault)
    decision_id = queue.post(
        title="Pick",
        body="Question?",
        options=["No", "Yes"],
        context={"x": 1},
        dedup_key="test:1",
    )
    assert decision_id.startswith("dec_")
    assert len(saved) == 1
    assert saved[0].status == "pending"
    assert saved[0].dedup_key == "test:1"


def test_queue_post_returns_existing_dedup_key_if_pending():
    """If a pending decision already exists for the dedup_key, post() must
    return the existing ID rather than create a duplicate."""
    from systemu.approval.decision_queue import OperatorDecisionQueue, OperatorDecision
    existing = OperatorDecision(
        id="dec_existing",
        title="Pick",
        body="Question?",
        options=["No", "Yes"],
        context={},
        dedup_key="test:dedup",
        status="pending",
        choice=None,
        created_at=datetime(2026, 5, 26, tzinfo=timezone.utc),
        resolved_at=None,
    )
    fake_vault = MagicMock()
    fake_vault.load_index.return_value = [existing.to_dict()]
    fake_vault.get_decision.return_value = existing

    queue = OperatorDecisionQueue(fake_vault)
    returned_id = queue.post(
        title="Pick",
        body="Question?",
        options=["No", "Yes"],
        context={},
        dedup_key="test:dedup",
    )
    assert returned_id == "dec_existing"
    fake_vault.save_decision.assert_not_called()  # no duplicate created


def test_queue_get_resolved_by_dedup_key_returns_none_when_pending():
    from systemu.approval.decision_queue import OperatorDecisionQueue, OperatorDecision
    pending = OperatorDecision(
        id="dec_p",
        title="x", body="x", options=["a", "b"], context={},
        dedup_key="k1",
        status="pending", choice=None,
        created_at=datetime(2026, 5, 26, tzinfo=timezone.utc),
        resolved_at=None,
    )
    fake_vault = MagicMock()
    fake_vault.load_index.return_value = [pending.to_dict()]
    fake_vault.get_decision.return_value = pending

    queue = OperatorDecisionQueue(fake_vault)
    assert queue.get_resolved_choice("k1") is None


def test_queue_get_resolved_by_dedup_key_returns_choice_when_resolved():
    from systemu.approval.decision_queue import OperatorDecisionQueue, OperatorDecision
    resolved = OperatorDecision(
        id="dec_r",
        title="x", body="x", options=["Skip", "Forge"], context={},
        dedup_key="k2",
        status="resolved", choice="Forge",
        created_at=datetime(2026, 5, 26, tzinfo=timezone.utc),
        resolved_at=datetime(2026, 5, 26, 12, tzinfo=timezone.utc),
    )
    fake_vault = MagicMock()
    fake_vault.load_index.return_value = [resolved.to_dict()]
    fake_vault.get_decision.return_value = resolved

    queue = OperatorDecisionQueue(fake_vault)
    assert queue.get_resolved_choice("k2") == "Forge"


def test_queue_resolve_marks_decision_resolved():
    from systemu.approval.decision_queue import OperatorDecisionQueue, OperatorDecision
    pending = OperatorDecision(
        id="dec_r1",
        title="x", body="x", options=["Skip", "Forge"], context={},
        dedup_key="k3",
        status="pending", choice=None,
        created_at=datetime(2026, 5, 26, tzinfo=timezone.utc),
        resolved_at=None,
    )
    fake_vault = MagicMock()
    fake_vault.get_decision.return_value = pending
    saved = []
    fake_vault.save_decision.side_effect = lambda d: saved.append(d)

    queue = OperatorDecisionQueue(fake_vault)
    queue.resolve("dec_r1", choice="Forge")

    assert len(saved) == 1
    assert saved[0].status == "resolved"
    assert saved[0].choice == "Forge"
    assert saved[0].resolved_at is not None


def test_queue_resolve_rejects_invalid_choice():
    from systemu.approval.decision_queue import OperatorDecisionQueue, OperatorDecision
    pending = OperatorDecision(
        id="dec_r2",
        title="x", body="x", options=["A", "B"], context={},
        dedup_key="k4",
        status="pending", choice=None,
        created_at=datetime(2026, 5, 26, tzinfo=timezone.utc),
        resolved_at=None,
    )
    fake_vault = MagicMock()
    fake_vault.get_decision.return_value = pending

    queue = OperatorDecisionQueue(fake_vault)
    with pytest.raises(ValueError, match="not in options"):
        queue.resolve("dec_r2", choice="C")


def test_file_vault_round_trip(tmp_path):
    """OperatorDecisionQueue works end-to-end against a real file vault."""
    from systemu.vault.vault import Vault
    from systemu.approval.decision_queue import OperatorDecisionQueue

    vault = Vault(str(tmp_path))
    queue = OperatorDecisionQueue(vault)

    decision_id = queue.post(
        title="Forge new tool?",
        body="Tool: file_writer",
        options=["Skip", "Forge"],
        context={"tool_id": "tool_x"},
        dedup_key="tool_forge:tool_x",
    )
    assert decision_id.startswith("dec_")

    # Confirm it shows up in pending list
    pending = queue.list_pending()
    assert any(d.id == decision_id for d in pending)

    # Resolved-lookup returns None while pending
    assert queue.get_resolved_choice("tool_forge:tool_x") is None

    # Resolve the decision
    resolved = queue.resolve(decision_id, choice="Forge")
    assert resolved.status == "resolved"
    assert resolved.choice == "Forge"

    # Now the resolved-lookup returns the choice
    assert queue.get_resolved_choice("tool_forge:tool_x") == "Forge"

    # And the decision is no longer in pending
    pending_after = queue.list_pending()
    assert not any(d.id == decision_id for d in pending_after)


def test_sqlite_vault_round_trip(tmp_path, monkeypatch):
    """OperatorDecisionQueue works end-to-end against a real SQLite vault."""
    db_path = tmp_path / "v.db"
    db_url = f"sqlite:///{db_path}"
    monkeypatch.setenv("SYSTEMU_DATABASE_URL", db_url)
    monkeypatch.setenv("SYSTEMU_STORAGE", "sqlite")

    # Build schema via SqliteVault (it calls Base.metadata.create_all internally).
    from systemu.storage.sqlite.vault import SqliteVault
    vault = SqliteVault(db_url)

    from systemu.approval.decision_queue import OperatorDecisionQueue
    queue = OperatorDecisionQueue(vault)

    decision_id = queue.post(
        title="x", body="x",
        options=["No", "Yes"],
        dedup_key="sqlite-test:1",
    )
    assert decision_id.startswith("dec_")
    assert queue.get_resolved_choice("sqlite-test:1") is None

    pending = queue.list_pending()
    assert any(d.id == decision_id for d in pending)

    queue.resolve(decision_id, choice="Yes")
    assert queue.get_resolved_choice("sqlite-test:1") == "Yes"

    pending_after = queue.list_pending()
    assert not any(d.id == decision_id for d in pending_after)


def test_file_vault_wrapper_proxies_decisions(tmp_path):
    """v0.8.0.1 regression: the FileVault adapter (used by the dashboard's
    AppState when SYSTEMU_STORAGE=file) must proxy save_decision and
    get_decision to the inner Vault.

    Without this, OperatorDecisionQueue.list_pending swallows the
    AttributeError from get_decision and silently returns an empty list,
    causing the dashboard's Pending Actions tab to render the empty state
    even when decisions exist in the vault. See UAT report
    2026-05-26-uat-pypi-v0.8.0.md for the full live trace.
    """
    from systemu.vault.vault import Vault
    from systemu.storage.file_vault import FileVault
    from systemu.approval.decision_queue import OperatorDecisionQueue

    raw = Vault(str(tmp_path))
    wrapped = FileVault(raw)

    # Required surface — must exist
    assert hasattr(wrapped, "save_decision"), (
        "FileVault is missing save_decision proxy — dashboard queue is broken"
    )
    assert hasattr(wrapped, "get_decision"), (
        "FileVault is missing get_decision proxy — dashboard queue is broken"
    )

    # Round-trip through the wrapper (NOT the raw Vault — that's the v0.8.0 hole)
    queue = OperatorDecisionQueue(wrapped)
    decision_id = queue.post(
        title="Wrapper round-trip",
        body="x",
        options=["Skip", "Forge"],
        dedup_key="filevault-wrapper-test:1",
    )

    # The dashboard's render path: must see the pending decision via wrapper
    pending = queue.list_pending()
    assert len(pending) == 1, (
        f"expected 1 pending via FileVault wrapper, got {len(pending)} -- "
        "the dashboard's Pending Actions tab will show empty state"
    )
    assert pending[0].id == decision_id
    assert pending[0].dedup_key == "filevault-wrapper-test:1"

    # Operator resolves through the wrapper (same path as dashboard button click)
    queue.resolve(decision_id, choice="Forge")
    assert queue.get_resolved_choice("filevault-wrapper-test:1") == "Forge"

    pending_after_resolve = queue.list_pending()
    assert not any(d.id == decision_id for d in pending_after_resolve)
