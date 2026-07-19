"""E2E: docker-compose profile rendering.

Skipped when ``docker`` is not on PATH.  For each profile we expect to ship,
runs ``docker compose --profile <p> config`` and parses the YAML output to
confirm the right services exist with the right env wiring.

This catches typos that compose itself would only surface at ``up`` time.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
HAS_DOCKER = shutil.which("docker") is not None

pytestmark = pytest.mark.skipif(not HAS_DOCKER, reason="docker CLI not on PATH")


def _has_compose_v2() -> bool:
    if not HAS_DOCKER:
        return False
    try:
        r = subprocess.run(
            ["docker", "compose", "version"],
            capture_output=True, text=True, timeout=10,
        )
        return r.returncode == 0
    except Exception:
        return False


@pytest.fixture
def stage(tmp_path):
    """Stage the compose file + a synthetic .env so tests don't touch the repo's."""
    stage_dir = tmp_path / "compose-stage"
    stage_dir.mkdir()
    shutil.copy(REPO_ROOT / "docker-compose.yml", stage_dir / "docker-compose.yml")
    shutil.copy(REPO_ROOT / "Dockerfile", stage_dir / "Dockerfile")
    (stage_dir / ".env").write_text(
        "POSTGRES_USER=systemu\n"
        "POSTGRES_PASSWORD=secret\n"
        "POSTGRES_DB=systemu\n"
        "REDIS_PASSWORD=redis-secret\n"
        "REDIS_AUTH=:redis-secret@\n"
        "WORKER_REPLICAS=3\n"
        "HUEY_WORKERS=4\n"
        "SYSTEMU_PORT=8765\n",
        encoding="utf-8",
    )
    return stage_dir


def _config_yaml(stage_dir: Path, profile: str | None) -> dict:
    yaml = pytest.importorskip("yaml")
    args = ["docker", "compose"]
    if profile:
        args += ["--profile", profile]
    args += ["config"]
    proc = subprocess.run(
        args, cwd=str(stage_dir), capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, (
        f"compose config failed for profile={profile}:\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    return yaml.safe_load(proc.stdout)


def test_default_profile_exposes_no_services(stage):
    """With no profile specified, compose should bring up nothing.

    Smoke-run Bug 7 moved the legacy ``systemu`` service behind
    ``profiles: ["legacy"]`` so that ``docker compose up`` no longer
    starts it and collides on container name + port 8765 with the
    profile-driven services.  This test pins that behaviour: no
    services are enabled by default — operators must pick a profile.
    """
    if not _has_compose_v2():
        pytest.skip("docker compose V2 plugin not available")
    cfg = _config_yaml(stage, profile=None)
    services = set(cfg.get("services", {}).keys())
    # Every service in docker-compose.yml has a `profiles:` key (legacy,
    # local, enterprise, docker-sandbox), so no-profile must be empty.
    assert services == set(), (
        f"Default profile should expose no services, found: {services}"
    )


def test_legacy_profile_brings_up_systemu_service(stage):
    """The legacy file-backend ``systemu`` service is still available
    explicitly via ``--profile legacy`` for backwards compatibility."""
    if not _has_compose_v2():
        pytest.skip("docker compose V2 plugin not available")
    cfg = _config_yaml(stage, profile="legacy")
    services = set(cfg.get("services", {}).keys())
    assert "systemu" in services


def test_local_profile_brings_up_postgres_and_one_worker(stage):
    if not _has_compose_v2():
        pytest.skip("docker compose V2 plugin not available")
    cfg = _config_yaml(stage, profile="local")
    services = cfg["services"]
    assert "postgres-local" in services
    assert "systemu-dashboard-local" in services
    assert "systemu-worker-local" in services
    # Worker uses sqlite broker
    worker_env = services["systemu-worker-local"]["environment"]
    assert worker_env.get("SYSTEMU_QUEUE_BROKER") == "sqlite"
    assert worker_env.get("SYSTEMU_STORAGE") == "postgres"


def test_enterprise_profile_brings_up_redis_and_scaled_workers(stage):
    if not _has_compose_v2():
        pytest.skip("docker compose V2 plugin not available")
    cfg = _config_yaml(stage, profile="enterprise")
    services = cfg["services"]
    assert "postgres" in services
    assert "redis" in services
    assert "systemu-dashboard" in services
    assert "systemu-worker" in services

    worker_env = services["systemu-worker"]["environment"]
    assert worker_env.get("SYSTEMU_QUEUE_BROKER") == "redis"
    redis_url = worker_env.get("SYSTEMU_REDIS_URL", "")
    # REDIS_AUTH=":redis-secret@" must have interpolated into the URL
    assert "redis-secret" in redis_url


def test_enterprise_dashboard_depends_on_healthy_services(stage):
    if not _has_compose_v2():
        pytest.skip("docker compose V2 plugin not available")
    cfg = _config_yaml(stage, profile="enterprise")
    deps = cfg["services"]["systemu-dashboard"]["depends_on"]
    # depends_on may be a dict {svc: {condition: ...}} or a list — check both
    if isinstance(deps, dict):
        assert deps["postgres"]["condition"] == "service_healthy"
        assert deps["redis"]["condition"] == "service_healthy"
    else:
        assert "postgres" in deps and "redis" in deps


def test_docker_sandbox_pip_cache_volume_matches_the_backend_default(stage):
    """DockerBackend (systemu/runtime/backend/docker.py) mounts the pip cache
    into each EPHEMERAL tool container via a bare `docker run -v
    <name>:/root/.cache/pip` — never through compose. That `docker run` call
    creates the volume under its LITERAL name (no compose project prefix), so
    the compose-declared `pip_cache` volume must carry a `name:` override
    equal to that same literal name. Otherwise compose creates/tracks its own
    "<project>_pip_cache" — a distinct volume the tool containers never
    touch — and `docker compose down -v` cleans up the wrong one, orphaning
    the cache the tools actually use."""
    if not _has_compose_v2():
        pytest.skip("docker compose V2 plugin not available")
    import inspect
    from systemu.runtime.backend.docker import DockerBackend

    backend_default = inspect.signature(DockerBackend.__init__).parameters[
        "pip_cache_volume"
    ].default

    cfg = _config_yaml(stage, profile="docker-sandbox")
    declared = cfg["volumes"]["pip_cache"]
    assert declared.get("name") == backend_default, (
        f"compose's pip_cache volume resolves to {declared.get('name')!r} but "
        f"DockerBackend's ephemeral tool containers mount {backend_default!r} — "
        "`docker compose down -v` would clean up the wrong volume"
    )
