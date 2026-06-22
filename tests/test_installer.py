"""Tests for install.py — env rendering, marker file, mode picker."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Load install.py as a module (it lives at repo root, not on the package path).
_spec = importlib.util.spec_from_file_location("install_module", REPO_ROOT / "install.py")
assert _spec and _spec.loader
install = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(install)


# ─── render_env ─────────────────────────────────────────────────────────────

def test_render_env_simple_keys() -> None:
    out = install.render_env({"FOO": "bar", "BAZ": "42"})
    assert out == "FOO=bar\nBAZ=42\n"


def test_render_env_quotes_special_chars() -> None:
    out = install.render_env({"PASS": "p@ss word"})
    assert "PASS=\"p@ss word\"" in out


def test_render_env_escapes_quotes_and_backslashes() -> None:
    out = install.render_env({"X": 'it has "quotes" and \\ backslash'})
    # \\\" is the escape sequence inside the quoted string
    assert 'X="it has \\"quotes\\" and \\\\ backslash"' in out


# ─── merge_existing_env ─────────────────────────────────────────────────────

def test_merge_existing_env_carries_unspecified_keys(tmp_path, monkeypatch) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("EXISTING_CUSTOM=keepme\nMODE=oldvalue\n", encoding="utf-8")
    monkeypatch.setattr(install, "ENV_PATH", env_path)

    merged = install.merge_existing_env({"MODE": "newvalue", "ADDED": "yes"})
    assert merged["EXISTING_CUSTOM"] == "keepme"  # preserved
    assert merged["MODE"] == "newvalue"            # overridden
    assert merged["ADDED"] == "yes"                # newly added


def test_merge_existing_env_strips_quotes_when_reading(tmp_path, monkeypatch) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text('QUOTED="has spaces"\n', encoding="utf-8")
    monkeypatch.setattr(install, "ENV_PATH", env_path)

    merged = install.merge_existing_env({})
    assert merged["QUOTED"] == "has spaces"


def test_merge_existing_env_no_existing_file(tmp_path, monkeypatch) -> None:
    env_path = tmp_path / ".env"   # doesn't exist
    monkeypatch.setattr(install, "ENV_PATH", env_path)

    merged = install.merge_existing_env({"A": "1"})
    assert merged == {"A": "1"}


# ─── mode marker file ───────────────────────────────────────────────────────

def test_mode_marker_round_trip(tmp_path, monkeypatch) -> None:
    marker = tmp_path / ".systemu_mode"
    monkeypatch.setattr(install, "MODE_MARKER", marker)
    monkeypatch.setattr(install, "REPO_ROOT", tmp_path)

    install.write_mode_marker("docker-enterprise")
    assert install.read_existing_mode() == "docker-enterprise"


def test_read_existing_mode_returns_none_when_missing(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(install, "MODE_MARKER", tmp_path / ".systemu_mode")
    assert install.read_existing_mode() is None


# ─── per-mode .env content (renders the right keys for each mode) ───────────

def _env_for_mode(values: dict) -> str:
    return install.render_env(values)


def test_local_env_includes_sqlite_broker_keys() -> None:
    # The local-mode dict written by setup_local()
    out = _env_for_mode({
        "SYSTEMU_MODE": "local",
        "SYSTEMU_STORAGE": "sqlite",
        "SYSTEMU_DATABASE_URL": "sqlite:///./data/systemu.db",
        "SYSTEMU_QUEUE": "huey",
        "SYSTEMU_QUEUE_BROKER": "sqlite",
    })
    assert "SYSTEMU_MODE=local" in out
    assert "SYSTEMU_QUEUE_BROKER=sqlite" in out
    assert "SYSTEMU_STORAGE=sqlite" in out
    assert "SYSTEMU_REDIS_URL" not in out


def test_docker_local_env_uses_postgres_with_sqlite_broker() -> None:
    out = _env_for_mode({
        "SYSTEMU_MODE": "docker-local",
        "SYSTEMU_STORAGE": "postgres",
        "SYSTEMU_DATABASE_URL": "postgresql://systemu:pw@postgres-local:5432/systemu",
        "SYSTEMU_QUEUE": "huey",
        "SYSTEMU_QUEUE_BROKER": "sqlite",
        "POSTGRES_PASSWORD": "pw",
    })
    assert "SYSTEMU_MODE=docker-local" in out
    assert "SYSTEMU_STORAGE=postgres" in out
    assert "SYSTEMU_QUEUE_BROKER=sqlite" in out
    assert "SYSTEMU_REDIS_URL" not in out


def test_docker_enterprise_env_uses_redis_broker_and_replicas() -> None:
    out = _env_for_mode({
        "SYSTEMU_MODE": "docker-enterprise",
        "SYSTEMU_STORAGE": "postgres",
        "SYSTEMU_QUEUE": "huey",
        "SYSTEMU_QUEUE_BROKER": "redis",
        "SYSTEMU_REDIS_URL": "redis://:s3cret@redis:6379/0",
        "WORKER_REPLICAS": "5",
    })
    assert "SYSTEMU_QUEUE_BROKER=redis" in out
    assert "SYSTEMU_REDIS_URL=" in out
    assert "WORKER_REPLICAS=5" in out


# ─── pick_mode argument logic (without prompting) ──────────────────────────-

def test_pick_mode_respects_explicit_flag() -> None:
    args = install.build_parser().parse_args(["--mode", "docker-enterprise", "--non-interactive"])
    assert install.pick_mode(args, existing=None) == "docker-enterprise"


def test_pick_mode_non_interactive_without_mode_exits() -> None:
    args = install.build_parser().parse_args(["--non-interactive"])
    with pytest.raises(SystemExit):
        install.pick_mode(args, existing=None)
