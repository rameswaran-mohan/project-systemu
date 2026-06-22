"""v0.9.0 — user model + persistent context (Layer 1 of 7)."""
from __future__ import annotations
import json
import pytest


class TestUserProfileModel:
    def test_required_fields(self):
        from systemu.core.models import UserProfile
        p = UserProfile(
            name="Jane Doe",
            location_text="Springfield, USA",
            timezone="Asia/Kolkata",
            default_output_dir="C:/Users/R/systemu-output",
        )
        assert p.schema_version == 1
        assert p.name == "Jane Doe"
        assert p.location_text == "Springfield, USA"

    def test_extra_forbid(self):
        from systemu.core.models import UserProfile
        with pytest.raises(Exception):
            UserProfile(
                name="x", location_text="x", timezone="UTC",
                default_output_dir="/tmp", weirdfield="x",
            )

    def test_missing_field_raises(self):
        from systemu.core.models import UserProfile
        with pytest.raises(Exception):
            UserProfile(name="x", location_text="x")  # missing timezone, default_output_dir


class TestUserFactModel:
    def test_minimal_fact(self):
        from systemu.core.models import UserFact
        f = UserFact(id="fact_a1b2", ts="2026-06-06T00:00:00Z",
                     fact="User prefers Italian food", source="explicit_user")
        assert f.confidence == 1.0
        assert f.tags == []
        assert f.superseded_by is None

    def test_full_fact(self):
        from systemu.core.models import UserFact
        f = UserFact(id="fact_c3d4", ts="2026-06-06T00:00:00Z",
                     fact="User's son is in 3rd grade",
                     tags=["family"], source="auto_extract",
                     source_ref="chat:2026-06-05T21:08",
                     confidence=0.82)
        assert f.tags == ["family"]
        assert f.source == "auto_extract"
        assert f.confidence == 0.82


class TestUserProfileRuntimeAPI:
    def _vault(self, tmp_path):
        from systemu.vault.vault import Vault
        # bootstrap an empty vault — same pattern as other v0.8.22.x tests
        for sub in ["scrolls", "activities", "shadow_army", "skills",
                    "tools/implementations", "evolutions", "notifications",
                    "executions", "decisions", "elder"]:
            (tmp_path / sub).mkdir(parents=True, exist_ok=True)
        for idx in ["scrolls", "activities", "shadow_army", "skills",
                    "tools", "evolutions", "decisions"]:
            (tmp_path / idx / "index.json").write_text("[]", encoding="utf-8")
        return Vault(str(tmp_path))

    def test_save_and_get_profile_roundtrip(self, tmp_path):
        from systemu.runtime.user_profile import save_profile, get_profile
        from systemu.core.models import UserProfile
        vlt = self._vault(tmp_path)
        p = UserProfile(name="R", location_text="Bangalore, India",
                        timezone="Asia/Kolkata", default_output_dir="/tmp/out")
        save_profile(vlt, p)
        loaded = get_profile(vlt)
        assert loaded == p

    def test_get_profile_absent_returns_none(self, tmp_path):
        from systemu.runtime.user_profile import get_profile
        vlt = self._vault(tmp_path)
        assert get_profile(vlt) is None

    def test_add_fact_then_find(self, tmp_path):
        from systemu.runtime.user_profile import add_fact, get_facts
        vlt = self._vault(tmp_path)
        f1 = add_fact(vlt, "User prefers Italian", source="explicit_user",
                      tags=["preference", "cuisine"])
        f2 = add_fact(vlt, "User has a son in 3rd grade",
                      source="auto_extract", tags=["family"],
                      source_ref="chat:2026-06-06T00:00", confidence=0.85)
        all_facts = get_facts(vlt)
        assert len(all_facts) == 2
        cuisine = get_facts(vlt, tags=["cuisine"])
        assert len(cuisine) == 1 and cuisine[0].id == f1.id
        family = get_facts(vlt, tags=["family"])
        assert len(family) == 1 and family[0].id == f2.id

    def test_forget_marks_superseded(self, tmp_path):
        from systemu.runtime.user_profile import add_fact, forget_fact, get_facts
        vlt = self._vault(tmp_path)
        f = add_fact(vlt, "obsolete", source="explicit_user")
        forget_fact(vlt, f.id)
        # default filter excludes superseded
        assert get_facts(vlt) == []
        # but they survive in the raw log
        all_with = get_facts(vlt, include_superseded=True)
        assert len(all_with) == 1
        assert all_with[0].superseded_by == "forgotten"

    def test_wipe_removes_both(self, tmp_path):
        from systemu.runtime.user_profile import save_profile, add_fact, wipe, get_profile, get_facts
        from systemu.core.models import UserProfile
        import os
        vlt = self._vault(tmp_path)
        save_profile(vlt, UserProfile(name="R", location_text="x",
                                       timezone="UTC", default_output_dir="/tmp"))
        add_fact(vlt, "x", source="explicit_user")
        wipe(vlt)
        assert get_profile(vlt) is None
        assert get_facts(vlt) == []
        assert not os.path.exists(str(vlt.root) + "/user_profile.json")
        assert not os.path.exists(str(vlt.root) + "/user_facts.jsonl")


