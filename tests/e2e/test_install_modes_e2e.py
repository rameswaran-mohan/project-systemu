"""E2E: drive install.py as a subprocess in tmp_path.

Verifies the installer wizard produces the right .env / .systemu_mode and that
re-running with a different mode preserves user-supplied keys (the
``merge_existing_env`` contract).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]


def _stage(tmp_path: Path) -> Path:
    """Copy the minimum file set for install.py to operate.  We can't symlink
    or run install.py from the repo root because it would touch the live .env."""
    stage = tmp_path / "stage"
    stage.mkdir()
    for name in ("install.py", "pyproject.toml", "docker-compose.yml", ".env.example"):
        shutil.copy(REPO_ROOT / name, stage / name)
    return stage


def _read_env(env_path: Path) -> dict:
    """Parse a simple .env into a dict (mirrors install.merge_existing_env)."""
    out = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", s)
        if not m:
            continue
        k, v = m.group(1), m.group(2)
        if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
            v = v[1:-1]
        out[k] = v
    return out


def _run_install(stage: Path, *args: str, expect_ok: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        [sys.executable, "install.py", *args],
        cwd=str(stage),
        capture_output=True,
        text=True,
        timeout=60,
    )
    if expect_ok:
        assert proc.returncode == 0, f"install.py failed:\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    return proc


def test_install_local_mode_writes_expected_env(tmp_path):
    stage = _stage(tmp_path)
    _run_install(
        stage,
        "--mode", "local",
        "--non-interactive",
        "--skip-playwright", "--skip-deps",
        "--openrouter-key", "or-test",
        "--google-key", "ggl-test",
    )
    env = _read_env(stage / ".env")
    assert env["SYSTEMU_MODE"] == "local"
    assert env["SYSTEMU_QUEUE_BROKER"] == "sqlite"
    assert env["SYSTEMU_STORAGE"] == "sqlite"
    assert env["OPENROUTER_API_KEY"] == "or-test"
    assert env["GOOGLE_API_KEY"] == "ggl-test"
    assert (stage / ".systemu_mode").read_text(encoding="utf-8").strip() == "local"


def test_install_docker_enterprise_mode_writes_expected_env(tmp_path):
    stage = _stage(tmp_path)
    _run_install(
        stage,
        "--mode", "docker-enterprise",
        "--non-interactive",
        "--skip-pull",
        "--pg-password", "pg-secret",
        "--redis-password", "redis-secret",
        "--worker-replicas", "5",
        "--openrouter-key", "or",
        "--google-key", "ggl",
    )
    env = _read_env(stage / ".env")
    assert env["SYSTEMU_MODE"] == "docker-enterprise"
    assert env["SYSTEMU_QUEUE_BROKER"] == "redis"
    assert env["WORKER_REPLICAS"] == "5"
    assert env["POSTGRES_PASSWORD"] == "pg-secret"
    assert env["REDIS_PASSWORD"] == "redis-secret"
    assert "redis-secret" in env["SYSTEMU_REDIS_URL"]
    # enterprise must NOT expose Postgres by default
    assert env["SYSTEMU_DB_BIND"] == ""


def test_install_docker_local_writes_db_bind_for_capture_flow(tmp_path):
    """docker-local exposes Postgres on 127.0.0.1:5432 by default so
    `sharing_on record` on the host can reach the container's vault."""
    stage = _stage(tmp_path)
    _run_install(
        stage,
        "--mode", "docker-local",
        "--non-interactive",
        "--skip-pull",
        "--pg-password", "pg-secret",
        "--openrouter-key", "or",
        "--google-key", "ggl",
    )
    env = _read_env(stage / ".env")
    assert env["SYSTEMU_MODE"] == "docker-local"
    assert env["SYSTEMU_STORAGE"] == "postgres"
    assert env["SYSTEMU_QUEUE_BROKER"] == "sqlite"
    # The killer assertion:
    assert env["SYSTEMU_DB_BIND"] == "127.0.0.1:5432"
    # docker-local must write an absolute outputs host dir so Docker
    # Desktop on Windows doesn't degrade the bind mount to a named volume.
    # on Windows the default is ~/SystemuOutputs (auto-shared by
    # Docker Desktop); on Linux/macOS it remains project-relative ./outputs.
    assert "SYSTEMU_HOST_OUTPUTS_DIR" in env
    assert "outputs" in env["SYSTEMU_HOST_OUTPUTS_DIR"].lower()


