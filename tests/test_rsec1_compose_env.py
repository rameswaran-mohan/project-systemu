"""R-SEC1 — the non-loopback (0.0.0.0) dashboard profiles must carry a
``SYSTEMU_DASHBOARD_PASSPHRASE_HASH`` env key.

With R-SEC1's fail-closed gate, a ``0.0.0.0`` bind REFUSES to start unless a
passphrase is configured. Every compose service that binds the dashboard on
``0.0.0.0`` must therefore surface the env var (defaulted empty via
``${SYSTEMU_DASHBOARD_PASSPHRASE_HASH:-}``) so an operator can supply it from
``.env`` without editing the compose file.

This is a light stdlib+PyYAML parse of ``docker-compose.yml`` (no docker CLI),
complementing the docker-gated e2e test in ``tests/e2e/``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parents[1]
_ENV_KEY = "SYSTEMU_DASHBOARD_PASSPHRASE_HASH"

# every compose service that binds the dashboard on 0.0.0.0 (non-loopback)
_DASHBOARD_SERVICES = (
    "systemu",                    # legacy profile
    "systemu-dashboard-local",    # docker-local profile
    "systemu-dashboard",          # docker-enterprise profile
    "systemu-docker",             # docker-sandbox profile
)


def _load_compose() -> dict:
    text = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    return yaml.safe_load(text)


def test_compose_dashboard_services_have_passphrase_hash_env():
    cfg = _load_compose()
    services = cfg["services"]
    for name in _DASHBOARD_SERVICES:
        assert name in services, f"missing dashboard service {name!r}"
        env = services[name]["environment"]
        # environment may be a mapping (our style) — assert the key is present
        assert isinstance(env, dict), f"{name} environment is not a mapping: {env!r}"
        assert _ENV_KEY in env, (
            f"dashboard service {name!r} binds 0.0.0.0 but is missing {_ENV_KEY} "
            f"(R-SEC1 fail-closed gate would refuse to start)"
        )


def test_compose_passphrase_env_defaults_empty():
    """The env value passes an operator-supplied hash through, defaulting empty
    so a fresh compose file still parses (the gate then refuses at start-time)."""
    cfg = _load_compose()
    services = cfg["services"]
    for name in _DASHBOARD_SERVICES:
        env = services[name]["environment"]
        assert env[_ENV_KEY] == "${SYSTEMU_DASHBOARD_PASSPHRASE_HASH:-}", (
            f"{name}: expected passthrough default, got {env[_ENV_KEY]!r}"
        )


def test_compose_only_zero_bind_services_listed():
    """Guard: every service we assert on genuinely binds 0.0.0.0 (so the test
    stays honest if a profile's bind host ever changes)."""
    cfg = _load_compose()
    services = cfg["services"]
    for name in _DASHBOARD_SERVICES:
        env = services[name]["environment"]
        assert env.get("SYSTEMU_DASHBOARD_HOST") == "0.0.0.0", (
            f"{name}: expected a 0.0.0.0 bind, got {env.get('SYSTEMU_DASHBOARD_HOST')!r}"
        )
