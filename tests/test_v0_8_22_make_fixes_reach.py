"""v0.8.22 — make existing fixes reach users (A vault migrator + B toolkit-aware extractor + C inline pending-decision card)."""
from pathlib import Path
import json
import os
import pytest


# ── shared fixtures (used across tasks) ─────────────────────────────────────
@pytest.fixture
def tmp_vault(tmp_path):
    from systemu.vault.vault import Vault
    for sub in ["scrolls", "activities", "shadow_army", "skills", "tools/implementations",
                "evolutions", "notifications", "executions", "decisions"]:
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    for idx in ["scrolls", "activities", "shadow_army", "skills", "tools", "evolutions", "decisions"]:
        (tmp_path / idx / "index.json").write_text("[]", encoding="utf-8")
    return Vault(str(tmp_path))


@pytest.fixture
def fake_pkg_vault(tmp_path):
    """Builds a fake 'installed package vault' that the migrator scans, so tests
    are hermetic and never touch the real site-packages."""
    pkg = tmp_path / "_pkg_vault"
    (pkg / "tools" / "implementations").mkdir(parents=True)
    (pkg / "shadow_army" / "shadow_shadow_wildcard").mkdir(parents=True)
    # one seed tool
    (pkg / "tools" / "implementations" / "extract_records.py").write_text(
        "TOOL_META = {'name':'extract_records'}\ndef run(**kw):\n    return {'success': True}\n",
        encoding="utf-8")
    (pkg / "tools" / "tool_tool_extract_records.json").write_text(json.dumps({
        "id": "tool_extract_records", "name": "extract_records",
        "tool_type": "api_call", "status": "deployed", "enabled": True,
        "forged_by_systemu": False, "version": 1,
    }), encoding="utf-8")
    (pkg / "tools" / "index.json").write_text(json.dumps([{
        "id": "tool_extract_records", "name": "extract_records",
        "tool_type": "api_call", "status": "deployed", "enabled": True,
        "forged_by_systemu": False, "created_at": "2026-06-02",
    }]), encoding="utf-8")
    # Wild Card index entry
    (pkg / "shadow_army" / "index.json").write_text(json.dumps([{
        "id": "shadow_wildcard", "name": "Wild Card", "status": "awakened",
        "tool_ids": ["tool_extract_records"], "skill_ids": [],
    }]), encoding="utf-8")
    return pkg


class TestVaultMigratorFastPath:
    def test_version_match_short_circuits(self, tmp_vault, fake_pkg_vault, monkeypatch):
        from systemu.runtime.vault_migrator import run
        # vault is already at the target version
        (Path(tmp_vault.root) / ".seed_version").write_text("0.8.21", encoding="utf-8")
        monkeypatch.setattr("systemu.runtime.vault_migrator._installed_version", lambda: "0.8.21")
        monkeypatch.setattr("systemu.runtime.vault_migrator._package_vault_root",
                            lambda: fake_pkg_vault)
        out = run(Path(tmp_vault.root))
        assert out["fast_path"] is True
        # confirm no copies were performed
        assert not (Path(tmp_vault.root) / "tools" / "implementations" / "extract_records.py").exists()


class TestVaultMigratorOffSwitch:
    def test_env_off_short_circuits(self, tmp_vault, monkeypatch):
        from systemu.runtime.vault_migrator import run
        monkeypatch.setenv("SYSTEMU_VAULT_AUTO_MIGRATE", "off")
        out = run(Path(tmp_vault.root))
        assert out == {"skipped": True, "reason": "disabled"}


class TestVaultMigratorAddNew:
    def test_add_new_seed_tool(self, tmp_vault, fake_pkg_vault, monkeypatch):
        from systemu.runtime.vault_migrator import run
        monkeypatch.setattr("systemu.runtime.vault_migrator._installed_version", lambda: "0.8.22")
        monkeypatch.setattr("systemu.runtime.vault_migrator._package_vault_root",
                            lambda: fake_pkg_vault)
        out = run(Path(tmp_vault.root))
        assert out["added"] == 1 and out["updated"] == 0
        # files copied
        assert (Path(tmp_vault.root) / "tools" / "implementations" / "extract_records.py").exists()
        assert (Path(tmp_vault.root) / "tools" / "tool_tool_extract_records.json").exists()
        # index entry appended
        idx = json.loads((Path(tmp_vault.root) / "tools" / "index.json").read_text())
        assert any(e["name"] == "extract_records" for e in idx)
        # .seed_version written atomically
        assert (Path(tmp_vault.root) / ".seed_version").read_text().strip() == "0.8.22"


