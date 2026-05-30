"""v0.8.11 fixes: RC1 dep-message, RC2 chat-refresh, wildcard wiring-lock."""
from __future__ import annotations
import json, pathlib
import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent  # repo root


class TestInstallHint:
    def test_real_package_gives_pip_hint(self):
        from systemu.runtime.shadow_runtime import _install_hint
        assert _install_hint(["playwright"]) == "pip install playwright"

    def test_phrase_fallback_gives_manifest_hint(self):
        from systemu.runtime.shadow_runtime import _install_hint
        # a human phrase (has spaces) is NOT a package — must not produce 'pip install <phrase>'
        assert _install_hint(["a required package (see tool manifest)"]) == "see the tool's manifest"

    def test_empty_list_gives_manifest_hint(self):
        from systemu.runtime.shadow_runtime import _install_hint
        assert _install_hint([]) == "see the tool's manifest"

    def test_first_real_package_used(self):
        from systemu.runtime.shadow_runtime import _install_hint
        assert _install_hint(["requests", "lxml"]) == "pip install requests"


class TestDepFallback:
    def test_uses_result_missing_packages_when_present(self):
        from systemu.runtime.shadow_runtime import _resolve_missing_packages
        out = _resolve_missing_packages(
            result_missing=["playwright"], declared=["something-else"])
        assert out == ["playwright"]

    def test_falls_back_to_declared_when_result_empty(self):
        from systemu.runtime.shadow_runtime import _resolve_missing_packages
        out = _resolve_missing_packages(result_missing=None, declared=["requests"])
        assert out == ["requests"]

    def test_phrase_when_nothing_known(self):
        from systemu.runtime.shadow_runtime import _resolve_missing_packages
        out = _resolve_missing_packages(result_missing=None, declared=[])
        assert out == ["a required package (see tool manifest)"]
        # critically: NOT ["unknown"]
        assert "unknown" not in out


class TestClassifierWiring:
    def test_classifier_consumes_structured_missing_packages(self):
        from systemu.recovery.classifier import classify_dry_run_error
        c = classify_dry_run_error("opaque error text", missing_packages=["lxml"])
        assert c.kind == "DEP_PENDING"
        assert c.missing_package == "lxml"

    def test_engine_passes_structured_when_present(self):
        # engine reads evidence.get('missing_packages') and forwards it.
        # We assert the caller threads it through (source inspection).
        import inspect
        from systemu.recovery import engine
        src = inspect.getsource(engine)
        assert "missing_packages=" in src, "engine must forward structured missing_packages"

    def test_jobs_record_failure_forwards_structured(self):
        import inspect
        from systemu.scheduler import jobs
        src = inspect.getsource(jobs)
        assert "missing_packages=" in src, "jobs must forward structured missing_packages"


class TestChatRefreshGuard:
    def test_guard_true_when_connected(self):
        from systemu.interface.pages.chat_page import _should_schedule_refresh
        class C:  has_socket_connection = True
        assert _should_schedule_refresh(C()) is True

    def test_guard_false_when_disconnected(self):
        from systemu.interface.pages.chat_page import _should_schedule_refresh
        class C:  has_socket_connection = False
        assert _should_schedule_refresh(C()) is False

    def test_guard_false_on_missing_attr(self):
        from systemu.interface.pages.chat_page import _should_schedule_refresh
        class C:  pass  # no has_socket_connection
        assert _should_schedule_refresh(C()) is False

    def test_run_finally_checks_guard_before_timer(self):
        import inspect
        from systemu.interface.pages import chat_page
        src = inspect.getsource(chat_page)
        # the guard must be consulted (so the async ui.timer crash can't escape)
        assert "_should_schedule_refresh" in src


