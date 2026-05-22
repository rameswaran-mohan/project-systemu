#!/usr/bin/env python3
"""Search the web using DuckDuckGo HTML (no API key required)."""
from __future__ import annotations

from html.parser import HTMLParser
from urllib.parse import quote_plus

TOOL_META = {
    "name": "web_search",
    "tool_type": "web",
    "dependencies": ["requests"],
}


class _DDGParser(HTMLParser):
    """Parse DuckDuckGo HTML results page."""

    def __init__(self):
        super().__init__()
        self.results: list[dict] = []
        self._in_result = False
        self._in_title = False
        self._in_snippet = False
        self._in_url = False
        self._current: dict = {}
        self._depth = 0
        self._result_depth = 0
        self._title_buf: list[str] = []
        self._snippet_buf: list[str] = []
        self._url_buf: list[str] = []

    def _get_class(self, attrs):
        for name, val in attrs:
            if name == "class":
                return val or ""
        return ""

    def handle_starttag(self, tag, attrs):
        cls = self._get_class(attrs)
        self._depth += 1

        if tag == "div" and "result" in cls.split() and "result--ad" not in cls:
            self._in_result = True
            self._result_depth = self._depth
            self._current = {"title": "", "url": "", "snippet": ""}

        if self._in_result:
            if tag == "a" and "result__a" in cls:
                self._in_title = True
                self._title_buf = []
                for name, val in attrs:
                    if name == "href" and val:
                        self._current["url"] = val
            elif tag == "a" and "result__url" in cls:
                self._in_url = True
                self._url_buf = []
            elif tag == "a" and "result__snippet" in cls:
                self._in_snippet = True
                self._snippet_buf = []
            elif tag == "div" and "result__snippet" in cls:
                self._in_snippet = True
                self._snippet_buf = []

    def handle_endtag(self, tag):
        if self._in_title and tag == "a":
            self._current["title"] = "".join(self._title_buf).strip()
            self._in_title = False

        if self._in_url and tag == "a":
            if not self._current.get("url"):
                self._current["url"] = "".join(self._url_buf).strip()
            self._in_url = False

        if self._in_snippet and tag in ("a", "div"):
            self._current["snippet"] = "".join(self._snippet_buf).strip()
            self._in_snippet = False

        if tag == "div" and self._in_result and self._depth == self._result_depth:
            if self._current.get("title"):
                self.results.append(self._current)
            self._in_result = False
            self._current = {}

        self._depth -= 1

    def handle_data(self, data):
        if self._in_title:
            self._title_buf.append(data)
        if self._in_snippet:
            self._snippet_buf.append(data)
        if self._in_url:
            self._url_buf.append(data)


def run(**kwargs) -> dict:
    query: str = kwargs.get("query", "")
    max_results: int = int(kwargs.get("max_results", 5))

    if not query:
        return {"success": False, "results": [], "error": "query is required"}

    try:
        import requests

        encoded = quote_plus(query)
        url = f"https://html.duckduckgo.com/html/?q={encoded}"

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        }

        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()

        parser = _DDGParser()
        parser.feed(response.text)

        results = parser.results[:max_results]

        return {"success": True, "results": results, "error": None}

    except Exception as exc:
        return {"success": False, "results": [], "error": str(exc)}
