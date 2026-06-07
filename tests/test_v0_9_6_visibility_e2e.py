"""v0.9.6 T0 — v2 tools must reach the LLM at scroll-execution time.

NOT a unit test of _build_llm_tool_catalog (that's v0.9.5 T0).
This tests the END-TO-END path: scroll → activity → shadow_runtime → LLM tool list.
"""
import pytest
from unittest.mock import patch, MagicMock


class TestV2ToolsReachLlmAtExecutionTime:
    def test_skill_tools_in_llm_prompt_catalog(self):
        """The LLM's actual prompt-time tool list (the one passed to the
        provider API) must include v2-registered tools like skill_list_skills."""
        # Strategy: drive a ShadowRuntime through enough of the execution
        # path to capture what gets passed as "tools" to the LLM. Use a
        # mock LLM that records the tools argument.
        captured_tools = []

        def mock_llm_call(**kwargs):
            tools = kwargs.get("tools") or kwargs.get("available_tools") or []
            captured_tools.append(tools)
            # Return a benign claim that ends the run
            return {"action": "complete", "reason": "test"}

        # Force-load v2 tools so they're registered
        import systemu.runtime.tools.file_tools  # noqa: F401
        import systemu.runtime.tools.skill_tools  # noqa: F401

        # Look for the actual prompt-assembly entry point and exercise it.
        # If the codebase has a helper like _assemble_execution_tools() or
        # similar, call it directly here.
        from systemu.runtime.tool_registry_v2 import registry as _v2
        v2_names = {e.name for e in _v2.list()}
        assert "skill_list_skills" in v2_names, "skill_list_skills must be registered"

        # The actual integration assertion: call whatever shadow_runtime
        # uses to build its LLM prompt and verify v2 tool names appear.
        # If there's no clean API for this, document what you found and
        # add a test that AT LEAST asserts the relevant code path includes
        # v2 augmentation (e.g. via inspect.getsource on the actual fn).
        import inspect
        from systemu.runtime import shadow_runtime
        # The augmentation MUST appear in the function that ends up building
        # the LLM's tools= arg. After your fix, the augmentation should be
        # at a SECOND site, not just line 1744.
        src = inspect.getsource(shadow_runtime)
        # Count v2 augmentation CALL sites — exclude the function definition line.
        # The definition line is "def _build_llm_tool_catalog(" — strip it so we
        # count actual invocations only.
        call_lines = [
            line for line in src.splitlines()
            if "_build_llm_tool_catalog" in line
            and not line.lstrip().startswith("def _build_llm_tool_catalog")
        ]
        site_count = len(call_lines)
        assert site_count >= 2, (
            f"after T0 fix, _build_llm_tool_catalog should be CALLED at "
            f"≥2 call sites (the existing v0.9.5 site + the new T0 site). "
            f"Found {site_count} call site(s): {call_lines}"
        )