class TestVaultMigratorUpdateByName:
    def test_seed_updated_even_when_mis_flagged_forged(self, tmp_vault, fake_pkg_vault, monkeypatch):
        """POC finding: vault has all seed tools mis-flagged forged=true.
        Identity must be by NAME (not flag) so updates actually reach the vault."""
        from systemu.runtime.vault_migrator import run
        # Vault has the same tool name, but with OLD impl content + mis-flag
        impl = Path(tmp_vault.root) / "tools" / "implementations" / "extract_records.py"
        impl.write_text("# OLD CONTENT\n", encoding="utf-8")
        (Path(tmp_vault.root) / "tools" / "index.json").write_text(json.dumps([{
            "id": "tool_old_id", "name": "extract_records",
            "forged_by_systemu": True,  # mis-flagged!
            "enabled": True, "status": "deployed",
        }]), encoding="utf-8")
        monkeypatch.setattr("systemu.runtime.vault_migrator._installed_version", lambda: "0.8.22")
        monkeypatch.setattr("systemu.runtime.vault_migrator._package_vault_root",
                            lambda: fake_pkg_vault)
        out = run(Path(tmp_vault.root))
        assert out["updated"] == 1 and out["added"] == 0
        assert "OLD CONTENT" not in impl.read_text(encoding="utf-8")
        # auto-heal: index entry replaced with package authoritative version
        idx = json.loads((Path(tmp_vault.root) / "tools" / "index.json").read_text())
        seed_entry = next(e for e in idx if e["name"] == "extract_records")
        assert seed_entry["forged_by_systemu"] is False


class TestVaultMigratorSkipForged:
    def test_unique_name_forged_tool_untouched(self, tmp_vault, fake_pkg_vault, monkeypatch):
        from systemu.runtime.vault_migrator import run
        # user-forged tool with unique name NOT in package
        impl = Path(tmp_vault.root) / "tools" / "implementations" / "my_custom.py"
        impl.write_text("# USER CUSTOM\n", encoding="utf-8")
        (Path(tmp_vault.root) / "tools" / "index.json").write_text(json.dumps([{
            "id": "tool_custom", "name": "my_custom",
            "forged_by_systemu": True, "enabled": True, "status": "deployed",
        }]), encoding="utf-8")
        monkeypatch.setattr("systemu.runtime.vault_migrator._installed_version", lambda: "0.8.22")
        monkeypatch.setattr("systemu.runtime.vault_migrator._package_vault_root",
                            lambda: fake_pkg_vault)
        run(Path(tmp_vault.root))
        # user's custom tool untouched
        assert impl.read_text(encoding="utf-8") == "# USER CUSTOM\n"


class TestVaultMigratorWildCardWiring:
    def test_wild_card_gets_new_tool_ids(self, tmp_vault, fake_pkg_vault, monkeypatch):
        from systemu.runtime.vault_migrator import run
        # pre-populate vault Wild Card with no v0.8.22 tools
        wc_dir = Path(tmp_vault.root) / "shadow_army" / "shadow_shadow_wildcard"
        wc_dir.mkdir(parents=True, exist_ok=True)
        (wc_dir / "shadow.json").write_text(json.dumps({
            "id": "shadow_wildcard", "name": "Wild Card",
            "tool_ids": ["tool_existing"], "skill_ids": [],
        }), encoding="utf-8")
        (Path(tmp_vault.root) / "shadow_army" / "index.json").write_text(json.dumps([{
            "id": "shadow_wildcard", "name": "Wild Card",
            "tool_ids": ["tool_existing"], "skill_ids": [],
        }]), encoding="utf-8")
        monkeypatch.setattr("systemu.runtime.vault_migrator._installed_version", lambda: "0.8.22")
        monkeypatch.setattr("systemu.runtime.vault_migrator._package_vault_root",
                            lambda: fake_pkg_vault)
        out = run(Path(tmp_vault.root))
        assert out["wild_card_added"] == 1
        # shadow.json updated; user's tool_existing preserved
        wc = json.loads((wc_dir / "shadow.json").read_text())
        assert "tool_extract_records" in wc["tool_ids"]
        assert "tool_existing" in wc["tool_ids"]  # never removes


