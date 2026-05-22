"""— Stage 2 GUI-verb post-processor + re-prompt loop."""
from __future__ import annotations

from unittest.mock import MagicMock

from systemu.core.models import Objective


def _obj(gid: int, goal: str) -> Objective:
    return Objective(id=gid, goal=goal, success_criteria="...")


class TestDetectGuiCodification:
    def test_screenshot_verb_detected(self):
        from systemu.pipelines.scroll_refiner import detect_gui_codification
        out = detect_gui_codification([
            _obj(1, "capture screenshot of weather"),
        ])
        assert len(out) == 1
        assert out[0][0] == 1

    def test_snipping_tool_detected(self):
        from systemu.pipelines.scroll_refiner import detect_gui_codification
        out = detect_gui_codification([
            _obj(1, "use Snipping Tool to grab a region"),
        ])
        assert len(out) == 1

    def test_word_app_name_detected(self):
        from systemu.pipelines.scroll_refiner import detect_gui_codification
        out = detect_gui_codification([
            _obj(1, "open Word and paste content"),
        ])
        assert len(out) == 1

    def test_docx_extension_detected(self):
        from systemu.pipelines.scroll_refiner import detect_gui_codification
        out = detect_gui_codification([
            _obj(1, "save the .docx file to D:"),
        ])
        assert len(out) == 1

    def test_clean_objective_passes(self):
        from systemu.pipelines.scroll_refiner import detect_gui_codification
        out = detect_gui_codification([
            _obj(1, "Fetch current weather data for London"),
            _obj(2, "Persist the data as a structured report"),
        ])
        assert out == []

    def test_mixed_returns_only_offenders(self):
        from systemu.pipelines.scroll_refiner import detect_gui_codification
        out = detect_gui_codification([
            _obj(1, "Fetch weather data"),
            _obj(2, "capture screenshot"),
            _obj(3, "save report"),
        ])
        assert len(out) == 1
        assert out[0][0] == 2

    def test_dict_input_works(self):
        """Detector should also accept raw dicts (from LLM output)."""
        from systemu.pipelines.scroll_refiner import detect_gui_codification
        out = detect_gui_codification([
            {"id": 1, "goal": "click on the Submit button"},
            {"id": 2, "goal": "Submit the form"},
        ])
        assert len(out) == 1
        assert out[0][0] == 1


class TestReprompLoop:
    """The re-prompt loop fires once when detection succeeds; emits warn if
    the rewrite still contains GUI codification."""

    def test_reprompt_called_once_on_detection(self, monkeypatch):
        from systemu.pipelines import scroll_refiner as sr

        # First LLM call returns GUI-codified objectives
        # Second LLM call (the re-prompt) returns clean ones
        call_count = [0]
        def fake_llm(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return {"objectives": [
                    {"id": 1, "goal": "capture screenshot of weather",
                     "success_criteria": "image saved"},
                ]}
            return {"objectives": [
                {"id": 1, "goal": "Fetch and persist current weather data",
                 "success_criteria": "data stored"},
            ]}

        monkeypatch.setattr(sr, "llm_call_json", fake_llm)

        result = sr._refine_with_gui_guard(
            payload={"some": "input"},
            prompt_text="ignored",
            config=MagicMock(),
        )
        assert call_count[0] == 2
        # final objectives are clean
        assert "screenshot" not in result["objectives"][0]["goal"].lower()
        # gui guard metadata recorded
        guard = result.get("_v065_gui_guard")
        assert guard is not None
        assert guard["first_pass_offenders"]
        assert guard["second_pass_offenders"] == []

    def test_no_reprompt_when_clean_first_time(self, monkeypatch):
        from systemu.pipelines import scroll_refiner as sr

        call_count = [0]
        def fake_llm(*args, **kwargs):
            call_count[0] += 1
            return {"objectives": [
                {"id": 1, "goal": "Fetch weather data",
                 "success_criteria": "data stored"},
            ]}

        monkeypatch.setattr(sr, "llm_call_json", fake_llm)
        result = sr._refine_with_gui_guard(
            payload={"x": 1}, prompt_text="ignored", config=MagicMock(),
        )
        assert call_count[0] == 1
        # No guard metadata when no rewrite needed
        assert result.get("_v065_gui_guard") is None

    def test_retry_still_dirty_emits_metadata(self, monkeypatch):
        """If LLM still returns GUI codification after retry, record second_pass_offenders."""
        from systemu.pipelines import scroll_refiner as sr

        call_count = [0]
        def fake_llm(*args, **kwargs):
            call_count[0] += 1
            # Both passes return GUI-codified
            return {"objectives": [
                {"id": 1, "goal": "take screenshot of weather",
                 "success_criteria": "image saved"},
            ]}

        monkeypatch.setattr(sr, "llm_call_json", fake_llm)
        result = sr._refine_with_gui_guard(
            payload={"x": 1}, prompt_text="ignored", config=MagicMock(),
        )
        assert call_count[0] == 2  # one retry
        guard = result["_v065_gui_guard"]
        assert guard["first_pass_offenders"]
        assert guard["second_pass_offenders"]
