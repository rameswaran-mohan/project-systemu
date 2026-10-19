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

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from systemu.winpkg.build_step import (
    BUILD_DIRNAME,
    EMBEDDABLE_BUILD_DIRNAME,
    EMBEDDABLE_SHA256,
    LAUNCHER_BUILD_DIRNAME,
    LAUNCHER_ERROR_LOG,
    LAUNCHER_EXE_NAME,
    LAUNCHER_QUIET_ENV_VAR,
    WHEELHOUSE_BUILD_DIRNAME,
    BuildInputsError,
    build_all,
    build_embeddable,
    build_inputs_for,
    build_launcher,
    download_embeddable,
    embeddable_pth_text,
    find_csc,
    launcher_compile_command,
    render_launcher_source,
    rewrite_embeddable_pth,
    verify_build_inputs,
)
from systemu.winpkg.launcher import (
    ACTION_HOTKEY,
    ACTION_OPEN,
    ACTION_STATUS,
    EXIT_NOT_BUILT,
    INSTALL_ROOT_ENV_VAR,
    VAULT_DIR_ENV_VAR,
    hotkey_host_status,
    launch,
    point_runtime_at_the_sibling_vault,
    resolve_install_layout,
)
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


# ── the build step: does anything actually PRODUCE the [Files] sources? ──────

def _iss_file_sources() -> list:
    """The real ``Source:`` values from ``[Files]`` — comments excluded.

    Reading the section rather than the whole file, for the same reason
    :func:`_iss_pip_command` does: a comment naming a path is not a Source line.
    """
    text = ISS_PATH.read_text(encoding="utf-8")
    block = text.split("[Files]", 1)[1].split("[Icons]", 1)[0]
    sources = []
    for line in block.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(";"):
            continue
        match = re.search(r'Source:\s*"([^"]+)"', stripped)
        if match:
            sources.append(match.group(1))
    return sources


def test_every_iss_build_input_is_produced_by_the_build_step(tmp_path):
    """The gap this packet closed: ``[Files]`` read three trees that nothing in
    the repo created, so ISCC aborted at the first Source line.

    Resolve each Source the way Inno does — relative to the script's own
    directory — and require it to be a path :mod:`systemu.winpkg.build_step`
    builds. A new Source with no producer fails here.
    """
    text = ISS_PATH.read_text(encoding="utf-8")
    exe_name = re.search(r'#define\s+AppExeName\s+"([^"]+)"', text).group(1)
    assert exe_name == LAUNCHER_EXE_NAME

    inputs = build_inputs_for(ISS_PATH.parent)
    produced = {
        inputs.embeddable_dir: "embeddable",
        inputs.wheelhouse_dir: "wheelhouse",
        inputs.launcher_exe: "launcher exe",
    }

    sources = _iss_file_sources()
    assert len(sources) == 3, f"expected three build inputs, found {sources}"

    resolved = []
    for source in sources:
        concrete = source.replace("{#AppExeName}", exe_name).rstrip("\\*")
        resolved.append((ISS_PATH.parent / concrete).resolve())

    assert resolved == list(produced), (
        "an [Files] Source has no producer in systemu.winpkg.build_step; "
        f"script wants {resolved}, the build step makes {list(produced)}"
    )
    # And the literal segment names, so a rename on either side is caught.
    assert sources[0] == f"{BUILD_DIRNAME}\\{EMBEDDABLE_BUILD_DIRNAME}\\*"
    assert sources[1] == f"{BUILD_DIRNAME}\\{WHEELHOUSE_BUILD_DIRNAME}\\*"
    assert sources[2] == f"{BUILD_DIRNAME}\\{LAUNCHER_BUILD_DIRNAME}\\{{#AppExeName}}"


