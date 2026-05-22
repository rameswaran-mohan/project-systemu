import pytest
from unittest.mock import MagicMock, patch

pytest.importorskip("browser_use", reason="browser-use extra not installed")


def test_register_tools_registers_four_tools():
    from plugins.browser_use_wrapper import register_tools
    registry = MagicMock()
    register_tools(registry)
    assert registry.register.call_count == 4
    names = {c.args[0]["name"] for c in registry.register.call_args_list}
    assert {
        "browser_use_wrapper.web_navigate",
        "browser_use_wrapper.web_extract_text",
        "browser_use_wrapper.web_click",
        "browser_use_wrapper.web_fill_form",
    } <= names


def test_web_navigate_returns_success_shape():
    from plugins.browser_use_wrapper import web_navigate
    with patch("plugins.browser_use_wrapper._run_browser_use",
               return_value={"title": "Google", "url": "https://google.com"}):
        result = web_navigate(url="https://google.com")
    assert result["success"] is True
    assert result.get("title") == "Google"
    assert "output_path" in result


def test_web_navigate_returns_failure_shape_on_exception():
    from plugins.browser_use_wrapper import web_navigate
    with patch("plugins.browser_use_wrapper._run_browser_use",
               side_effect=RuntimeError("nav failed")):
        result = web_navigate(url="https://example.com")
    assert result["success"] is False
    assert "nav failed" in result["error"]


def test_web_extract_text_returns_text_field():
    from plugins.browser_use_wrapper import web_extract_text
    with patch("plugins.browser_use_wrapper._run_browser_use",
               return_value={"text": "hello world"}):
        result = web_extract_text(url="https://example.com")
    assert result["success"] is True
    assert result["text"] == "hello world"
