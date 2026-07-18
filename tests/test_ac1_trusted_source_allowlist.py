"""Spec AC1(b), structural half — WHICH bind sources may emit a trusted (silent-bind
capable) origin at all.

The behavioural pins live in ``test_ac1_silent_bind_invariant.py`` (kept free of
``inspect.getsource`` so they run in the edit-safe tier). This module source-inspects the
bind pipeline, so conftest auto-tags it ``source_sensitive`` — it runs in the full tier,
which is the pre-push gate.

Why it exists: the existing binder tests pin AC1(b) for specific known sources. Nothing
pinned the general rule, so a NEW bind source (a world-model source, say) that stamped a
trusted origin at high confidence would create the very silent-bind AC1(b) forbids and
every existing test would still pass. This converts "true today by inspection of a few
call sites" into "cannot regress without a human deliberately saying so".
"""
from __future__ import annotations

import inspect

from systemu.runtime import requirement_binder as rb


# The ONLY bind sources permitted to emit a non-content_derived (trusted-axis) origin.
# Each is backed by data systemu genuinely trusts — never tool output or file bytes:
#   _bind_provided_params  → systemu_authored (a value the current tool call supplied)
#   _bind_inventory_entry  → operator         (credentials branch, over the operator's
#                                              own credential store — names only)
#   _bind_profile          → operator         (facts from the operator's own prompt)
#   _bind_schema_default   → systemu_authored (systemu's own capability catalog)
# Everything else MUST clamp to content_derived, so it lands in the ask bundle.
_TRUSTED_EMITTERS = {
    "_bind_provided_params",
    "_bind_inventory_entry",
    "_bind_profile",
    "_bind_schema_default",
}


def test_only_allowlisted_sources_can_emit_a_trusted_origin():
    emitters = set()
    for fn in rb._SOURCES:
        src = inspect.getsource(fn)
        if "_OPERATOR" in src or "_SYSTEMU" in src:
            emitters.add(fn.__name__)
    assert emitters == _TRUSTED_EMITTERS, (
        "the set of bind sources able to emit a TRUSTED (silent-bind-capable) origin "
        f"changed: {emitters ^ _TRUSTED_EMITTERS}. A new trusted emitter must be "
        "reviewed against spec AC1(b) and added to _TRUSTED_EMITTERS deliberately."
    )


def test_the_untrusted_sources_clamp_unconditionally():
    """The two always-clamped sources must not acquire a trusted-origin path."""
    for name in ("_bind_filehandle", "_bind_run_context"):
        src = inspect.getsource(getattr(rb, name))
        assert "_OPERATOR" not in src and "_SYSTEMU" not in src, (
            f"{name} must clamp to content_derived unconditionally")
        assert rb._CONTENT_DERIVED in src