def _valid_build_tree(tmp_path: Path):
    """A structurally complete build tree, built from real files on disk.

    Each verification test breaks exactly ONE thing in it, so a check that
    stopped looking at that thing shows up as a test that no longer fails.
    """
    inputs = build_inputs_for(tmp_path)
    env_dir = inputs.embeddable_dir
    env_dir.mkdir(parents=True)
    (env_dir / "python.exe").write_bytes(b"MZ\x90\x00")
    (env_dir / "pythonw.exe").write_bytes(b"MZ\x90\x00")
    (env_dir / "python312._pth").write_text(embeddable_pth_text("3.12.10"),
                                            encoding="ascii")
    pip_pkg = env_dir / "Lib" / "site-packages" / "pip"
    pip_pkg.mkdir(parents=True)
    (pip_pkg / "__init__.py").write_text("__version__ = '26.1.2'\n", encoding="utf-8")

    inputs.wheelhouse_dir.mkdir(parents=True)
    (inputs.wheelhouse_dir / "systemu-0.10.21-py3-none-any.whl").write_bytes(b"PK\x03\x04")
    (inputs.wheelhouse_dir / "click-8.1.7-py3-none-any.whl").write_bytes(b"PK\x03\x04")

    inputs.launcher_dir.mkdir(parents=True)
    inputs.launcher_exe.write_bytes(b"MZ\x90\x00")
    return inputs


def test_a_complete_build_tree_verifies(tmp_path):
    summary = verify_build_inputs(_valid_build_tree(tmp_path))
    assert summary["wheels"] == 2
    assert summary["launcher"].endswith(LAUNCHER_EXE_NAME)


def test_an_embeddable_without_pip_is_refused(tmp_path):
    """The embeddable distribution has no pip and no ensurepip. ``[Run]`` step 1
    is ``env\\python.exe -m pip install``; shipping without pip vendored in makes
    that step die on the target machine, and get-pip.py would need the network
    AC5 forbids."""
    inputs = _valid_build_tree(tmp_path)
    (inputs.embeddable_dir / "Lib" / "site-packages" / "pip" / "__init__.py").unlink()

    with pytest.raises(BuildInputsError) as excinfo:
        verify_build_inputs(inputs)
    assert "pip is not vendored" in str(excinfo.value)


def test_a_pth_that_only_MENTIONS_import_site_is_refused(tmp_path):
    """``#import site`` is what the archive ships — the line is present but
    commented out, so site never loads and ``python -m pip`` cannot work. A
    substring check for "import site" passes on it; this must not."""
    inputs = _valid_build_tree(tmp_path)
    (inputs.embeddable_dir / "python312._pth").write_text(
        "python312.zip\n.\nLib\\site-packages\n# Uncomment to run site.main()\n"
        "#import site\n",
        encoding="ascii",
    )

    with pytest.raises(BuildInputsError) as excinfo:
        verify_build_inputs(inputs)
    assert "does not enable site" in str(excinfo.value)


def test_a_pth_without_site_packages_on_the_path_is_refused(tmp_path):
    inputs = _valid_build_tree(tmp_path)
    (inputs.embeddable_dir / "python312._pth").write_text(
        "python312.zip\n.\nimport site\n", encoding="ascii")

    with pytest.raises(BuildInputsError) as excinfo:
        verify_build_inputs(inputs)
    assert "Lib\\site-packages" in str(excinfo.value)


def test_an_sdist_in_the_wheelhouse_is_refused(tmp_path):
    """``pip install --no-index`` accepts an sdist and then tries to BUILD it,
    which needs a build backend and, in practice, the network. AC5 is wheels or
    it is nothing."""
    inputs = _valid_build_tree(tmp_path)
    (inputs.wheelhouse_dir / "some-dep-1.0.tar.gz").write_bytes(b"\x1f\x8b")

    with pytest.raises(BuildInputsError) as excinfo:
        verify_build_inputs(inputs)
    assert "some-dep-1.0.tar.gz" in str(excinfo.value)


def test_a_wheelhouse_holding_only_the_systemu_wheel_is_refused(tmp_path):
    inputs = _valid_build_tree(tmp_path)
    (inputs.wheelhouse_dir / "click-8.1.7-py3-none-any.whl").unlink()

    with pytest.raises(BuildInputsError) as excinfo:
        verify_build_inputs(inputs)
    assert "dependencies are missing" in str(excinfo.value)


def test_a_launcher_that_is_not_a_windows_executable_is_refused(tmp_path):
    inputs = _valid_build_tree(tmp_path)
    inputs.launcher_exe.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")

    with pytest.raises(BuildInputsError) as excinfo:
        verify_build_inputs(inputs)
    assert "not a Windows executable" in str(excinfo.value)


