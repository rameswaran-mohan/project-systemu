"""R-4 -- a tool-declared credential key can NEVER name a variable the runtime or
the interpreter reads as configuration.

Witnessed escalation: a forged tool's ``requires_credentials`` is LLM output;
the registry's credential gate PROMOTES each resolved credential into this
process's environment (``CredentialResolver.promote_to_env``), so a declared key
is a write to process configuration under a model-chosen name.
``SYSTEMU_HARNESS_ALLOWED_MCP_HOSTS`` widened the SSRF allowance to
169.254.169.254; ``PATH`` / ``PYTHONPATH`` / the proxy variables would redirect
execution and traffic.

Fence, in TWO places, both refusing: ``CredentialRequirement`` validation, and
the writer ``promote_to_env`` itself (even for a row that bypassed validation).
A legacy persisted tool that declares a reserved key still LOADS, is marked with
the reason, never has it promoted, and its run gate says why.

Witnesses here: the reserved set can never go stale silently (an AST scan of
``systemu/`` + ``sharing_on/`` fails on any env name the set does not cover, and
pins every dynamic access site); the env-writer set is pinned; planted refusals
at both fences; a legacy row on disk on BOTH vault backends.
"""
from __future__ import annotations

import logging
import os
from types import SimpleNamespace

import pytest

import _env_scan

RESERVED_PLANTS = ("SYSTEMU_HARNESS_ALLOWED_MCP_HOSTS", "PATH")


def _reserved_reason(name):
    from systemu.core.reserved_env import reserved_reason
    return reserved_reason(name)


# ---------------------------------------------------------------------------
# 1. The reserved set covers every env name the runtime reads (AST witness).
# ---------------------------------------------------------------------------

def _resolved_dynamic_names():
    """What each pinned DYNAMIC access site can name. A value of CREDENTIAL means
    the site reads (or, for promote_to_env, writes under the fence) names that
    TOOLS declare -- credentials, not runtime configuration."""
    from systemu.interface.command import verbs
    from systemu.interface.pages import settings, welcome
    from systemu.runtime import ambient_census, costing, provider_status, spend_caps
    from systemu.runtime.adherence import ADHERENCE_PRESETS
    from systemu.runtime.backend import local
    preset_keys = sorted({k for v in ADHERENCE_PRESETS.values() for k in v})
    return {
        "systemu/interface/command/verbs.py::_persist_setting::env_key":
            tuple(verbs._SETTING_ENV_KEYS.values()),
        "systemu/interface/pages/settings.py::preset_loosening::key":
            tuple(settings._AUTO_GRANT_DEFAULTS),
        "systemu/interface/pages/settings.py::save_adherence_preset::key": tuple(preset_keys),
        "systemu/interface/pages/settings.py::save_price_overrides::costing.PRICE_OVERRIDE_ENV":
            (costing.PRICE_OVERRIDE_ENV,),
        "systemu/interface/pages/welcome.py::_refresh_key_status::spec.env":
            tuple(s.env for s in provider_status.PROVIDER_SPECS),
        "systemu/interface/pages/welcome.py::finalize_onboarding::_var":
            tuple(welcome.PRESET_TIER_ENV),
        "systemu/interface/pages/welcome.py::saved_preset::name": tuple(welcome.PRESET_TIER_ENV),
        "systemu/messaging/gateway.py::allowlist_from_env::env_var":
            ("SHARING_ON_TELEGRAM_ALLOWED_USER_IDS",),
        "systemu/runtime/ambient_census.py::probe_cloud_sync_roots::var":
            tuple(ambient_census._CLOUD_ENV_VARS),
        "systemu/runtime/backend/local.py::LocalBackend._build_restricted_env::key":
            tuple(local._SAFE_ENV_KEYS),
        "systemu/runtime/credentials/known_values.py::_declared_env_digests::key": "CREDENTIAL",
        "systemu/runtime/credentials/resolver.py::CredentialResolver.promote_to_env::r.key":
            "CREDENTIAL",
        "systemu/runtime/credentials/resolver.py::CredentialResolver.resolve::req.key":
            "CREDENTIAL",
        "systemu/runtime/governor.py::Governor._provision_mcp::k": "CREDENTIAL",
        "systemu/runtime/mcp/client.py::_resolve_env_keys::k": "CREDENTIAL",
        "systemu/runtime/harness_policy.py::_env_bool::key": ("SYSTEMU_HARNESS_X",),
        "systemu/runtime/harness_policy.py::_env_float::key": ("SYSTEMU_HARNESS_X",),
        "systemu/runtime/harness_policy.py::_env_int::key": ("SYSTEMU_HARNESS_X",),
        "systemu/runtime/harness_policy.py::_env_set::key": ("SYSTEMU_HARNESS_X",),
        "systemu/runtime/interpreter_check.py::assert_or_fail::strict_env_var":
            ("SYSTEMU_STRICT_INTERPRETER",),
        "systemu/runtime/spend_caps.py::load_caps::_ENV[kind]": tuple(spend_caps._ENV.values()),
        "systemu/runtime/web/browser_pool.py::_domains::env":
            ("SYSTEMU_WEB_DENY_DOMAINS", "SYSTEMU_WEB_ALLOW_DOMAINS"),
    }


