"""Fix #1: the spec→code forge review dialog must only open for a PROPOSED tool.

A stale /tools?forge=<id> deep-link survives a websocket reconnect / page rebuild;
without a status guard the dialog re-opens at gate 1 for an ALREADY-forged tool,
looping the operator gate1→gate2→gate1 (the live bug: encrypt_file was 'deployed'
yet the review kept re-appearing).
"""
from systemu.interface.pages.tools import _forge_review_eligible
from systemu.core.models import ToolStatus


class _Tool:
    def __init__(self, status):
        self.status = status
        self.name = "t"


def test_eligible_only_for_proposed_enum():
    assert _forge_review_eligible(_Tool(ToolStatus.PROPOSED)) is True
    assert _forge_review_eligible(_Tool(ToolStatus.FORGED)) is False
    assert _forge_review_eligible(_Tool(ToolStatus.DEPLOYED)) is False


def test_eligible_handles_string_status():
    # vault-loaded tools may carry the raw string value
    assert _forge_review_eligible(_Tool("proposed")) is True
    assert _forge_review_eligible(_Tool("forged")) is False
    assert _forge_review_eligible(_Tool("deployed")) is False


def test_eligible_false_on_missing_status():
    assert _forge_review_eligible(_Tool(None)) is False