def test_building_only_one_stage_still_verifies_all_three(tmp_path):
    """``--only`` narrows what is REBUILT, never what is checked. A partial
    build that leaves a tree missing must not exit 0."""
    inputs = build_inputs_for(tmp_path)
    inputs.build_dir.mkdir(parents=True)

    def _make_launcher():
        inputs.launcher_dir.mkdir(parents=True, exist_ok=True)
        inputs.launcher_exe.write_bytes(b"MZ\x90\x00")

    with pytest.raises(BuildInputsError) as excinfo:
        build_all(inputs, project_root=tmp_path, only=["launcher"],
                  builders={"launcher": _make_launcher})

    assert inputs.launcher_exe.is_file(), "the selected stage must still run"
    assert "embeddable" in str(excinfo.value) or "wheelhouse" in str(excinfo.value)


def test_an_unknown_build_stage_is_refused(tmp_path):
    inputs = build_inputs_for(tmp_path)
    with pytest.raises(BuildInputsError) as excinfo:
        build_all(inputs, project_root=tmp_path, only=["lancher"], builders={})
    assert "lancher" in str(excinfo.value)


# ── the embeddable runtime is fetched under a pinned digest ─────────────────

def test_an_unpinned_python_version_is_never_fetched():
    """The build step is upstream of every installed byte. An unknown version
    must not be downloaded 'and then checked' — it must not be downloaded."""
    calls = []

    def _fetch(url):
        calls.append(url)
        return b"whatever"

    with pytest.raises(BuildInputsError) as excinfo:
        download_embeddable("3.13.99", fetch=_fetch)

    assert calls == [], "an unpinned version must not reach the network at all"
    assert "no pinned sha256" in str(excinfo.value)


def test_a_tampered_embeddable_archive_is_refused():
    version = next(iter(EMBEDDABLE_SHA256))
    with pytest.raises(BuildInputsError) as excinfo:
        download_embeddable(version, fetch=lambda url: b"not the real archive")
    assert "digest mismatch" in str(excinfo.value)


def test_a_matching_archive_is_returned_unchanged():
    import hashlib

    payload = b"pretend this is CPython"
    digest = hashlib.sha256(payload).hexdigest()
    assert download_embeddable("9.9.9", sha256=digest,
                              fetch=lambda url: payload) == payload


def test_rewriting_the_pth_turns_a_commented_import_site_into_a_live_one(tmp_path):
    env_dir = tmp_path / "embeddable"
    env_dir.mkdir()
    shipped = env_dir / "python312._pth"
    shipped.write_text(
        "python312.zip\r\n.\r\n\r\n# Uncomment to run site.main() automatically\r\n"
        "#import site\r\n",
        encoding="ascii",
    )

    rewrite_embeddable_pth(env_dir, version="3.12.10")

    lines = [ln.split("#", 1)[0].strip()
             for ln in shipped.read_text(encoding="ascii").splitlines()]
    assert "import site" in lines
    assert "Lib\\site-packages" in lines
    assert "python312.zip" in lines