class TestVaultMigratorCorruptedIndexSafe:
    def test_corrupted_pkg_index_safe_fail(self, tmp_vault, fake_pkg_vault, monkeypatch):
        from systemu.runtime.vault_migrator import run
        (fake_pkg_vault / "tools" / "index.json").write_text("not valid json", encoding="utf-8")
        monkeypatch.setattr("systemu.runtime.vault_migrator._installed_version", lambda: "0.8.22")
        monkeypatch.setattr("systemu.runtime.vault_migrator._package_vault_root",
                            lambda: fake_pkg_vault)
        out = run(Path(tmp_vault.root))
        # no crash; errors recorded; daemon would boot regardless
        assert "errors" in out and len(out["errors"]) >= 1


class TestDaemonHook:
    def test_migrator_called_from_daemon(self, monkeypatch, tmp_vault, fake_pkg_vault):
        """The daemon hook calls vault_migrator.run with the unified vault."""
        from systemu.runtime import vault_migrator
        called = {}
        original = vault_migrator.run
        def _spy(vault_dir, *, logger_=None):
            called["vault_dir"] = Path(vault_dir)
            return {"fast_path": True}
        monkeypatch.setattr("systemu.runtime.vault_migrator.run", _spy)
        # call the hook helper directly
        from systemu.scheduler.daemon import _v0822_run_vault_migrator
        _v0822_run_vault_migrator(tmp_vault, logger_=None)
        assert called["vault_dir"] == Path(tmp_vault.root)

    def test_migrator_failure_never_raises(self, monkeypatch, tmp_vault):
        from systemu.scheduler.daemon import _v0822_run_vault_migrator
        def _explode(*a, **kw): raise RuntimeError("migrator boom")
        monkeypatch.setattr("systemu.runtime.vault_migrator.run", _explode)
        # must NOT raise — daemon would crash otherwise
        _v0822_run_vault_migrator(tmp_vault, logger_=None)


# ── v0.8.22 Task 3: toolkit-aware activity extractor ────────────────────────


class TestEnrichToolWithBody:
    def test_falls_back_to_body_when_summary_empty(self, tmp_path):
        """v0.8.22 B: when index header has no parameters_schema_summary,
        the enricher reads the per-tool body file to get the full schema."""
        from systemu.vault.vault import Vault
        # set up a minimal vault with a tool whose header is sparse but body is rich
        for sub in ["tools/implementations", "scrolls", "activities", "shadow_army",
                    "skills", "evolutions", "notifications", "executions", "decisions"]:
            (tmp_path / sub).mkdir(parents=True, exist_ok=True)
        (tmp_path / "tools" / "index.json").write_text(json.dumps([{
            "id": "tool_t1", "name": "t1",
            "description": "test tool", "enabled": True, "status": "deployed",
            # NO parameters_schema_summary
        }]), encoding="utf-8")
        (tmp_path / "tools" / "tool_tool_t1.json").write_text(json.dumps({
            "id": "tool_t1", "name": "t1",
            "parameters_schema": {"x": {"type": "string", "required": True}},
            "return_schema": {"ok": {"type": "boolean"}},
        }), encoding="utf-8")
        for idx in ["scrolls","activities","shadow_army","skills","evolutions","decisions"]:
            (tmp_path / idx / "index.json").write_text("[]", encoding="utf-8")
        v = Vault(str(tmp_path))
        from systemu.pipelines.activity_extractor import _enrich_tool_for_catalog
        header = json.loads((tmp_path/"tools"/"index.json").read_text())[0]
        enriched = _enrich_tool_for_catalog(header, v)
        # parameters_schema fell back from body file
        assert enriched["parameters_schema"] == {"x": {"type": "string", "required": True}}


