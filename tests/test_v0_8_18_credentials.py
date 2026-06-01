"""v0.8.18 — tool credentials & connections."""
import asyncio
import os
import pytest


class TestCredentialRequirement:
    def test_defaults_and_fields(self):
        from systemu.core.models import CredentialRequirement
        r = CredentialRequirement(key="OPENWEATHER_API_KEY", label="OpenWeather API key",
                                  signup_url="https://openweathermap.org/api", free_tier=True)
        assert r.key == "OPENWEATHER_API_KEY" and r.auth_type == "api_key" and r.free_tier is True

    def test_key_must_be_envvar_shaped(self):
        from systemu.core.models import CredentialRequirement
        with pytest.raises(ValueError):
            CredentialRequirement(key="bad-key", label="x")

    def test_tool_has_requires_credentials_default_empty(self):
        from systemu.core.models import Tool, ToolType
        t = Tool(id="tool_x", name="x", description="d", tool_type=ToolType.API_CALL)
        assert t.requires_credentials == []

    def test_tool_accepts_requirements(self):
        from systemu.core.models import Tool, ToolType, CredentialRequirement
        t = Tool(id="tool_x", name="x", description="d", tool_type=ToolType.API_CALL,
                 requires_credentials=[CredentialRequirement(key="API_KEY", label="API key")])
        assert t.requires_credentials[0].key == "API_KEY"


class TestCredentialStore:
    def test_keyring_roundtrip(self, tmp_path):
        from systemu.runtime.credentials.store import CredentialStore
        s = CredentialStore(base_dir=tmp_path)
        s.set("POC_TEST_KEY", "secret-123")
        assert s.get("POC_TEST_KEY") == "secret-123"
        assert s.status("POC_TEST_KEY")["present"] is True
        assert s.status("POC_TEST_KEY")["last4"] == "-123"
        s.delete("POC_TEST_KEY")
        assert s.get("POC_TEST_KEY") is None

    def test_file_fallback_when_no_keyring(self, tmp_path, monkeypatch):
        import systemu.runtime.credentials.store as st
        s = st.CredentialStore(base_dir=tmp_path)
        s._keyring = None                       # force the 0600-file path
        s.set("FILE_KEY", "filesecret")
        assert s.get("FILE_KEY") == "filesecret"
        assert (tmp_path / ".credentials.json").exists()
        s.delete("FILE_KEY")
        assert s.get("FILE_KEY") is None

    def test_mask_secret(self):
        from systemu.runtime.credentials.store import mask_secret
        assert mask_secret("abcdef") == "<redacted:cdef>"
        assert mask_secret("") == "<none>"
        assert mask_secret(None) == "<none>"


class TestCredentialResolver:
    def _req(self, key="API_KEY", auth="api_key"):
        from systemu.core.models import CredentialRequirement
        return CredentialRequirement(key=key, label="k", auth_type=auth)

    def test_precedence_keyring_then_env_then_none(self, tmp_path, monkeypatch):
        from systemu.runtime.credentials.store import CredentialStore
        from systemu.runtime.credentials.resolver import CredentialResolver
        monkeypatch.delenv("API_KEY", raising=False)
        r = CredentialResolver(store=CredentialStore(base_dir=tmp_path))
        assert r.resolve(self._req())[0] is None                 # missing
        monkeypatch.setenv("API_KEY", "from-env")
        assert r.resolve(self._req()) == ("from-env", "env")      # env fallback
        r._store.set("API_KEY", "from-keyring")
        assert r.resolve(self._req())[0] == "from-keyring"        # keyring wins
        r._store.delete("API_KEY")

    def test_auth_none_always_present(self, tmp_path):
        from systemu.runtime.credentials.store import CredentialStore
        from systemu.runtime.credentials.resolver import CredentialResolver
        r = CredentialResolver(store=CredentialStore(base_dir=tmp_path))
        assert r.resolve(self._req(auth="none"))[0] == ""

    def test_missing_and_promote(self, tmp_path, monkeypatch):
        from systemu.runtime.credentials.store import CredentialStore
        from systemu.runtime.credentials.resolver import CredentialResolver
        monkeypatch.delenv("API_KEY", raising=False)
        store = CredentialStore(base_dir=tmp_path); store.set("API_KEY", "kr")
        r = CredentialResolver(store=store)
        reqs = [self._req()]
        assert r.missing(reqs) == []
        promoted = r.promote_to_env(reqs)
        assert os.environ["API_KEY"] == "kr" and promoted == {"API_KEY": "keyring"}
        store.delete("API_KEY")
        monkeypatch.delenv("API_KEY", raising=False)