class TestTheReservedSetCannotGoStale:
    def test_every_named_env_read_is_reserved(self):
        accesses = _env_scan.scan()
        named = {a.name for a in accesses if a.name}
        assert len(named) > 150, "non-vacuity: the scan must find the runtime's reads"
        uncovered = sorted(n for n in named if _reserved_reason(n) is None)
        assert not uncovered, (
            "systemu/ or sharing_on/ reads these environment names, but the "
            "reserved set does not cover them -- add each to "
            f"systemu/core/reserved_env.py RUNTIME_READ_NAMES: {uncovered}")

    def test_every_prefix_built_read_is_a_covered_prefix(self):
        for a in _env_scan.scan():
            if a.prefix:
                assert _reserved_reason(a.prefix + "X") is not None, a.site

    def test_every_dynamic_access_site_is_pinned_and_covered(self):
        found = {a.site for a in _env_scan.scan() if a.dynamic}
        resolved = _resolved_dynamic_names()
        assert found == set(resolved), (
            "a dynamic environment access appeared or vanished; classify it in "
            f"_resolved_dynamic_names(). new: {sorted(found - set(resolved))} "
            f"gone: {sorted(set(resolved) - found)}")
        for site, names in resolved.items():
            if names == "CREDENTIAL":
                continue
            uncovered = [n for n in names if _reserved_reason(n) is None]
            assert not uncovered, (site, uncovered)

    @pytest.mark.parametrize("name", [
        "PATH", "path", "Path", "PATHEXT", "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP",
        "VIRTUAL_ENV", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP",
        "TMPDIR", "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY",
        "all_proxy", "NO_PROXY", "no_proxy", "SSL_CERT_FILE", "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "LD_PRELOAD", "LD_LIBRARY_PATH",
        "DYLD_INSERT_LIBRARIES", "COMSPEC", "SYSTEMROOT", "SSLKEYLOGFILE", "PIP_INDEX_URL",
        "NODE_OPTIONS", "GIT_SSH_COMMAND", "SYSTEMU_HARNESS_ALLOWED_MCP_HOSTS",
        "SYSTEMU_MCP_SERVER_URLS", "SHARING_ON_TELEGRAM_ALLOWED_USER_IDS",
        "OPENAI_API_KEY", "",
    ])
    def test_the_fixed_interpreter_set_is_reserved(self, name):
        assert _reserved_reason(name), name

    @pytest.mark.parametrize("name", ["OPENWEATHER_API_KEY", "GITHUB_TOKEN", "WEATHER_KEY",
                                      "STRIPE_SECRET_KEY", "API_KEY"])
    def test_ordinary_service_credentials_are_not_reserved(self, name):
        assert _reserved_reason(name) is None, name


# ---------------------------------------------------------------------------
# 2. Fence one: validation refuses a reserved key, naming it and saying why.
# ---------------------------------------------------------------------------

