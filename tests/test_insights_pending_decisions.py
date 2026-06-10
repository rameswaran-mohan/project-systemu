"""Tests for the v0.8.0 Pending Actions tab view-model helper.

_build_pending_decision_view_model is a pure-data function that does not
touch NiceGUI, so it can be tested without booting a UI runtime.
"""
from unittest.mock import MagicMock, patch


def test_view_model_empty():
    from systemu.interface.pages.insights import _build_pending_decision_view_model

    fake_vault = MagicMock()
    with patch(
        "systemu.approval.decision_queue.OperatorDecisionQueue"
    ) as mock_queue_cls:
        mock_queue_cls.return_value.list_pending.return_value = []
        result = _build_pending_decision_view_model(fake_vault)
    assert result == {"_empty": True}


def test_view_model_no_vault():
    from systemu.interface.pages.insights import _build_pending_decision_view_model

    assert _build_pending_decision_view_model(None) == {"_no_vault": True}


def test_view_model_returns_card_data():
    from systemu.interface.pages.insights import _build_pending_decision_view_model
    from systemu.approval.decision_queue import OperatorDecision
    from datetime import datetime, timezone

    d = OperatorDecision(
        id="dec_1",
        title="T",
        body="B",
        options=["No", "Yes"],
        context={"k": "v"},
        dedup_key="dk:1",
        status="pending",
        choice=None,
        created_at=datetime(2026, 5, 26, tzinfo=timezone.utc),
        resolved_at=None,
    )
    fake_vault = MagicMock()
    with patch(
        "systemu.approval.decision_queue.OperatorDecisionQueue"
    ) as mock_queue_cls:
        mock_queue_cls.return_value.list_pending.return_value = [d]
        result = _build_pending_decision_view_model(fake_vault)
    assert isinstance(result, list)
    assert result[0]["id"] == "dec_1"
    assert result[0]["options"] == ["No", "Yes"]


def test_view_model_error_handling():
    from systemu.interface.pages.insights import _build_pending_decision_view_model

    fake_vault = MagicMock()
    with patch(
        "systemu.approval.decision_queue.OperatorDecisionQueue"
    ) as mock_queue_cls:
        mock_queue_cls.return_value.list_pending.side_effect = RuntimeError("boom")
        result = _build_pending_decision_view_model(fake_vault)
    assert "_error" in result
    assert "boom" in result["_error"]


# ── Slice 4d: the "actions" tab is gone; tab=actions redirects to /inbox ─────

def test_resolve_tab_valid_tabs_passthrough():
    from systemu.interface.pages.insights import _resolve_tab

    for tab in ("memory", "flywheel", "events"):
        assert _resolve_tab(tab) == tab


def test_resolve_tab_actions_redirects_to_inbox():
    from systemu.interface.pages.insights import _resolve_tab

    # The removed decision-surface deep-link must land on /inbox, not 404.
    assert _resolve_tab("actions") == "REDIRECT_INBOX"


def test_resolve_tab_unknown_falls_back_to_memory():
    from systemu.interface.pages.insights import _resolve_tab

    assert _resolve_tab("bogus") == "memory"
    assert _resolve_tab("") == "memory"
    assert _resolve_tab(None) == "memory"
