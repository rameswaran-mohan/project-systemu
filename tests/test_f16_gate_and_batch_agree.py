"""F16 — an effect class may not be both too safe to gate and too risky to batch.

Found by walking the live first-gate card against the real seeded catalog.

`net_read` was absent from `action_governance._APPROVAL_TAGS`, so seven shipped
tools carrying it alone — web_search, fetch_html, fetch_json, api_call_get,
extract_records, find_places, web_extract — ran with **no approval at all**,
ever. The same class was also absent from `effect_tags.BATCH_APPROVABLE`, so it
*blocked* batch approval: `web_read` is gated by `browser_actuate` (which the
operator ruled batch-approvable on 2026-08-07) yet could never be batched,
because it also touches the network.

Both cannot be true. If reading the network does not warrant stopping the agent
to ask, it cannot simultaneously be the reason a standing allow is refused.
Operator ruling 2026-08-09: make it batch-approvable — matching the behaviour
already shipped, where those seven tools are ungated.

The fence is the GENERAL coherence property, quantified over the whole
vocabulary, not a pin on net_read: any future class added to one set and
forgotten in the other fails here.
"""
from __future__ import annotations

import pytest

from systemu.runtime import action_governance as ag
from systemu.runtime import effect_tags as et


def _known_tags():
    return [t for t in et.EffectTag if t is not et.EffectTag.UNKNOWN]


@pytest.mark.parametrize("tag", _known_tags(), ids=lambda t: t.value)
def test_a_class_that_does_not_require_approval_does_not_block_batch(tag):
    """not-in-_APPROVAL_TAGS  =>  in BATCH_APPROVABLE.

    UNKNOWN is the one exemption and is excluded above: "we could not classify
    this" must both gate AND refuse a standing allow, by definition.
    """
    requires_approval = tag.value in ag._APPROVAL_TAGS
    batchable = et.is_batch_approvable_tag(tag.value)

    if not requires_approval:
        assert batchable, (
            f"{tag.value!r} does not require approval — a tool carrying only this "
            f"class runs UNGATED — yet it is not batch-approvable, so it silently "
            f"blocks a standing allow on tools that carry it alongside a class the "
            f"operator DID approve. It cannot be both too safe to gate and too "
            f"risky to batch. Add it to BATCH_APPROVABLE, or add it to "
            f"_APPROVAL_TAGS so it actually gates."
        )


def test_unknown_is_the_exemption_and_stays_strict():
    """The one class that legitimately gates without being batchable."""
    assert not et.is_batch_approvable_tag(et.EffectTag.UNKNOWN.value)


def test_the_seven_ungated_net_read_tools_are_the_reason_this_rule_exists():
    """Documents the concrete case, and fails if net_read starts gating without
    someone revisiting the ruling."""
    assert et.EffectTag.NET_READ.value not in ag._APPROVAL_TAGS, (
        "net_read now requires approval — that is a real policy change (seven "
        "shipped tools begin prompting). Revisit the 2026-08-09 ruling rather "
        "than letting the two sets drift apart again."
    )
    assert et.is_batch_approvable_tag(et.EffectTag.NET_READ.value)


def test_the_ruling_did_not_leak_into_network_MUTATION():
    """net_read is reading. Writing to the network stays gated and unbatchable."""
    for danger in (
        et.EffectTag.NET_MUTATE,
        et.EffectTag.SEND_MESSAGE,
        et.EffectTag.MONEY_MOVE,
        et.EffectTag.OAUTH_CALL,
        et.EffectTag.SHELL_EXEC,
        et.EffectTag.LOCAL_DELETE,
    ):
        assert danger.value in ag._APPROVAL_TAGS, f"{danger.value} must still gate"
        assert not et.is_batch_approvable_tag(danger.value), (
            f"{danger.value} must never be batch-approvable"
        )
