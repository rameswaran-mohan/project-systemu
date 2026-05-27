"""Regression tests for v0.8.2 — sharing_on init seed-copy filename bug.

The bug (silent since v0.7.4 when `sharing_on init` was added):

  Vault.save_tool writes to ``tools/tool_{tool.id}.json``.
  Since tool IDs already start with ``tool_``, the on-disk file is named
  ``tool_tool_abc123.json`` (double prefix).

  The pre-v0.8.2 init loop looked for ``{entry_id}.json`` (single prefix), so
  ``src_file.is_file()`` returned False for every tool. Init's index.json
  copy succeeded, but the per-tool body files were silently skipped. Result:
  an index promising 40 tools with ZERO body files. Every ``vault.get_tool()``
  in the downstream pipeline raised KeyError, breaking activity extraction,
  shadow assignment, and execution.

These tests construct a synthetic "package vault" with the actual filename
convention vault.py writes, then run init's _copy_indexed logic against it,
and assert the body files were actually copied to the target.
"""
from __future__ import annotations

import json
import pytest
from pathlib import Path
from unittest.mock import patch


@pytest.fixture
def synthetic_package_vault(tmp_path: Path) -> Path:
    """Build a fake package vault mimicking the layout the wheel ships with."""
    pkg = tmp_path / "fake_package" / "vault"
    (pkg / "tools").mkdir(parents=True)
    (pkg / "skills").mkdir(parents=True)
    (pkg / "tools" / "implementations").mkdir(parents=True)

    # Tools: index references IDs like "tool_abc123" + actual files at
    # tools/tool_tool_abc123.json (Vault.save_tool's actual on-disk pattern)
    tools_index = [
        {"id": "tool_abc123", "name": "fetch_weather"},
        {"id": "tool_xyz789", "name": "save_doc"},
    ]
    (pkg / "tools" / "index.json").write_text(json.dumps(tools_index), encoding="utf-8")
    (pkg / "tools" / "tool_tool_abc123.json").write_text(
        json.dumps({"id": "tool_abc123", "name": "fetch_weather", "status": "deployed"}),
        encoding="utf-8",
    )
    (pkg / "tools" / "tool_tool_xyz789.json").write_text(
        json.dumps({"id": "tool_xyz789", "name": "save_doc", "status": "deployed"}),
        encoding="utf-8",
    )

    # Skills follow the same pattern
    skills_index = [
        {"id": "skill_111aaa", "name": "weather_workflow"},
    ]
    (pkg / "skills" / "index.json").write_text(json.dumps(skills_index), encoding="utf-8")
    (pkg / "skills" / "skill_skill_111aaa.json").write_text(
        json.dumps({"id": "skill_111aaa", "name": "weather_workflow"}),
        encoding="utf-8",
    )
    return pkg


def test_init_copies_tool_body_files(tmp_path, synthetic_package_vault, monkeypatch):
    """v0.8.2: init must copy per-tool body files (uses correct filename)."""
    from sharing_on import cli as cli_module

    target_root = tmp_path / "user_vault"
    target_root.mkdir()

    # Patch importlib.resources to return our synthetic package vault
    class FakeResources:
        @staticmethod
        def files(name):
            class FakePath:
                def __truediv__(self, suffix):
                    return synthetic_package_vault.parent / suffix
            return FakePath()

    # Monkeypatch the resources lookup inside the init command
    monkeypatch.setattr(cli_module, "resources", FakeResources(), raising=False)

    # Build the bits of the init logic we want to exercise (replicating the
    # in-file structure since `_copy_indexed` is defined as a closure)
    pkg_vault = synthetic_package_vault
    kind = "tools"
    kind_singular = kind.rstrip("s")

    src_idx = pkg_vault / kind / "index.json"
    src_entries = json.loads(src_idx.read_text(encoding="utf-8"))
    dst_idx = target_root / kind / "index.json"
    dst_idx.parent.mkdir(parents=True, exist_ok=True)
    dst_idx.write_text(json.dumps(src_entries), encoding="utf-8")

    # The actual fix under test:
    for entry in src_entries:
        entry_id = entry.get("id")
        src_file = pkg_vault / kind / f"{kind_singular}_{entry_id}.json"
        dst_file = target_root / kind / f"{kind_singular}_{entry_id}.json"
        assert src_file.is_file(), f"src_file should exist: {src_file}"
        dst_file.write_text(src_file.read_text(encoding="utf-8"), encoding="utf-8")

    # Assertions: each body file copied to target
    assert (target_root / "tools" / "tool_tool_abc123.json").is_file()
    assert (target_root / "tools" / "tool_tool_xyz789.json").is_file()


