"""v0.8.10 tiered web capability tests."""
from __future__ import annotations
from unittest.mock import MagicMock, patch
import pytest


class TestFetchCore:
    def test_extract_readable_pulls_title_and_text(self):
        from systemu.runtime.web.fetch_core import extract_readable
        html = """
        <html><head><title>Cheese Guide</title></head>
        <body><nav>menu</nav><article><h1>Best Cheese</h1>
        <p>Gouda is great. Brie is creamy.</p></article>
        <script>var x=1;</script><footer>foot</footer></body></html>
        """
        out = extract_readable(html, "https://example.com/guide")
        assert out["title"] == "Cheese Guide"
        assert "Gouda is great" in out["text"]
        assert "var x=1" not in out["text"]      # script stripped
        assert "menu" not in out["text"]          # nav stripped

    def test_extract_readable_resolves_relative_links(self):
        from systemu.runtime.web.fetch_core import extract_readable
        html = '<html><body><a href="/shop">Shop</a><a href="https://x.com/a">A</a></body></html>'
        out = extract_readable(html, "https://example.com/page")
        urls = [l["url"] for l in out["links"]]
        assert "https://example.com/shop" in urls
        assert "https://x.com/a" in urls

    def test_looks_like_spa_true_on_empty_shell(self):
        from systemu.runtime.web.fetch_core import looks_like_spa
        html = '<html><body><div id="root"></div><script src="a.js"></script><script src="b.js"></script><script src="c.js"></script><script src="d.js"></script><script src="e.js"></script><script src="f.js"></script></body></html>'
        assert looks_like_spa(html, "") is True

    def test_looks_like_spa_false_on_content_page(self):
        from systemu.runtime.web.fetch_core import looks_like_spa
        html = "<html><body><article>" + ("real content " * 100) + "</article></body></html>"
        text = "real content " * 100
        assert looks_like_spa(html, text) is False


class TestSearchProviders:
    def test_keyed_provider_wins_when_available(self, monkeypatch):
        from systemu.runtime.web import search_providers as sp
        monkeypatch.setenv("SYSTEMU_BRAVE_API_KEY", "k")
        fake = [{"title": "T", "url": "https://x", "snippet": "s"}]
        monkeypatch.setattr(sp.BraveProvider, "search", lambda self, q, n: fake)
        out = sp.search("cheese", 5)
        assert out["provider"] == "brave"
        assert out["degraded"] is False
        assert out["results"] == fake

    def test_falls_back_to_free_when_no_key(self, monkeypatch):
        from systemu.runtime.web import search_providers as sp
        sp._CACHE.clear()
        monkeypatch.delenv("SYSTEMU_TAVILY_API_KEY", raising=False)
        monkeypatch.delenv("SYSTEMU_EXA_API_KEY", raising=False)
        monkeypatch.delenv("SYSTEMU_BRAVE_API_KEY", raising=False)
        monkeypatch.delenv("SYSTEMU_SERPER_API_KEY", raising=False)
        free = [{"title": "F", "url": "https://f", "snippet": "x"}]
        monkeypatch.setattr(sp.DdgsProvider, "search", lambda self, q, n: free)
        out = sp.search("cheese", 5)
        assert out["provider"] == "ddgs"
        assert out["degraded"] is True

    def test_all_fail_returns_empty_with_chain(self, monkeypatch):
        from systemu.runtime.web import search_providers as sp
        sp._CACHE.clear()
        monkeypatch.delenv("SYSTEMU_TAVILY_API_KEY", raising=False)
        monkeypatch.delenv("SYSTEMU_EXA_API_KEY", raising=False)
        monkeypatch.delenv("SYSTEMU_BRAVE_API_KEY", raising=False)
        monkeypatch.delenv("SYSTEMU_SERPER_API_KEY", raising=False)
        monkeypatch.setattr(sp.DdgsProvider, "search", lambda self, q, n: [])
        out = sp.search("cheese", 5)
        assert out["results"] == []
        assert "ddgs" in out.get("error", "")

    def test_keyed_provider_unavailable_without_env(self, monkeypatch):
        from systemu.runtime.web import search_providers as sp
        monkeypatch.delenv("SYSTEMU_BRAVE_API_KEY", raising=False)
        assert sp.BraveProvider().available() is False


