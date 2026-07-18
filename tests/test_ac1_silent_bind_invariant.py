"""Spec AC1(b) — "a content_derived fact can NEVER silent-bind (asserted at the §5.3
binder)" — pinned BEHAVIOURALLY.

Grounding note (the point of this file): AC1(b) is ALREADY implemented — it IS
``requirement_binder._needs_ask``. The existing binder suite pins it for specific known
sources; nothing pinned the RULE itself. These tests pin the rule directly.

Deliberately free of ``inspect.getsource`` so this module is NOT auto-tagged
``source_sensitive`` — the core silent-bind assertion must be checked by the EDIT-SAFE
gate too, not only by the full tier. The structural companion (which source-inspects the
bind pipeline, and is correctly source_sensitive) lives in
``test_ac1_trusted_source_allowlist.py``.
"""
from __future__ import annotations

from systemu.runtime import requirement_binder as rb


def test_needs_ask_is_the_ac1_assertion():
    """A content_derived value is forced to ask even at full confidence — ``state="have"``
    alone can never make it silent (the confidence threshold governs have-vs-resolvable
    and cannot rescue a tainted value)."""
    assert rb._needs_ask({"state": "have", "value_origin": rb._CONTENT_DERIVED}) is True
    # the trusted axis may bind silently once it is already "have"
    assert rb._needs_ask({"state": "have", "value_origin": rb._OPERATOR}) is False
    assert rb._needs_ask({"state": "have", "value_origin": rb._SYSTEMU}) is False
    # anything not yet "have" always asks, regardless of origin
    assert rb._needs_ask({"state": "resolvable", "value_origin": rb._OPERATOR}) is True
    assert rb._needs_ask({"state": "missing", "value_origin": rb._SYSTEMU}) is True


def test_entry_origin_never_trusts_a_claimed_origin_class():
    """The anti-laundering rule: taint is derived from the SOURCE KIND, never copied from
    an entry's self-declared ``origin_class`` (which a forged — or snapshot-rehydrated —
    inventory entry could set to "operator"). This is exactly why a world-model fact's
    STORED origin_class must never be used as bind-taint: the populator copies each
    entry's declared origin, and the service model's default is "operator" for every
    service."""
    for entry in ({"origin_class": "operator"}, {"origin_class": "systemu_authored"},
                  {"origin_class": "content_derived"}, {}, None):
        assert rb._entry_origin(entry) == rb._CONTENT_DERIVED
