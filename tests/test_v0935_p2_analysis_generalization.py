"""Phase 2 (v0.9.35) — generalization-aware analysis."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from sharing_on.analyzer.intent_extractor import (
    IntentExtraction,
    extract_intent,
)


def _fake_step(n, label=None, event_summary=None):
    s = MagicMock()
    s.step_number = n
    s.label = label
    s.primary_app = None
    s.event_summary = event_summary or {}
    return s


def _fake_event(application=None):
    ev = MagicMock()
    ev.application = application
    ev.action = None
    ev.file_path = None
    ev.category = None
    ev.url = None
    ev.data = {}
    return ev


def _run(generalization=None, content=None):
    steps = [_fake_step(1, event_summary={"file": 1})]
    events = [_fake_event(application="Chrome")]
    fake = MagicMock()
    fake.choices = [MagicMock()]
    fake.choices[0].message.content = content or json.dumps({
        "intent": "i", "expected_outcome": "e", "success_signal": "s",
        "abstracted_steps": [], "confidence": "high", "parameters": [],
    })
    with patch("sharing_on.analyzer.intent_extractor.OpenAI") as OpenAI_mock:
        client = MagicMock()
        client.chat.completions.create.return_value = fake
        OpenAI_mock.return_value = client
        kwargs = {} if generalization is None else {"generalization": generalization}
        result = extract_intent(
            steps=steps, events=events,
            session_name="weather", platform_info="windows",
            api_key="k", **kwargs,
        )
        call = client.chat.completions.create.call_args
    return result, call


class TestStandardInvariance:
    def test_default_generalization_is_standard(self):
        result, _ = _run()
        assert result.generalization == "standard"

    def test_standard_messages_byte_identical_to_default(self):
        _, call_default = _run()
        _, call_standard = _run(generalization="standard")
        assert call_default.kwargs["messages"] == call_standard.kwargs["messages"]

    def test_standard_appends_no_mode_directive(self):
        _, call = _run(generalization="standard")
        system_msg = call.kwargs["messages"][0]["content"]
        assert "GENERALIZATION MODE" not in system_msg

    def test_broad_appends_mode_directive(self):
        _, call = _run(generalization="broad")
        system_msg = call.kwargs["messages"][0]["content"]
        assert "GENERALIZATION MODE" in system_msg
        assert "broad" in system_msg.lower()

    def test_invalid_mode_coerces_to_standard(self):
        result, call = _run(generalization="garbage")
        assert result.generalization == "standard"
        system_msg = call.kwargs["messages"][0]["content"]
        assert "GENERALIZATION MODE" not in system_msg


class TestParameterParsing:
    def test_broad_parses_parameters_with_captured_default(self):
        content = json.dumps({
            "intent": "Look up the index value for a market",
            "expected_outcome": "e", "success_signal": "s",
            "abstracted_steps": [], "confidence": "high",
            "parameters": [
                {"name": "market", "description": "Which market index",
                 "type": "string", "default": "NYSE", "salient_kind": "value",
                 "required": True},
            ],
        })
        result, _ = _run(generalization="broad", content=content)
        assert result.generalization == "broad"
        assert len(result.parameters) == 1
        p = result.parameters[0]
        assert p["name"] == "market"
        assert p["default"] == "NYSE"
        assert p["salient_kind"] == "value"
        assert p["required"] is True          # pinned-contract default
        assert p["type"] == "string"

    def test_param_defaults_to_required_true_when_omitted(self):
        content = json.dumps({
            "intent": "i", "expected_outcome": "e", "success_signal": "s",
            "abstracted_steps": [], "confidence": "high",
            "parameters": [{"name": "site", "default": "example.com"}],
        })
        result, _ = _run(generalization="broad", content=content)
        assert result.parameters[0]["required"] is True
        assert result.parameters[0]["type"] == "string"     # default type

    def test_invalid_type_coerces_to_string(self):
        content = json.dumps({
            "intent": "i", "expected_outcome": "e", "success_signal": "s",
            "abstracted_steps": [], "confidence": "high",
            "parameters": [{"name": "n", "type": "datetime", "default": "x"}],
        })
        result, _ = _run(generalization="broad", content=content)
        assert result.parameters[0]["type"] == "string"

    def test_param_without_name_is_dropped(self):
        content = json.dumps({
            "intent": "i", "expected_outcome": "e", "success_signal": "s",
            "abstracted_steps": [], "confidence": "high",
            "parameters": [{"default": "orphan"}, {"name": "ok", "default": "v"}],
        })
        result, _ = _run(generalization="broad", content=content)
        assert [p["name"] for p in result.parameters] == ["ok"]

    def test_standard_ignores_parameters_even_if_model_emits_them(self):
        content = json.dumps({
            "intent": "i", "expected_outcome": "e", "success_signal": "s",
            "abstracted_steps": [], "confidence": "high",
            "parameters": [{"name": "leak", "default": "v"}],
        })
        result, _ = _run(generalization="standard", content=content)
        assert result.parameters == []

    def test_narrow_ignores_parameters(self):
        content = json.dumps({
            "intent": "i", "expected_outcome": "e", "success_signal": "s",
            "abstracted_steps": [], "confidence": "high",
            "parameters": [{"name": "leak", "default": "v"}],
        })
        result, _ = _run(generalization="narrow", content=content)
        assert result.parameters == []

    def test_broad_default_value_is_redacted(self):
        content = json.dumps({
            "intent": "i", "expected_outcome": "e", "success_signal": "s",
            "abstracted_steps": [], "confidence": "high",
            "parameters": [{"name": "recipient", "default": "alice@example.com"}],
        })
        result, _ = _run(generalization="broad", content=content)
        assert result.parameters[0]["default"] == "[EMAIL_REDACTED]"


class TestIntentJsonRoundTrip:
    def test_write_then_read_carries_generalization_and_params(self, tmp_path):
        from sharing_on.analyzer.intent_extractor import (
            write_intent_json, read_intent_json,
        )
        e = IntentExtraction(
            intent="Look up index for a market",
            expected_outcome="e", success_signal="s",
            confidence="high", generalization="broad",
            parameters=[{
                "name": "market", "description": "which market",
                "type": "string", "default": "NYSE",
                "salient_kind": "value", "required": True,
            }],
        )
        write_intent_json(e, tmp_path)
        data = json.loads((tmp_path / "intent.json").read_text(encoding="utf-8"))
        assert data["generalization"] == "broad"
        assert data["parameters"][0]["name"] == "market"

        back = read_intent_json(tmp_path)
        assert back is not None
        assert back.generalization == "broad"
        assert back.parameters[0]["default"] == "NYSE"
        assert back.parameters[0]["required"] is True

    def test_read_legacy_intent_json_defaults_to_standard(self, tmp_path):
        from sharing_on.analyzer.intent_extractor import read_intent_json
        (tmp_path / "intent.json").write_text(json.dumps({
            "intent": "old", "expected_outcome": "e", "success_signal": "s",
            "abstracted_steps": [], "confidence": "high",
        }), encoding="utf-8")
        back = read_intent_json(tmp_path)
        assert back.generalization == "standard"
        assert back.parameters == []


class TestIntentMarkdownBlock:
    def _intent(self, **kw):
        from sharing_on.analyzer.intent_extractor import IntentExtraction
        base = dict(intent="i", expected_outcome="e", success_signal="s",
                    confidence="high")
        base.update(kw)
        return IntentExtraction(**base)

    def test_standard_block_has_no_parameters_section(self):
        from sharing_on.output.markdown import _render_intent_block
        md = _render_intent_block(self._intent(generalization="standard"))
        assert "### Parameters" not in md
        assert "**Generalization:**" not in md  # standard stays silent

    def test_broad_block_renders_parameters(self):
        from sharing_on.output.markdown import _render_intent_block
        md = _render_intent_block(self._intent(
            generalization="broad",
            parameters=[{
                "name": "market", "description": "which market",
                "type": "string", "default": "NYSE",
                "salient_kind": "value", "required": True,
            }],
        ))
        assert "### Parameters" in md
        assert "`market`" in md
        assert "NYSE" in md            # captured value visible as default
        assert "string" in md
        assert "**Generalization:** broad" in md

    def test_broad_with_no_params_still_notes_mode(self):
        from sharing_on.output.markdown import _render_intent_block
        md = _render_intent_block(self._intent(generalization="broad", parameters=[]))
        assert "**Generalization:** broad" in md
        assert "### Parameters" not in md


class _FakeConfig:
    openrouter_api_key = "k"
    openrouter_base_url = "https://openrouter.ai/api/v1"
    tier2_model = "m"
    tier3_model = "m"


class TestAnalyzeCallSitesThreadGeneralization:
    def test_sharing_cli_passes_generalization_from_meta(self, monkeypatch):
        import sharing_on.analyzer.intent_extractor as ie
        captured = {}

        def fake_extract(**kwargs):
            captured.update(kwargs)
            return ie.IntentExtraction(intent="i", confidence="high",
                                       generalization=kwargs.get("generalization", "standard"))

        # The cli imports extract_intent locally inside the command; patch the
        # source symbol so the local import binds the fake.
        monkeypatch.setattr(ie, "extract_intent", fake_extract)
        meta = {"name": "s", "platform": "p", "generalization": "broad"}
        # helper under test (added in Step 3): pulls the key + calls extract_intent
        from sharing_on.cli import _extract_intent_for_meta
        result = _extract_intent_for_meta(meta, steps=[object()], events=[],
                                          config=_FakeConfig())
        assert captured["generalization"] == "broad"
        assert result.generalization == "broad"

    def test_missing_key_defaults_standard(self, monkeypatch):
        import sharing_on.analyzer.intent_extractor as ie
        captured = {}
        monkeypatch.setattr(ie, "extract_intent",
                            lambda **k: (captured.update(k) or
                                         ie.IntentExtraction(intent="i", confidence="high")))
        from sharing_on.cli import _extract_intent_for_meta
        _extract_intent_for_meta({"name": "s"}, steps=[object()], events=[],
                                 config=_FakeConfig())
        assert captured["generalization"] == "standard"


class TestScrollRefinerParameterMapping:
    def test_build_scroll_parameters_from_result(self):
        from systemu.pipelines.scroll_refiner import _build_scroll_parameters
        from systemu.core.models import ScrollParameter
        params = _build_scroll_parameters({
            "generalization": "broad",
            "parameters": [
                {"name": "market", "description": "which market",
                 "type": "string", "default": "NYSE",
                 "salient_kind": "value", "required": True},
            ],
        })
        assert len(params) == 1
        assert isinstance(params[0], ScrollParameter)
        assert params[0].name == "market"
        assert params[0].default == "NYSE"
        assert params[0].required is True

    def test_nameless_param_dropped(self):
        from systemu.pipelines.scroll_refiner import _build_scroll_parameters
        params = _build_scroll_parameters({
            "generalization": "broad",
            "parameters": [{"default": "x"}, {"name": "ok", "default": "v"}],
        })
        assert [p.name for p in params] == ["ok"]

    def test_standard_yields_no_params(self):
        from systemu.pipelines.scroll_refiner import _build_scroll_parameters
        params = _build_scroll_parameters({
            "generalization": "standard",
            "parameters": [{"name": "leak", "default": "v"}],
        })
        assert params == []

    def test_missing_keys_are_safe(self):
        from systemu.pipelines.scroll_refiner import _build_scroll_parameters
        assert _build_scroll_parameters({}) == []

    def test_generalization_normalised(self):
        from systemu.pipelines.scroll_refiner import _coerce_scroll_generalization
        assert _coerce_scroll_generalization("broad") == "broad"
        assert _coerce_scroll_generalization("garbage") is None
        assert _coerce_scroll_generalization("standard") is None  # None == standard
        assert _coerce_scroll_generalization(None) is None


class TestEndToEndAnalysisFlow:
    def test_broad_capture_value_reaches_scroll_parameter(self):
        # 1) extract (broad) — mocked LLM emits a lifted param
        content = json.dumps({
            "intent": "Look up the index value for a market",
            "expected_outcome": "data exists", "success_signal": "file exists",
            "abstracted_steps": ["Obtain the index value"],
            "confidence": "high",
            "parameters": [{"name": "market", "description": "which market",
                            "type": "string", "default": "NYSE",
                            "salient_kind": "value", "required": True}],
        })
        result, _ = _run(generalization="broad", content=content)

        # 2) render the intent block, then prove the refiner-side projection
        #    reads back the same captured value (simulating the refiner LLM
        #    faithfully echoing the parameters block).
        from sharing_on.output.markdown import _render_intent_block
        md = _render_intent_block(result)
        assert "NYSE" in md

        from systemu.pipelines.scroll_refiner import _build_scroll_parameters
        params = _build_scroll_parameters({
            "generalization": result.generalization,
            "parameters": result.parameters,
        })
        assert params[0].name == "market"
        assert params[0].default == "NYSE"
        assert params[0].required is True

    def test_standard_capture_produces_no_scroll_parameters(self):
        content = json.dumps({
            "intent": "i", "expected_outcome": "e", "success_signal": "s",
            "abstracted_steps": [], "confidence": "high",
            "parameters": [{"name": "leak", "default": "v"}],
        })
        result, _ = _run(generalization="standard", content=content)
        from systemu.pipelines.scroll_refiner import _build_scroll_parameters
        assert _build_scroll_parameters(
            {"generalization": "standard", "parameters": result.parameters}
        ) == []


class TestModeDirectiveEncodesConfirmedBehavior:
    """v0.9.35 P2 review fix: the broad/narrow prompt directives must encode the
    operator-confirmed salient-only rule + the canonical amazon example, and the
    broad directive must NOT invite lifting incidental values."""

    def test_broad_directive_is_salient_only_with_canonical_example(self):
        from sharing_on.analyzer.intent_extractor import _mode_directive
        dl = _mode_directive("broad").lower()
        # canonical what/where example is encoded
        assert "product" in dl and "site" in dl
        assert "samsung galaxy s24" in dl and "amazon.com" in dl
        # explicit don't-lift guidance for incidental values
        assert "do not lift" in dl
        for incidental in ("quantit", "timestamp", "date", "keystroke",
                           "scroll", "search-box", "session id"):
            assert incidental in dl, f"broad directive should warn off {incidental!r}"
        # the old over-lifting phrasing must be gone
        assert "a date, a numeric value" not in dl

    def test_narrow_directive_generalizes_instance_to_category(self):
        from sharing_on.analyzer.intent_extractor import _mode_directive
        dl = _mode_directive("narrow").lower()
        assert "categor" in dl                      # category generalization
        assert "phone" in dl and "amazon" in dl     # canonical narrow example
        assert "generaliz" in dl                    # the exact instance is generalized
        assert "empty array" in dl                  # no params in narrow

    def test_standard_directive_is_empty_noop(self):
        from sharing_on.analyzer.intent_extractor import _mode_directive
        assert _mode_directive("standard") == ""
