"""IMPL-1: tool_signature approval key for forged/registry tools (S1b, Task 1).

Mirrors mcp_signature (systemu/runtime/command_approvals.py). Fields:
(tool name, body_hash, sorted effect_tags, optional host_class). Order-
insensitive over effect_tags; a changed body_hash / tag set / host_class
invalidates the key. Disjoint from the "mcp" namespace via a "tool" prefix.
"""

import pytest

from systemu.runtime.command_approvals import (
    tool_signature, mcp_signature,
    init_default_store, reset_default_store_for_tests, get_default_store,
)


def test_order_insensitive_effect_tags():
    a = tool_signature("upload_report", "bh1", ["net_mutate", "send_message"])
    b = tool_signature("upload_report", "bh1", ["send_message", "net_mutate"])
    assert a == b


def test_body_hash_change_invalidates():
    assert tool_signature("t", "bh1", ["net_mutate"]) != tool_signature("t", "bh2", ["net_mutate"])


def test_tag_change_invalidates():
    assert tool_signature("t", "bh1", ["net_mutate"]) != tool_signature("t", "bh1", ["net_read"])


def test_host_class_regate_and_default():
    base = tool_signature("t", "bh1", ["net_mutate"])
    assert base == tool_signature("t", "bh1", ["net_mutate"], host_class="")
    assert base != tool_signature("t", "bh1", ["net_mutate"], host_class="a.com")
    assert tool_signature("t", "bh1", ["net_mutate"], host_class="a.com") != \
           tool_signature("t", "bh1", ["net_mutate"], host_class="b.com")


def test_namespace_disjoint_from_mcp():
    assert tool_signature("srv", "x", []) != mcp_signature("srv", "x")


def test_store_round_trip(tmp_path):
    reset_default_store_for_tests()
    init_default_store(tmp_path)
    store = get_default_store()
    sig = tool_signature("t", "bh1", ["net_mutate"])
    assert store.is_approved(sig) is False
    store.approve(sig, command="tool:t")
    assert store.is_approved(sig) is True
    store.mark_resume_approved(sig)
    assert store.consume_resume_approved(sig) is True
    assert store.consume_resume_approved(sig) is False