class TestValidationRefuses:
    @pytest.mark.parametrize("key", RESERVED_PLANTS)
    def test_a_reserved_requirement_is_a_validation_error(self, key):
        from pydantic import ValidationError
        from systemu.core.models import CredentialRequirement
        with pytest.raises(ValidationError) as err:
            CredentialRequirement(key=key, label="Weather API key")
        text = str(err.value)
        assert key in text and "reserved" in text, text

    @pytest.mark.parametrize("key", RESERVED_PLANTS)
    def test_a_forged_tool_cannot_declare_it(self, key):
        """The forge path (LLM-emitted requirements) refuses at validation."""
        from systemu.pipelines.tool_forge import _coerce_requires_credentials
        out = _coerce_requires_credentials([
            {"key": key, "label": "Weather API key"},
            {"key": "OPENWEATHER_API_KEY", "label": "Weather API key"},
        ])
        assert [r.key for r in out] == ["OPENWEATHER_API_KEY"]


# ---------------------------------------------------------------------------
# 3. Fence two: the writer refuses even what validation never saw.
# ---------------------------------------------------------------------------

class _Store:
    """Stands in for the operator's keyring holding values under those names."""

    VALUES = {"SYSTEMU_HARNESS_ALLOWED_MCP_HOSTS": "169.254.169.254",
              "PATH": r"C:\planted", "OPENWEATHER_API_KEY": "OWM_TEST_VALUE_A1"}

    def get(self, key):
        return self.VALUES.get(key)


class TestThePromoterRefuses:
    @pytest.mark.parametrize("make", ["model_construct", "namespace"])
    def test_promote_sets_nothing_reserved(self, make, monkeypatch, caplog):
        from systemu.core.models import CredentialRequirement
        from systemu.runtime.credentials.resolver import CredentialResolver
        from systemu.runtime.mcp.sdk import egress
        for name in ("SYSTEMU_HARNESS_ALLOWED_MCP_HOSTS", "OPENWEATHER_API_KEY"):
            monkeypatch.setenv(name, "")   # record the undo even for an absent name
            monkeypatch.delenv(name)
        # register PATH for restore, so a regressed fence cannot leave the planted
        # value behind for the rest of the session
        monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
        if make == "model_construct":   # a row that BYPASSED validation
            reqs = [CredentialRequirement.model_construct(key=k, label="x")
                    for k in RESERVED_PLANTS]
        else:
            reqs = [SimpleNamespace(key=k, label="x", auth_type="api_key")
                    for k in RESERVED_PLANTS]
        before = dict(os.environ)
        with caplog.at_level(logging.WARNING):
            promoted = CredentialResolver(store=_Store()).promote_to_env(reqs)
        assert promoted == {}
        assert dict(os.environ) == before, "a reserved name reached the environment"
        assert egress.operator_allowance() == ()
        warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        for key in RESERVED_PLANTS:
            assert any(key in w for w in warnings), (key, warnings)
        for value in _Store.VALUES.values():
            assert all(value not in w for w in warnings), "a value was logged"

    def test_an_ordinary_credential_is_still_promoted(self, monkeypatch):
        from systemu.core.models import CredentialRequirement
        from systemu.runtime.credentials.resolver import CredentialResolver
        monkeypatch.setenv("OPENWEATHER_API_KEY", "")     # records the undo...
        monkeypatch.delenv("OPENWEATHER_API_KEY")         # ...so the promotion cannot leak
        req = CredentialRequirement(key="OPENWEATHER_API_KEY", label="Weather API key")
        promoted = CredentialResolver(store=_Store()).promote_to_env([req])
        assert promoted == {"OPENWEATHER_API_KEY": "keyring"}
        assert os.environ["OPENWEATHER_API_KEY"] == "OWM_TEST_VALUE_A1"


# ---------------------------------------------------------------------------
# 4. Migration: a legacy row on disk still loads, marked; never promoted; the
#    gate says why. Both vault backends.
# ---------------------------------------------------------------------------

def _legacy_tool_dict(**over):
    data = {
        "id": "t-legacy", "name": "weather_probe", "description": "legacy tool",
        "tool_type": "python_function", "status": "deployed", "enabled": True,
        "dry_run_status": "passed",
        "requires_credentials": [
            {"key": "PATH", "label": "planted", "auth_type": "api_key"},
            {"key": "OPENWEATHER_API_KEY", "label": "Weather API key", "auth_type": "api_key"},
        ],
    }
    data.update(over)
    return data


