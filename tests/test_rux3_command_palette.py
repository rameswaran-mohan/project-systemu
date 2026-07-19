"""R-UX3 / UX-13 — the Ctrl+K command palette (SPEC Part II 15-UX, AC-U13).

What these pins cover
---------------------
* The SAFETY LINE (the spec's own "hard rule"): the palette navigates and
  prefills ONLY. No effectful execution path may exist. Pinned three ways —
  the constructor refuses any other intent, every built entry is checked,
  and the module source is scanned for execution seams (that scan, plus the
  wiring pins, live in test_rux3_source_guards.py -- see its docstring).
* "no vault read on open" — ``match()`` takes a prebuilt index and has no vault
  parameter at all, so opening the overlay cannot re-read the vault.
* Deterministic lexical matching (no model call).
* Defensive index build: one broken projection must not blank the palette.

What these pins DO NOT cover (stated plainly, not implied)
----------------------------------------------------------
* The Ctrl+K keybinding, the overlay dialog, and the up/down/Enter/Esc
  behaviour are NiceGUI client-side interactions and are NOT exercised here.
* The spec's "<50ms" index-build budget is NOT asserted — a wall-clock
  threshold is machine-dependent and would be a flaky gate. The structural
  properties that make the budget achievable (no LLM, no per-open vault read)
  are asserted instead.
"""
from __future__ import annotations

import inspect

import pytest


@pytest.fixture
def vault(tmp_path):
    from systemu.vault.vault import Vault
    for sub in ("scrolls", "activities", "shadow_army", "skills", "tools",
                "evolutions"):
        (tmp_path / sub).mkdir()
        (tmp_path / sub / "index.json").write_text("[]")
    return Vault(str(tmp_path))


def _seed_tool(vault, name="send_invoice_email"):
    from systemu.core.models import Tool, ToolStatus, ToolType
    vault.save_tool(Tool(
        id=f"tool_{name}", name=name, description="Sends the invoice",
        tool_type=ToolType.PYTHON_FUNCTION, status=ToolStatus.DEPLOYED,
        implementation_path=f"p/{name}.py", parameters_schema={}, enabled=True,
    ))


# ── the safety line ─────────────────────────────────────────────────────────

class TestPaletteCannotExecute:
    def test_entry_rejects_an_effectful_intent(self):
        """AC-U13: only navigate/prefill intents exist."""
        from systemu.interface.components.command_palette import PaletteEntry
        with pytest.raises(ValueError):
            PaletteEntry(group="Tools", label="x", intent="execute", target="/x")

    def test_entry_accepts_the_two_allowed_intents(self):
        from systemu.interface.components.command_palette import (
            NAVIGATE, PREFILL, PaletteEntry)
        assert PaletteEntry(group="Tools", label="x", intent=NAVIGATE,
                            target="/x").intent == NAVIGATE
        assert PaletteEntry(group="Tools", label="x", intent=PREFILL,
                            target="/chat?prefill=run%3A+x").intent == PREFILL

    def test_allowed_intents_are_exactly_navigate_and_prefill(self):
        from systemu.interface.components.command_palette import ALLOWED_INTENTS
        assert set(ALLOWED_INTENTS) == {"navigate", "prefill"}

    def test_every_built_entry_is_navigate_or_prefill(self, vault):
        from systemu.interface.components.command_palette import (
            ALLOWED_INTENTS, build_index)
        _seed_tool(vault)
        entries = build_index(vault)
        assert entries, "index must not be empty (the static actions alone are non-empty)"
        for e in entries:
            assert e.intent in ALLOWED_INTENTS, f"{e.label} carries intent {e.intent!r}"

    def test_entry_rejects_an_off_box_target(self):
        """Pins the CONSTRUCTOR guard directly.

        (The sweep over build_index below cannot pin it: every built entry is
        local anyway, so removing the guard leaves that sweep green. Mutation
        testing caught exactly that.)
        """
        from systemu.interface.components.command_palette import PaletteEntry
        for bad in ("https://evil.example/x", "//evil.example/x",
                    "javascript:alert(1)", "chat?prefill=x"):
            with pytest.raises(ValueError):
                PaletteEntry(group="Actions", label="x", intent="navigate",
                             target=bad)

    def test_navigate_targets_are_local_paths_only(self, vault):
        """A palette entry can never point off-box."""
        from systemu.interface.components.command_palette import build_index
        _seed_tool(vault)
        for e in build_index(vault):
            assert e.target.startswith("/"), f"{e.label} -> {e.target!r}"
            assert "//" not in e.target, f"{e.label} -> {e.target!r}"