class TestProvision:
    def test_chromium_present_true_when_executable_exists(self, monkeypatch, tmp_path):
        from systemu.runtime.web import provision
        exe = tmp_path / "chrome"; exe.write_text("x")
        monkeypatch.setattr(provision, "_chromium_executable_path", lambda: str(exe))
        assert provision.chromium_present() is True

    def test_chromium_present_false_when_missing(self, monkeypatch):
        from systemu.runtime.web import provision
        monkeypatch.setattr(provision, "_chromium_executable_path", lambda: None)
        assert provision.chromium_present() is False

    def test_ensure_skips_when_env_set(self, monkeypatch):
        from systemu.runtime.web import provision
        monkeypatch.setenv("SYSTEMU_SKIP_BROWSER_AUTOINSTALL", "true")
        spy = MagicMock()
        monkeypatch.setattr(provision.subprocess, "Popen", spy)
        provision._bootstrapped = False
        provision.ensure_chromium_async()
        spy.assert_not_called()

    def test_ensure_spawns_install_when_missing(self, monkeypatch):
        from systemu.runtime.web import provision
        monkeypatch.delenv("SYSTEMU_SKIP_BROWSER_AUTOINSTALL", raising=False)
        monkeypatch.setattr(provision, "chromium_present", lambda: False)
        spy = MagicMock()
        monkeypatch.setattr(provision.subprocess, "Popen", spy)
        provision._bootstrapped = False
        provision.ensure_chromium_async()
        assert spy.called

    def test_ensure_idempotent(self, monkeypatch):
        from systemu.runtime.web import provision
        monkeypatch.delenv("SYSTEMU_SKIP_BROWSER_AUTOINSTALL", raising=False)
        monkeypatch.setattr(provision, "chromium_present", lambda: False)
        spy = MagicMock()
        monkeypatch.setattr(provision.subprocess, "Popen", spy)
        provision._bootstrapped = False
        provision.ensure_chromium_async()
        provision.ensure_chromium_async()
        assert spy.call_count == 1


class TestDaemonProvisionHook:
    def test_daemon_module_imports_provision(self):
        import importlib, inspect
        from systemu.scheduler import daemon
        src = inspect.getsource(daemon)
        assert "ensure_chromium_async" in src, "daemon does not call the provision probe"


class TestClassifier:
    def test_structured_missing_packages_preferred(self):
        from systemu.recovery.classifier import classify_dry_run_error
        c = classify_dry_run_error("Tool failed", missing_packages=["playwright"])
        assert c.kind == "DEP_PENDING"
        assert c.missing_package == "playwright"

    def test_regex_fallback_still_works(self):
        from systemu.recovery.classifier import classify_dry_run_error
        c = classify_dry_run_error("ModuleNotFoundError: No module named 'lxml'")
        assert c.kind == "DEP_PENDING"
        assert c.missing_package == "lxml"

    def test_no_unknown_when_structured_present(self):
        from systemu.recovery.classifier import classify_dry_run_error
        c = classify_dry_run_error("some opaque error", missing_packages=["requests"])
        assert c.missing_package == "requests"
        assert c.missing_package != "unknown"


class TestPackaging:
    def test_package_data_includes_vault_and_implementations(self):
        import pathlib
        try:
            import tomllib  # Python 3.11+
        except ModuleNotFoundError:  # Python 3.10
            tomllib = pytest.importorskip("tomli")
        root = pathlib.Path(__file__).resolve().parent.parent
        data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        # include-package-data on, and package-data has vault globs
        tool = data.get("tool", {}).get("setuptools", {})
        assert tool.get("include-package-data") is True
        pdata = tool.get("package-data", {})
        globs = " ".join(v for vals in pdata.values() for v in vals)
        assert "vault" in globs
        assert "implementations" in globs or "*.py" in globs