class TestRequestCredential:
    def _req(self):
        from systemu.core.models import CredentialRequirement
        return CredentialRequirement(key="API_KEY", label="API key", signup_url="https://x.example")

    def test_returns_value_when_present(self, tmp_path, monkeypatch):
        import systemu.interface.notifications as nf
        from systemu.runtime.credentials.store import CredentialStore
        from systemu.runtime.credentials.resolver import CredentialResolver
        store = CredentialStore(base_dir=tmp_path); store.set("API_KEY", "v")
        try:
            assert nf.request_credential(self._req(), resolver=CredentialResolver(store=store)) == "v"
        finally:
            store.delete("API_KEY")

    def test_no_queue_returns_none(self, tmp_path, monkeypatch):
        import systemu.interface.notifications as nf
        from systemu.runtime.credentials.store import CredentialStore
        from systemu.runtime.credentials.resolver import CredentialResolver
        monkeypatch.delenv("API_KEY", raising=False)
        monkeypatch.setattr(nf, "_get_decision_queue", lambda: None)
        out = nf.request_credential(self._req(), resolver=CredentialResolver(store=CredentialStore(base_dir=tmp_path)))
        assert out is None

    def test_pending_raised_when_unresolved(self, tmp_path, monkeypatch):
        import systemu.interface.notifications as nf
        from systemu.approval.exceptions import PendingCredentialRequest
        from systemu.runtime.credentials.store import CredentialStore
        from systemu.runtime.credentials.resolver import CredentialResolver
        monkeypatch.delenv("API_KEY", raising=False)
        class _Q:
            def get_resolved_choice(self, k): return None
            def post(self, **kw): return "dec_1"
        monkeypatch.setattr(nf, "_get_decision_queue", lambda: _Q())
        with pytest.raises(PendingCredentialRequest) as ei:
            nf.request_credential(self._req(), resolver=CredentialResolver(store=CredentialStore(base_dir=tmp_path)))
        assert ei.value.credential_key == "API_KEY" and ei.value.dedup_key == "cred:API_KEY"

    def test_none_auth_returns_empty_without_queue(self, monkeypatch):
        # M3: auth_type="none" resolves to "" — must return it directly (not ask).
        # The resolver yields "" for none-auth; the guard `if value is not None`
        # returns it WITHOUT touching the decision queue.  A queue that explodes
        # on access proves request_credential never reaches the ask path.
        import systemu.interface.notifications as nf
        from systemu.core.models import CredentialRequirement
        def _boom():
            raise AssertionError("decision queue must not be consulted for none-auth")
        monkeypatch.setattr(nf, "_get_decision_queue", _boom)
        req = CredentialRequirement(key="X_KEY", label="x", auth_type="none")
        assert nf.request_credential(req) == ""


class TestGate4:
    def _tool(self, reqs):
        from systemu.core.models import Tool, ToolType, ToolStatus
        return Tool(id="tool_c", name="cred_tool", description="d", tool_type=ToolType.API_CALL,
                    enabled=True, dry_run_status="passed", requires_credentials=reqs)

    def _registry_with(self, tool):
        from systemu.runtime.tool_registry import ToolRegistry
        class _V:
            def find_tool_by_name(self, n): return tool
        reg = ToolRegistry.__new__(ToolRegistry)
        reg._vault = _V()
        return reg

    def test_missing_credential_degrades_when_headless(self, tmp_path, monkeypatch):
        import asyncio
        from systemu.core.models import CredentialRequirement
        monkeypatch.delenv("SYSTEMU_DECISION_QUEUE", raising=False)  # headless
        monkeypatch.delenv("WEATHER_KEY", raising=False)
        monkeypatch.setenv("SYSTEMU_VAULT_DIR", str(tmp_path))
        tool = self._tool([CredentialRequirement(key="WEATHER_KEY", label="Weather key",
                                                 signup_url="https://w.example")])
        reg = self._registry_with(tool)
        out = asyncio.run(reg.execute("cred_tool", {}))
        assert out["success"] is False and out["degraded"] is True
        assert out["error_type"] == "tool_credential_missing" and "Weather key" in out["note"]


