"""T1 — multi-provider web search. Chain: Tavily → Exa → Brave → Serper
(keyed, skipped without an API key) → ddgs (keyless metasearch, the always-
available free floor). First non-empty wins; short-TTL cache; never raises."""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


def _DDGS():
    """Indirection so tests can monkeypatch the ddgs client. Imported lazily."""
    from ddgs import DDGS
    return DDGS()


_CACHE: Dict[tuple, tuple] = {}   # (norm_query, max_results) -> (expiry_monotonic, result_dict)
_CACHE_TTL = 300.0
_CACHE_MAX = 128


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


class TavilyProvider(_Provider):
    name = "tavily"
    def available(self) -> bool:
        return bool(os.environ.get("SYSTEMU_TAVILY_API_KEY"))
    def search(self, query, max_results):
        import httpx
        key = os.environ.get("SYSTEMU_TAVILY_API_KEY", "")
        r = httpx.post("https://api.tavily.com/search",
                       json={"api_key": key, "query": query, "max_results": max_results},
                       timeout=20)
        r.raise_for_status()
        items = r.json().get("results", [])
        return [{"title": x.get("title", ""), "url": x.get("url", ""),
                 "snippet": x.get("content", "")} for x in items[:max_results]]


class ExaProvider(_Provider):
    name = "exa"
    def available(self) -> bool:
        return bool(os.environ.get("SYSTEMU_EXA_API_KEY"))
    def search(self, query, max_results):
        import httpx
        key = os.environ.get("SYSTEMU_EXA_API_KEY", "")
        r = httpx.post("https://api.exa.ai/search",
                       json={"query": query, "numResults": max_results,
                             "contents": {"text": {"maxCharacters": 300}}},
                       headers={"x-api-key": key, "Content-Type": "application/json"},
                       timeout=20)
        r.raise_for_status()
        items = r.json().get("results", [])
        return [{"title": x.get("title", ""), "url": x.get("url", ""),
                 "snippet": (x.get("text") or x.get("snippet") or "")} for x in items[:max_results]]


class DdgsProvider(_Provider):
    """Free out-of-box floor: keyless metasearch via the `ddgs` library
    (backend='auto' rotates across bing/google/mojeek/startpage/yandex/…)."""
    name = "ddgs"
    def available(self) -> bool:
        return True  # always the floor of the chain

    def search(self, query, max_results):
        from ddgs.exceptions import DDGSException, RatelimitException, TimeoutException
        for attempt in range(2):  # initial + one retry with backoff
            try:
                rows = _DDGS().text(
                    query, region="us-en", safesearch="moderate",
                    max_results=max_results, backend="auto",
                )
                return [{"title": r.get("title", ""), "url": r.get("href", ""),
                         "snippet": r.get("body", "")} for r in (rows or [])][:max_results]
            except (RatelimitException, TimeoutException, DDGSException) as exc:
                logger.warning("[search] ddgs attempt %d failed: %s", attempt + 1, exc)
                if attempt == 0:
                    time.sleep(1.5)   # brief backoff before the single retry
                    continue
                return []
            except Exception as exc:   # any other failure → degrade gracefully, never raise
                logger.warning("[search] ddgs unexpected error: %s", exc)
                return []
        return []


_CHAIN = [TavilyProvider, ExaProvider, BraveProvider, SerperProvider, DdgsProvider]


def search(query: str, max_results: int = 5) -> Dict[str, Any]:
    key = (query.strip().lower(), max_results)
    now = time.monotonic()
    hit = _CACHE.get(key)
    if hit and hit[0] > now:
        return hit[1]
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
            out = {"results": results, "provider": prov.name, "degraded": prov.name == "ddgs"}
            if len(_CACHE) >= _CACHE_MAX:
                _CACHE.clear()   # simple bound; cheap reset
            _CACHE[key] = (now + _CACHE_TTL, out)   # cache only non-empty
            return out
    return {"results": [], "provider": None, "degraded": True,
            "error": f"all providers failed/empty (tried: {', '.join(tried)})"}
