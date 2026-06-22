"""Tests for v0.6.0-a — Stage 1 capture intent extractor."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from sharing_on.analyzer.intent_extractor import (
    IntentExtraction,
    extract_intent,
    read_intent_json,
    write_intent_json,
)


# ─────────────────────────────────────────────────────────────────────────────
# IntentExtraction dataclass

class TestIntentExtraction:
    def test_is_usable_high(self):
        e = IntentExtraction(intent="document weather", confidence="high")
        assert e.is_usable is True

    def test_is_usable_medium(self):
        e = IntentExtraction(intent="something", confidence="medium")
        assert e.is_usable is True

    def test_is_usable_low_is_false(self):
        e = IntentExtraction(intent="x", confidence="low")
        assert e.is_usable is False

    def test_is_usable_empty_intent_is_false(self):
        e = IntentExtraction(intent="   ", confidence="high")
        assert e.is_usable is False


# ─────────────────────────────────────────────────────────────────────────────
# extract_intent — LLM mocked

class TestExtractIntent:
    def _fake_event(self, application=None, action=None, file_path=None, category=None, url=None):
        ev = MagicMock()
        ev.application = application
        ev.action = action
        ev.file_path = file_path
        ev.category = category
        ev.url = url
        ev.data = {}
        return ev

    def _fake_step(self, step_number, label=None, primary_app=None, event_summary=None):
        s = MagicMock()
        s.step_number = step_number
        s.label = label
        s.primary_app = primary_app
        s.event_summary = event_summary or {}
        return s

    def test_returns_low_confidence_when_no_steps_or_events(self):
        result = extract_intent(
            steps=[], events=[],
            session_name="x", platform_info="y",
            api_key="key",
        )
        assert result.confidence == "low"
        assert "no steps or events" in (result.error or "")
        assert not result.is_usable

    def test_happy_path_parses_llm_json(self):
        steps = [self._fake_step(1, event_summary={"file": 2})]
        events = [self._fake_event(application="Chrome")]

        fake_response = MagicMock()
        fake_response.choices = [MagicMock()]
        fake_response.choices[0].message.content = json.dumps({
            "intent": "Document current weather for personal reference",
            "expected_outcome": "A file containing today's weather data exists",
            "success_signal": "file exists with temp data",
            "abstracted_steps": [
                "Find current weather information",
                "Save it as a dated artifact",
            ],
            "confidence": "high",
        })

        with patch("sharing_on.analyzer.intent_extractor.OpenAI") as OpenAI_mock:
            client = MagicMock()
            client.chat.completions.create.return_value = fake_response
            OpenAI_mock.return_value = client

            result = extract_intent(
                steps=steps, events=events,
                session_name="weather", platform_info="windows",
                api_key="test-key",
            )

        assert result.intent == "Document current weather for personal reference"
        assert result.confidence == "high"
        assert result.is_usable is True
        assert len(result.abstracted_steps) == 2

    def test_strips_markdown_fences_from_response(self):
        steps = [self._fake_step(1)]
        events = []

        fake_response = MagicMock()
        fake_response.choices = [MagicMock()]
        fake_response.choices[0].message.content = (
            "```json\n"
            + json.dumps({
                "intent": "x", "expected_outcome": "y", "success_signal": "z",
                "abstracted_steps": [], "confidence": "high",
            })
            + "\n```"
        )

        with patch("sharing_on.analyzer.intent_extractor.OpenAI") as OpenAI_mock:
            client = MagicMock()
            client.chat.completions.create.return_value = fake_response
            OpenAI_mock.return_value = client

            result = extract_intent(
                steps=steps, events=events,
                session_name="s", platform_info="p",
                api_key="k",
            )

        assert result.intent == "x"
        assert result.confidence == "high"

    def test_invalid_json_returns_low_confidence(self):
        steps = [self._fake_step(1)]
        events = []

        fake_response = MagicMock()
        fake_response.choices = [MagicMock()]
        fake_response.choices[0].message.content = "not json at all"

        with patch("sharing_on.analyzer.intent_extractor.OpenAI") as OpenAI_mock:
            client = MagicMock()
            client.chat.completions.create.return_value = fake_response
            OpenAI_mock.return_value = client

            result = extract_intent(
                steps=steps, events=events,
                session_name="s", platform_info="p",
                api_key="k",
            )

        assert result.confidence == "low"
        assert "parse_error" in (result.error or "")

    def test_llm_failure_returns_low_confidence(self):
        steps = [self._fake_step(1)]
        events = []

        with patch("sharing_on.analyzer.intent_extractor.OpenAI") as OpenAI_mock:
            client = MagicMock()
            client.chat.completions.create.side_effect = RuntimeError("api down")
            OpenAI_mock.return_value = client

            result = extract_intent(
                steps=steps, events=events,
                session_name="s", platform_info="p",
                api_key="k",
            )

        assert result.confidence == "low"
        assert "api down" in (result.error or "")
        assert not result.is_usable


# ─────────────────────────────────────────────────────────────────────────────
# Persistence round-trip

class TestPersistence:
    def test_write_then_read(self, tmp_path):
        original = IntentExtraction(
            intent="capture weather",
            expected_outcome="dated weather doc exists",
            success_signal="file at D:/Weather Status/Weather on 13.docx",
            abstracted_steps=["Find weather", "Save it"],
            confidence="high",
        )
        write_intent_json(original, tmp_path)
        assert (tmp_path / "intent.json").exists()

        loaded = read_intent_json(tmp_path)
        assert loaded is not None
        assert loaded.intent == original.intent
        assert loaded.confidence == "high"
        assert loaded.abstracted_steps == original.abstracted_steps

    def test_read_missing_returns_none(self, tmp_path):
        assert read_intent_json(tmp_path) is None

    def test_write_drops_error_field_on_success(self, tmp_path):
        e = IntentExtraction(
            intent="x", confidence="high", error="leftover-noise",
        )
        write_intent_json(e, tmp_path)
        raw = json.loads((tmp_path / "intent.json").read_text(encoding="utf-8"))
        assert "error" not in raw   # success path strips it

    def test_write_keeps_error_on_low_confidence(self, tmp_path):
        e = IntentExtraction(intent="x", confidence="low", error="api_down")
        write_intent_json(e, tmp_path)
        raw = json.loads((tmp_path / "intent.json").read_text(encoding="utf-8"))
        assert raw.get("error") == "api_down"


# ─────────────────────────────────────────────────────────────────────────────
# Generator integration — intent surfaces in the prompt

class TestGeneratorIntegration:
    def test_intent_block_appears_in_user_prompt(self):
        from sharing_on.analyzer.generator import generate_instructions

        intent = IntentExtraction(
            intent="document current weather",
            expected_outcome="a dated weather doc exists",
            success_signal="weather file written",
            abstracted_steps=["find weather", "save it"],
            confidence="high",
        )

        fake_response = MagicMock()
        fake_response.choices = [MagicMock()]
        fake_response.choices[0].message.content = "# Mocked instructions"
        fake_response.usage = None

        captured_messages = []

        with patch("sharing_on.analyzer.generator.OpenAI") as OpenAI_mock:
            client = MagicMock()

            def capture(**kwargs):
                captured_messages.extend(kwargs.get("messages", []))
                return fake_response

            client.chat.completions.create.side_effect = capture
            OpenAI_mock.return_value = client

            fake_step = MagicMock()
            fake_step.step_number = 1
            fake_step.label = None
            fake_step.primary_app = "Chrome"
            fake_step.start_time = None
            fake_step.duration_seconds = 1.0
            fake_step.events = []

            generate_instructions(
                steps=[fake_step],
                session_name="weather", platform_info="windows",
                duration_seconds=10.0, api_key="k",
                intent=intent,
            )

        # User prompt should contain the intent block
        user_msg = next(m for m in captured_messages if m["role"] == "user")
        assert "Pre-Inferred User Intent" in user_msg["content"]
        assert "document current weather" in user_msg["content"]

    def test_no_intent_block_when_intent_is_none(self):
        from sharing_on.analyzer.generator import generate_instructions

        fake_response = MagicMock()
        fake_response.choices = [MagicMock()]
        fake_response.choices[0].message.content = "# Mocked"
        fake_response.usage = None

        captured = []

        with patch("sharing_on.analyzer.generator.OpenAI") as OpenAI_mock:
            client = MagicMock()
            client.chat.completions.create.side_effect = lambda **kw: (
                captured.extend(kw.get("messages", [])) or fake_response
            )
            OpenAI_mock.return_value = client

            fake_step = MagicMock()
            fake_step.step_number = 1
            fake_step.label = None
            fake_step.primary_app = "Chrome"
            fake_step.start_time = None
            fake_step.duration_seconds = 1.0
            fake_step.events = []

            generate_instructions(
                steps=[fake_step],
                session_name="x", platform_info="y",
                duration_seconds=1.0, api_key="k",
                intent=None,
            )

        user_msg = next(m for m in captured if m["role"] == "user")
        assert "Pre-Inferred User Intent" not in user_msg["content"]


# ─────────────────────────────────────────────────────────────────────────────
# Markdown integration — ## Intent block appears

class TestMarkdownIntegration:
    def test_intent_block_rendered(self, tmp_path):
        from sharing_on.output.markdown import render_markdown

        intent = IntentExtraction(
            intent="capture weather data",
            expected_outcome="weather doc on disk",
            success_signal="file exists",
            abstracted_steps=["find weather", "save it"],
            confidence="high",
        )

        output = render_markdown(
            instructions="# Body",
            steps=[],
            session_name="t",
            session_id="sess-1",
            platform_info="windows",
            start_time=None,
            end_time=None,
            output_dir=tmp_path,
            event_count=0,
            intent=intent,
        )

        text = output.read_text(encoding="utf-8")
        assert "## Intent" in text
        assert "capture weather data" in text
        assert "find weather" in text
        assert "Inference confidence: **high**" in text

    def test_no_intent_block_when_intent_none(self, tmp_path):
        from sharing_on.output.markdown import render_markdown

        output = render_markdown(
            instructions="# Body",
            steps=[],
            session_name="t",
            session_id="sess-2",
            platform_info="windows",
            start_time=None,
            end_time=None,
            output_dir=tmp_path,
            event_count=0,
            intent=None,
        )

        text = output.read_text(encoding="utf-8")
        assert "## Intent" not in text
