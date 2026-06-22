"""v0.8.17 — free out-of-box web search."""
import pytest


class TestDdgsProvider:
    def test_maps_rows_to_title_url_snippet(self, monkeypatch):
        import systemu.runtime.web.search_providers as sp
        class _FakeDDGS:
            def text(self, query, **kw):
                return [{"title": "T1", "href": "http://a", "body": "B1"},
                        {"title": "T2", "href": "http://b", "body": "B2"}]
        monkeypatch.setattr(sp, "_DDGS", lambda: _FakeDDGS())
        out = sp.DdgsProvider().search("salons near me", 5)
        assert out == [{"title": "T1", "url": "http://a", "snippet": "B1"},
                       {"title": "T2", "url": "http://b", "snippet": "B2"}]

    def test_never_raises_on_ratelimit_returns_empty(self, monkeypatch):
        import systemu.runtime.web.search_providers as sp
        from ddgs.exceptions import RatelimitException
        calls = {"n": 0}
        class _Boom:
            def text(self, *a, **k):
                calls["n"] += 1
                raise RatelimitException("202")
        monkeypatch.setattr(sp, "_DDGS", lambda: _Boom())
        monkeypatch.setattr(sp.time, "sleep", lambda *_: None)  # no real backoff sleep in test
        assert sp.DdgsProvider().search("x", 3) == []  # backoff+retry then empty, no raise
        assert calls["n"] == 2  # locks the contract: initial attempt + exactly one retry

    def test_always_available(self):
        import systemu.runtime.web.search_providers as sp
        assert sp.DdgsProvider().available() is True


class TestKeyedProviders:
    def test_tavily_available_only_with_key(self, monkeypatch):
        import systemu.runtime.web.search_providers as sp
        monkeypatch.delenv("SYSTEMU_TAVILY_API_KEY", raising=False)
        assert sp.TavilyProvider().available() is False
        monkeypatch.setenv("SYSTEMU_TAVILY_API_KEY", "k")
        assert sp.TavilyProvider().available() is True

    def test_exa_available_only_with_key(self, monkeypatch):
        import systemu.runtime.web.search_providers as sp
        monkeypatch.delenv("SYSTEMU_EXA_API_KEY", raising=False)
        assert sp.ExaProvider().available() is False
        monkeypatch.setenv("SYSTEMU_EXA_API_KEY", "k")
        assert sp.ExaProvider().available() is True

    def test_chain_order(self):
        import systemu.runtime.web.search_providers as sp
        names = [c.name for c in sp._CHAIN]
        assert names == ["tavily", "exa", "brave", "serper", "ddgs"]

    def test_keyed_provider_wins_over_ddgs(self, monkeypatch):
        import systemu.runtime.web.search_providers as sp
        monkeypatch.setenv("SYSTEMU_TAVILY_API_KEY", "k")
        monkeypatch.setattr(sp.TavilyProvider, "search",
                            lambda self, q, n: [{"title": "tav", "url": "u", "snippet": "s"}])
        # ddgs must never be consulted when tavily returns results. Use a COUNTER
        # (not a raise) — search() wraps every provider in try/except, which would
        # silently swallow an AssertionError and give a false pass.
        ddgs_calls = {"n": 0}
        def _ddgs_spy(self, q, n):
            ddgs_calls["n"] += 1
            return [{"title": "ddg", "url": "u", "snippet": "s"}]
        monkeypatch.setattr(sp.DdgsProvider, "search", _ddgs_spy)
        sp._CACHE.clear()
        out = sp.search("q", 3)
        assert out["provider"] == "tavily" and out["results"][0]["title"] == "tav"
        assert ddgs_calls["n"] == 0  # chain short-circuited at tavily; ddgs never reached


