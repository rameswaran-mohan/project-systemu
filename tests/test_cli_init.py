"""Tests for the new `sharing_on init` command (v0.7.4 Pattern 4)."""
import json
import os
import sys
import subprocess
from pathlib import Path


def test_init_creates_vault_and_seeds_starter_catalog(tmp_path):
    """sharing_on init must copy starter tools + skills from package data
    into the CWD vault if the vault doesn't already exist."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("OPENROUTER_", "SYSTEMU_", "SHARING_ON_"))}
    env["PATH"] = os.environ.get("PATH", "")
    env["PYTHONIOENCODING"] = "utf-8"

    result = subprocess.run(
        [sys.executable, "-m", "sharing_on", "init"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=60,
    )
    assert result.returncode == 0, f"init failed: {result.stdout}\n{result.stderr}"
    # Vault should exist
    vault = tmp_path / "systemu" / "vault"
    assert vault.exists(), f"vault dir not created at {vault}"
    # Should have at least one starter tool
    tools_idx = vault / "tools" / "index.json"
    assert tools_idx.exists(), "tools/index.json missing after init"
    tools = json.loads(tools_idx.read_text())
    assert len(tools) > 0, "starter catalog produced 0 tools — wheel package-data not honored"
    # Should have a seed log
    seed_log = vault / ".seed_log.json"
    assert seed_log.exists(), "seed log not written"


def test_init_is_idempotent(tmp_path):
    """Running init twice must not duplicate entries."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("OPENROUTER_", "SYSTEMU_", "SHARING_ON_"))}
    env["PATH"] = os.environ.get("PATH", "")
    env["PYTHONIOENCODING"] = "utf-8"

    for run_n in range(2):
        result = subprocess.run(
            [sys.executable, "-m", "sharing_on", "init"],
            cwd=str(tmp_path), capture_output=True, text=True,
            encoding="utf-8", errors="replace", env=env, timeout=60,
        )
        assert result.returncode == 0, f"run {run_n} failed: {result.stdout}{result.stderr}"

    # Count tools — should match what one run would have produced
    tools = json.loads((tmp_path / "systemu" / "vault" / "tools" / "index.json").read_text())
    ids = [t["id"] for t in tools]
    assert len(ids) == len(set(ids)), "duplicate tool ids after second init run"