class TestWildcardWiring:
    def _vault(self):
        return _ROOT / "systemu/vault"

    def test_exactly_one_wild_card(self):
        v = self._vault()
        idx = json.loads((v / "shadow_army/index.json").read_text(encoding="utf-8"))
        assert len(idx) == 1
        assert idx[0]["name"] == "Wild Card"
        assert idx[0]["id"] == "shadow_wildcard"

    def test_wild_card_tool_ids_match_curated_set(self):
        v = self._vault()
        wc = json.loads((v / "shadow_army/index.json").read_text(encoding="utf-8"))[0]
        tool_ids = {e["id"] for e in json.loads((v / "tools/index.json").read_text(encoding="utf-8"))}
        wc_tools = set(wc["tool_ids"])
        assert wc_tools == tool_ids, f"drift: missing={tool_ids-wc_tools} extra={wc_tools-tool_ids}"

    def test_wild_card_skill_ids_match_curated_set(self):
        v = self._vault()
        wc = json.loads((v / "shadow_army/index.json").read_text(encoding="utf-8"))[0]
        skill_ids = {e["id"] for e in json.loads((v / "skills/index.json").read_text(encoding="utf-8"))}
        wc_skills = set(wc["skill_ids"])
        assert wc_skills == skill_ids, f"drift: missing={skill_ids-wc_skills} extra={wc_skills-skill_ids}"

    def test_wild_card_body_loads_via_model(self):
        from systemu.core.models import Shadow
        v = self._vault()
        body = json.loads((v / "shadow_army/shadow_shadow_wildcard/shadow.json").read_text(encoding="utf-8"))
        sh = Shadow(**body)
        assert sh.name == "Wild Card"
        assert str(sh.status).lower().endswith("awakened") or sh.status == "awakened"

    def test_exactly_one_shadow_dir_determinism_guard(self):
        # build-pollution guard: a stray shadow_shadow_* dir would mean the
        # wheel could ship extra Wild Cards (the v0.8.10 mode).
        v = self._vault()
        dirs = sorted(p.name for p in (v / "shadow_army").glob("shadow_shadow_*") if p.is_dir())
        assert dirs == ["shadow_shadow_wildcard"], f"unexpected shadow dirs: {dirs}"


class TestEngineDepMessage:
    def test_engine_dep_message_uses_declared_deps_not_unknown(self):
        # v0.8.11 RC1 fold-in: the recovery card for a DEP_PENDING tool with no
        # classifier-resolved package must fall back to the tool's declared
        # dependencies, never the literal '<unknown>'.
        import inspect, re
        from systemu.recovery import engine
        src = inspect.getsource(engine.RecoveryEngine.diagnose_tool)
        # the operator-facing dep-message must not surface '<unknown>'
        assert '"<unknown>"' not in src, (
            "diagnose_tool must not emit the literal '<unknown>' package"
        )
        assert "tool.dependencies" in src, (
            "engine must derive missing package from declared deps"
        )
        assert "a required package (see tool manifest)" in src, (
            "engine must use the clear human fallback phrase"
        )

    def test_engine_dep_message_names_declared_requests(self, monkeypatch):
        # Functional check: classifier returns DEP_PENDING with NO resolved
        # package (the path that previously produced '<unknown>'); the tool
        # declares dependencies=['requests'] -> the recovery card names
        # 'requests', never '<unknown>'.
        from systemu.recovery import engine
        from systemu.recovery.engine import RecoveryEngine
        from systemu.recovery.classifier import ClassifiedError

        # Force the classifier to yield DEP_PENDING with no missing_package so
        # the engine's declared-deps fallback is exercised.
        monkeypatch.setattr(
            engine, "classify_dry_run_error",
            lambda *a, **k: ClassifiedError(kind="DEP_PENDING", missing_package=None),
        )

        class _Tool:
            name = "fetcher"
            status = "approved"
            enabled = True
            dependencies = ["requests"]
            dry_run_status = "failed"
            dry_run_evidence = {"error": "opaque failure with no package name"}

        class _Vault:
            def find_tool(self, tool_id):
                return _Tool()

        actions = RecoveryEngine(_Vault()).diagnose_tool("tool_x")
        dep_actions = [a for a in actions if a.kind == "DEP_PENDING"]
        assert dep_actions, "expected a DEP_PENDING recovery action"
        reason = dep_actions[0].reason
        assert "requests" in reason, f"expected 'requests' in reason, got: {reason!r}"
        assert "<unknown>" not in reason
