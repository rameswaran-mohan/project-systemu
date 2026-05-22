"""Tests for systemu.runtime.dependency_installer.

Coverage:
  * resolve_install_mode — env > config > systemu_mode default precedence
  * package validation — accepts well-formed specs, rejects shell metachars,
    enforces a hard cap on count
  * ensure_satisfied — every InstallStatus branch with pip stubbed out
  * caching — second call for an already-installed package is a no-op
  * lock isolation — different packages don't block each other
  * approval store integration — PROMPT mode without approval records pending

pip is stubbed via monkeypatching ``_run_pip_install`` so no real network
calls / disk installs happen during the test run.
"""
from __future__ import annotations

import threading
import time

import pytest

from systemu.runtime import dependency_installer as di
from systemu.runtime.dep_approvals import DepApprovalStore


# ─────────────────────────────────────────────────────────────────────────────
# Helpers

@pytest.fixture(autouse=True)
def _reset_caches():
    di.reset_cache_for_tests()
    yield
    di.reset_cache_for_tests()


def _ok_install(packages, *, timeout):
    return di.InstallResult(
        ok=True, status=di.InstallStatus.INSTALLED, installed_now=list(packages),
    )


def _fail_install(packages, *, timeout):
    return di.InstallResult(
        ok=False, status=di.InstallStatus.FAILED,
        error=f"pip install failed for {packages}",
        pip_stderr_tail="boom",
    )


# ─────────────────────────────────────────────────────────────────────────────
# resolve_install_mode

class TestResolveInstallMode:
    def test_env_var_wins(self):
        mode = di.resolve_install_mode(
            env={"SYSTEMU_TOOL_DEP_INSTALL_MODE": "always"},
            config_mode="off",
            systemu_mode="local",
        )
        assert mode is di.InstallMode.ALWAYS

    def test_env_var_invalid_falls_through(self, caplog):
        mode = di.resolve_install_mode(
            env={"SYSTEMU_TOOL_DEP_INSTALL_MODE": "garbage"},
            config_mode="always",
            systemu_mode="local",
        )
        assert mode is di.InstallMode.ALWAYS

    def test_config_value_used_when_env_unset(self):
        mode = di.resolve_install_mode(
            env={},
            config_mode="off",
            systemu_mode="local",
        )
        assert mode is di.InstallMode.OFF

    def test_auto_defaults_local_to_prompt(self):
        mode = di.resolve_install_mode(env={}, config_mode="auto", systemu_mode="local")
        assert mode is di.InstallMode.PROMPT

    def test_auto_defaults_docker_local_to_always(self):
        mode = di.resolve_install_mode(env={}, config_mode=None, systemu_mode="docker-local")
        assert mode is di.InstallMode.ALWAYS

    def test_auto_defaults_docker_enterprise_to_off(self):
        mode = di.resolve_install_mode(env={}, config_mode=None, systemu_mode="docker-enterprise")
        assert mode is di.InstallMode.OFF

    def test_empty_config_treated_as_auto(self):
        mode = di.resolve_install_mode(env={}, config_mode="", systemu_mode="local")
        assert mode is di.InstallMode.PROMPT


# ─────────────────────────────────────────────────────────────────────────────
# Package validation

