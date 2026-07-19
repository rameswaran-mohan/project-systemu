"""E2 — Windows packaging tests (SPEC §14 E2; AC2, AC3, AC4, AC5).

NAMING: this file is NOT ``test_installer.py`` — that name is already taken by
the tests for the developer-facing ``install.py``, and ``dependency_installer``
is the pip self-heal for forged tools. Neither is E2.

Everything here drives real ``pathlib.Path`` objects and writes into a real
tmp_path. The one place a stub appears is a credential store that raises with
the secret inside its own message — that is modelling a real keyring backend's
misbehaviour, which is the thing under test.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from systemu.winpkg.layout import (
    ENV_DIRNAME,
    ENV_PREVIOUS_DIRNAME,
    ENV_STAGING_DIRNAME,
    MARKER_FILENAME,
    VAULT_DIRNAME,
    WHEELHOUSE_DIRNAME,
    InstallLayout,
    resolve_layout,
)
from systemu.winpkg.first_run import (
    HANDOFF_DETERMINISTIC_PALETTE,
    HANDOFF_T3_CONSULT,
    FirstRunResult,
    ProviderKeyReceipt,
    ProviderKeyRejected,
    decide_handoff,
    record_provider_key,
    run_first_run,
)
from systemu.winpkg.lifecycle import (
    UnsafeLayout,
    UpgradeFailed,
    perform_uninstall,
    perform_upgrade,
    vault_fingerprint,
)
from systemu.winpkg.metrics import FirstRunMetrics

ISS_PATH = Path(__file__).resolve().parent.parent / "packaging" / "windows" / "systemu.iss"


# ── helpers ─────────────────────────────────────────────────────────────────

def _real_layout(tmp_path: Path) -> InstallLayout:
    """A layout rooted in a real tmp dir, built the way resolve_layout builds
    one (so the sibling invariant is the real one, not a test-local shortcut)."""
    return resolve_layout(tmp_path)


def _seed_vault(layout: InstallLayout) -> None:
    layout.vault_dir.mkdir(parents=True, exist_ok=True)
    (layout.vault_dir / "tasks.json").write_text('{"tasks": [1, 2]}', encoding="utf-8")
    nested = layout.vault_dir / "world"
    nested.mkdir(parents=True, exist_ok=True)
    (nested / "facts.jsonl").write_text('{"fact": "x"}\n', encoding="utf-8")


def _seed_env(layout: InstallLayout, marker: str = "old") -> None:
    layout.env_dir.mkdir(parents=True, exist_ok=True)
    (layout.env_dir / "python.exe").write_text(marker, encoding="utf-8")


# ── layout ──────────────────────────────────────────────────────────────────

def test_layout_roots_under_localappdata(tmp_path):
    layout = resolve_layout(environ={"LOCALAPPDATA": str(tmp_path)})
    assert layout.root == tmp_path / "systemu"
    assert layout.is_windows_native is True


def test_explicit_local_app_data_wins_over_environ(tmp_path):
    other = tmp_path / "explicit"
    layout = resolve_layout(other, environ={"LOCALAPPDATA": str(tmp_path / "ignored")})
    assert layout.root == other / "systemu"


def test_layout_without_localappdata_is_reported_not_faked(tmp_path):
    layout = resolve_layout(environ={})
    assert layout.is_windows_native is False, (
        "a non-Windows host must be a visible fact (PAR-1), not a silent fake"
    )


def test_vault_is_a_sibling_of_every_env_dir(tmp_path):
    """The AC2/AC3 invariant. If this breaks, uninstall can eat the vault."""
    layout = _real_layout(tmp_path)
    assert layout.vault_is_outside_env() is True
    assert layout.vault_dir.parent == layout.root
    assert layout.env_dir.parent == layout.root


def test_vault_nested_inside_env_is_detected_as_unsafe(tmp_path):
    layout = _real_layout(tmp_path)
    unsafe = InstallLayout(
        root=layout.root,
        env_dir=layout.env_dir,
        env_staging_dir=layout.env_staging_dir,
        env_previous_dir=layout.env_previous_dir,
        vault_dir=layout.env_dir / "vault",      # <- nested
        wheelhouse_dir=layout.wheelhouse_dir,
        marker_file=layout.marker_file,
        uninstall_notice_file=layout.uninstall_notice_file,
        is_windows_native=True,
    )
    assert unsafe.vault_is_outside_env() is False


def test_layout_paths_are_real_paths_not_filesystem_root(tmp_path):
    """Regression guard for the ``getattr(x, "root", None) or x`` class of bug:
    ``Path`` HAS a ``.root`` attribute (the anchor), so that idiom silently
    resolves any Path to the drive root. Assert we never sit at the anchor."""
    layout = _real_layout(tmp_path)
    for value in (layout.root, layout.env_dir, layout.vault_dir):
        assert isinstance(value, Path)
        assert str(value) != value.root
        assert len(value.parts) > 1


# ── first-run wizard: DEC-8 / AC4 ───────────────────────────────────────────

class _StoreThatLeaksTheKey:
    """A backend that quotes the credential inside its own failure message.
    Real keyring backends have done this; it is why scrubbing exists."""

    def set(self, key, value):
        raise RuntimeError(f"backend refused to store value {value!r} for {key}")


class _RecordingStore:
    def __init__(self):
        self.calls = []

    def set(self, key, value):
        self.calls.append((key, value))
        return "file"


def _real_store(tmp_path, monkeypatch):
    """A REAL CredentialStore writing to a real file in tmp.

    keyring is forced off so the test exercises the on-disk fallback and never
    touches the operator's actual OS keychain.
    """
    from systemu.runtime.credentials import store as store_mod

    monkeypatch.setattr(store_mod, "usable_keyring", lambda: None)
    return store_mod.CredentialStore(base_dir=tmp_path / "vault")


def test_provider_key_is_stored_through_a_real_credential_store(tmp_path, monkeypatch):
    store = _real_store(tmp_path, monkeypatch)
    receipt = record_provider_key("sk-live-abcd1234", store=store)

    assert receipt.backend == "file"
    assert store.get("llm_api_key") == "sk-live-abcd1234"


def test_receipt_masks_the_key_and_cannot_carry_it(tmp_path, monkeypatch):
    """AC4: the wizard never renders a stored key back."""
    secret = "sk-live-abcd1234"
    store = _real_store(tmp_path, monkeypatch)
    receipt = record_provider_key(secret, store=store)

    assert receipt.masked == "<redacted:1234>"
    assert secret not in str(receipt)
    assert secret not in repr(receipt)
    # Structural: no field of the receipt holds the raw value.
    for value in vars(receipt).values():
        assert secret not in str(value)


def test_receipt_has_no_field_that_could_hold_a_raw_key():
    fields = set(ProviderKeyReceipt.__dataclass_fields__)
    assert fields == {"key_name", "masked", "backend"}, (
        "a new field on the receipt is a new place for a secret to leak"
    )


def test_blank_key_is_rejected_without_storing_anything():
    store = _RecordingStore()
    with pytest.raises(ProviderKeyRejected) as excinfo:
        record_provider_key("   ", store=store)
    assert store.calls == []
    assert "no provider key" in str(excinfo.value)


def test_a_backend_that_leaks_the_key_is_scrubbed(tmp_path):
    """The laundering guard: the secret must not survive into our error text
    just because somebody else's exception put it there."""
    secret = "sk-live-supersecret9999"
    with pytest.raises(ProviderKeyRejected) as excinfo:
        record_provider_key(secret, store=_StoreThatLeaksTheKey())

    message = str(excinfo.value)
    assert secret not in message
    assert "<redacted:9999>" in message