class TestForgeRationalePersistence:
    def test_forge_rationale_logged_on_new_tool(self, monkeypatch, tmp_path, caplog):
        """When the LLM emits forge_rationale on a new tool, it shows in logs."""
        import logging
        caplog.set_level(logging.INFO, logger="systemu.pipelines.activity_extractor")
        from systemu.pipelines import activity_extractor as ae
        ae._log_forge_rationale({"name": "search_places", "is_new": True,
                                 "forge_rationale": "no existing tool returns places"})
        assert any("search_places" in r.message and "no existing tool" in r.message
                   for r in caplog.records)


class TestPromptSteeringLanguage:
    def test_prompt_includes_strong_steering(self):
        from pathlib import Path
        import systemu
        p = Path(systemu.__file__).parent / "prompts" / "extract_skills_tools.md"
        text = p.read_text(encoding="utf-8")
        # v0.8.22 must add explicit steering + forge_rationale instructions
        assert "PREFER existing" in text or "prefer existing" in text.lower()
        assert "forge_rationale" in text


class TestChatSubmissionContext:
    def test_default_is_none(self):
        from systemu.runtime.chat_submission_ctx import current_chat_submission_id
        assert current_chat_submission_id() is None

    def test_set_and_get(self):
        from systemu.runtime.chat_submission_ctx import (
            current_chat_submission_id, set_chat_submission_id,
        )
        tok = set_chat_submission_id("sub_123")
        try:
            assert current_chat_submission_id() == "sub_123"
        finally:
            set_chat_submission_id(None, reset_token=tok)
        assert current_chat_submission_id() is None


class TestNotificationsThreadChatSubmissionId:
    # NOTE: notify_user() auto-accepts when len(actions)==1 (early return before
    # queue.post), and only routes to the queue when SYSTEMU_DECISION_QUEUE=true.
    # Tests below use two actions + env-flag so queue.post is actually reached.
    def test_notify_user_threads_chat_id(self, monkeypatch):
        from systemu.interface import notifications as nf
        from systemu.runtime.chat_submission_ctx import set_chat_submission_id
        from systemu.approval.exceptions import PendingOperatorDecision
        monkeypatch.setenv("SYSTEMU_DECISION_QUEUE", "true")
        captured = {}
        class _Q:
            def get_resolved_choice(self, k): return None
            def post(self, **kw): captured.update(kw); return "dec_x"
        monkeypatch.setattr(nf, "_get_decision_queue", lambda: _Q())
        tok = set_chat_submission_id("sub_abc")
        try:
            try:
                nf.notify_user("title", "body", ["A", "B"], dedup_key="dk1")
            except PendingOperatorDecision:
                pass
        finally:
            set_chat_submission_id(None, reset_token=tok)
        assert captured["context"].get("chat_submission_id") == "sub_abc"

    def test_request_choice_threads_chat_id(self, monkeypatch):
        from systemu.interface import notifications as nf
        from systemu.runtime.chat_submission_ctx import set_chat_submission_id
        from systemu.approval.exceptions import PendingChoiceRequest
        captured = {}
        class _Q:
            def get_resolved_choice(self, k): return None
            def post(self, **kw): captured.update(kw); return "dec_y"
        monkeypatch.setattr(nf, "_get_decision_queue", lambda: _Q())
        tok = set_chat_submission_id("sub_xyz")
        try:
            try:
                nf.request_choice([{"id": "a", "options": []}], dedup_key="k1")
            except PendingChoiceRequest:
                pass
        finally:
            set_chat_submission_id(None, reset_token=tok)
        assert captured["context"].get("chat_submission_id") == "sub_xyz"

    def test_no_chat_id_means_no_field(self, monkeypatch):
        from systemu.interface import notifications as nf
        from systemu.approval.exceptions import PendingChoiceRequest
        # Use request_choice (always posts) so we reliably hit queue.post.
        captured = {}
        class _Q:
            def get_resolved_choice(self, k): return None
            def post(self, **kw): captured.update(kw); return "dec_z"
        monkeypatch.setattr(nf, "_get_decision_queue", lambda: _Q())
        # ContextVar default is None
        try:
            nf.request_choice([{"id": "a", "options": []}], dedup_key="k_no_chat")
        except PendingChoiceRequest:
            pass
        assert "chat_submission_id" not in (captured.get("context") or {})