class TestPackageValidation:
    def test_accepts_simple_name(self):
        assert di._normalise_and_validate(["python-docx"]) == ["python-docx"]

    def test_accepts_version_specifier(self):
        assert di._normalise_and_validate(["requests>=2.31"]) == ["requests>=2.31"]

    def test_accepts_extras(self):
        assert di._normalise_and_validate(["uvicorn[standard]"]) == ["uvicorn[standard]"]

    def test_dedupes_and_strips_whitespace(self):
        assert di._normalise_and_validate(["  numpy ", "numpy", ""]) == ["numpy"]

    def test_rejects_shell_metachars(self):
        with pytest.raises(di.InvalidPackageSpecError):
            di._normalise_and_validate(["python-docx; rm -rf /"])

    def test_rejects_quotes(self):
        with pytest.raises(di.InvalidPackageSpecError):
            di._normalise_and_validate(['python-docx"'])

    def test_rejects_subshell(self):
        with pytest.raises(di.InvalidPackageSpecError):
            di._normalise_and_validate(["$(curl evil)"])

    def test_enforces_count_cap(self):
        many = [f"pkg{i}" for i in range(26)]
        with pytest.raises(di.InvalidPackageSpecError):
            di._normalise_and_validate(many)

    def test_rejects_non_string(self):
        with pytest.raises(di.InvalidPackageSpecError):
            di._normalise_and_validate([123])


# ─────────────────────────────────────────────────────────────────────────────
# ensure_satisfied — branches

class TestEnsureSatisfied:
    def test_empty_returns_satisfied(self):
        r = di.ensure_satisfied([], mode=di.InstallMode.ALWAYS)
        assert r.ok and r.status is di.InstallStatus.SATISFIED

    def test_auto_rejected_by_caller_contract(self):
        with pytest.raises(ValueError):
            di.ensure_satisfied(["x"], mode=di.InstallMode.AUTO)

    def test_off_mode_blocks(self):
        r = di.ensure_satisfied(["python-docx"], mode=di.InstallMode.OFF)
        assert not r.ok
        assert r.status is di.InstallStatus.BLOCKED_DISABLED

    def test_prompt_without_store_blocks_all(self):
        r = di.ensure_satisfied(["python-docx"], mode=di.InstallMode.PROMPT)
        assert not r.ok
        assert r.status is di.InstallStatus.BLOCKED_PENDING_APPROVAL
        assert r.pending_approval == ["python-docx"]

    def test_prompt_with_unapproved_blocks_records_pending(self, tmp_path):
        store = DepApprovalStore(tmp_path / "approvals.json")
        r = di.ensure_satisfied(
            ["python-docx"],
            mode=di.InstallMode.PROMPT,
            approvals=store,
            tool_name="create_word_doc",
            tool_id="tool_xyz",
        )
        assert not r.ok
        assert r.status is di.InstallStatus.BLOCKED_PENDING_APPROVAL
        pending = store.list_pending()
        assert len(pending) == 1
        assert pending[0]["package"] == "python-docx"
        assert pending[0]["first_seen_tool"] == "create_word_doc"

    def test_prompt_with_approved_installs(self, tmp_path, monkeypatch):
        store = DepApprovalStore(tmp_path / "approvals.json")
        store.approve("python-docx")
        monkeypatch.setattr(di, "_run_pip_install", _ok_install)

        r = di.ensure_satisfied(
            ["python-docx"],
            mode=di.InstallMode.PROMPT,
            approvals=store,
        )
        assert r.ok and r.status is di.InstallStatus.INSTALLED
        assert r.installed_now == ["python-docx"]

    def test_always_mode_installs_without_approval(self, monkeypatch):
        monkeypatch.setattr(di, "_run_pip_install", _ok_install)
        r = di.ensure_satisfied(["python-docx"], mode=di.InstallMode.ALWAYS)
        assert r.ok and r.status is di.InstallStatus.INSTALLED

    def test_failed_install_surfaces(self, monkeypatch):
        monkeypatch.setattr(di, "_run_pip_install", _fail_install)
        r = di.ensure_satisfied(["python-docx"], mode=di.InstallMode.ALWAYS)
        assert not r.ok
        assert r.status is di.InstallStatus.FAILED
        assert r.pip_stderr_tail == "boom"

    def test_invalid_spec_returns_failed_not_raise(self):
        r = di.ensure_satisfied(
            ["python-docx; rm -rf /"],
            mode=di.InstallMode.ALWAYS,
        )
        assert not r.ok
        assert r.status is di.InstallStatus.FAILED
        assert "invalid package spec" in (r.error or "")

    def test_second_call_hits_cache(self, monkeypatch):
        calls = []
        def stub(packages, *, timeout):
            calls.append(list(packages))
            return _ok_install(packages, timeout=timeout)
        monkeypatch.setattr(di, "_run_pip_install", stub)

        r1 = di.ensure_satisfied(["python-docx"], mode=di.InstallMode.ALWAYS)
        r2 = di.ensure_satisfied(["python-docx"], mode=di.InstallMode.ALWAYS)
        assert r1.ok and r2.ok
        # Only one actual pip call.
        assert len(calls) == 1
        assert r2.status is di.InstallStatus.SATISFIED