def test_handoff_goes_to_t3_consult_only_when_provider_is_live():
    assert decide_handoff(True) == HANDOFF_T3_CONSULT
    assert decide_handoff(False) == HANDOFF_DETERMINISTIC_PALETTE


def test_first_run_falls_back_to_palette_when_verification_fails(tmp_path, monkeypatch):
    secret = "sk-live-zzzz7777"
    store = _real_store(tmp_path, monkeypatch)

    def _verify_that_leaks():
        raise RuntimeError(f"401 unauthorized for key {secret}")

    result = run_first_run(secret, store=store, verify_provider=_verify_that_leaks)

    assert isinstance(result, FirstRunResult)
    assert result.provider_live is False
    assert result.handoff == HANDOFF_DETERMINISTIC_PALETTE
    joined = " ".join(result.notes)
    assert secret not in joined, "a failing verify must not launder the key into notes"


def test_first_run_without_a_key_never_claims_a_live_provider(tmp_path, monkeypatch):
    store = _real_store(tmp_path, monkeypatch)
    result = run_first_run(None, store=store, verify_provider=lambda: True)
    assert result.receipt is None
    assert result.provider_live is False
    assert result.handoff == HANDOFF_DETERMINISTIC_PALETTE


# ── upgrade / uninstall: AC2, AC3 ───────────────────────────────────────────

