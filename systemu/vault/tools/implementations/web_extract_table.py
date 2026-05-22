#!/usr/bin/env python3
"""Extract an HTML table from a URL and return it as a list-of-lists."""
from __future__ import annotations

from html.parser import HTMLParser

TOOL_META = {
    "name": "web_extract_table",
    "tool_type": "web",
    "dependencies": ["playwright"],
}


class _TableParser(HTMLParser):
    """Minimal HTML parser that extracts table data."""

    def __init__(self):
        super().__init__()
        self.tables: list[list[list[str]]] = []
        self._in_table = False
        self._in_row = False
        self._in_cell = False
        self._current_table: list[list[str]] = []
        self._current_row: list[str] = []
        self._current_cell: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._in_table = True
            self._current_table = []
        elif tag == "tr" and self._in_table:
            self._in_row = True
            self._current_row = []
        elif tag in ("td", "th") and self._in_row:
            self._in_cell = True
            self._current_cell = []

    def handle_endtag(self, tag):
        if tag == "table":
            self.tables.append(self._current_table)
            self._in_table = False
        elif tag == "tr" and self._in_table:
            if self._current_row:
                self._current_table.append(self._current_row)
            self._in_row = False
        elif tag in ("td", "th") and self._in_row:
            self._current_row.append("".join(self._current_cell).strip())
            self._in_cell = False

    def handle_data(self, data):
        if self._in_cell:
            self._current_cell.append(data)


_PLAYWRIGHT_MISSING_SIGNALS = (
    "Executable doesn't exist",
    "playwright install",
    "BrowserType.launch",
    "No module named 'playwright'",
    "No module named \"playwright\"",
)


def _is_playwright_missing(exc: Exception) -> bool:
    msg = str(exc)
    return any(s in msg for s in _PLAYWRIGHT_MISSING_SIGNALS)


def run(**kwargs) -> dict:
    url: str = kwargs.get("url", "")
    table_index: int = int(kwargs.get("table_index", 0))
    selector: str = kwargs.get("selector", "table")

    if not url:
        return {"success": False, "headers": [], "rows": [], "error": "url is required"}

    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, wait_until="networkidle")
            html = page.content()
            browser.close()

        parser = _TableParser()
        parser.feed(html)

        if not parser.tables:
            return {"success": False, "headers": [], "rows": [], "error": "No tables found on the page"}

        if table_index >= len(parser.tables):
            return {
                "success": False,
                "headers": [],
                "rows": [],
                "error": f"table_index {table_index} out of range; found {len(parser.tables)} table(s)",
            }

        table = parser.tables[table_index]
        if not table:
            return {"success": True, "headers": [], "rows": [], "error": None}

        headers = table[0]
        rows = table[1:]

        return {"success": True, "headers": headers, "rows": rows, "error": None}

    except Exception as exc:
        if _is_playwright_missing(exc):
            return {
                "success": False,
                "headers": [],
                "rows": [],
                "error": str(exc),
                "error_type": "missing_dependency",
                "fix": "Run: playwright install chromium",
            }
        return {"success": False, "headers": [], "rows": [], "error": str(exc)}