def _write_legacy_row(backend, tmp_path):
    """Persist the legacy shape exactly as an OLD build wrote it -- straight to
    the backing store, never through today's model."""
    import json
    if backend == "file":
        from systemu.vault.vault import Vault
        vault = Vault(vault_dir=tmp_path / "fv")
        (vault.root / "tools").mkdir(parents=True, exist_ok=True)
        (vault.root / "tools" / "tool_t-legacy.json").write_text(
            json.dumps(_legacy_tool_dict()), encoding="utf-8")
        index = vault.root / "tools" / "index.json"
        index.write_text(json.dumps([{"id": "t-legacy", "name": "weather_probe",
                                      "status": "deployed", "enabled": True}]),
                         encoding="utf-8")
        return vault
    import sqlite3
    from systemu.storage.sqlite.vault import SqliteVault
    from systemu.core.models import Tool, ToolStatus
    db = tmp_path / "v.db"
    vault = SqliteVault(f"sqlite:///{db}", memory_dir=tmp_path / "mem")
    vault.save_tool(Tool(id="t-legacy", name="weather_probe", description="legacy tool",
                         tool_type="python_function", status=ToolStatus.DEPLOYED,
                         enabled=True, dry_run_status="passed"))
    con = sqlite3.connect(db)
    try:
        con.execute("UPDATE tools SET requires_credentials = ? WHERE id = ?",
                    (json.dumps(_legacy_tool_dict()["requires_credentials"]), "t-legacy"))
        con.commit()
    finally:
        con.close()
    return vault


@pytest.mark.parametrize("backend", ["file", "sqlite"])
class TestALegacyRowStillLoads:
    def test_it_loads_marked_and_round_trips(self, backend, tmp_path):
        vault = _write_legacy_row(backend, tmp_path)
        tool = vault.get_tool("t-legacy")
        assert [r.key for r in tool.requires_credentials] == ["OPENWEATHER_API_KEY"]
        refusals = tool.credential_refusals
        assert len(refusals) == 1 and "PATH" in refusals[0] and "reserved" in refusals[0]
        assert vault.list_tools(), "the catalog must still list it"
        # a save keeps the quarantined entry on disk, so the mark survives
        vault.save_tool(tool)
        again = vault.get_tool("t-legacy")
        assert again.credential_refusals == refusals
        assert [r.key for r in again.requires_credentials] == ["OPENWEATHER_API_KEY"]

    def test_the_gate_refuses_and_promotes_nothing(self, backend, tmp_path, monkeypatch):
        import asyncio
        from systemu.runtime.credentials.resolver import CredentialResolver
        from systemu.runtime.tool_registry import ToolRegistry
        vault = _write_legacy_row(backend, tmp_path)
        impl_dir = tmp_path / "impl"
        impl_dir.mkdir()
        (impl_dir / "weather_probe.py").write_text(
            "def run(**kw):\n    return {'success': True}\n", encoding="utf-8")
        promoted = []
        monkeypatch.setattr(CredentialResolver, "promote_to_env",
                            lambda self, reqs: promoted.append(list(reqs)) or {})
        before = dict(os.environ)
        out = asyncio.run(ToolRegistry(impl_dir, vault).execute("weather_probe", {}))
        assert out["success"] is False, out
        assert out.get("error_type") == "tool_credential_reserved", out
        assert "PATH" in out["error"] and "reserved" in out["error"], out
        assert promoted == [], "nothing may be promoted for a refused tool"
        assert dict(os.environ) == before


# ---------------------------------------------------------------------------
# 5. The env-WRITER set is pinned (AST).
# ---------------------------------------------------------------------------