class TestBrowserPool:
    def test_pool_caps_concurrency(self, monkeypatch):
        from systemu.runtime.web import browser_pool as bp
        monkeypatch.setenv("SYSTEMU_BROWSER_MAX_CONTEXTS", "2")
        import importlib; importlib.reload(bp)
        sem = bp._make_semaphore()
        assert sem._value == 2

    def test_a11y_snapshot_parses_nodes(self):
        from systemu.runtime.web.browser_pool import parse_a11y_snapshot
        raw = {"role": "WebArea", "name": "Page", "children": [
            {"role": "link", "name": "Login"},
            {"role": "button", "name": "Submit", "children": [
                {"role": "text", "name": "Submit"}]},
        ]}
        nodes = parse_a11y_snapshot(raw)
        roles = {(n["role"], n["name"]) for n in nodes}
        assert ("link", "Login") in roles
        assert ("button", "Submit") in roles

    def test_domain_denied_blocks(self, monkeypatch):
        from systemu.runtime.web import browser_pool as bp
        monkeypatch.setenv("SYSTEMU_WEB_DENY_DOMAINS", "evil.com,bad.org")
        import importlib; importlib.reload(bp)
        assert bp.is_url_allowed("https://evil.com/x") is False
        assert bp.is_url_allowed("https://good.com/x") is True

    def test_allow_list_restricts(self, monkeypatch):
        from systemu.runtime.web import browser_pool as bp
        monkeypatch.setenv("SYSTEMU_WEB_DENY_DOMAINS", "")
        monkeypatch.setenv("SYSTEMU_WEB_ALLOW_DOMAINS", "good.com")
        import importlib; importlib.reload(bp)
        assert bp.is_url_allowed("https://good.com/x") is True
        assert bp.is_url_allowed("https://other.com/x") is False


class TestWebAct:
    def test_act_loop_executes_click_type_done(self, monkeypatch):
        from systemu.runtime.web import act_loop
        # fake page with a11y snapshot + locator recorder
        calls = []
        class FakePage:
            def accessibility_snapshot(self): return {"role":"WebArea","children":[
                {"role":"link","name":"Login"},{"role":"textbox","name":"User"}]}
            def click_ref(self, ref): calls.append(("click", ref))
            def type_ref(self, ref, text): calls.append(("type", ref, text))
            def read_text(self): return "Welcome"
        # LLM returns CLICK then TYPE then DONE
        plan = iter([
            {"action":"CLICK","ref":"e1"},
            {"action":"TYPE","ref":"e2","text":"bob"},
            {"action":"DONE","result":"logged in"},
        ])
        monkeypatch.setattr(act_loop, "_plan_next", lambda *a, **k: next(plan))
        out = act_loop.run_act_loop(FakePage(), "log in as bob", max_steps=8)
        assert out["success"] is True
        assert out["result"] == "logged in"
        assert ("click", "e1") in calls
        assert ("type", "e2", "bob") in calls

    def test_act_loop_max_steps_terminates(self, monkeypatch):
        from systemu.runtime.web import act_loop
        class FakePage:
            def accessibility_snapshot(self): return {"role":"WebArea","children":[]}
            def click_ref(self, ref): pass
            def type_ref(self, ref, text): pass
            def read_text(self): return ""
        monkeypatch.setattr(act_loop, "_plan_next", lambda *a, **k: {"action":"READ"})
        out = act_loop.run_act_loop(FakePage(), "loop forever", max_steps=3)
        assert out["success"] is False
        assert len(out["steps"]) == 3

    def test_act_loop_refuses_password_type(self, monkeypatch):
        from systemu.runtime.web import act_loop
        typed = []
        class FakePage:
            def accessibility_snapshot(self): return {"role":"WebArea","children":[
                {"role":"textbox","name":"Password"}]}
            def click_ref(self, ref): pass
            def type_ref(self, ref, text): typed.append(text)
            def read_text(self): return ""
        plan = iter([{"action":"TYPE","ref":"e1","text":"secret"},{"action":"DONE","result":"x"}])
        monkeypatch.setattr(act_loop, "_plan_next", lambda *a, **k: next(plan))
        out = act_loop.run_act_loop(FakePage(), "type password", max_steps=8)
        assert "secret" not in typed   # password field refused