# ── no vault read on open ───────────────────────────────────────────────────

class TestOpenDoesNotReadTheVault:
    def test_match_signature_takes_no_vault(self):
        """Structural: opening the palette cannot reach the vault because the
        matcher has no vault to reach."""
        from systemu.interface.components.command_palette import match
        params = set(inspect.signature(match).parameters)
        assert "vault" not in params

    def test_matching_never_touches_the_vault_again(self, vault):
        from systemu.interface.components.command_palette import build_index, match

        _seed_tool(vault)
        entries = build_index(vault)

        calls = {"n": 0}
        real_load = vault.load_index

        def _counting(*a, **k):
            calls["n"] += 1
            return real_load(*a, **k)

        vault.load_index = _counting          # type: ignore[assignment]
        for q in ("inv", "tool", "", "settings", "zzz"):
            match(entries, q)
        assert calls["n"] == 0, "the palette re-read the vault while matching"


# ── deterministic lexical matching ──────────────────────────────────────────

class TestMatching:
    def test_groups_are_the_five_spec_groups(self):
        from systemu.interface.components.command_palette import GROUPS
        assert list(GROUPS) == ["Actions", "Tools", "Runs", "Table", "Asks"]

    def test_match_is_deterministic(self, vault):
        from systemu.interface.components.command_palette import build_index, match
        _seed_tool(vault)
        entries = build_index(vault)
        a = [(e.group, e.label) for e in match(entries, "in")]
        b = [(e.group, e.label) for e in match(entries, "in")]
        assert a == b

    def test_exact_match_wins(self):
        from systemu.interface.components.command_palette import PaletteEntry, match
        entries = [
            PaletteEntry(group="Tools", label="zzz settings zzz",
                         intent="navigate", target="/a"),
            PaletteEntry(group="Tools", label="settings",
                         intent="navigate", target="/b"),
        ]
        assert match(entries, "settings")[0].label == "settings"

    def test_prefix_match_outranks_a_late_substring(self):
        """Neither candidate is an EXACT match, so this actually exercises the
        prefix branch. (The earlier version of this test included an exact
        match, which decided the order before the prefix rule was consulted --
        mutating the prefix score left it green.)
        """
        from systemu.interface.components.command_palette import PaletteEntry, match
        entries = [
            PaletteEntry(group="Tools", label="zzz settings zzz",
                         intent="navigate", target="/a"),
            PaletteEntry(group="Tools", label="settings page",
                         intent="navigate", target="/b"),
        ]
        assert match(entries, "settings")[0].label == "settings page"

    def test_match_is_case_insensitive(self):
        from systemu.interface.components.command_palette import PaletteEntry, match
        entries = [PaletteEntry(group="Tools", label="Send Invoice",
                                intent="navigate", target="/a")]
        assert match(entries, "invoice")
        assert match(entries, "INVOICE")

    def test_non_matching_query_returns_nothing(self):
        from systemu.interface.components.command_palette import PaletteEntry, match
        entries = [PaletteEntry(group="Tools", label="Send Invoice",
                                intent="navigate", target="/a")]
        assert match(entries, "qqqqqq") == []

    def test_empty_query_returns_a_capped_preview(self):
        from systemu.interface.components.command_palette import PaletteEntry, match
        entries = [PaletteEntry(group="Tools", label=f"t{i}",
                                intent="navigate", target=f"/{i}")
                   for i in range(50)]
        got = match(entries, "", limit=7)
        assert len(got) == 7

    def test_limit_is_respected(self):
        from systemu.interface.components.command_palette import PaletteEntry, match
        entries = [PaletteEntry(group="Tools", label=f"invoice {i}",
                                intent="navigate", target=f"/{i}")
                   for i in range(50)]
        assert len(match(entries, "invoice", limit=5)) == 5


