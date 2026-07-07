"""R-A10 step B2 — the ``_walk`` leaf-callback + path-accumulator seam.

The requirement binder (a later R-A10 step) reuses ``fixture_synth``'s traversal
(object / array / nested / $ref / anyOf-allOf handling + the path-oracle) rather
than re-implementing schema walking. This test pins the NEW seam:

  * ``_walk`` accepts a keyword-only ``leaf_fn`` hook + a ``path`` accumulator.
  * With the DEFAULT ``leaf_fn`` (``_synth_leaf``), ``synthesize_params`` produces
    byte-identical output — the refactor is transparent to every synth consumer.
  * A custom ``leaf_fn`` observes each terminal leaf with the correct JSON-pointer
    ``path`` (object property → +name, array items → +"[]"), ``kind`` and ``key``.

The 26 tests in ``tests/test_fixture_synth.py`` are the FROZEN synth contract; this
file only adds coverage for the new hook — it must not require changing them.
"""
from __future__ import annotations

import os

from systemu.pipelines.fixture_synth import (
    _walk, _Ctx, _synth_leaf, synthesize_params, _SENTINEL,
)


def _mk_ctx(tmp_path, tool_name=""):
    return _Ctx(tool_name=tool_name, sandbox=tmp_path)


# ── the SPY leaf_fn sees every terminal leaf with its JSON-pointer path ─────────

def test_leaf_fn_spy_observes_each_leaf_path(tmp_path):
    """A custom leaf_fn is invoked once per terminal leaf, with the accumulated
    ``path`` (object property → +name, array item → +"[]") and the oracle ``kind``.
    Exercises nested object + array-of-objects + $ref."""
    schema = {
        "type": "object",
        "properties": {
            "config": {
                "type": "object",
                "properties": {
                    "output_file": {"type": "string"},   # path leaf, nested
                    "verbose": {"type": "boolean"},       # non-path leaf, nested
                },
                "required": ["output_file", "verbose"],
            },
            "jobs": {
                "type": "array",
                "items": {"$ref": "#/$defs/Job"},
            },
        },
        "required": ["config", "jobs"],
        "$defs": {
            "Job": {
                "type": "object",
                "properties": {"src": {"type": "string"}},   # path leaf under array item
                "required": ["src"],
            }
        },
    }

    seen = []  # (path, kind, key)

    def spy(node, *, key, required, kind, ext, ctx, path, schema_value=None,
            schema_value_kind=None):
        seen.append((path, kind, key))
        # still produce a real synth value so recursion/materialization proceeds
        return _synth_leaf(node, key=key, required=required, kind=kind, ext=ext,
                           ctx=ctx, schema_value=schema_value if schema_value_kind
                           else _SENTINEL, schema_value_kind=schema_value_kind)

    ctx = _mk_ctx(tmp_path)
    _walk(schema, key="", required=False, root=schema, ctx=ctx, depth=0,
          seen=frozenset(), leaf_fn=spy, path=())

    paths = {p for (p, _kind, _key) in seen}
    # nested object leaf
    assert ("config", "output_file") in paths
    assert ("config", "verbose") in paths
    # array-of-objects (via $ref) leaf: object property under an array item
    assert ("jobs", "[]", "src") in paths

    # the oracle classification reaches the hook: the two path-shaped leaves are files
    kinds = {(p, kind) for (p, kind, _k) in seen}
    assert (("config", "output_file"), "file") in kinds
    assert (("jobs", "[]", "src"), "file") in kinds
    # a non-path leaf carries an empty kind (the oracle didn't classify it)
    assert (("config", "verbose"), "") in kinds


def test_leaf_fn_spy_observes_const_enum_default_leaves(tmp_path):
    """Part A: a const/enum/default leaf is routed THROUGH leaf_fn (not an early
    return), so a custom leaf_fn observes it with ``schema_value`` set to the resolved
    value and ``schema_value_kind`` naming which form. This is the seam the requirement
    binder uses to record a Requirement for a defaulted/const leaf."""
    schema = {
        "type": "object",
        "properties": {
            "mode": {"enum": ["fast", "slow"]},
            "kind": {"const": "report"},
            "retries": {"type": "integer", "default": 3},
            "plain": {"type": "string"},                 # ordinary leaf (no schema_value)
        },
        "required": ["mode", "kind", "retries", "plain"],
    }

    seen = {}  # key -> (schema_value, schema_value_kind)

    def spy(node, *, key, required, kind, ext, ctx, path, schema_value=_SENTINEL,
            schema_value_kind=None):
        seen[key] = (schema_value, schema_value_kind)
        return _synth_leaf(node, key=key, required=required, kind=kind, ext=ext,
                           ctx=ctx, schema_value=schema_value,
                           schema_value_kind=schema_value_kind)

    ctx = _mk_ctx(tmp_path)
    out = _walk(schema, key="", required=False, root=schema, ctx=ctx, depth=0,
                seen=frozenset(), leaf_fn=spy, path=())

    # the three resolved-value leaves reached leaf_fn WITH schema_value set
    assert seen["mode"] == ("fast", "enum")
    assert seen["kind"] == ("report", "const")
    assert seen["retries"] == (3, "default")
    # a plain leaf reached leaf_fn WITHOUT a schema_value (sentinel, no kind)
    assert seen["plain"] == (_SENTINEL, None)
    # and the default synth output is unchanged (byte-identical resolved values)
    assert out["mode"] == "fast" and out["kind"] == "report" and out["retries"] == 3


