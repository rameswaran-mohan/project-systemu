"""v0.9.7 Phase 0b — executor parameter handling.

Guards the two real bugs the triage found:
(A) a non-dict tool-call ``parameters`` (bare scalar) must not crash the loop;
(B) the v1 tool_index must surface the tool's REAL parameters_schema (not {}),
    so the LLM emits correctly-shaped args in the first place.
"""
from types import SimpleNamespace


def test_coerce_single_param_tool_wraps_scalar():
    from systemu.runtime.shadow_runtime import _coerce_scalar_parameter
    tools = [SimpleNamespace(name="fetch_json", parameter_names=["url"], parameters_schema={"url": {}})]
    out = _coerce_scalar_parameter("http://ip-api.com/json/", "fetch_json", tools)
    assert out == {"url": "http://ip-api.com/json/"}


def test_coerce_multi_param_tool_returns_empty():
    from systemu.runtime.shadow_runtime import _coerce_scalar_parameter
    tools = [SimpleNamespace(name="write_file", parameter_names=["path", "content"], parameters_schema={})]
    assert _coerce_scalar_parameter("oops", "write_file", tools) == {}


def test_coerce_falls_back_to_schema_keys_when_no_param_names():
    from systemu.runtime.shadow_runtime import _coerce_scalar_parameter
    tools = [SimpleNamespace(name="t", parameter_names=[], parameters_schema={"q": {"type": "string"}})]
    assert _coerce_scalar_parameter("hello", "t", tools) == {"q": "hello"}


def test_coerce_unknown_tool_returns_empty():
    from systemu.runtime.shadow_runtime import _coerce_scalar_parameter
    assert _coerce_scalar_parameter("x", "nonexistent", []) == {}


def test_tool_index_no_longer_hardcodes_empty_schema():
    """Regression guard for bug B: neither v1 catalog builder may ship a
    hardcoded empty parameters_schema."""
    import inspect, systemu.runtime.shadow_runtime as sr
    src = inspect.getsource(sr)
    assert '"parameters_schema": {}' not in src, (
        "v1 tool catalog must surface the tool's real parameters_schema, "
        "not a hardcoded {} (left the executor LLM blind to params)."
    )
    # And the real-schema expression must be present in both builders.
    assert src.count('"parameters_schema": dict(getattr(t, "parameters_schema"') >= 2