# ── index construction ──────────────────────────────────────────────────────

class TestIndexBuild:
    def test_static_actions_need_no_vault(self):
        """The page/action registry is static — it renders even with a dead vault."""
        from systemu.interface.components.command_palette import build_index
        entries = build_index(None)
        assert any(e.group == "Actions" for e in entries)

    def test_a_broken_projection_does_not_blank_the_palette(self):
        from systemu.interface.components.command_palette import build_index

        class _Exploding:
            root = "/nope"

            def __getattr__(self, name):
                raise RuntimeError("projection down")

        entries = build_index(_Exploding())
        assert any(e.group == "Actions" for e in entries), \
            "a broken projection must degrade to the static actions, not to nothing"

    def test_a_deployed_tool_becomes_a_run_prefill(self, vault):
        from systemu.interface.components.command_palette import PREFILL, build_index
        _seed_tool(vault, "send_invoice_email")
        tools = [e for e in build_index(vault) if e.group == "Tools"]
        assert tools, "the deployed tool did not reach the palette index"
        hit = next(e for e in tools if "send_invoice_email" in e.label)
        assert hit.intent == PREFILL
        assert hit.target.startswith("/chat?prefill=")
        assert "run" in hit.target

    def test_settings_action_is_present(self):
        from systemu.interface.components.command_palette import build_index
        labels = [e.label.lower() for e in build_index(None)]
        assert any("settings" in lab for lab in labels)

    def test_the_capability_index_is_the_PRIMARY_source_for_tools(self, vault):
        """Spec source order: "capability-index rows post-R-CAP1, ELSE the tool
        index". This pins the FIRST branch.

        History worth keeping: this test used to assert ``derive_index`` returned
        ``[]`` on a live vault and that the palette therefore fell back. That was
        accurate, and it was a BUG -- ``_ready()`` demanded a truthy
        ``implementation_path`` that no ``_tool_header`` producer ever emitted, so
        the index was empty on every real vault and ``find-tools`` told operators
        they had no tools at all. The assertion was planted as a deliberate
        tripwire to fire when that got fixed. It fired. The fallback is now pinned
        separately below, against a FORCED-empty index rather than against a defect.
        """
        from systemu.runtime import capability_index
        from systemu.interface.components.command_palette import build_index

        _seed_tool(vault, "send_invoice_email")
        rows = capability_index.derive_index(vault)
        assert rows, "the capability index is empty for a real seeded vault"
        assert any(r.name == "send_invoice_email" for r in rows)

        tools = [e for e in build_index(vault) if e.group == "Tools"]
        assert any("send_invoice_email" in e.label for e in tools)

    def test_tools_fall_back_to_the_vault_index_when_the_capability_index_is_empty(
            self, vault, monkeypatch):
        """The ELSE branch, pinned against a DELIBERATELY empty index.

        Still load-bearing: a vault whose tools genuinely cannot be indexed must
        surface them in the palette anyway, rather than showing the operator an
        empty Tools group.
        """
        from systemu.runtime import capability_index
        from systemu.interface.components.command_palette import build_index

        _seed_tool(vault, "send_invoice_email")
        # build_index imports the module inside the function, so patching the
        # module attribute is what the call site actually resolves.
        monkeypatch.setattr(capability_index, "derive_index", lambda _v: [])

        tools = [e for e in build_index(vault) if e.group == "Tools"]
        assert any("send_invoice_email" in e.label for e in tools)

    def test_disabled_tools_are_not_offered(self, vault):
        from systemu.core.models import Tool, ToolStatus, ToolType
        from systemu.interface.components.command_palette import build_index
        vault.save_tool(Tool(
            id="t_off", name="dormant_tool", description="d",
            tool_type=ToolType.PYTHON_FUNCTION, status=ToolStatus.PROPOSED,
            implementation_path="p/d.py", parameters_schema={}, enabled=False))
        labels = [e.label for e in build_index(vault) if e.group == "Tools"]
        assert "dormant_tool" not in labels