class TestVaultUserProfileWrappers:
    def _vault(self, tmp_path):
        from systemu.vault.vault import Vault
        for sub in ["scrolls", "activities", "shadow_army", "skills",
                    "tools/implementations", "evolutions", "notifications",
                    "executions", "decisions", "elder"]:
            (tmp_path / sub).mkdir(parents=True, exist_ok=True)
        for idx in ["scrolls", "activities", "shadow_army", "skills",
                    "tools", "evolutions", "decisions"]:
            (tmp_path / idx / "index.json").write_text("[]", encoding="utf-8")
        return Vault(str(tmp_path))

    def test_vault_get_save_profile(self, tmp_path):
        from systemu.core.models import UserProfile
        vlt = self._vault(tmp_path)
        assert vlt.get_user_profile() is None
        p = UserProfile(name="R", location_text="Bangalore, India",
                        timezone="Asia/Kolkata", default_output_dir="/tmp")
        vlt.save_user_profile(p)
        assert vlt.get_user_profile() == p

    def test_vault_facts_round_trip(self, tmp_path):
        vlt = self._vault(tmp_path)
        assert vlt.load_user_facts() == []
        f = vlt.append_user_fact(fact="User likes pizza",
                                  source="explicit_user", tags=["preference"])
        assert f.id.startswith("fact_")
        facts = vlt.load_user_facts()
        assert len(facts) == 1
        assert facts[0].fact == "User likes pizza"


class TestConfigAutoExtractToggle:
    def test_default_is_on(self, monkeypatch):
        monkeypatch.delenv("SYSTEMU_AUTO_EXTRACT_USER_FACTS", raising=False)
        # re-import sharing_on.config to re-evaluate field default
        import importlib
        from sharing_on import config as c
        importlib.reload(c)
        cfg = c.Config()
        assert cfg.auto_extract_user_facts is True

    def test_env_disables(self, monkeypatch):
        monkeypatch.setenv("SYSTEMU_AUTO_EXTRACT_USER_FACTS", "false")
        import importlib
        from sharing_on import config as c
        importlib.reload(c)
        cfg = c.Config()
        assert cfg.auto_extract_user_facts is False

    def test_env_other_values_keep_on(self, monkeypatch):
        monkeypatch.setenv("SYSTEMU_AUTO_EXTRACT_USER_FACTS", "yes")
        import importlib
        from sharing_on import config as c
        importlib.reload(c)
        cfg = c.Config()
        assert cfg.auto_extract_user_facts is True