def test_upgrade_swaps_the_env_and_leaves_the_vault_byte_for_byte(tmp_path):
    layout = _real_layout(tmp_path)
    _seed_vault(layout)
    _seed_env(layout, marker="old")
    before = vault_fingerprint(layout.vault_dir)
    assert before, "the fixture must actually seed a vault"

    def _install(staging: Path):
        (staging / "python.exe").write_text("new", encoding="utf-8")

    perform_upgrade(layout, _install)

    assert (layout.env_dir / "python.exe").read_text(encoding="utf-8") == "new"
    assert vault_fingerprint(layout.vault_dir) == before
    assert not layout.env_staging_dir.exists()
    assert not layout.env_previous_dir.exists()


def test_a_failed_upgrade_rolls_back_and_touches_nothing(tmp_path):
    layout = _real_layout(tmp_path)
    _seed_vault(layout)
    _seed_env(layout, marker="old")
    before = vault_fingerprint(layout.vault_dir)

    def _install_that_fails(staging: Path):
        (staging / "half-written").write_text("junk", encoding="utf-8")
        raise RuntimeError("wheelhouse install blew up")

    with pytest.raises(UpgradeFailed):
        perform_upgrade(layout, _install_that_fails)

    assert (layout.env_dir / "python.exe").read_text(encoding="utf-8") == "old"
    assert vault_fingerprint(layout.vault_dir) == before
    assert not layout.env_staging_dir.exists(), "staging must be cleaned up"


def test_a_failed_env_swap_restores_the_previous_env(tmp_path, monkeypatch):
    """The mid-swap rollback path (step 2 of perform_upgrade).

    Fault-injected at ``Path.rename`` because that is exactly how this fails in
    the real world on Windows: the env directory is held open by antivirus or a
    still-running daemon, and the rename raises. Mutation M8 (delete the restore
    branch) survived until this test existed.
    """
    layout = _real_layout(tmp_path)
    _seed_vault(layout)
    _seed_env(layout, marker="old")
    before = vault_fingerprint(layout.vault_dir)

    real_rename = Path.rename
    calls = {"n": 0}

    def flaky_rename(self, target):
        calls["n"] += 1
        if calls["n"] == 2:          # env.new -> env, the swap itself
            raise OSError("the environment is locked by another process")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", flaky_rename)

    def _install(staging: Path):
        (staging / "python.exe").write_text("new", encoding="utf-8")

    with pytest.raises(UpgradeFailed):
        perform_upgrade(layout, _install)

    monkeypatch.undo()

    assert layout.env_dir.exists(), "the previous env must be restored"
    assert (layout.env_dir / "python.exe").read_text(encoding="utf-8") == "old"
    assert vault_fingerprint(layout.vault_dir) == before
    assert not layout.env_staging_dir.exists()


