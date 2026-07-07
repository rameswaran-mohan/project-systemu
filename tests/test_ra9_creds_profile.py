"""R-A9 T2: S4 credentials (names-only, AC2) + S5 profile builders."""
from systemu.runtime.situational_inventory import build_credentials, build_profile


def test_credentials_returns_names_only_never_values(tmp_path):
    # AC2: the report must carry credential NAMES, never the secret VALUES.
    from systemu.runtime.credentials.store import CredentialStore
    store = CredentialStore(base_dir=str(tmp_path))
    store.set("github", "ghp_supersecretvalue")
    store.set("openai", "sk-anothersecret")
    names = build_credentials(store)
    assert sorted(names) == ["github", "openai"]     # names present
    assert "ghp_supersecretvalue" not in names        # AC2: value NEVER present
    assert "sk-anothersecret" not in names
    assert all("ghp_" not in n and "sk-" not in n for n in names)


def test_credentials_empty_and_defensive():
    class _Boom:
        def list_names(self): raise RuntimeError("store down")
    assert build_credentials(_Boom()) == []           # defensive, never raises
    class _Empty:
        def list_names(self): return []
    assert build_credentials(_Empty()) == []


def test_credentials_never_calls_get():
    # AC2 (structural): build_credentials must NEVER pull a value via .get().
    class _Spy:
        def __init__(self): self.get_calls = 0
        def list_names(self): return ["svc_a", "svc_b"]
        def get(self, key):
            self.get_calls += 1
            raise AssertionError("build_credentials must never call store.get()")
    spy = _Spy()
    names = build_credentials(spy)
    assert names == ["svc_a", "svc_b"]
    assert spy.get_calls == 0


def test_profile_reads_typed_fields_and_facts(tmp_path):
    # Build a real vault, add a profile + a durable-default fact.
    from systemu.vault.vault import Vault
    from systemu.core.models import UserProfile
    from systemu.runtime import user_profile as up
    for sub in ["scrolls", "activities", "shadow_army", "skills", "tools/implementations",
                "evolutions", "notifications", "executions", "decisions"]:
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    for idx in ["scrolls", "activities", "shadow_army", "skills", "tools", "evolutions", "decisions"]:
        (tmp_path / idx / "index.json").write_text("[]", encoding="utf-8")
    vault = Vault(str(tmp_path))
    up.save_profile(vault, UserProfile(name="R", location_text="X", timezone="UTC",
                                       default_output_dir=str(tmp_path)))
    up.add_fact(vault, "default repo is acme/app", source="operator", tags=["default_repo"])
    prof = build_profile(vault)
    assert prof.get("name") == "R" and prof.get("default_output_dir")
    assert isinstance(prof.get("user_facts"), list) and len(prof["user_facts"]) >= 1


def test_profile_empty_when_absent(tmp_path):
    class _NoVault: ...   # get_profile will raise/return None internally
    assert build_profile(_NoVault()) == {}   # defensive → {}