class TestSearchCache:
    def test_repeat_query_served_from_cache(self, monkeypatch):
        import systemu.runtime.web.search_providers as sp
        sp._CACHE.clear()
        calls = {"n": 0}
        def fake_ddgs_search(self, q, n):
            calls["n"] += 1
            return [{"title": "t", "url": "u", "snippet": "s"}]
        monkeypatch.delenv("SYSTEMU_TAVILY_API_KEY", raising=False)
        monkeypatch.delenv("SYSTEMU_EXA_API_KEY", raising=False)
        monkeypatch.delenv("SYSTEMU_BRAVE_API_KEY", raising=False)
        monkeypatch.delenv("SYSTEMU_SERPER_API_KEY", raising=False)
        monkeypatch.setattr(sp.DdgsProvider, "search", fake_ddgs_search)
        a = sp.search("cached query", 3)
        b = sp.search("cached query", 3)   # same query → cache hit, provider not called again
        assert a["results"] == b["results"]
        assert calls["n"] == 1

    def test_empty_not_cached(self, monkeypatch):
        import systemu.runtime.web.search_providers as sp
        sp._CACHE.clear()
        monkeypatch.delenv("SYSTEMU_TAVILY_API_KEY", raising=False)
        monkeypatch.delenv("SYSTEMU_EXA_API_KEY", raising=False)
        monkeypatch.delenv("SYSTEMU_BRAVE_API_KEY", raising=False)
        monkeypatch.delenv("SYSTEMU_SERPER_API_KEY", raising=False)
        monkeypatch.setattr(sp.DdgsProvider, "search", lambda self, q, n: [])
        sp.search("empty q", 3)
        assert ("empty q", 3) not in {k[:2] for k in sp._CACHE}  # degraded/empty not cached


class TestWebSearchNote:
    # v0.9.8: legacy free-search path (monkeypatched providers). The v0.9.8 web
    # stack is default-ON, so pin the legacy path here.
    @pytest.fixture(autouse=True)
    def _legacy_web(self, monkeypatch):
        monkeypatch.setenv("SYSTEMU_WEB_STACK_V2", "false")

    def test_note_present_when_degraded_empty(self, monkeypatch):
        import importlib.util, pathlib, systemu
        ws_path = pathlib.Path(systemu.__file__).parent / "vault" / "tools" / "implementations" / "web_search.py"
        spec = importlib.util.spec_from_file_location("ws_under_test", ws_path)
        ws = importlib.util.module_from_spec(spec); spec.loader.exec_module(ws)
        monkeypatch.setattr(ws, "_search_impl", None, raising=False)
        import systemu.runtime.web.search_providers as sp
        monkeypatch.setattr(sp, "search", lambda q, n=5: {"results": [], "provider": None,
                                                          "degraded": True, "error": "all providers failed/empty"})
        out = ws.run(query="salons", max_results=5)
        assert out["degraded"] is True and out["success"] is False
        assert "note" in out and "search" in out["note"].lower()

    def test_no_note_when_results(self, monkeypatch):
        import importlib.util, pathlib, systemu
        ws_path = pathlib.Path(systemu.__file__).parent / "vault" / "tools" / "implementations" / "web_search.py"
        spec = importlib.util.spec_from_file_location("ws_under_test2", ws_path)
        ws = importlib.util.module_from_spec(spec); spec.loader.exec_module(ws)
        import systemu.runtime.web.search_providers as sp
        monkeypatch.setattr(sp, "search", lambda q, n=5: {"results": [{"title":"t","url":"u","snippet":"s"}],
                                                          "provider": "ddgs", "degraded": True, "error": None})
        out = ws.run(query="salons", max_results=5)
        assert out["success"] is True
        assert "note" not in out  # no spurious note when results are present


class TestSearchFailFast:
    def test_is_degraded_search_result(self):
        from systemu.runtime.shadow_runtime import _is_degraded_search_result
        assert _is_degraded_search_result("web_search", {"degraded": True, "results": []}) is True
        assert _is_degraded_search_result("web_search", {"degraded": True, "results": [{"x":1}]}) is False
        assert _is_degraded_search_result("web_search", {"degraded": False, "results": []}) is False
        assert _is_degraded_search_result("create_word_doc", {"degraded": True, "results": []}) is False
        assert _is_degraded_search_result("web_search", None) is False