def test_uninstall_removes_the_env_and_keeps_the_vault(tmp_path):
    layout = _real_layout(tmp_path)
    _seed_vault(layout)
    _seed_env(layout)
    layout.wheelhouse_dir.mkdir(parents=True, exist_ok=True)
    (layout.wheelhouse_dir / "systemu.whl").write_text("wheel", encoding="utf-8")
    before = vault_fingerprint(layout.vault_dir)

    report = perform_uninstall(layout)

    assert not layout.env_dir.exists()
    assert not layout.wheelhouse_dir.exists()
    assert layout.vault_dir.exists()
    assert vault_fingerprint(layout.vault_dir) == before
    assert report.vault_kept == layout.vault_dir


def test_uninstall_notice_names_the_vault_path(tmp_path):
    layout = _real_layout(tmp_path)
    _seed_vault(layout)
    _seed_env(layout)

    report = perform_uninstall(layout)

    assert report.notice_file is not None and report.notice_file.exists()
    text = report.notice_file.read_text(encoding="utf-8")
    assert str(layout.vault_dir) in text
    assert "NOT deleted" in text


def test_uninstall_refuses_an_unsafe_layout_without_deleting(tmp_path):
    layout = _real_layout(tmp_path)
    _seed_env(layout)
    nested_vault = layout.env_dir / "vault"
    nested_vault.mkdir(parents=True, exist_ok=True)
    (nested_vault / "tasks.json").write_text("{}", encoding="utf-8")

    unsafe = InstallLayout(
        root=layout.root, env_dir=layout.env_dir,
        env_staging_dir=layout.env_staging_dir,
        env_previous_dir=layout.env_previous_dir,
        vault_dir=nested_vault, wheelhouse_dir=layout.wheelhouse_dir,
        marker_file=layout.marker_file,
        uninstall_notice_file=layout.uninstall_notice_file,
        is_windows_native=True,
    )

    with pytest.raises(UnsafeLayout):
        perform_uninstall(unsafe)

    assert nested_vault.exists(), "refusing must mean nothing was deleted"
    assert layout.env_dir.exists()


# ── metrics ─────────────────────────────────────────────────────────────────

def test_time_to_first_completed_task_is_computed_from_real_stamps(tmp_path):
    layout = _real_layout(tmp_path)
    metrics = FirstRunMetrics(layout.marker_file)
    t0 = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)

    metrics.stamp_installed(version="0.9.59", when=t0)
    metrics.stamp_first_task_completed(when=t0 + timedelta(minutes=11))

    assert layout.marker_file.exists()
    assert metrics.seconds_to_first_completed_task() == pytest.approx(660.0)
    assert metrics.human_summary() == "you were set up in 11 min"


def test_first_task_stamp_is_idempotent(tmp_path):
    layout = _real_layout(tmp_path)
    metrics = FirstRunMetrics(layout.marker_file)
    t0 = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
    metrics.stamp_installed(version="0.9.59", when=t0)

    assert metrics.stamp_first_task_completed(when=t0 + timedelta(minutes=5)) is True
    assert metrics.stamp_first_task_completed(when=t0 + timedelta(minutes=50)) is False
    assert metrics.seconds_to_first_completed_task() == pytest.approx(300.0)


def test_metric_is_unknown_rather_than_wrong_when_unstamped(tmp_path):
    layout = _real_layout(tmp_path)
    metrics = FirstRunMetrics(layout.marker_file)
    metrics.stamp_installed(version="0.9.59")
    assert metrics.seconds_to_first_completed_task() is None
    assert metrics.human_summary() is None