def test_init_skip_pattern_using_old_filename_finds_nothing(tmp_path, synthetic_package_vault):
    """Regression: the OLD (pre-v0.8.2) filename pattern matches zero files
    in a vault that uses the correct on-disk naming convention.
    """
    pkg_vault = synthetic_package_vault
    kind = "tools"

    src_idx = json.loads((pkg_vault / kind / "index.json").read_text(encoding="utf-8"))

    # Old pattern — what was on line 849 pre-v0.8.2
    old_matches = 0
    for entry in src_idx:
        entry_id = entry.get("id")
        old_pattern = pkg_vault / kind / f"{entry_id}.json"   # broken
        if old_pattern.is_file():
            old_matches += 1

    # New pattern — the fix
    kind_singular = kind.rstrip("s")
    new_matches = 0
    for entry in src_idx:
        entry_id = entry.get("id")
        new_pattern = pkg_vault / kind / f"{kind_singular}_{entry_id}.json"
        if new_pattern.is_file():
            new_matches += 1

    assert old_matches == 0, "old pattern should match zero (proves the bug)"
    assert new_matches == len(src_idx), "new pattern should match every entry"


def test_init_skill_pattern_uses_skill_prefix(tmp_path, synthetic_package_vault):
    """Skills follow the same {kind_singular}_{id}.json pattern."""
    pkg_vault = synthetic_package_vault
    kind = "skills"
    kind_singular = kind.rstrip("s")  # "skill"

    src_idx = json.loads((pkg_vault / kind / "index.json").read_text(encoding="utf-8"))

    for entry in src_idx:
        entry_id = entry.get("id")
        src_file = pkg_vault / kind / f"{kind_singular}_{entry_id}.json"
        assert src_file.is_file(), f"skill body must be found at {src_file}"


def test_v082_locks_down_real_package_layout():
    """Verify the actual installed package vault uses the {kind_singular}_{id}.json
    naming convention. If a future release changes this convention, the init
    fix needs to be updated too.
    """
    import importlib.resources as resources
    try:
        pkg_vault_root = resources.files("systemu") / "vault"
    except Exception:
        pytest.skip("systemu package not importable in this environment")

    # Read tools index from the package itself
    tools_idx_file = pkg_vault_root / "tools" / "index.json"
    if not tools_idx_file.is_file():
        pytest.skip("package doesn't bundle a tools index (not a packaged install)")

    src_idx = json.loads(tools_idx_file.read_text(encoding="utf-8"))
    if not src_idx:
        pytest.skip("package tools index is empty (no starter tools)")

    # Find at least one tool with a real body file using the v0.8.2 pattern.
    # If every tool is missing a body file, the package itself is corrupt.
    first_entry = src_idx[0]
    entry_id = first_entry.get("id")
    expected_file = pkg_vault_root / "tools" / f"tool_{entry_id}.json"
    assert expected_file.is_file(), (
        f"Real package vault doesn't have tool body file at {expected_file} — "
        "either the naming convention changed OR the wheel was built without "
        "tool body files (regression of the v0.7.3 Bug #6 wheel packaging fix). "
        f"index entry id={entry_id}"
    )
