"""install.py auto-renames SYSTEMU_AUTO_APPROVE_SCROLLS to
SYSTEMU_NON_INTERACTIVE during merge_existing_env."""
from pathlib import Path


def test_auto_migrate_deprecated_scrolls_env_var(tmp_path):
    from install import merge_existing_env

    existing_env = tmp_path / ".env"
    existing_env.write_text(
        "OPENROUTER_API_KEY=ortest\n"
        "SYSTEMU_AUTO_APPROVE_SCROLLS=true\n"
        "SOMETHING_ELSE=preserved\n",
        encoding="utf-8",
    )

    merged = merge_existing_env(existing_env, new_vars={
        "SYSTEMU_MODE": "docker-local",
    })

    assert "SYSTEMU_AUTO_APPROVE_SCROLLS" not in merged, \
        "deprecated key should be removed from the merged result"
    assert merged.get("SYSTEMU_NON_INTERACTIVE") == "true", \
        "deprecated value should be migrated to canonical key"
    assert merged["OPENROUTER_API_KEY"] == "ortest"
    assert merged["SOMETHING_ELSE"] == "preserved"
    assert merged["SYSTEMU_MODE"] == "docker-local"


def test_auto_migrate_does_not_clobber_explicit_new_key(tmp_path):
    """If the operator has set the new key explicitly, do NOT overwrite it
    with the deprecated value — explicit beats migrated."""
    from install import merge_existing_env

    existing_env = tmp_path / ".env"
    existing_env.write_text(
        "SYSTEMU_AUTO_APPROVE_SCROLLS=true\n"
        "SYSTEMU_NON_INTERACTIVE=false\n",
        encoding="utf-8",
    )

    merged = merge_existing_env(existing_env, new_vars={})
    assert "SYSTEMU_AUTO_APPROVE_SCROLLS" not in merged
    assert merged.get("SYSTEMU_NON_INTERACTIVE") == "false", \
        "explicit new-key value must survive migration"


def test_no_migration_when_old_key_absent(tmp_path):
    """If the deprecated key isn't present, nothing changes."""
    from install import merge_existing_env
    existing_env = tmp_path / ".env"
    existing_env.write_text("OPENROUTER_API_KEY=k\n", encoding="utf-8")
    merged = merge_existing_env(existing_env, new_vars={})
    assert "SYSTEMU_NON_INTERACTIVE" not in merged
    assert "SYSTEMU_AUTO_APPROVE_SCROLLS" not in merged


def test_migration_with_missing_env_file_is_noop(tmp_path):
    """When .env doesn't exist yet, merge_existing_env should return new_vars as-is."""
    from install import merge_existing_env
    missing = tmp_path / "nonexistent.env"
    merged = merge_existing_env(missing, new_vars={"SYSTEMU_MODE": "local"})
    assert merged.get("SYSTEMU_MODE") == "local"
    assert "SYSTEMU_NON_INTERACTIVE" not in merged