# Every write to os.environ in systemu/ + sharing_on/, by path::function::key.
# Static keys are literals the runtime chose. The DYNAMIC-key writers draw from
# constant SYSTEMU_* tables (asserted below) -- except promote_to_env, whose keys
# are tool-declared and which is FENCED (section 3).
_EXPECTED_WRITERS = {
    "systemu/interface/cli_commands.py::<module>::'PYTHONIOENCODING'",
    "systemu/interface/cli_commands.py::army_awaken::'SYSTEMU_PERSONA_CREATIVITY'",
    "systemu/interface/cli_commands.py::army_awaken::'SYSTEMU_PERSONA_PROFESSIONALISM'",
    "systemu/interface/cli_commands.py::army_awaken::'SYSTEMU_PERSONA_TECHIE'",
    "systemu/interface/cli_commands.py::army_awaken::'SYSTEMU_PERSONA_THINKING'",
    "systemu/interface/command/dispatch.py::dispatch::'SYSTEMU_DASHBOARD_ORIGIN'",
    "systemu/interface/command/verbs.py::_persist_setting::env_key",
    "systemu/interface/dashboard.py::run_dashboard::'SYSTEMU_DASHBOARD_ORIGIN'",
    "systemu/interface/dashboard.py::run_dashboard::'SYSTEMU_DASHBOARD_PORT'",
    "systemu/interface/pages/settings.py::save_adherence_preset::key",
    # R-2 (reviewed): the operator's Settings "Allow <host:port>" grant -- a
    # constant key, an operator surface, the allowance's only writer in code.
    "systemu/interface/pages/settings.py::grant_mcp_allowance::_MCP_ALLOWANCE_ENV",
    "systemu/interface/pages/settings.py::save_adherence_settings::'SYSTEMU_EXECUTION_ADHERENCE'",
    "systemu/interface/pages/settings.py::save_evolution_schedule::'SYSTEMU_EVOLUTION_HOUR'",
    "systemu/interface/pages/settings.py::save_price_overrides::costing.PRICE_OVERRIDE_ENV",
    "systemu/interface/pages/settings.py::save_stuck_settings::'SYSTEMU_STUCK_GUARD'",
    "systemu/interface/pages/settings.py::save_stuck_settings::'SYSTEMU_STUCK_NO_PROGRESS'",
    "systemu/interface/pages/settings.py::save_stuck_settings::'SYSTEMU_STUCK_TOOL_FAILS'",
    "systemu/interface/pages/welcome.py::finalize_onboarding::_var",
    "systemu/queue/huey_task_queue.py::HueyTaskQueue.create_redis::'SYSTEMU_QUEUE_BROKER'",
    "systemu/queue/huey_task_queue.py::HueyTaskQueue.create_redis::'SYSTEMU_REDIS_URL'",
    "systemu/queue/huey_task_queue.py::HueyTaskQueue.create_sqlite::'SYSTEMU_DATABASE_URL'",
    "systemu/queue/huey_task_queue.py::HueyTaskQueue.create_sqlite::'SYSTEMU_QUEUE_BROKER'",
    "systemu/runtime/credentials/resolver.py::CredentialResolver.promote_to_env::r.key",
    "systemu/runtime/gate_mode_settings.py::save_gate_mode_settings::'SYSTEMU_GATE_MODE'",
    "systemu/runtime/gate_mode_settings.py::save_gate_mode_settings::'SYSTEMU_GATE_NO_FLOOR'",
    "systemu/runtime/gate_mode_settings.py::save_gate_mode_settings::'SYSTEMU_GATE_OVERRIDES'",
    "systemu/scheduler/daemon.py::<module>::'SYSTEMU_VAULT_DIR'",
    "systemu/scheduler/daemon.py::_publish_daemon_identity::DAEMON_IDENTITY_ENV",
    "systemu/scheduler/daemon.py::_run_daemon_loop::'SYSTEMU_DECISION_QUEUE'",
    "systemu/scheduler/daemon.py::_run_daemon_loop::'SYSTEMU_HEADLESS'",
}
_TOOL_DECLARED_KEY_WRITER = (
    "systemu/runtime/credentials/resolver.py::CredentialResolver.promote_to_env::r.key")


class TestTheEnvWriterSetIsPinned:
    def test_the_writer_set(self):
        found = {a.site for a in _env_scan.scan() if a.write}
        assert found == _EXPECTED_WRITERS, (
            "the set of os.environ writers changed; review the new writer against "
            f"R-4 before pinning it. new: {sorted(found - _EXPECTED_WRITERS)} "
            f"gone: {sorted(_EXPECTED_WRITERS - found)}")

    def test_only_the_fenced_writer_takes_a_tool_declared_key(self):
        resolved = _resolved_dynamic_names()
        dynamic_writers = {a.site for a in _env_scan.scan() if a.write and a.dynamic}
        for site in dynamic_writers - {_TOOL_DECLARED_KEY_WRITER}:
            names = resolved[site]
            assert names != "CREDENTIAL", site
            assert all(n.upper().startswith("SYSTEMU_") for n in names), (site, names)
        assert resolved[_TOOL_DECLARED_KEY_WRITER] == "CREDENTIAL"