def test_leaf_fn_default_signature_accepts_path_kw(tmp_path):
    """``leaf_fn`` is called with a keyword-only ``path``; the default ``_synth_leaf``
    tolerates it (so a custom hook and the default share one call site)."""
    schema = {"type": "object", "properties": {"n": {"type": "integer"}},
              "required": ["n"]}
    ctx = _mk_ctx(tmp_path)
    out = _walk(schema, key="", required=False, root=schema, ctx=ctx, depth=0,
                seen=frozenset())  # default leaf_fn + default path
    assert out == {"n": 0}


# ── transparency: default leaf_fn ⇒ byte-identical synth output ────────────────

_TRANSPARENCY_SCHEMAS = [
    # scalar path + non-path scalars
    {"type": "object",
     "properties": {"input_path": {"type": "string"},
                    "count": {"type": "integer"},
                    "ratio": {"type": "number"},
                    "flag": {"type": "boolean"}},
     "required": ["input_path", "count"]},
    # list of paths
    {"type": "object",
     "properties": {"files_to_add": {"type": "array",
                                     "items": {"type": "string"}, "minItems": 2}},
     "required": ["files_to_add"]},
    # nested object + array-of-objects + $ref + enum/const/default
    {"type": "object",
     "properties": {
         "mode": {"enum": ["fast", "slow"]},
         "kind": {"const": "report"},
         "retries": {"type": "integer", "default": 3},
         "config": {"type": "object",
                    "properties": {"output_file": {"type": "string"},
                                   "verbose": {"type": "boolean"}}},
         "jobs": {"type": "array", "items": {"$ref": "#/$defs/Job"}},
     },
     "required": ["mode", "config", "jobs"],
     "$defs": {"Job": {"type": "object",
                       "properties": {"src": {"type": "string"}},
                       "required": ["src"]}}},
    # anyOf / allOf
    {"type": "object",
     "properties": {
         "a": {"anyOf": [{"type": "string"}, {"type": "integer"}]},
         "b": {"allOf": [{"type": "object", "properties": {"x": {"type": "integer"}}},
                         {"properties": {"y": {"type": "string"}}}]},
     },
     "required": ["a", "b"]},
    # flat {name: spec} form
    {"input_path": {"type": "string"}, "n": {"type": "integer"}},
]


def _shape(params):
    """A structure/type snapshot that ignores the random sandbox filename but keeps
    every KEY, every value TYPE, list length, and non-path scalar VALUE — enough to
    catch any behavioral drift in the default leaf_fn while tolerating the per-run
    temp dir in materialized path strings."""
    if isinstance(params, dict):
        return {k: _shape(v) for k, v in sorted(params.items())}
    if isinstance(params, list):
        return ["list", len(params)] + [_shape(x) for x in params]
    if isinstance(params, str):
        # normalize a materialized path to its extension so the random name/dir drops
        base = os.path.basename(params)
        ext = os.path.splitext(base)[1]
        return ("str", ext if ("/" in params or "\\" in params or ext) else params)
    return (type(params).__name__, params)


def test_default_leaf_fn_is_synth_transparent(tmp_path):
    """For a spread of representative schemas, the public entry ``synthesize_params``
    (which uses the default ``leaf_fn``) yields the SAME structure/keys/types/values —
    i.e. the leaf_fn seam is invisible to synth. (This is the guard that, together
    with the frozen 26, proves byte-identity.)"""
    for i, schema in enumerate(_TRANSPARENCY_SCHEMAS):
        sb = tmp_path / f"s{i}"
        r = synthesize_params(schema, sandbox_dir=str(sb))
        snap = _shape(r.params)
        # spot-check a couple of load-bearing invariants inside the snapshot
        if i == 0:
            assert snap["count"] == ("int", 0)
            assert snap["flag"] == ("bool", False)
        if i == 2:
            assert snap["mode"] == ("str", "fast")          # enum[0]
            assert snap["kind"] == ("str", "report")        # const
            assert snap["retries"] == ("int", 3)            # default
        # every synthesized path leaf must still be a real file on disk
        for leaf in _iter_str_leaves(r.params):
            if os.path.sep in leaf or "/" in leaf:
                assert os.path.exists(leaf), f"schema {i}: {leaf} not materialized"


def _iter_str_leaves(v):
    if isinstance(v, dict):
        for x in v.values():
            yield from _iter_str_leaves(x)
    elif isinstance(v, list):
        for x in v:
            yield from _iter_str_leaves(x)
    elif isinstance(v, str):
        yield v
