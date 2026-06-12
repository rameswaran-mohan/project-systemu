"""W12-B7 — the one-page operator SOP (R2: "a clear SOP for working on").

Ships at the repo root (the public sync includes root docs), linked from
the README and surfaced in Settings → Help.
"""
from __future__ import annotations

import inspect
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SOP = REPO / "OPERATOR-SOP.md"


class TestOperatorSop:
    def test_exists_and_covers_the_loop(self):
        assert SOP.exists(), "OPERATOR-SOP.md must ship at the repo root"
        text = SOP.read_text(encoding="utf-8")
        for needle in ("Record", "Approve", "Inbox", "Status",
                       "Troubleshooting", "Safety"):
            assert needle in text, f"SOP must cover {needle!r}"

    def test_plain_language_not_lore_only(self):
        """Every lore term used must appear alongside its translation."""
        text = SOP.read_text(encoding="utf-8")
        if "shadows" in text.lower():
            assert "specialist" in text.lower(), \
                "lore terms need their plain-English translation beside them"

    def test_readme_links_it(self):
        readme = (REPO / "README.md").read_text(encoding="utf-8")
        assert "OPERATOR-SOP.md" in readme

    def test_settings_help_mentions_it(self):
        from systemu.interface.pages import settings
        src = inspect.getsource(settings)
        assert "OPERATOR-SOP.md" in src