def test_install_reconfigure_preserves_user_keys(tmp_path):
    """Reconfiguring from local → docker-local must not drop a custom key the
    operator added by hand to .env between runs."""
    stage = _stage(tmp_path)
    _run_install(
        stage,
        "--mode", "local", "--non-interactive", "--skip-playwright", "--skip-deps",
        "--openrouter-key", "first-key", "--google-key", "ggl-first",
    )
    # Operator hand-edits .env to add a custom proxy setting
    with (stage / ".env").open("a", encoding="utf-8") as fh:
        fh.write("\nMY_CUSTOM_PROXY=https://proxy.example.com\n")

    _run_install(
        stage,
        "--mode", "docker-local",
        "--non-interactive",
        "--skip-pull",
        "--pg-password", "pg-x",
        # NOTE: NOT passing API keys this time — they should survive from the
        # first run via merge_existing_env.
    )
    env = _read_env(stage / ".env")
    assert env["SYSTEMU_MODE"] == "docker-local"
    assert env["SYSTEMU_QUEUE_BROKER"] == "sqlite"
    # User-added key survived
    assert env.get("MY_CUSTOM_PROXY") == "https://proxy.example.com"
    # API keys from first run survived
    assert env["OPENROUTER_API_KEY"] == "first-key"
    assert env["GOOGLE_API_KEY"] == "ggl-first"


def test_install_non_interactive_without_mode_exits_nonzero(tmp_path):
    stage = _stage(tmp_path)
    proc = _run_install(stage, "--non-interactive", expect_ok=False)
    assert proc.returncode != 0


def test_install_docker_local_sets_dep_install_mode_allowlist(tmp_path):
    """docker-local defaults to allow-list dep install mode."""
    stage = _stage(tmp_path)
    _run_install(
        stage,
        "--mode", "docker-local",
        "--non-interactive",
        "--skip-pull",
        "--pg-password", "p",
        "--openrouter-key", "or",
        "--google-key", "ggl",
        "--approve-tool-deps",
    )
    env = _read_env(stage / ".env")
    assert env["SYSTEMU_TOOL_DEP_INSTALL_MODE"] == "allow-list"


def test_install_local_keeps_auto_mode_v068(tmp_path):
    """local mode keeps auto (unchanged from v0.6.7)."""
    stage = _stage(tmp_path)
    _run_install(
        stage,
        "--mode", "local",
        "--non-interactive",
        "--skip-playwright",
        "--skip-deps",
        "--openrouter-key", "or",
        "--google-key", "ggl",
    )
    env = _read_env(stage / ".env")
    assert env["SYSTEMU_TOOL_DEP_INSTALL_MODE"] == "auto"


def test_install_docker_enterprise_sets_dep_install_mode_allowlist(tmp_path):
    stage = _stage(tmp_path)
    _run_install(
        stage,
        "--mode", "docker-enterprise",
        "--non-interactive",
        "--skip-pull",
        "--pg-password", "p",
        "--redis-password", "r",
        "--worker-replicas", "2",
        "--openrouter-key", "or",
        "--google-key", "ggl",
        "--approve-tool-deps",
    )
    env = _read_env(stage / ".env")
    assert env["SYSTEMU_TOOL_DEP_INSTALL_MODE"] == "allow-list"


def test_install_docker_local_writes_requirements_tools(tmp_path):
    """wizard scans tool deps and writes tools/requirements-tools.txt
    when --approve-tool-deps is set."""
    stage = _stage(tmp_path)
    impl = stage / "systemu" / "vault" / "tools" / "implementations"
    impl.mkdir(parents=True)
    (impl / "fetch_json.py").write_text("# deps: requests\n")
    (impl / "create_word_doc.py").write_text("# deps: python-docx\n")

    _run_install(
        stage,
        "--mode", "docker-local",
        "--non-interactive",
        "--skip-pull",
        "--pg-password", "p",
        "--openrouter-key", "or",
        "--google-key", "ggl",
        "--approve-tool-deps",
    )
    reqs_path = stage / "tools" / "requirements-tools.txt"
    assert reqs_path.exists(), f"missing {reqs_path}"
    reqs = reqs_path.read_text(encoding="utf-8").splitlines()
    assert "requests" in reqs
    assert "python-docx" in reqs
