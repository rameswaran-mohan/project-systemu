"""e2e-v0.10.27 defect, reachability half: the near-duplicate advisory must be
computed WITH THE PROPOSED TOOL'S DESCRIPTION at every forge path.

The v0.10.27 slice wired ``_dedup_advisory_line`` into all five forge paths but
handed it only ``tool.name``. The token-similarity detector this packet adds needs
the proposal's description too (a vague name with an honest description is exactly
the case the name-only detector cannot see), so the description has to be threaded
through EVERY call site - not one.

``tests/test_p4_forge_advisory_all_paths.py`` enumerates the five paths by name:

    1. governor / daemon     forge_proposed_tools     -> _generate_and_save_code
    2. CLI                   forge_tool               -> (own call site)
    3. dashboard /tools      forge_tool_from_spec     -> _generate_and_save_code
    4. self-heal reforge     reforge_failed_tool_code -> _generate_and_save_code
    5. Gate-2 sign-off       save_approved_code       -> (own call site)

MUTATION-CHECKED: delete the ``description=`` argument from ANY ONE of the three
call sites and ``test_every_dedup_advisory_call_site_passes_the_description``
goes red naming that site.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from systemu.core.models import Scroll, Tool, ToolStatus, ToolType
from systemu.core.utils import generate_id
from systemu.vault.vault import Vault

FORGE_SRC = Path(
    __import__("systemu.pipelines.tool_forge", fromlist=["x"]).__file__).resolve()

ADVISORY_FN = "_dedup_advisory_line"
SPINE = "_generate_and_save_code"

# The five forge paths, as named by tests/test_p4_forge_advisory_all_paths.py.
# value = the function that must OWN a `_dedup_advisory_line` call for that path.
FORGE_PATHS = {
    "forge_proposed_tools": SPINE,
    "forge_tool": "forge_tool",
    "forge_tool_from_spec": SPINE,
    "reforge_failed_tool_code": SPINE,
    "save_approved_code": "save_approved_code",
}

GOOD_CODE = "def run(**kwargs):\n    return {'ok': True}\n"


# --------------------------------------------------------------------------- #
# AST helpers
# --------------------------------------------------------------------------- #

def _tree():
    return ast.parse(FORGE_SRC.read_text(encoding="utf-8"))


def _functions(tree):
    return {n.name: n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _named_calls(fn_node, callee):
    return [c for c in ast.walk(fn_node)
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
            and c.func.id == callee]


# --------------------------------------------------------------------------- #
# 1. every one of the five paths reaches a call site
# --------------------------------------------------------------------------- #

def test_every_forge_path_reaches_a_dedup_advisory_call_site():
    fns = _functions(_tree())
    for path, owner in FORGE_PATHS.items():
        assert path in fns, "forge path vanished from tool_forge.py: " + path
        assert owner in fns, "advisory owner vanished from tool_forge.py: " + owner
        assert _named_calls(fns[owner], ADVISORY_FN), (
            "forge path " + path + " reaches " + owner
            + ", which no longer calls " + ADVISORY_FN)
        if owner == SPINE and path != SPINE:
            assert _named_calls(fns[path], SPINE), (
                "forge path " + path + " no longer routes through " + SPINE)


# --------------------------------------------------------------------------- #
# 2. THE MUTATION PIN - every call site passes the description
# --------------------------------------------------------------------------- #

def test_every_dedup_advisory_call_site_passes_the_description():
    """Drop ``description=`` at ANY ONE call site and this test names it."""
    tree = _tree()
    fns = _functions(tree)
    owners = sorted({owner for owner in FORGE_PATHS.values()})
    missing = []
    total = 0
    for owner in owners:
        for call in _named_calls(fns[owner], ADVISORY_FN):
            total += 1
            if not any(kw.arg == "description" for kw in call.keywords):
                missing.append(owner + ":" + str(call.lineno))
    assert total >= len(owners), (
        "expected at least one " + ADVISORY_FN + " call per owner, found "
        + str(total))
    assert missing == [], (
        "these " + ADVISORY_FN + " call sites forge without the proposed tool's "
        "description, so the token-similarity detector is blind there: "
        + repr(missing))


def test_no_dedup_advisory_call_site_hides_outside_the_known_owners():
    """A new, undeclared call site would escape the pin above."""
    tree = _tree()
    fns = _functions(tree)
    owners = {owner for owner in FORGE_PATHS.values()}
    stray = []
    for name, node in fns.items():
        if name in owners:
            continue
        if _named_calls(node, ADVISORY_FN):
            stray.append(name)
    assert stray == [], (
        "undeclared " + ADVISORY_FN + " call site(s): " + repr(stray))


# --------------------------------------------------------------------------- #
# 3. RUNTIME pin - the description really arrives at the detector
# --------------------------------------------------------------------------- #

@pytest.fixture
def vault(tmp_path):
    v = Vault(str(tmp_path))
    (tmp_path / "tools" / "index.json").write_text(json.dumps([{
        "id": "tool_fetch_json", "name": "fetch_json",
        "description": "HTTP GET a JSON API endpoint", "enabled": True,
        "status": "deployed", "tool_type": "python_function", "dependencies": [],
    }]), encoding="utf-8")
    return v


@pytest.fixture
def config(tmp_path):
    cfg = MagicMock()
    cfg.vault_dir = str(tmp_path)
    cfg.tier2_model = "test-model"
    return cfg


def _spy_near_duplicates(monkeypatch):
    from systemu.runtime import capability_index as ci
    seen = []
    real = ci.near_duplicates

    def _spy(v, name, description="", **kw):
        seen.append((name, description))
        return real(v, name, description, **kw)

    monkeypatch.setattr(ci, "near_duplicates", _spy)
    return seen


def _proposed(vault, name="grab_json_v2", description="fetch json from a url"):
    tool = Tool(id=generate_id("tool"), name=name, description=description,
                tool_type=ToolType.PYTHON_FUNCTION, status=ToolStatus.PROPOSED)
    vault.save_tool(tool)
    return tool


def test_generate_and_save_code_passes_the_description_to_the_detector(
        vault, config, monkeypatch):
    seen = _spy_near_duplicates(monkeypatch)
    tool = _proposed(vault)
    scroll = Scroll(id=generate_id("scroll"), name="s1", source_session_id="t",
                    raw_instructions_path="", narrative_md="context")
    vault.save_scroll(scroll)

    with patch("systemu.pipelines.tool_forge.llm_call_json",
               return_value={"implementation": GOOD_CODE}):
        from systemu.pipelines.tool_forge import _generate_and_save_code
        assert _generate_and_save_code(tool, scroll, config, vault) is not None

    assert ("grab_json_v2", "fetch json from a url") in seen, seen


def test_save_approved_code_passes_the_description_to_the_detector(
        vault, config, monkeypatch):
    seen = _spy_near_duplicates(monkeypatch)
    tool = _proposed(vault)

    from systemu.pipelines.tool_forge import save_approved_code
    assert save_approved_code(tool, GOOD_CODE, config, vault) is not None

    assert ("grab_json_v2", "fetch json from a url") in seen, seen


def test_forge_tool_passes_the_description_to_the_detector(
        vault, config, monkeypatch):
    seen = _spy_near_duplicates(monkeypatch)
    tool = _proposed(vault)
    scroll = Scroll(id=generate_id("scroll"), name="s1", source_session_id="t",
                    raw_instructions_path="", narrative_md="context")
    vault.save_scroll(scroll)
    monkeypatch.setattr("systemu.pipelines.tool_forge.notify_user",
                        lambda **kw: "Skip")

    from systemu.pipelines.tool_forge import forge_tool
    assert forge_tool(tool, scroll, config, vault) is None

    assert ("grab_json_v2", "fetch json from a url") in seen, seen


def test_the_forge_gate_actually_names_the_near_duplicate(
        vault, config, monkeypatch):
    """End to end on the CLI gate: the operator SEES fetch_json named, which is
    the whole promise the release makes and the thing that was silent."""
    tool = _proposed(vault)
    scroll = Scroll(id=generate_id("scroll"), name="s1", source_session_id="t",
                    raw_instructions_path="", narrative_md="context")
    vault.save_scroll(scroll)
    captured = {}

    def _spy_notify(**kwargs):
        captured.update(kwargs)
        return "Skip"

    monkeypatch.setattr("systemu.pipelines.tool_forge.notify_user", _spy_notify)
    from systemu.pipelines.tool_forge import forge_tool
    forge_tool(tool, scroll, config, vault)

    assert "fetch_json" in captured.get("message", ""), captured.get("message")