class TestDecisionQueueEventBus:
    def test_post_publishes_event(self, tmp_vault, monkeypatch):
        from systemu.approval.decision_queue import OperatorDecisionQueue
        published = []
        class _Bus:
            def publish(self, ev): published.append(ev)
        from systemu.interface import event_bus
        monkeypatch.setattr(event_bus.EventBus, "get", classmethod(lambda cls: _Bus()))
        q = OperatorDecisionQueue(tmp_vault)
        did = q.post(title="t", body="b", options=["A"],
                    context={"chat_submission_id": "sub_x"})
        # the posted event carries category + decision_id + chat_submission_id
        assert any(ev.get("category") == "operator_decision_posted"
                   and ev.get("context", {}).get("decision_id") == did
                   and ev.get("context", {}).get("chat_submission_id") == "sub_x"
                   for ev in published)

    def test_resolve_publishes_event(self, tmp_vault, monkeypatch):
        from systemu.approval.decision_queue import OperatorDecisionQueue
        published = []
        class _Bus:
            def publish(self, ev): published.append(ev)
        from systemu.interface import event_bus
        monkeypatch.setattr(event_bus.EventBus, "get", classmethod(lambda cls: _Bus()))
        q = OperatorDecisionQueue(tmp_vault)
        did = q.post(title="t", body="b", options=["A"],
                    context={"chat_submission_id": "sub_y"})
        published.clear()
        q.resolve(did, choice="A")
        assert any(ev.get("category") == "operator_decision_resolved"
                   and ev.get("context", {}).get("decision_id") == did
                   for ev in published)

    def test_eventbus_failure_does_not_break_queue(self, tmp_vault, monkeypatch):
        from systemu.approval.decision_queue import OperatorDecisionQueue
        class _Boom:
            def publish(self, ev): raise RuntimeError("bus down")
        from systemu.interface import event_bus
        monkeypatch.setattr(event_bus.EventBus, "get", classmethod(lambda cls: _Boom()))
        q = OperatorDecisionQueue(tmp_vault)
        # MUST NOT raise even though publish blows up
        did = q.post(title="t", body="b", options=["A"])
        assert did and did.startswith("dec_")


# ── T7: status="pending_decision" carve-out + inline chat card ──────────────
class TestPendingDecisionCardComponent:
    def test_component_imports_clean(self):
        from systemu.interface.components.pending_decision_card import build_pending_decision_card
        assert callable(build_pending_decision_card)


class TestDirectTaskCarveOut:
    def test_pending_operator_decision_status(self, monkeypatch, tmp_vault):
        """When runtime.execute raises PendingOperatorDecision, the chat history
        entry should get status='pending_decision' instead of 'failed'."""
        from systemu.pipelines import direct_task as dt
        # _handle_pending_decision_in_chat: the helper we add to convert the
        # exception into a status update on the chat history entry.
        ts = "2026-06-02T10:00:00"
        tmp_vault.append_chat_history({"ts": ts, "prompt": "hi", "scroll_id": "s1",
                                       "status": "running"})
        dt._handle_pending_decision_in_chat(tmp_vault, ts,
                                            decision_id="dec_zz", dedup_key="k1",
                                            options=["A", "B"])
        # vault entry now reflects pending_decision
        entries = tmp_vault.load_chat_history(limit=10)
        e = next(x for x in entries if x.get("ts") == ts)
        assert e["status"] == "pending_decision"
        assert e["decision_id"] == "dec_zz"
        assert e["dedup_key"] == "k1"
        assert e["options"] == ["A", "B"]
