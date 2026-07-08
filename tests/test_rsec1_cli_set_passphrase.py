"""R-SEC1 — `systemu doctor --set-passphrase` CLI.

Operators need a non-interactive way to set the dashboard passphrase (and to
print the ``SYSTEMU_DASHBOARD_PASSPHRASE_HASH=<hash>`` env line for Docker /
headless deployers). These tests drive the top-level ``doctor`` command via
Click's ``CliRunner`` with a ``--vault`` pointing at ``tmp_path``.
"""
from __future__ import annotations

from click.testing import CliRunner

from sharing_on.cli import doctor
from systemu.runtime import dashboard_auth as da


def test_set_passphrase_configures_vault(tmp_path):
    runner = CliRunner()
    result = runner.invoke(
        doctor,
        ["--set-passphrase", "--vault", str(tmp_path), "--passphrase", "hunter2"],
    )
    assert result.exit_code == 0, result.output

    assert da.is_configured_vault(tmp_path) is True
    stored = da.get_passphrase_hash_vault(tmp_path)
    assert stored is not None
    assert da.verify("hunter2", stored) is True
    # never verifies against a wrong passphrase
    assert da.verify("wrong", stored) is False


def test_set_passphrase_prints_env_hash_line(tmp_path):
    runner = CliRunner()
    result = runner.invoke(
        doctor,
        ["--set-passphrase", "--vault", str(tmp_path), "--passphrase", "hunter2"],
    )
    assert result.exit_code == 0, result.output

    assert "SYSTEMU_DASHBOARD_PASSPHRASE_HASH=" in result.output
    # the printed hash is the real stored scrypt hash
    stored = da.get_passphrase_hash_vault(tmp_path)
    assert stored.startswith("scrypt$")
    assert f"SYSTEMU_DASHBOARD_PASSPHRASE_HASH={stored}" in result.output
    # the raw passphrase must NEVER be printed
    assert "hunter2" not in result.output


def test_set_passphrase_reads_from_stdin_when_no_arg(tmp_path):
    runner = CliRunner()
    result = runner.invoke(
        doctor,
        ["--set-passphrase", "--vault", str(tmp_path)],
        input="hunter2\n",
    )
    assert result.exit_code == 0, result.output
    assert da.verify("hunter2", da.get_passphrase_hash_vault(tmp_path)) is True


def test_set_passphrase_empty_errors_without_storing(tmp_path):
    runner = CliRunner()
    result = runner.invoke(
        doctor,
        ["--set-passphrase", "--vault", str(tmp_path), "--passphrase", ""],
    )
    assert result.exit_code != 0
    assert da.is_configured_vault(tmp_path) is False
