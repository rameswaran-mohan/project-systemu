"""v0.9.8 minimal Phase 3 — activity-extraction robustness. Malformed LLM output
(non-dict result, string specs) must not crash, and an empty extraction must fall
back to the curated toolset instead of silently aborting the run."""
import inspect

from systemu.pipelines import activity_extractor as ae


def test_upsert_tool_skips_non_dict():
    assert ae._upsert_tool("not-a-dict", None) == ("", False)
    assert ae._upsert_tool(123, None) == ("", False)
    assert ae._upsert_tool(["a", "b"], None) == ("", False)


def test_upsert_skill_skips_non_dict():
    assert ae._upsert_skill("not-a-dict", "scroll_x", None) == ""
    assert ae._upsert_skill(None, "scroll_x", None) == ""


def test_extract_guards_nondict_result_and_empty_fallback():
    src = inspect.getsource(ae.extract_and_process)
    # non-dict LLM result is coerced (prevents 'str' has no .get)
    assert "if not isinstance(result, dict)" in src
    # empty extraction falls back to the curated toolset instead of return None
    assert "not any(skill_ids)" in src
    assert "curated" in src.lower()
    assert 'Path(vlt.root) / "tools" / "index.json"' in src