def test_clock_skew_reports_unknown_rather_than_a_negative_setup_time(tmp_path):
    """A first task stamped BEFORE the install (clock change, restored backup)
    must read as unknown. Reporting "set up in -4 min" would be worse than
    saying nothing."""
    layout = _real_layout(tmp_path)
    metrics = FirstRunMetrics(layout.marker_file)
    t0 = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
    metrics.stamp_installed(version="0.9.59", when=t0)
    metrics.stamp_first_task_completed(when=t0 - timedelta(minutes=4))

    assert metrics.seconds_to_first_completed_task() is None
    assert metrics.human_summary() is None


def test_a_corrupt_marker_never_blocks_an_install(tmp_path):
    layout = _real_layout(tmp_path)
    layout.marker_file.parent.mkdir(parents=True, exist_ok=True)
    layout.marker_file.write_text("{not json at all", encoding="utf-8")

    metrics = FirstRunMetrics(layout.marker_file)
    assert metrics.seconds_to_first_completed_task() is None
    metrics.stamp_installed(version="0.9.59")     # must not raise
    assert json.loads(layout.marker_file.read_text(encoding="utf-8"))["version"] == "0.9.59"


# ── the installer script itself ─────────────────────────────────────────────

def test_iss_and_layout_agree_on_directory_names():
    """Cross-artifact pin: the Inno script and the runtime must not drift."""
    text = ISS_PATH.read_text(encoding="utf-8")
    assert f'#define EnvDirName        "{ENV_DIRNAME}"' in text
    assert f'#define VaultDirName      "{VAULT_DIRNAME}"' in text
    assert f'#define WheelhouseDirName "{WHEELHOUSE_DIRNAME}"' in text
    assert ENV_STAGING_DIRNAME in text
    assert ENV_PREVIOUS_DIRNAME in text


def test_iss_never_deletes_the_vault_on_uninstall():
    """AC3, read straight off the installer script."""
    text = ISS_PATH.read_text(encoding="utf-8")
    block = text.split("[UninstallDelete]", 1)[1].split("[Code]", 1)[0]
    assert "{#EnvDirName}" in block
    assert "{#VaultDirName}" not in block, "the vault must never be an uninstall target"
    assert f"\\{VAULT_DIRNAME}" not in block


def _iss_pip_command() -> str:
    """The actual pip invocation line from [Run] — NOT the whole file.

    An earlier version of this test asserted ``"--no-index" in text``, which
    passed on the strength of a COMMENT mentioning --no-index. Mutation M16
    (drop --no-index from the real command) survived it. Read the command.
    """
    text = ISS_PATH.read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines()
             if "Parameters:" in ln and "pip install" in ln and not ln.strip().startswith(";")]
    assert len(lines) == 1, f"expected exactly one pip install line, found {len(lines)}"
    return lines[0]


def test_iss_installs_offline_from_the_wheelhouse():
    """AC5: a core install must not need the network."""
    command = _iss_pip_command()
    assert "--no-index" in command, (
        "the pip command itself must be offline; a comment saying so is not the install"
    )
    assert "--find-links" in command


def test_iss_does_not_use_pyinstaller():
    """The spec rejects PyInstaller onefile explicitly; the installer wraps the
    pip artifact instead."""
    text = ISS_PATH.read_text(encoding="utf-8").lower()
    assert "pyinstaller" not in text.replace("pyinstaller onefile is explicitly", "")


def test_iss_installs_per_user_without_admin():
    """PrivilegesRequired=lowest is what makes AC1 reachable on a locked-down
    machine; an admin prompt would put a UAC wall in the 15-minute path."""
    text = ISS_PATH.read_text(encoding="utf-8")
    assert "PrivilegesRequired=lowest" in text
    # Follow the define chain rather than a hardcoded literal: the install dir
    # must resolve to %LOCALAPPDATA%\systemu, which is what layout.py assumes.
    assert '#define AppName        "systemu"' in text
    assert "DefaultDirName={localappdata}\\{#AppName}" in text