class TestUserCLICommands:
    def _vault_dir(self, tmp_path):
        for sub in ["scrolls", "activities", "shadow_army", "skills",
                    "tools/implementations", "evolutions", "notifications",
                    "executions", "decisions", "elder"]:
            (tmp_path / sub).mkdir(parents=True, exist_ok=True)
        for idx in ["scrolls", "activities", "shadow_army", "skills",
                    "tools", "evolutions", "decisions"]:
            (tmp_path / idx / "index.json").write_text("[]", encoding="utf-8")
        return tmp_path

    def _invoke(self, args, vault_dir, monkeypatch, input_text=""):
        from click.testing import CliRunner
        monkeypatch.setenv("SYSTEMU_VAULT_DIR", str(vault_dir))
        monkeypatch.setenv("SYSTEMU_STORAGE", "file")
        from systemu.interface.cli_commands import user_group
        runner = CliRunner()
        return runner.invoke(user_group, args, input=input_text)

    def test_user_init_writes_profile(self, tmp_path, monkeypatch):
        vd = self._vault_dir(tmp_path)
        result = self._invoke(
            ["init"],
            vault_dir=vd, monkeypatch=monkeypatch,
            input_text="Jane Doe\nSpringfield, USA\nAsia/Kolkata\n/tmp/systemu-out\n",
        )
        assert result.exit_code == 0, result.output
        from systemu.vault.vault import Vault
        prof = Vault(str(vd)).get_user_profile()
        assert prof is not None
        assert prof.name == "Jane Doe"
        assert prof.location_text == "Springfield, USA"
        assert prof.timezone == "Asia/Kolkata"
        assert prof.default_output_dir == "/tmp/systemu-out"

    def test_user_show_prints_profile(self, tmp_path, monkeypatch):
        vd = self._vault_dir(tmp_path)
        from systemu.vault.vault import Vault
        from systemu.core.models import UserProfile
        Vault(str(vd)).save_user_profile(UserProfile(
            name="R", location_text="X", timezone="UTC",
            default_output_dir="/tmp/o"))
        result = self._invoke(["show"], vault_dir=vd, monkeypatch=monkeypatch)
        assert result.exit_code == 0
        assert "X" in result.output

    def test_user_remember_adds_fact(self, tmp_path, monkeypatch):
        vd = self._vault_dir(tmp_path)
        result = self._invoke(
            ["remember", "I prefer Italian food"],
            vault_dir=vd, monkeypatch=monkeypatch)
        assert result.exit_code == 0
        from systemu.vault.vault import Vault
        facts = Vault(str(vd)).load_user_facts()
        assert len(facts) == 1
        assert "Italian" in facts[0].fact
        assert facts[0].source == "explicit_user"

    def test_user_forget_marks_superseded(self, tmp_path, monkeypatch):
        vd = self._vault_dir(tmp_path)
        from systemu.vault.vault import Vault
        f = Vault(str(vd)).append_user_fact(fact="bad", source="explicit_user")
        result = self._invoke(["forget", f.id],
                              vault_dir=vd, monkeypatch=monkeypatch)
        assert result.exit_code == 0
        assert Vault(str(vd)).load_user_facts() == []


class TestFactExtractor:
    def _vault(self, tmp_path):
        from systemu.vault.vault import Vault
        for sub in ["scrolls", "activities", "shadow_army", "skills",
                    "tools/implementations", "evolutions", "notifications",
                    "executions", "decisions", "elder"]:
            (tmp_path / sub).mkdir(parents=True, exist_ok=True)
        for idx in ["scrolls", "activities", "shadow_army", "skills",
                    "tools", "evolutions", "decisions"]:
            (tmp_path / idx / "index.json").write_text("[]", encoding="utf-8")
        return Vault(str(tmp_path))

    def test_extract_from_chat_writes_facts(self, tmp_path, monkeypatch):
        """Given a stubbed LLM returning two facts, both land in the vault
        with correct provenance."""
        from systemu.pipelines import fact_extractor as fe
        from sharing_on.config import Config
        vlt = self._vault(tmp_path)

        captured = {}
        def fake_llm(*, tier, system, user, config, temperature=0.2, max_tokens=2000, **kw):
            captured["tier"] = tier
            captured["user"] = user
            return {
                "facts": [
                    {"fact": "User prefers Italian food",
                     "tags": ["preference", "cuisine"], "confidence": 0.85},
                    {"fact": "User lives in Bangalore",
                     "tags": ["location"], "confidence": 0.95},
                ]
            }
        monkeypatch.setattr(fe, "llm_call_json", fake_llm)

        cfg = Config()
        cfg.openrouter_api_key = "sk-fake-for-test"
        entry = {"ts": "2026-06-06T00:00:00", "prompt": "find me pizza near me",
                 "status": "completed"}
        fe.extract_from_chat(entry, vlt, cfg)

        facts = vlt.load_user_facts()
        assert len(facts) == 2
        assert all(f.source == "auto_extract" for f in facts)
        assert all(f.source_ref == "chat:2026-06-06T00:00:00" for f in facts)
        assert any(f.confidence == 0.95 for f in facts)
        assert captured["tier"] == 1
        assert "find me pizza" in captured["user"]

    def test_extract_with_empty_facts_is_noop(self, tmp_path, monkeypatch):
        from systemu.pipelines import fact_extractor as fe
        from sharing_on.config import Config
        vlt = self._vault(tmp_path)
        monkeypatch.setattr(fe, "llm_call_json",
                            lambda **k: {"facts": []})
        cfg = Config()
        cfg.openrouter_api_key = "sk-fake"
        fe.extract_from_chat({"ts": "t", "prompt": "?", "status": "completed"},
                              vlt, cfg)
        assert vlt.load_user_facts() == []

    def test_extract_swallows_llm_errors(self, tmp_path, monkeypatch):
        """Auto-extract must never crash the caller; LLM errors are swallowed."""
        from systemu.pipelines import fact_extractor as fe
        from sharing_on.config import Config
        vlt = self._vault(tmp_path)
        def boom(**k):
            raise RuntimeError("API rate-limited")
        monkeypatch.setattr(fe, "llm_call_json", boom)
        cfg = Config()
        cfg.openrouter_api_key = "sk-fake"
        # MUST NOT raise
        fe.extract_from_chat({"ts": "t", "prompt": "?", "status": "completed"},
                              vlt, cfg)
        assert vlt.load_user_facts() == []