class TestToolWrappers:
    # v0.9.8: these exercise the LEGACY web-tool backends (monkeypatched). The
    # v0.9.8 web stack (Jina/DDG/Overpass via web_access) is default-ON, so pin
    # the legacy path here; the V2 path is covered by test_v0_9_8_web_tools_wiring.
    @pytest.fixture(autouse=True)
    def _legacy_web(self, monkeypatch):
        monkeypatch.setenv("SYSTEMU_WEB_STACK_V2", "false")

    def test_web_search_tool_shape(self, monkeypatch):
        import importlib.util, pathlib
        p = pathlib.Path(__file__).resolve().parent.parent / "systemu/vault/tools/implementations/web_search.py"
        spec = importlib.util.spec_from_file_location("ws", p); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
        assert m.TOOL_META["name"] == "web_search"
        monkeypatch.setattr("systemu.runtime.web.search_providers.search",
                            lambda q, n=5: {"results":[{"title":"T","url":"u","snippet":"s"}],"provider":"brave","degraded":False})
        out = m.run(query="cheese")
        assert out["success"] is True
        assert out["results"][0]["title"] == "T"

    def test_web_read_uses_t0_then_escalates(self, monkeypatch):
        import importlib.util, pathlib
        p = pathlib.Path(__file__).resolve().parent.parent / "systemu/vault/tools/implementations/web_read.py"
        spec = importlib.util.spec_from_file_location("wr", p); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
        from systemu.runtime.web import fetch_core
        monkeypatch.setattr(fetch_core, "fetch_url", lambda url, timeout=20: fetch_core.FetchResult(ok=True, status=200, html="<html><body><article>"+("x "*200)+"</article></body></html>"))
        out = m.run(url="https://example.com")
        assert out["success"] is True
        assert out["tier_used"] == "fetch"


class TestSeedIntegrity:
    def _impl_dir(self):
        import pathlib
        return pathlib.Path(__file__).resolve().parent.parent / "systemu/vault/tools/implementations"

    def test_old_web_tools_removed(self):
        d = self._impl_dir()
        for gone in ["web_extract_text.py","web_extract_table.py","browser_navigate.py","mouse_click.py","mouse_drag.py"]:
            assert not (d / gone).exists(), f"{gone} should be deleted"

    def test_new_web_tools_present(self):
        d = self._impl_dir()
        for f in ["web_search.py","web_read.py","web_screenshot.py","web_act.py"]:
            assert (d / f).exists()

    def test_cruft_tools_removed(self):
        d = self._impl_dir()
        for gone in ["fetch_github_prs.py","fetch_nse_stock_data.py","fetch_reddit_posts.py","search_emails.py","send_email.py","fetch_docker_hub_metadata.py","calculate_rsi.py","calculate_sma.py","github_get_commit.py"]:
            assert not (d / gone).exists(), f"cruft {gone} should be deleted"

    # v0.8.10: curated KEEP list is 38 tools. The 7 below were wrongly deleted
    # during curation (they had impls but no index entries) and restored.
    RESTORED_CURATED_TOOLS = {
        "api_call_get", "detect_language_from_extension", "file_scan_directory",
        "run_cli_command", "write_csv_file", "write_markdown_file", "write_text_file",
    }

    def test_index_matches_disk(self):
        import json, pathlib
        root = pathlib.Path(__file__).resolve().parent.parent / "systemu/vault/tools"
        idx = json.loads((root / "index.json").read_text(encoding="utf-8"))
        idx_names = {e["name"] for e in idx}
        disk_impls = {p.stem for p in (root / "implementations").glob("*.py")}
        # index set == impl set (bidirectional, no orphans either way)
        assert idx_names == disk_impls, (
            f"index/impl mismatch: index-only={idx_names - disk_impls}, "
            f"impl-only={disk_impls - idx_names}"
        )
        # curated count locked at 41
        # v0.8.21: bumped 38 -> 40 after adding `extract_records` and `web_extract`.
        # v0.9.8: bumped 40 -> 41 after adding `find_places` (OSM local-POI tool).
        assert len(idx) == 41, f"expected 41 curated tools, got {len(idx)}"
        assert len(disk_impls) == 41, f"expected 41 impl files, got {len(disk_impls)}"
        # no dangling old web tools in index
        for gone in ["web_extract_text","web_extract_table","browser_navigate","mouse_click","mouse_drag"]:
            assert gone not in idx_names

    def test_restored_curated_tools_present(self):
        """The 7 KEEP-list tools wrongly dropped during curation are restored:
        present on disk, in tools/index.json, and resolvable by deterministic id."""
        import json, pathlib
        root = pathlib.Path(__file__).resolve().parent.parent / "systemu/vault/tools"
        idx = json.loads((root / "index.json").read_text(encoding="utf-8"))
        idx_names = {e["name"] for e in idx}
        idx_ids = {e["id"] for e in idx}
        disk_impls = {p.stem for p in (root / "implementations").glob("*.py")}
        for name in self.RESTORED_CURATED_TOOLS:
            assert name in disk_impls, f"restored tool impl missing: {name}.py"
            assert name in idx_names, f"restored tool not in index: {name}"
            assert f"tool_{name}" in idx_ids, f"deterministic id tool_{name} missing"
            rec = root / f"tool_tool_{name}.json"
            assert rec.exists(), f"tool record missing: {rec.name}"