class TestPreflight:
    def test_batched_decision_for_missing_creds(self, tmp_path, monkeypatch):
        from systemu.core.models import Tool, ToolType, CredentialRequirement
        from systemu.pipelines import activity_extractor as ax
        monkeypatch.delenv("AAA_KEY", raising=False)
        monkeypatch.delenv("BBB_KEY", raising=False)
        monkeypatch.setenv("SYSTEMU_VAULT_DIR", str(tmp_path))
        t1 = Tool(id="t1", name="t1", description="d", tool_type=ToolType.API_CALL,
                  requires_credentials=[CredentialRequirement(key="AAA_KEY", label="A")])
        t2 = Tool(id="t2", name="t2", description="d", tool_type=ToolType.API_CALL,
                  requires_credentials=[CredentialRequirement(key="BBB_KEY", label="B")])
        class _V:
            def get_tool(self, tid): return {"t1": t1, "t2": t2}[tid]
        posted = {}
        class _Q:
            def post(self, **kw): posted.update(kw); return "dec_x"
        monkeypatch.setattr(ax, "_get_decision_queue", lambda: _Q(), raising=False)
        ax._queue_credential_requests(["t1", "t2"], activity_id="act_1", vlt=_V())
        assert "AAA_KEY" in str(posted["context"]) and "BBB_KEY" in str(posted["context"])
        assert posted["dedup_key"] == "creds:act_1"


class TestConfigPolicy:
    def test_default_is_prompt(self, monkeypatch):
        from sharing_on.config import Config
        monkeypatch.delenv("SYSTEMU_CREDENTIAL_POLICY", raising=False)
        assert Config.from_env().credential_policy == "prompt"

    def test_env_override_lowercased(self, monkeypatch):
        from sharing_on.config import Config
        monkeypatch.setenv("SYSTEMU_CREDENTIAL_POLICY", "DEGRADE")
        assert Config.from_env().credential_policy == "degrade"


class TestConnectionsHelpers:
    def test_connection_rows_lists_declared_requirements(self, tmp_path, monkeypatch):
        from systemu.core.models import Tool, ToolType, CredentialRequirement
        from systemu.interface.pages import settings as sp
        monkeypatch.setenv("SYSTEMU_VAULT_DIR", str(tmp_path))
        monkeypatch.delenv("ZZZ_KEY", raising=False)
        t = Tool(id="t1", name="weather", description="d", tool_type=ToolType.API_CALL,
                 requires_credentials=[CredentialRequirement(key="ZZZ_KEY", label="Zzz key",
                                                             signup_url="https://z.example")])
        # connection_rows enumerates full Tool records via the REAL Vault API:
        # load_index("tools") (header dicts) + get_tool(id) (full Tool).
        class _V:
            def load_index(self, entity):
                assert entity == "tools"
                return [{"id": "t1"}]
            def get_tool(self, tool_id):
                assert tool_id == "t1"
                return t
        rows = sp.connection_rows(_V())
        assert rows == [{"tool": "weather", "key": "ZZZ_KEY", "label": "Zzz key",
                         "signup_url": "https://z.example", "present": False, "last4": None}]

    def test_save_credential_roundtrips(self, tmp_path, monkeypatch):
        from systemu.interface.pages import settings as sp
        monkeypatch.setenv("SYSTEMU_VAULT_DIR", str(tmp_path))
        try:
            sp.save_credential("ZZZ_KEY", "topsecret")
            from systemu.runtime.credentials.store import CredentialStore
            assert CredentialStore(base_dir=tmp_path).get("ZZZ_KEY") == "topsecret"
        finally:
            from systemu.runtime.credentials.store import CredentialStore
            CredentialStore(base_dir=tmp_path).delete("ZZZ_KEY")


