"""v0.7-f: tool registry discovers plugins from `plugins/` dir + entry-points."""
import sys
from pathlib import Path
from unittest.mock import MagicMock


def test_discover_plugins_from_dir(tmp_path, monkeypatch):
    plugins_root = tmp_path / "plugins"
    plugins_root.mkdir()
    pkg = plugins_root / "myplugin"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(
        "def register_tools(registry):\n"
        "    registry.register({'name': 'myplugin.hello', 'fn': lambda **kw: {'success': True}})\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    from systemu.runtime.tool_registry import _discover_plugin_tools
    registry = MagicMock()
    _discover_plugin_tools(registry, plugins_root=plugins_root)
    registry.register.assert_called_once()
    args, _ = registry.register.call_args
    assert args[0]["name"] == "myplugin.hello"


def test_discover_plugins_handles_missing_register_callable(tmp_path, monkeypatch):
    """A plugin without a register_tools() should not crash the loader."""
    plugins_root = tmp_path / "plugins"
    plugins_root.mkdir()
    pkg = plugins_root / "broken"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("# no register_tools function\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))

    from systemu.runtime.tool_registry import _discover_plugin_tools
    registry = MagicMock()
    _discover_plugin_tools(registry, plugins_root=plugins_root)  # must not raise
    registry.register.assert_not_called()


def test_discover_plugins_missing_dir_is_noop():
    from systemu.runtime.tool_registry import _discover_plugin_tools
    registry = MagicMock()
    _discover_plugin_tools(registry, plugins_root=Path("/does/not/exist"))
    registry.register.assert_not_called()


def test_discover_plugins_isolates_failures(tmp_path, monkeypatch):
    """One plugin raising an exception during register_tools must NOT
    prevent later plugins from being loaded."""
    plugins_root = tmp_path / "plugins"
    plugins_root.mkdir()

    bad = plugins_root / "bad"
    bad.mkdir()
    (bad / "__init__.py").write_text(
        "def register_tools(registry):\n"
        "    raise RuntimeError('boom')\n",
        encoding="utf-8",
    )

    good = plugins_root / "good"
    good.mkdir()
    (good / "__init__.py").write_text(
        "def register_tools(registry):\n"
        "    registry.register({'name': 'good.tool'})\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    from systemu.runtime.tool_registry import _discover_plugin_tools
    registry = MagicMock()
    _discover_plugin_tools(registry, plugins_root=plugins_root)
    # Good plugin must still register despite bad's failure
    names = [c.args[0]["name"] for c in registry.register.call_args_list]
    assert "good.tool" in names