class TestScrollRefinerConsumesProfile:
    def _vault(self, tmp_path):
        from systemu.vault.vault import Vault
        for sub in ["scrolls", "activities", "shadow_army", "skills",
                    "tools/implementations", "evolutions", "notifications",
                    "executions", "decisions", "elder"]:
            (tmp_path / sub).mkdir(parents=True, exist_ok=True)
        for idx in ["scrolls", "activities", "shadow_army", "skills",
                    "tools", "evolutions", "decisions"]:
            (tmp_path / idx / "index.json").write_text("[]", encoding="utf-8")
        return Vault(str(tmp_path))

    def test_refine_from_text_passes_profile_to_llm(self, tmp_path, monkeypatch):
        """The user_payload sent to elder_intake.md must include user_profile
        when one is set. (Burrito acceptance test at the wiring layer.)"""
        from systemu.pipelines import scroll_refiner as sr
        from systemu.vault.vault import Vault
        from systemu.core.models import UserProfile
        from sharing_on.config import Config
        vlt = self._vault(tmp_path)
        vlt.save_user_profile(UserProfile(
            name="R", location_text="Bangalore, India",
            timezone="Asia/Kolkata", default_output_dir="/tmp/o"))
        vlt.append_user_fact(fact="User prefers Italian", source="explicit_user",
                              tags=["preference"])

        captured = {}
        def fake_llm(*, tier, system, user, config, temperature=0.2, max_tokens=4000, **kw):
            captured["user"] = user
            return {
                "title": "Find burritos in Bangalore",
                "intent": "Discover top burrito restaurants in Bangalore.",
                "expected_outcome": "Ranked list of burrito places.",
                "narrative_md": "...",
                "objectives": [
                    {"id": 1, "goal": "Identify top burrito restaurants in Bangalore",
                     "success_criteria": ">=10 names", "depends_on": []},
                ],
                "action_blocks": [],
                "constraints": {},
                "observed_preferences": {},
            }
        monkeypatch.setattr(sr, "llm_call_json", fake_llm)
        monkeypatch.setattr(Vault, "load_global_memory", lambda self: "")

        cfg = Config()
        cfg.openrouter_api_key = "sk-fake"
        scroll = sr.refine_from_text("find top burrito places near me", vlt, cfg)

        payload = json.loads(captured["user"])
        assert "user_profile" in payload
        assert payload["user_profile"]["location_text"] == "Bangalore, India"
        assert payload["user_profile"]["default_output_dir"] == "/tmp/o"
        assert "user_facts" in payload
        assert len(payload["user_facts"]) >= 1
        assert scroll.name == "Find burritos in Bangalore"

    def test_refine_from_text_works_without_profile(self, tmp_path, monkeypatch):
        """No profile yet: the payload omits user_profile gracefully."""
        from systemu.pipelines import scroll_refiner as sr
        from systemu.vault.vault import Vault
        from sharing_on.config import Config
        vlt = self._vault(tmp_path)
        captured = {}
        def fake_llm(**kw):
            captured["user"] = kw["user"]
            return {
                "title": "x", "intent": "x", "expected_outcome": "x",
                "narrative_md": "x", "objectives": [
                    {"id": 1, "goal": "x", "success_criteria": "x", "depends_on": []}
                ],
                "action_blocks": [], "constraints": {}, "observed_preferences": {},
            }
        monkeypatch.setattr(sr, "llm_call_json", fake_llm)
        monkeypatch.setattr(Vault, "load_global_memory", lambda self: "")
        cfg = Config()
        cfg.openrouter_api_key = "sk-fake"
        sr.refine_from_text("x", vlt, cfg)
        payload = json.loads(captured["user"])
        # user_profile may be absent or None — both are fine; key is no crash
        assert "user_profile" not in payload or payload["user_profile"] is None