# ─────────────────────────────────────────────────────────────────────────────
# Concurrency

class TestConcurrency:
    def test_different_packages_dont_block(self, monkeypatch):
        """Two installs of different packages should be able to run in parallel.

        We assert non-blocking by recording the timeline: if the per-package
        locks were a single global lock, the two installs would serialise
        and total wall time ≈ 2 * sleep.  We make the install slow enough
        to detect the difference but cap it so the test is fast.
        """
        bar = threading.Barrier(2, timeout=2.0)
        def slow_install(packages, *, timeout):
            # Wait for both threads to be inside the install — if a single
            # global lock existed only one would arrive, and barrier.wait
            # would time out.
            bar.wait()
            return _ok_install(packages, timeout=timeout)
        monkeypatch.setattr(di, "_run_pip_install", slow_install)

        results: list[di.InstallResult] = [None, None]  # type: ignore[list-item]
        def worker(idx: int, pkg: str):
            results[idx] = di.ensure_satisfied([pkg], mode=di.InstallMode.ALWAYS)

        t1 = threading.Thread(target=worker, args=(0, "alpha"))
        t2 = threading.Thread(target=worker, args=(1, "beta"))
        t1.start(); t2.start()
        t1.join(timeout=3.0)
        t2.join(timeout=3.0)
        assert results[0].ok
        assert results[1].ok


# ─────────────────────────────────────────────────────────────────────────────
# DepApprovalStore basics — sanity for the fixture used above

class TestApprovalStore:
    def test_round_trip_persists(self, tmp_path):
        path = tmp_path / "approvals.json"
        s1 = DepApprovalStore(path)
        assert not s1.is_approved("foo")
        s1.approve("foo", tool_name="t1", tool_id="tool_1")
        assert s1.is_approved("foo")

        # Fresh instance reads from disk.
        s2 = DepApprovalStore(path)
        assert s2.is_approved("foo")
        approved = s2.list_approved()
        assert len(approved) == 1 and approved[0]["package"] == "foo"
        assert approved[0]["first_seen_tool"] == "t1"

    def test_approve_is_idempotent(self, tmp_path):
        s = DepApprovalStore(tmp_path / "a.json")
        assert s.approve("foo") is True
        assert s.approve("foo") is False

    def test_revoke_removes(self, tmp_path):
        s = DepApprovalStore(tmp_path / "a.json")
        s.approve("foo")
        assert s.revoke("foo") is True
        assert s.revoke("foo") is False
        assert not s.is_approved("foo")

    def test_record_pending_increments_request_count(self, tmp_path):
        s = DepApprovalStore(tmp_path / "a.json")
        s.record_pending("foo", tool_name="t1")
        s.record_pending("foo", tool_name="t1")
        s.record_pending("foo", tool_name="t1")
        pending = s.list_pending()
        assert len(pending) == 1
        assert pending[0]["package"] == "foo"
        assert pending[0]["request_count"] == 3

    def test_approving_clears_pending(self, tmp_path):
        s = DepApprovalStore(tmp_path / "a.json")
        s.record_pending("foo", tool_name="t1")
        assert len(s.list_pending()) == 1
        s.approve("foo")
        assert len(s.list_pending()) == 0
        assert s.is_approved("foo")