class TestSkillSeed:
    def test_task_specific_skills_dropped(self):
        import json, pathlib
        root = pathlib.Path(__file__).resolve().parent.parent / "systemu/vault/skills"
        idx = json.loads((root / "index.json").read_text(encoding="utf-8"))
        names = {e.get("name","") for e in idx}
        for gone in ["clinical_trial_search","clinical_trial_detail_extraction","ci_cd_pipeline_analysis"]:
            assert gone not in names

    def test_skill_index_matches_disk(self):
        import json, pathlib
        root = pathlib.Path(__file__).resolve().parent.parent / "systemu/vault/skills"
        idx = json.loads((root / "index.json").read_text(encoding="utf-8"))
        for e in idx:
            name = e.get("name", "")
            hyph = name.replace("_", "-")
            assert (root / hyph).exists(), f"index entry {name} has no dir {hyph}"


class TestFreshPosture:
    def _vault(self):
        import pathlib
        return pathlib.Path(__file__).resolve().parent.parent / "systemu/vault"

    def test_seed_has_no_runtime_state(self):
        import json
        v = self._vault()
        for kind in ["scrolls","activities","evolutions"]:
            idx = v / kind / "index.json"
            if idx.exists():
                assert json.loads(idx.read_text(encoding="utf-8")) == [], f"{kind} not empty"

    def test_exactly_one_shadow_wild_card(self):
        import json
        v = self._vault()
        idx = json.loads((v / "shadow_army/index.json").read_text(encoding="utf-8"))
        assert len(idx) == 1
        assert idx[0].get("name") == "Wild Card"

    def test_wild_card_tool_ids_resolve(self):
        import json
        v = self._vault()
        tools = {e["id"] for e in json.loads((v / "tools/index.json").read_text(encoding="utf-8"))}
        sh_idx = json.loads((v / "shadow_army/index.json").read_text(encoding="utf-8"))
        # load the wild card shadow record
        wc_id = sh_idx[0]["id"]
        rec = json.loads((v / f"shadow_army/shadow_{wc_id}/shadow.json").read_text(encoding="utf-8"))
        for tid in rec.get("available_tool_ids", []):
            assert tid in tools, f"Wild Card references missing tool {tid}"
