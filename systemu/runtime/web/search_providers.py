"""T1 — multi-provider search. Keyed providers (Brave/Serper) preferred;
free DuckDuckGo-lite fallback. Free out-of-box; one env var → reliable."""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List
from urllib.parse import quote_plus

logger = logging.getLogger(__name__)


class _Provider:
    name = "base"
    def available(self) -> bool: return False
    def search(self, query: str, max_results: int) -> List[Dict[str, str]]: return []


class BraveProvider(_Provider):
    name = "brave"
    def available(self) -> bool:
        return bool(os.environ.get("SYSTEMU_BRAVE_API_KEY"))
    def search(self, query, max_results):
        import httpx
        key = os.environ.get("SYSTEMU_BRAVE_API_KEY", "")
        r = httpx.get("https://api.search.brave.com/res/v1/web/search",
                      params={"q": query, "count": max_results},
                      headers={"X-Subscription-Token": key, "Accept": "application/json"},
                      timeout=20)
        r.raise_for_status()
        web = (r.json().get("web") or {}).get("results", [])
        return [{"title": x.get("title", ""), "url": x.get("url", ""),
                 "snippet": x.get("description", "")} for x in web[:max_results]]


class SerperProvider(_Provider):
    name = "serper"
    def available(self) -> bool:
        return bool(os.environ.get("SYSTEMU_SERPER_API_KEY"))
    def search(self, query, max_results):
        import httpx
        key = os.environ.get("SYSTEMU_SERPER_API_KEY", "")
        r = httpx.post("https://google.serper.dev/search",
                       json={"q": query, "num": max_results},
                       headers={"X-API-KEY": key, "Content-Type": "application/json"},
                       timeout=20)
        r.raise_for_status()
        organic = r.json().get("organic", [])
        return [{"title": x.get("title", ""), "url": x.get("link", ""),
                 "snippet": x.get("snippet", "")} for x in organic[:max_results]]


class DuckDuckGoLiteProvider(_Provider):
    name = "duckduckgo_lite"
    def available(self) -> bool:
        return True  # always the floor of the chain
    def search(self, query, max_results):
        import httpx
        from systemu.runtime.web.fetch_core import _Readable  # reuse link parse
        ua = "Mozilla/5.0 (compatible; SystemuBot/1.0)"
        r = httpx.get(f"https://html.duckduckgo.com/html/?q={quote_plus(query)}",
                      headers={"User-Agent": ua}, timeout=20, follow_redirects=True)
        r.raise_for_status()
        # Minimal parse: DDG-lite result anchors have class 'result__a'; we do a
        # tolerant extraction via the readable link collector + heuristic titles.
        from html.parser import HTMLParser

        class _DDG(HTMLParser):
            def __init__(self): super().__init__(); self.out=[]; self._a=False; self._buf=[]; self._href=""
            def handle_starttag(self, t, attrs):
                if t == "a":
                    d = dict(attrs)
                    cls = d.get("class", "")
                    if "result__a" in cls:
                        self._a = True; self._buf = []; self._href = d.get("href", "")
            def handle_endtag(self, t):
                if t == "a" and self._a:
                    self._a = False
                    self.out.append({"title": " ".join(self._buf).strip(),
                                     "url": self._href, "snippet": ""})
            def handle_data(self, data):
                if self._a: self._buf.append(data)
        p = _DDG(); p.feed(r.text)
        return p.out[:max_results]


_CHAIN = [BraveProvider, SerperProvider, DuckDuckGoLiteProvider]


def search(query: str, max_results: int = 5) -> Dict[str, Any]:
    tried = []
    for cls in _CHAIN:
        prov = cls()
        if not prov.available():
            continue
        tried.append(prov.name)
        try:
            results = prov.search(query, max_results)
        except Exception as exc:
            logger.warning("[search] provider %s failed: %s", prov.name, exc)
            results = []
        if results:
            return {"results": results, "provider": prov.name,
                    "degraded": prov.name == "duckduckgo_lite"}
    return {"results": [], "provider": None, "degraded": True,
            "error": f"all providers failed/empty (tried: {', '.join(tried)})"}