class TestActivityExtractorOutputDir:
    def test_task_spec_carries_default_output_dir(self, tmp_path, monkeypatch):
        """The task_spec sent to extract_skills_tools.md must carry the user's
        default_output_dir when a profile is set."""
        from systemu.pipelines import activity_extractor as ae
        from systemu.vault.vault import Vault
        from systemu.core.models import UserProfile, Scroll, ScrollStatus, Objective
        # bootstrap vault
        for sub in ["scrolls", "activities", "shadow_army", "skills",
                    "tools/implementations", "evolutions", "notifications",
                    "executions", "decisions", "elder"]:
            (tmp_path / sub).mkdir(parents=True, exist_ok=True)
        for idx in ["scrolls", "activities", "shadow_army", "skills",
                    "tools", "evolutions", "decisions"]:
            (tmp_path / idx / "index.json").write_text("[]", encoding="utf-8")
        vlt = Vault(str(tmp_path))
        vlt.save_user_profile(UserProfile(name="R", location_text="x",
                                            timezone="UTC",
                                            default_output_dir="/tmp/user-out"))
        scroll = Scroll(
            id="scroll_test", name="x", source_session_id="chat",
            raw_instructions_path="x", narrative_md="x",
            intent="Find burritos", expected_outcome="ranked list",
            objectives=[Objective(id=1, goal="x", success_criteria="x",
                                    depends_on=[])],
            status=ScrollStatus.APPROVED,
        )
        vlt.save_scroll(scroll)

        captured = {}
        def fake_llm(*, tier, system, user, config, temperature=0.1, max_tokens=4096, **kw):
            captured["user"] = user
            return {"tools": [], "skills": []}
        monkeypatch.setattr(ae, "llm_call_json", fake_llm)
        # neutralize side-effects
        from systemu.interface import notifications as notif
        monkeypatch.setattr(notif, "notify_user", lambda **k: "ok", raising=False)

        from sharing_on.config import Config
        cfg = Config()
        cfg.openrouter_api_key = "sk-fake"
        ae.init_pipeline(cfg, vlt)
        ae.extract_and_process(scroll, cfg, vlt)
        payload = json.loads(captured["user"])
        assert payload.get("default_output_dir") == "/tmp/user-out"


class TestRuntimeUserContext:
    def test_runtime_helper_builds_user_block(self, tmp_path):
        """The runtime has a small helper that compacts profile + facts into a
        one-paragraph block ready for prompt assembly."""
        from systemu.runtime.shadow_runtime import _build_user_context_block
        from systemu.vault.vault import Vault
        from systemu.core.models import UserProfile
        for sub in ["scrolls", "activities", "shadow_army", "skills",
                    "tools/implementations", "evolutions", "notifications",
                    "executions", "decisions", "elder"]:
            (tmp_path / sub).mkdir(parents=True, exist_ok=True)
        for idx in ["scrolls", "activities", "shadow_army", "skills",
                    "tools", "evolutions", "decisions"]:
            (tmp_path / idx / "index.json").write_text("[]", encoding="utf-8")
        vlt = Vault(str(tmp_path))

        # absent profile → empty string
        assert _build_user_context_block(vlt) == ""

        # populated → contains name + location + facts
        vlt.save_user_profile(UserProfile(
            name="Jane Doe", location_text="Springfield, USA",
            timezone="Asia/Kolkata", default_output_dir="/tmp/o"))
        vlt.append_user_fact(fact="Prefers Italian food",
                              source="explicit_user", tags=["preference"])
        vlt.append_user_fact(fact="Lives in Indiranagar",
                              source="auto_extract", tags=["location"],
                              confidence=0.9)
        block = _build_user_context_block(vlt)
        assert "Jane Doe" in block
        assert "Springfield" in block
        assert "Italian" in block or "Indiranagar" in block
        # bounded: at most ~10 lines so token budget doesn't blow up
        assert block.count("\n") < 12


