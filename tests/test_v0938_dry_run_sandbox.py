"""v0.9.38 Bug 14 — forge dry-run sandbox robustness on Windows.

Two systematic failure modes that withheld otherwise-correct forged tools:
  * WinError 267 ("the directory name is invalid") — subprocess.run(cwd=vault_root)
    when vault_root did not exist. Fix: ensure the cwd exists before launching.
  * exit 9009 ("command not found") — a forged tool shelling out to bare
    ``python``/``pip`` when the interpreter dir was not on the child PATH. Fix:
    put sys.executable's dir (and Scripts) on the restricted-env PATH.
"""
from __future__ import annotations

import inspect
import os
import sys


def test_restricted_env_puts_interpreter_dir_on_path():
    from systemu.runtime.backend.local import LocalBackend
    be = LocalBackend.__new__(LocalBackend)   # _build_restricted_env needs no state
    env = be._build_restricted_env()
    py_dir = os.path.dirname(sys.executable)
    assert py_dir, "sys.executable has no dirname?"
    assert py_dir in env.get("PATH", ""), \
        "interpreter dir must be on the child PATH so bare `python` resolves (9009)"


def test_execute_ensures_cwd_exists_before_subprocess():
    # getsource guard: execute() must create vault_root BEFORE subprocess.run,
    # so a missing cwd never raises WinError 267 (a full cross-platform subprocess
    # test would itself be the flaky thing we're fixing).
    from systemu.runtime.backend import local as mod
    src = inspect.getsource(mod.LocalBackend.execute)
    i_mkdir = src.find("vault_root.mkdir")
    i_run = src.find("subprocess.run(")   # the actual call, not the comment mentions
    assert i_mkdir != -1, "execute must ensure the cwd exists (vault_root.mkdir)"
    assert i_run != -1 and i_mkdir < i_run, "mkdir must precede the subprocess.run call"
