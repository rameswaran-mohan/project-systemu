"""F25 — a shipped tool declares its dependencies in three places; they must agree.

Found by cold-installing the v0.10.23 wheel into a fresh venv and running
`systemu init`, then `systemu tools list`:

    web_read   UNAVAILABLE   Browser automation is not installed (playwright)

but `web_read` does NOT need playwright. Its primary paths are a Jina Reader
fetch and a raw HTTP GET; Chromium is only a last-resort render escalation. The
install packet had already established this and corrected TWO of the three
declarations:

    systemu/vault/tools/index.json                     dependencies: []   fixed
    systemu/vault/tools/implementations/web_read.py    TOOL_META: []      fixed
    systemu/vault/tools/tool_tool_web_read.json        ["playwright"]     MISSED

`init` seeds from the per-tool record, so the vault got the stale value and a
default install refused a tool that works fine. **A false unavailability is the
same defect class as a false readiness** — the packet said so itself while
fixing the other two.

Root shape is DEC-43: one fact, three copies, one drifts. The fence is therefore
the AGREEMENT, quantified over every shipped tool — not a pin on web_read.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_TOOLS = _REPO / "systemu" / "vault" / "tools"
_IMPLS = _TOOLS / "implementations"


def _norm(names):
    """Compare CANONICAL distribution names, using the product's own normalizer.

    `pillow` vs `Pillow` is not a drift: PyPI names are case-insensitive, and
    optional_deps.is_installed — the single availability probe — already resolves
    through `canonical()`. Using any other rule here would make the test disagree
    with production about what "the same dependency" means, which is how a fence
    starts reporting phantoms and gets switched off.
    """
    from systemu.runtime.optional_deps import canonical

    return sorted(canonical(n) for n in (names or []))


def _index_declarations() -> dict:
    raw = json.loads((_TOOLS / "index.json").read_text(encoding="utf-8"))
    tools = raw if isinstance(raw, list) else raw.get("tools", raw)
    rows = tools if isinstance(tools, list) else list(tools.values())
    return {
        t["name"]: _norm(t.get("dependencies"))
        for t in rows
        if isinstance(t, dict) and t.get("name")
    }


def _seed_record_declarations() -> dict:
    out = {}
    for path in _TOOLS.glob("tool_*.json"):
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # pragma: no cover
            continue
        if isinstance(d, dict) and d.get("name"):
            out[d["name"]] = _norm(d.get("dependencies"))
    return out


def _tool_meta_declarations() -> dict:
    """TOOL_META read STATICALLY. Importing an implementation would execute it,
    and several import optional packages at call time."""
    out = {}
    for path in _IMPLS.glob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:  # pragma: no cover
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            if not any(isinstance(t, ast.Name) and t.id == "TOOL_META"
                       for t in node.targets):
                continue
            try:
                meta = ast.literal_eval(node.value)
            except (ValueError, SyntaxError):
                continue
            if isinstance(meta, dict) and meta.get("name"):
                out[meta["name"]] = _norm(meta.get("dependencies"))
    return out


_INDEX = _index_declarations()
_SEED = _seed_record_declarations()
_META = _tool_meta_declarations()


def test_the_three_sources_are_not_empty():
    """Guard the premise — a broken reader would make every check below vacuous."""
    assert len(_INDEX) >= 40, f"index.json parsed {len(_INDEX)} tools"
    assert len(_SEED) >= 40, f"parsed {len(_SEED)} per-tool seed records"
    assert len(_META) >= 40, f"parsed {len(_META)} TOOL_META blocks"


@pytest.mark.parametrize("name", sorted(set(_INDEX) & set(_SEED)))
def test_index_and_seed_record_declare_the_same_dependencies(name):
    assert _INDEX[name] == _SEED[name], (
        f"{name}: index.json says {_INDEX[name]} but the per-tool seed record "
        f"tool_tool_{name}.json says {_SEED[name]}. `init` seeds from the seed "
        f"record, so the vault — and every availability verdict derived from it "
        f"— follows the one that drifted."
    )


@pytest.mark.parametrize("name", sorted(set(_INDEX) & set(_META)))
def test_index_and_tool_meta_declare_the_same_dependencies(name):
    assert _INDEX[name] == _META[name], (
        f"{name}: index.json says {_INDEX[name]} but the implementation's "
        f"TOOL_META says {_META[name]}."
    )


def test_web_read_specifically_does_not_claim_playwright():
    """The concrete regression. web_read's Chromium tier is a last-resort
    escalation, not a requirement — declaring it makes a default install refuse
    a tool that works."""
    for label, table in (("index.json", _INDEX), ("seed record", _SEED),
                         ("TOOL_META", _META)):
        assert "playwright" not in table.get("web_read", []), (
            f"{label} declares playwright for web_read; a default install will "
            f"refuse it even though its httpx path works"
        )


def test_the_tools_that_genuinely_need_playwright_still_declare_it():
    """The opposite error — silently dropping a real dependency — would turn an
    honest refusal into an ImportError at call time."""
    for name in ("web_act", "web_screenshot"):
        for label, table in (("index.json", _INDEX), ("seed record", _SEED),
                             ("TOOL_META", _META)):
            assert "playwright" in table.get(name, []), (
                f"{label} no longer declares playwright for {name}, which drives "
                f"a real browser — it must refuse honestly, not ImportError"
            )