def _archive_shaped_like_the_python_org_embeddable() -> bytes:
    """The real archive's shape: a windowed + console interpreter and a ``._pth``
    with ``import site`` COMMENTED OUT, which is what python.org ships."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("python.exe", b"MZ\x90\x00")
        archive.writestr("pythonw.exe", b"MZ\x90\x00")
        archive.writestr("python312.zip", b"PK\x03\x04")
        archive.writestr(
            "python312._pth",
            b"python312.zip\r\n.\r\n\r\n# Uncomment to run site.main() "
            b"automatically\r\n#import site\r\n",
        )
    return buffer.getvalue()


def test_build_embeddable_produces_a_runtime_the_verifier_accepts(tmp_path):
    """The whole embeddable stage, offline: extract, fix the ._pth, vendor pip.

    Drop any one of those and the tree that comes out is one ``[Run]`` step 1
    cannot use — which is invisible until someone installs it.
    """
    inputs = _valid_build_tree(tmp_path)
    shutil.rmtree(inputs.embeddable_dir)

    payload = _archive_shaped_like_the_python_org_embeddable()
    pip_wheel = tmp_path / "pip-26.1.2-py3-none-any.whl"
    with zipfile.ZipFile(pip_wheel, "w") as wheel:
        wheel.writestr("pip/__init__.py", "__version__ = '26.1.2'\n")
        wheel.writestr("pip-26.1.2.dist-info/METADATA", "Name: pip\n")

    fetched = []
    build_embeddable(
        inputs,
        version="3.12.10",
        sha256=hashlib.sha256(payload).hexdigest(),
        fetch=lambda url: fetched.append(url) or payload,
        pip_wheels=[pip_wheel],
    )

    assert fetched == [
        "https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip"
    ]
    assert (inputs.embeddable_dir / "Lib" / "site-packages" / "pip" /
            "__init__.py").is_file()
    live = [ln.split("#", 1)[0].strip() for ln in
            (inputs.embeddable_dir / "python312._pth").read_text(encoding="ascii").splitlines()]
    assert "import site" in live

    # The same verification the real build runs must accept it.
    assert verify_build_inputs(inputs)["wheels"] == 2


def test_rewriting_the_pth_refuses_an_ambiguous_runtime(tmp_path):
    env_dir = tmp_path / "embeddable"
    env_dir.mkdir()
    (env_dir / "python312._pth").write_text("python312.zip\n", encoding="ascii")
    (env_dir / "python311._pth").write_text("python311.zip\n", encoding="ascii")

    with pytest.raises(BuildInputsError):
        rewrite_embeddable_pth(env_dir, version="3.12.10")


# ── the launcher stub ────────────────────────────────────────────────────────

def test_the_launcher_is_compiled_as_a_windowless_binary():
    """Read the real command, not a comment: a console-subsystem launcher
    flashes a black window on every Start-Menu click."""
    command = launcher_compile_command(Path("csc.exe"), Path("a.cs"), Path("b.exe"))
    assert "-target:winexe" in command
    assert command[-1] == "a.cs"
    assert "-out:b.exe" in command


def test_the_generated_stub_exports_the_install_root_variable_the_launcher_reads():
    """Cross-module pin: the stub sets it, launcher.py reads it, and the name
    lives in exactly one place."""
    source = render_launcher_source()
    assert f'const string InstallRootEnvVar = "{INSTALL_ROOT_ENV_VAR}";' in source
    assert f'start.EnvironmentVariables[InstallRootEnvVar] = dir;' in source
    # Structural only — the behavioural proof is the compiled-launcher test below.
    assert "Process.GetCurrentProcess()" in source
    assert "MainModule.FileName" in source


_CSC = find_csc()


@pytest.mark.skipif(sys.platform != "win32" or _CSC is None,
                    reason="needs Windows and the .NET Framework C# compiler")
def test_the_compiled_launcher_finds_its_interpreter_beside_itself_not_in_the_cwd(tmp_path):
    """The measured reason this is a compiled stub and not a distlib .exe shim.

    A distlib launcher resolves a RELATIVE shebang through ``SearchPath()``,
    which searches the current directory and PATH. Given ``#!env\\python.exe``
    it ran an ``env\\python.exe`` that happened to sit in the CWD — a silently
    wrong interpreter. Here the CWD holds exactly that decoy and must be
    ignored.
    """
    inputs = build_inputs_for(tmp_path)
    inputs.build_dir.mkdir(parents=True)
    exe = build_launcher(inputs)

    decoy = tmp_path / "decoy"
    (decoy / ENV_DIRNAME).mkdir(parents=True)
    (decoy / ENV_DIRNAME / "pythonw.exe").write_bytes(b"")

    environ = dict(os.environ)
    environ[LAUNCHER_QUIET_ENV_VAR] = "1"
    completed = subprocess.run([str(exe)], cwd=str(decoy), env=environ,
                               capture_output=True, timeout=180)

    assert completed.returncode != 0, (
        "there is no interpreter beside the launcher; it must refuse rather "
        "than run whatever the CWD offers"
    )

    log = inputs.launcher_dir / LAUNCHER_ERROR_LOG
    assert log.is_file(), "the launcher must log beside ITSELF, not in the CWD"
    assert not (decoy / LAUNCHER_ERROR_LOG).exists()
    text = log.read_text(encoding="utf-8")
    assert str(inputs.launcher_dir / ENV_DIRNAME / "pythonw.exe") in text
    assert str(decoy) not in text


# ── the launcher's own decisions ─────────────────────────────────────────────

def test_launcher_prefers_the_install_root_the_stub_exported(tmp_path):
    exported = tmp_path / "from-the-stub"
    layout = resolve_install_layout(
        environ={INSTALL_ROOT_ENV_VAR: str(exported),
                 "LOCALAPPDATA": str(tmp_path / "ignored")},
        prefix=str(tmp_path / "also-ignored"),
    )
    assert layout.root == exported
    assert layout.env_dir == exported / ENV_DIRNAME


def test_launcher_falls_back_to_the_embedded_prefix_only_when_it_IS_an_env(tmp_path):
    inside = resolve_install_layout(environ={}, prefix=str(tmp_path / "app" / ENV_DIRNAME))
    assert inside.root == tmp_path / "app"

    # A dev virtualenv is not an install root; guessing from it would put the
    # vault somewhere arbitrary.
    elsewhere = resolve_install_layout(
        environ={"LOCALAPPDATA": str(tmp_path / "lad")},
        prefix=str(tmp_path / "some" / ".venv"),
    )
    assert elsewhere.root == tmp_path / "lad" / "systemu"


def test_launcher_points_the_runtime_at_the_SIBLING_vault(tmp_path):
    """AC2's runtime half. ``Config``'s default vault_dir is the RELATIVE
    ``systemu/vault``, which for a wheel install resolves inside
    ``env\\Lib\\site-packages`` — an env swap on upgrade would then legitimately
    delete the operator's data, and lifecycle.py could do nothing about it.
    """
    layout = resolve_layout(tmp_path)
    environ: dict = {}

    point_runtime_at_the_sibling_vault(layout, environ=environ)

    resolved = Path(environ[VAULT_DIR_ENV_VAR])
    assert resolved == layout.vault_dir
    for env_path in (layout.env_dir, layout.env_staging_dir, layout.env_previous_dir):
        with pytest.raises(ValueError):
            resolved.relative_to(env_path)


def test_an_operator_vault_override_is_not_stomped(tmp_path):
    layout = resolve_layout(tmp_path)
    environ = {VAULT_DIR_ENV_VAR: str(tmp_path / "somewhere-else")}
    assert point_runtime_at_the_sibling_vault(layout, environ=environ) == \
        str(tmp_path / "somewhere-else")


def test_hotkey_is_reported_absent_and_never_silently_substituted(tmp_path):
    """R-P7 is not built. ``--hotkey`` must fail loudly rather than open the
    dashboard and look like it worked."""
    layout = resolve_layout(tmp_path)
    started = []
    opened = []

    result = launch(
        ACTION_HOTKEY,
        layout=layout,
        environ={},
        daemon_status=lambda vault: started.append(("status", vault)),
        start_daemon=lambda vault, port: started.append(("start", vault)),
        open_url=lambda url: opened.append(url) or True,
    )

    assert result.exit_code == EXIT_NOT_BUILT
    assert started == [], "nothing may be started for a capability that does not exist"
    assert opened == [], "a hotkey request must not turn into a dashboard open"
    assert result.dashboard_url is None
    assert hotkey_host_status()["available"] is False


def test_open_starts_the_daemon_only_when_it_is_down(tmp_path):
    layout = resolve_layout(tmp_path)

    starts = []
    opened = []
    result = launch(
        ACTION_OPEN, layout=layout, environ={}, port=8765,
        daemon_status=lambda vault: False,
        start_daemon=lambda vault, port: starts.append((vault, port)),
        open_url=lambda url: opened.append(url) or True,
    )
    assert result.daemon_started is True
    assert starts == [(str(layout.vault_dir), 8765)]
    assert opened == ["http://127.0.0.1:8765"]
    assert result.exit_code == 0

    starts.clear()
    again = launch(
        ACTION_OPEN, layout=layout, environ={},
        daemon_status=lambda vault: True,
        start_daemon=lambda vault, port: starts.append((vault, port)),
        open_url=lambda url: True,
    )
    assert again.daemon_started is False
    assert starts == [], "a second launch must not start a second daemon"


def test_a_browser_that_refuses_to_open_is_said_out_loud(tmp_path):
    layout = resolve_layout(tmp_path)
    result = launch(
        ACTION_OPEN, layout=layout, environ={},
        daemon_status=lambda vault: True,
        start_daemon=lambda vault, port: None,
        open_url=lambda url: False,
    )
    assert result.opened_dashboard is False
    assert any("127.0.0.1:8765" in note for note in result.notes)


def test_status_reports_the_layout_without_starting_anything(tmp_path):
    layout = resolve_layout(tmp_path)
    touched = []
    result = launch(
        ACTION_STATUS, layout=layout, environ={},
        daemon_status=lambda vault: touched.append(vault),
        start_daemon=lambda vault, port: touched.append(vault),
        open_url=lambda url: touched.append(url) or True,
    )
    assert touched == []
    assert result.exit_code == 0
    joined = " ".join(result.notes)
    assert str(layout.vault_dir) in joined
    assert "R-P7 is not built" in joined
