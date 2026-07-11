"""R-A13b-2ii-a TASK 4 — `_classify_external_effect` buckets by MOST-SEVERE tag.

The meter reads `effect_tags[0]` over the ALPHABETICALLY-sorted stored tags, so a
`net_mutate`+`send_message` tool mis-buckets as `net_mutate` (alphabetically first). Not
a safety issue (money_move still dominates via money_move_net_applies disjunct-1), but a
meter mis-bucket. The fix picks the most-severe tag for the fallback; the money_move
short-circuit is unchanged.
"""
from __future__ import annotations

from types import SimpleNamespace

from systemu.runtime.shadow_runtime import _classify_external_effect, _most_severe_effect


def _obj(**kw):
    base = dict(goal="post the update", success_criteria="visible",
                requires_external_verification=False)
    base.update(kw)
    return SimpleNamespace(**base)


def test_multi_tag_buckets_most_severe_send_message_over_net_mutate():
    # stored SORTED alphabetically → ["net_mutate", "send_message"]; the meter must
    # bucket the tool under its MOST-severe effect (send_message), not net_mutate.
    tool = SimpleNamespace(name="t", effect_tags=["net_mutate", "send_message"])
    assert _classify_external_effect(_obj(), {"parameters": {}}, tool) == "send_message"


def test_money_move_tool_still_buckets_money_move():
    # the money_move_net_applies short-circuit (disjunct-1) is unchanged.
    tool = SimpleNamespace(name="t", effect_tags=["money_move", "net_mutate"])
    assert _classify_external_effect(_obj(), {"parameters": {}}, tool) == "money_move"


def test_single_net_mutate_tag_unchanged():
    tool = SimpleNamespace(name="t", effect_tags=["net_mutate"])
    assert _classify_external_effect(_obj(), {"parameters": {}}, tool) == "net_mutate"


def test_no_tags_returns_none():
    tool = SimpleNamespace(name="t", effect_tags=[])
    assert _classify_external_effect(_obj(), {"parameters": {}}, tool) is None


def test_most_severe_helper_ordering():
    assert _most_severe_effect(["net_read", "net_mutate"]) == "net_mutate"
    assert _most_severe_effect(["send_message", "oauth_call"]) == "send_message"
    assert _most_severe_effect(["local_read", "local_write", "local_delete"]) == "local_delete"
    assert _most_severe_effect(["money_move", "send_message", "net_mutate"]) == "money_move"
    # an unlisted/exotic tag is least-severe but still returned when it is the only one.
    assert _most_severe_effect(["frobnicate"]) == "frobnicate"
    assert _most_severe_effect([]) is None
