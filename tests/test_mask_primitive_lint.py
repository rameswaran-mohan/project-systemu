"""Structural pin: every mask function in ``systemu/`` reaches the ONE shared
secret-span primitive (``messaging.gateway.mask_secret_spans``), or is listed in
``tools/mask_primitive_allowlist.json`` with a kind and a reason.

The baseline is enforced both ways (DEC-32c): an unlisted mask function that
does not reach the primitive fails, and so does a listed one that no longer
needs its entry -- the list only shrinks. The three maskers that each carried a
stale copy of the rules (the push, the evidence/audit/ledger value pass, the
resolve-audit row) are fenced: they must call the primitive DIRECTLY.
"""
from __future__ import annotations

import textwrap

import pytest

from tools import lint_mask_primitive as L


# ── the tree, as committed ───────────────────────────────────────────────────

def test_every_mask_function_reaches_the_primitive_or_is_allowlisted():
    new, stale = L.drift(L.scan_repo(), L.load_allowlist())
    assert new == [], "\n".join(new)
    assert stale == [], "\n".join(stale)


def test_the_fenced_maskers_call_the_primitive_directly():
    assert L.fence_breaks() == []


def test_the_fence_names_the_three_maskers_that_carried_their_own_rules():
    assert set(L.FENCED) == {
        ("systemu/messaging/gateway.py", "mask_outbound"),
        ("systemu/runtime/external_verifier.py", "_scrub_value_shapes"),
        ("systemu/messaging/decision_bridge.py", "_mask"),
    }


def test_every_allowlist_entry_carries_a_known_kind_and_a_reason():
    allow = L.load_allowlist()
    assert allow
    for key, e in allow.items():
        assert e["kind"] in L.KINDS, key
        assert len(e["reason"].strip()) > 20, key


def test_the_primitive_is_the_function_the_tree_actually_calls():
    from systemu.messaging import gateway
    assert callable(getattr(gateway, L.PRIMITIVE))
    assert L.PRIMITIVE_PATH == "systemu/messaging/gateway.py"


# ── the matcher, on synthetic code ──────────────────────────────────────────

def _scan(**modules):
    return {f.key for f in L.scan_sources(
        {f"systemu/{k}.py": textwrap.dedent(v) for k, v in modules.items()})}


def test_flags_a_mask_with_its_own_rules():
    assert _scan(a="""
        import re
        def redact_header(s):
            return re.sub(r"Bearer \\S+", "***", s)
    """) == {("systemu/a.py", "redact_header")}


def test_a_direct_call_to_the_primitive_passes():
    assert _scan(a="""
        from systemu.messaging.gateway import mask_secret_spans
        def redact_header(s):
            return mask_secret_spans(s)
    """) == set()


def test_reaching_it_through_another_function_passes():
    assert _scan(a="""
        def _core(s):
            return gw.mask_secret_spans(s)
        def mask_row(row):
            return {k: _core(v) for k, v in row.items()}
    """) == set()


def test_passing_a_reaching_function_as_a_callback_passes():
    assert _scan(a="""
        def _core(s):
            return mask_secret_spans(s)
        def _walk(obj, fn):
            return fn(obj)
        def mask_values(v):
            return _walk(v, _core)
    """) == set()


def test_a_same_named_function_cannot_launder_another():
    """``_core`` reaches the primitive in one module and not in another; a mask
    calling ``_core`` by bare name is NOT credited."""
    found = _scan(
        a="""
        def _core(s):
            return mask_secret_spans(s)
        """,
        b="""
        def _core(s):
            return s.replace("x", "*")
        def mask_thing(s):
            return _core(s)
        """)
    assert ("systemu/b.py", "mask_thing") in found


def test_a_nested_mask_is_its_own_scope():
    found = _scan(a="""
        def card(u):
            def _mask_query(x):
                return x.split("?")[0]
            return mask_secret_spans(_mask_query(u))
    """)
    assert found == {("systemu/a.py", "card._mask_query")}


def test_a_non_mask_name_is_not_in_scope():
    assert _scan(a="""
        def shorten(s):
            return s[:10]
    """) == set()


# ── baseline drift, both ways ────────────────────────────────────────────────

def test_drift_reports_a_new_unlisted_mask():
    new, stale = L.drift([L.Finding("systemu/x.py", "mask_x")], {})
    assert len(new) == 1 and "systemu/x.py::mask_x" in new[0]
    assert stale == []


def test_drift_reports_a_stale_entry_so_the_list_only_shrinks():
    allow = {("systemu/x.py", "mask_x"): {"kind": "residual", "reason": "r" * 30}}
    new, stale = L.drift([], allow)
    assert new == []
    assert len(stale) == 1 and "delete its entry" in stale[0]


# ── FAIL LOUD ───────────────────────────────────────────────────────────────

def test_an_unparseable_module_raises():
    with pytest.raises(L.MaskPrimitiveLintError):
        L.scan_sources({"systemu/bad.py": "def mask_x(:\n"})


def test_a_tree_without_the_primitive_raises(tmp_path):
    pkg = tmp_path / "systemu"
    pkg.mkdir()
    (pkg / "m.py").write_text("def mask_x(s):\n    return s\n", encoding="utf-8")
    with pytest.raises(L.MaskPrimitiveLintError, match="primitive"):
        L.scan_repo(tmp_path)


def test_an_empty_tree_raises(tmp_path):
    (tmp_path / "systemu").mkdir()
    with pytest.raises(L.MaskPrimitiveLintError):
        L.scan_repo(tmp_path)


def test_a_malformed_allowlist_raises(tmp_path):
    p = tmp_path / "allow.json"
    p.write_text('{"functions": [{"path": "a", "qualname": "b", "kind": "nope", '
                 '"reason": "x"}]}', encoding="utf-8")
    with pytest.raises(L.MaskPrimitiveLintError):
        L.load_allowlist(p)
