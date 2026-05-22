"""Tests for the v0.3.5 strict-mode interpreter check.

Pins the contract:
  * Match → returns silently regardless of strict flag.
  * Mismatch + strict OFF → falls back to assert_or_warn (no exit).
  * Mismatch + strict ON  → calls exit_fn(1).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from systemu.runtime import interpreter_check as ic


def _write_lock(data_dir: Path, interpreter: str, recorded_by: str = "daemon"):
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "runtime.lock").write_text(
        json.dumps({
            "version":      1,
            "interpreter":  interpreter,
            "recorded_by":  recorded_by,
            "recorded_pid": 12345,
            "recorded_at":  "2026-05-13T00:00:00+00:00",
        }),
        encoding="utf-8",
    )


def test_match_returns_silently(tmp_path, monkeypatch):
    _write_lock(tmp_path, sys.executable)
    monkeypatch.setenv("SYSTEMU_STRICT_INTERPRETER", "1")
    exit_calls = []
    result = ic.assert_or_fail(tmp_path, recorded_by="worker",
                               exit_fn=lambda code: exit_calls.append(code))
    assert result.matches
    assert exit_calls == []


def test_mismatch_strict_off_warns_only(tmp_path, monkeypatch):
    _write_lock(tmp_path, "C:\\fake\\other\\python.exe")
    monkeypatch.delenv("SYSTEMU_STRICT_INTERPRETER", raising=False)
    exit_calls = []
    result = ic.assert_or_fail(tmp_path, recorded_by="worker",
                               exit_fn=lambda code: exit_calls.append(code))
    assert not result.matches
    # Strict off → fall back to warn → no exit
    assert exit_calls == []


def test_mismatch_strict_on_exits(tmp_path, monkeypatch, capsys):
    _write_lock(tmp_path, "C:\\fake\\other\\python.exe")
    monkeypatch.setenv("SYSTEMU_STRICT_INTERPRETER", "1")
    exit_calls = []
    result = ic.assert_or_fail(tmp_path, recorded_by="worker",
                               exit_fn=lambda code: exit_calls.append(code))
    assert not result.matches
    assert exit_calls == [1]
    captured = capsys.readouterr()
    assert "FATAL" in captured.err
    assert "SYSTEMU_STRICT_INTERPRETER" in captured.err


def test_strict_env_truthy_variants(tmp_path, monkeypatch):
    _write_lock(tmp_path, "C:\\fake\\other\\python.exe")
    for val in ("1", "true", "yes", "TRUE", "YES"):
        monkeypatch.setenv("SYSTEMU_STRICT_INTERPRETER", val)
        exit_calls = []
        ic.assert_or_fail(tmp_path, recorded_by="worker",
                          exit_fn=lambda code: exit_calls.append(code))
        assert exit_calls == [1], f"value {val!r} should be truthy"


def test_strict_env_falsy_variants(tmp_path, monkeypatch):
    _write_lock(tmp_path, "C:\\fake\\other\\python.exe")
    for val in ("0", "false", "no", "FALSE", "", "garbage"):
        monkeypatch.setenv("SYSTEMU_STRICT_INTERPRETER", val)
        exit_calls = []
        ic.assert_or_fail(tmp_path, recorded_by="worker",
                          exit_fn=lambda code: exit_calls.append(code))
        assert exit_calls == [], f"value {val!r} should NOT be strict"


def test_record_then_check_matches(tmp_path):
    ic.record_interpreter(tmp_path, recorded_by="daemon")
    result = ic.check_interpreter(tmp_path)
    assert result.matches
    assert result.recorded_by == "daemon"
