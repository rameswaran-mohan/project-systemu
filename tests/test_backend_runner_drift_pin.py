"""DRIFT PIN — every executing backend must route through the shared runner.

Kept in its own module ON PURPOSE. It reads backend source via
``inspect.getsource``, and ``tests/conftest.py`` auto-applies the
``source_sensitive`` marker to the WHOLE module of any test that does so.
Folding this into ``test_docker_backend_runner_contract.py`` would deselect
that file's behavioural pins from the edit-safe gate
(``pytest -m "not source_sensitive"``), which is exactly where they are most
useful. This one test may sit out that gate; the behavioural ones may not.

Why the pin exists: LocalBackend was fixed in W6.1 to execute tools through
``tool_runner_script.py``. DockerBackend was not converged and kept running
``python /app/tool.py`` directly, so every module-style tool (all 41 curated
vault tools) silently did nothing under ``SYSTEMU_TOOL_BACKEND=docker``. The
backends drifted apart because nothing asserted they had to agree.
"""
from __future__ import annotations

import inspect


def test_every_executing_backend_routes_through_the_runner():
    from systemu.runtime.backend import docker as docker_mod
    from systemu.runtime.backend import local as local_mod

    for mod in (docker_mod, local_mod):
        src = inspect.getsource(mod)
        assert "tool_runner_script" in src, (
            f"{mod.__name__} does not reference the shared tool runner — "
            "it has drifted off the contract"
        )


def test_stub_backends_must_adopt_the_runner_if_they_gain_an_executor():
    """ssh/wsl execute nothing today, so they are exempt — but the moment one
    of them spawns a process, it must go through the runner like the others."""
    from systemu.runtime.backend import ssh as ssh_mod
    from systemu.runtime.backend import wsl as wsl_mod

    for mod in (ssh_mod, wsl_mod):
        src = inspect.getsource(mod)
        spawns = ("subprocess.run" in src
                  or "create_subprocess" in src
                  or "Popen" in src)
        if spawns:
            assert "tool_runner_script" in src, (
                f"{mod.__name__} gained an executor but bypasses the shared "
                "runner — module-style tools would silently no-op on it, "
                "which is the exact bug DockerBackend shipped with"
            )