class TestAutoforgeDeclares:
    def test_tool_model_round_trips_requires_credentials(self, tmp_path):
        from systemu.core.models import Tool, ToolType, CredentialRequirement
        t = Tool(id="t", name="weather", description="d", tool_type=ToolType.API_CALL,
                 requires_credentials=[CredentialRequirement(key="OWM_KEY", label="OpenWeather key",
                                                             signup_url="https://openweathermap.org/api", free_tier=True)])
        again = Tool(**t.model_dump())
        assert again.requires_credentials[0].key == "OWM_KEY"
        assert again.requires_credentials[0].free_tier is True

    def test_forge_spec_prompt_mentions_requires_credentials(self):
        import pathlib, systemu
        root = pathlib.Path(systemu.__file__).parent
        matches = list(root.rglob("forge_tool_spec.md"))
        assert matches, "forge_tool_spec.md not found"
        assert any("requires_credentials" in m.read_text(encoding="utf-8") for m in matches)


class TestNoLeakage:
    def test_degraded_result_has_no_secret(self):
        from systemu.runtime.tool_registry import _credential_degraded
        from systemu.core.models import CredentialRequirement
        out = _credential_degraded("weather", CredentialRequirement(key="K_KEY", label="K key"))
        assert "K key" in str(out)          # label is fine to surface
        assert out["degraded"] is True and out["success"] is False
        assert out["error_type"] == "tool_credential_missing"

    def test_mask_used_for_status(self, tmp_path, monkeypatch):
        from systemu.runtime.credentials.store import CredentialStore
        monkeypatch.setenv("SYSTEMU_VAULT_DIR", str(tmp_path))
        s = CredentialStore(base_dir=tmp_path)
        try:
            s.set("K_KEY", "supersecretvalue")
            st = s.status("K_KEY")
            assert "supersecret" not in str(st) and st["last4"] == "alue"
        finally:
            s.delete("K_KEY")


class TestSandboxPropagatesCredentialPause:
    """C1: the ToolSandbox fast path must NOT swallow a PendingCredentialRequest
    raised by the registry's Gate-4.  Without the `except PendingOperatorDecision:
    raise` guard, the broad `except Exception` falls through to the subprocess
    backend and runs the tool UN-GATED.  This test proves the seam: backend must
    never be reached, and the pause must propagate to the resume handlers."""

    def test_fast_path_reraises_pending_credential(self, tmp_path):
        from systemu.runtime.tool_sandbox import ToolSandbox
        from systemu.approval.exceptions import PendingCredentialRequest

        # A real implementation file on disk so the fast-path precondition
        # (impl_path.exists()) is satisfied and we enter the try/except block.
        impl = tmp_path / "weather.py"
        impl.write_text("print('{}')\n", encoding="utf-8")

        class _Registry:
            async def execute(self, tool_name, parameters, *, timeout=None):
                raise PendingCredentialRequest(
                    decision_id="dec_seam", dedup_key="cred:WEATHER_KEY",
                    options=["I've connected it", "Skip (disable tool)", "Cancel run"],
                    credential_key="WEATHER_KEY",
                )

        class _Backend:
            async def execute(self, *a, **kw):
                raise AssertionError("backend should not be reached — credential pause was swallowed")

        sbx = ToolSandbox(vault_root=tmp_path, registry=_Registry())
        # Replace the constructed backend with one that fails loudly if reached.
        sbx._backend = _Backend()

        with pytest.raises(PendingCredentialRequest) as ei:
            asyncio.run(sbx.execute_tool(str(impl), {}, tool_type="api_call"))
        assert ei.value.credential_key == "WEATHER_KEY"
        assert ei.value.dedup_key == "cred:WEATHER_KEY"