class TestDirectTaskAutoExtractHook:
    def _vault(self, tmp_path):
        from systemu.vault.vault import Vault
        for sub in ["scrolls", "activities", "shadow_army", "skills",
                    "tools/implementations", "evolutions", "notifications",
                    "executions", "decisions", "elder"]:
            (tmp_path / sub).mkdir(parents=True, exist_ok=True)
        for idx in ["scrolls", "activities", "shadow_army", "skills",
                    "tools", "evolutions", "decisions"]:
            (tmp_path / idx / "index.json").write_text("[]", encoding="utf-8")
        return Vault(str(tmp_path))

    def test_helper_calls_extractor_when_setting_on(self, tmp_path, monkeypatch):
        from systemu.pipelines import direct_task as dt
        from sharing_on.config import Config
        vlt = self._vault(tmp_path)
        vlt.append_chat_history({"ts": "ts1", "prompt": "find pizza",
                                  "status": "completed"})
        called = {"n": 0}
        from systemu.pipelines import fact_extractor as fe
        monkeypatch.setattr(fe, "extract_from_chat",
                            lambda entry, v, c: called.__setitem__("n", called["n"] + 1) or 0)
        cfg = Config()
        cfg.openrouter_api_key = "sk-fake"
        cfg.auto_extract_user_facts = True
        dt._maybe_trigger_fact_extraction(vlt, cfg, "ts1")
        assert called["n"] == 1

    def test_helper_noop_when_setting_off(self, tmp_path, monkeypatch):
        from systemu.pipelines import direct_task as dt
        from sharing_on.config import Config
        vlt = self._vault(tmp_path)
        vlt.append_chat_history({"ts": "ts1", "prompt": "x", "status": "completed"})
        called = {"n": 0}
        from systemu.pipelines import fact_extractor as fe
        monkeypatch.setattr(fe, "extract_from_chat",
                            lambda entry, v, c: called.__setitem__("n", called["n"] + 1) or 0)
        cfg = Config()
        cfg.openrouter_api_key = "sk-fake"
        cfg.auto_extract_user_facts = False
        dt._maybe_trigger_fact_extraction(vlt, cfg, "ts1")
        assert called["n"] == 0

    def test_helper_skips_non_terminal_status(self, tmp_path, monkeypatch):
        from systemu.pipelines import direct_task as dt
        from sharing_on.config import Config
        vlt = self._vault(tmp_path)
        vlt.append_chat_history({"ts": "ts1", "prompt": "x", "status": "queued"})
        called = {"n": 0}
        from systemu.pipelines import fact_extractor as fe
        monkeypatch.setattr(fe, "extract_from_chat",
                            lambda entry, v, c: called.__setitem__("n", called["n"] + 1) or 0)
        cfg = Config()
        cfg.openrouter_api_key = "sk-fake"
        cfg.auto_extract_user_facts = True
        dt._maybe_trigger_fact_extraction(vlt, cfg, "ts1")
        assert called["n"] == 0


class TestVaultMigratorUserProfileNotice:
    def test_logs_info_when_profile_absent(self, tmp_path, caplog):
        from systemu.runtime.vault_migrator import _maybe_log_profile_notice
        import logging
        caplog.set_level(logging.INFO)
        _maybe_log_profile_notice(tmp_path)
        joined = " ".join(r.getMessage() for r in caplog.records)
        assert "sharing_on user init" in joined or "user profile" in joined.lower()

    def test_silent_when_profile_present(self, tmp_path, caplog):
        from systemu.runtime.vault_migrator import _maybe_log_profile_notice
        import logging
        caplog.set_level(logging.INFO)
        (tmp_path / "user_profile.json").write_text(
            '{"schema_version": 1, "name": "x", "location_text": "x", '
            '"timezone": "UTC", "default_output_dir": "/tmp"}',
            encoding="utf-8")
        _maybe_log_profile_notice(tmp_path)
        joined = " ".join(r.getMessage() for r in caplog.records)
        assert "user profile" not in joined.lower()
