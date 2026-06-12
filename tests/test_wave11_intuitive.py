"""W11.6 — the intuitiveness sweep.

Operator requirement (2026-06-12): "I want to reduce the learning curve to
the tool. I want things to be intuitive and easy."

Three mechanical guarantees:
  * every spine page carries a one-line plain-language sublabel (the W10.4
    glossary, extended beyond the three lore pages);
  * empty states tell the operator what to DO next, not just what's absent;
  * the header's primary controls explain themselves on hover.
"""
from __future__ import annotations

import inspect


class TestGlossaryCoverage:
    def test_spine_terms_translated(self):
        from systemu.interface.design.glossary import lore_sublabel
        for term in ("work", "inbox", "build", "skills", "insights",
                     "scrolls", "activities", "shadows"):
            assert lore_sublabel(term), f"no plain-language line for {term!r}"

    def test_sublabels_are_explanations_not_names(self):
        from systemu.interface.design.glossary import lore_sublabel
        for term in ("work", "inbox", "build", "skills", "insights"):
            assert "—" in lore_sublabel(term), \
                f"{term!r} sublabel must explain, not just rename"


class TestPagesCarrySublabels:
    def _page_src(self, module_name):
        import importlib
        mod = importlib.import_module(f"systemu.interface.pages.{module_name}")
        return inspect.getsource(mod)

    def test_work_page(self):
        assert 'lore_sublabel("work")' in self._page_src("work")

    def test_inbox_page(self):
        assert 'lore_sublabel("inbox")' in self._page_src("inbox_page")

    def test_tools_page(self):
        assert 'lore_sublabel("build")' in self._page_src("tools")

    def test_skills_page(self):
        assert 'lore_sublabel("skills")' in self._page_src("skills_page")

    def test_insights_page(self):
        assert 'lore_sublabel("insights")' in self._page_src("insights")


class TestActionableEmptyStates:
    def test_work_empty_state_links_to_chat(self):
        from systemu.interface.pages import work
        src = inspect.getsource(work)
        empty_region = src.split("No workflows yet")[1][:400]
        assert "/chat" in empty_region, \
            "the empty state must take the operator to the action, not describe absence"


class TestHeaderTooltips:
    def test_new_and_needs_you_explain_themselves(self):
        from systemu.interface import dashboard
        src = inspect.getsource(dashboard)
        assert src.count("ui.tooltip(") >= 2 or src.count(".tooltip(") >= 2, \
            "＋New and the Needs-you badge need hover explanations"

    def test_status_button_explains_itself(self):
        from systemu.interface.components import status_menu
        src = inspect.getsource(status_menu)
        assert "tooltip(" in src
