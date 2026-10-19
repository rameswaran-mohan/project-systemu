"""`Tool.hosts` -- where a tool declares the hosts it may reach.

Until now a `hosts` key in a tool's JSON was silently DROPPED: the `Tool` model
ignores unknown keys on load, and `Vault.save_tool` writes `model_dump()`, so
the next save erased it from disk too. A host class derived from a declaration
that cannot survive a save is not a binding. These tests pin that the field
round-trips through the real file vault, that `tool_host_class` reads it, that
a tool with no declaration still loads (the field is optional), and that the
packaged seed tools declare none -- the generic fetchers reach whatever host
their ARGUMENTS name, so `undeclared` is their honest class.
"""
from __future__ import annotations

import json
from pathlib import Path

from systemu.core.models import Tool, ToolType
from systemu.runtime.command_approvals import tool_host_class

_SEED_TOOLS = Path(__file__).resolve().parent.parent / "systemu" / "vault" / "tools"


def _tool(**kw):
    base = dict(id="tool_fetch_quotes", name="fetch_quotes", description="d",
                tool_type=ToolType.PYTHON_FUNCTION, effect_tags=["net_read"])
    base.update(kw)
    return Tool(**base)


def test_hosts_is_optional_and_defaults_to_none_declared():
    t = _tool()
    assert t.hosts == []
    assert tool_host_class(t) == "undeclared"


def test_hosts_survive_a_save_and_a_load_through_the_real_vault(tmp_path):
    from systemu.vault.vault import Vault
    v = Vault(str(tmp_path / "vault"))
    v.save_tool(_tool(hosts=["api.example.com", "quotes.example.com"]))

    raw = json.loads((tmp_path / "vault" / "tools" / "tool_tool_fetch_quotes.json")
                     .read_text(encoding="utf-8"))
    assert raw["hosts"] == ["api.example.com", "quotes.example.com"]

    back = v.get_tool("tool_fetch_quotes")
    assert back.hosts == ["api.example.com", "quotes.example.com"]
    assert tool_host_class(back) == "api.example.com,quotes.example.com"

    # ...and a SECOND save (the one that used to erase it) keeps it.
    back.enabled = True
    v.save_tool(back)
    assert v.get_tool("tool_fetch_quotes").hosts == ["api.example.com",
                                                     "quotes.example.com"]


def test_a_tool_saved_before_the_field_existed_still_loads(tmp_path):
    from systemu.vault.vault import Vault
    v = Vault(str(tmp_path / "vault"))
    v.save_tool(_tool())
    p = tmp_path / "vault" / "tools" / "tool_tool_fetch_quotes.json"
    raw = json.loads(p.read_text(encoding="utf-8"))
    raw.pop("hosts", None)
    p.write_text(json.dumps(raw), encoding="utf-8")
    assert v.get_tool("tool_fetch_quotes").hosts == []


def test_the_packaged_seed_tools_declare_no_hosts():
    """Read-only over the shipped seed: none declares hosts, so every networked
    seed tool derives `undeclared` -- they are argument-determined fetchers."""
    seeds = sorted(_SEED_TOOLS.glob("tool_*.json"))
    assert seeds, "no seed tools found -- is the path right?"
    for p in seeds:
        tool = Tool.model_validate(json.loads(p.read_text(encoding="utf-8")))
        assert tool.hosts == [], p.name
