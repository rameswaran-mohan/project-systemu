"""W10.4 — plain language beside the lore + first-run demo staging.

Charter v2 requirement 3: a professional from any background must be able to
run this. The lore (Scrolls/Shadows/Forge) is charming and unexplained —
pages now carry plain-language sublabels from ONE glossary. And /welcome's
"Try it" step becomes actionable: one-click starter prompts that land in
Chat's quick lane pre-filled (?prefill=), plus the record-once pitch.
"""
from __future__ import annotations

import inspect


class TestGlossary:
    def test_lore_terms_have_plain_language(self):
        from systemu.interface.design.glossary import lore_sublabel
        assert "workflow" in lore_sublabel("scrolls").lower()
        assert "agent" in lore_sublabel("shadows").lower()
        assert "task" in lore_sublabel("activities").lower()
        assert lore_sublabel("unknown-term") == ""

    def test_lore_pages_render_sublabels(self):
        from systemu.interface.pages import scrolls, army, activities
        for mod in (scrolls, army, activities):
            src = inspect.getsource(mod)
            assert "lore_sublabel" in src, \
                f"{mod.__name__} must carry the plain-language sublabel"


class TestWelcomeTryIt:
    def test_starter_prompts_defined(self):
        from systemu.interface.pages.welcome import starter_prompts
        prompts = starter_prompts()
        assert 2 <= len(prompts) <= 4
        assert all(isinstance(p, str) and len(p) > 10 for p in prompts)

    def test_welcome_links_starters_into_chat(self):
        from systemu.interface.pages import welcome
        src = inspect.getsource(welcome)
        assert "starter_prompts" in src and "/chat?prefill=" in src


class TestChatPrefill:
    def test_route_accepts_prefill(self):
        from systemu.interface import dashboard
        src = inspect.getsource(dashboard)
        assert "prefill" in src, "/chat must accept a ?prefill= starter prompt"

    def test_chat_page_applies_prefill(self):
        from systemu.interface.pages import chat_page
        src = inspect.getsource(chat_page)
        assert "prefill" in src
        # Pre-fills the composer — never auto-submits (the operator clicks Run).
        assert "submit_btn.on_click" in src