"""DockerBackend must honour the SAME tool-runner contract as LocalBackend.

Field shape this pins (the W6 bug, re-opened on the Docker backend):

W6.1 fixed LocalBackend to execute tools through ``tool_runner_script.py`` —
the runner imports the implementation module and calls ``run(**params)``.
DockerBackend was never converged: it ran ``python /app/tool.py`` directly.
All 41 curated vault tools are module-style (``TOOL_META`` + ``run()``, no
``__main__`` block), so on the Docker backend every one of them defined its
functions and exited — exit 0, empty stdout, **no effect**.

Two distinct harms, both pinned here:

  * **Silent no-op.** The tool's ``run()`` never fires, so the operator's
    actual request does not happen.
  * **False pass.** ``_parse_execution_stdout``'s W6.2 guard (empty stdout +
    exit 0 = failure) catches the *common* shape, but it is defeated by any
    module-level stdout: a tool that prints a banner at import time gets
    ``success=True`` with ``run()`` never called and no effect produced.

These tests emulate the container rather than requiring a Docker daemon: the
fake ``docker run`` reads the backend's OWN ``-v host:container`` mount
arguments to build the container filesystem view, then executes the exact
``sh -c`` command the backend composed. A container path referenced by the
command but never mounted is therefore a test failure, not a silent pass.
"""
from __future__ import annotations

import asyncio
import json
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from systemu.runtime.backend.docker import DockerBackend
from systemu.runtime.backend.local import LocalBackend


# ── container emulator ───────────────────────────────────────────────────────

class _FakeProc:
    def __init__(self, out: bytes, err: bytes, rc: int) -> None:
        self._out, self._err, self.returncode = out, err, rc

    async def communicate(self):
        return self._out, self._err


def _parse_mount(spec: str) -> tuple[str, str]:
    """``host:container[:ro]`` -> (host, container), Windows drive-letter aware."""
    parts = spec.split(":")
    if len(parts) >= 3 and len(parts[0]) == 1 and parts[0].isalpha():
        return parts[0] + ":" + parts[1], parts[2]
    return parts[0], parts[1]


def _install_fake_docker(monkeypatch) -> dict:
    """Patch asyncio.create_subprocess_exec to emulate `docker run`.

    Returns a recorder dict populated on each call.
    """
    rec: dict = {}

    async def _fake(*cmd, stdout=None, stderr=None, **kwargs):
        rec["argv"] = list(cmd)
        assert cmd[0] == "docker", f"expected a docker invocation, got {cmd[0]!r}"

        mounts: dict[str, str] = {}
        for i, arg in enumerate(cmd):
            if arg == "-v" and i + 1 < len(cmd):
                host, container = _parse_mount(cmd[i + 1])
                mounts[container] = host
        rec["mounts"] = mounts

        shell_cmd = cmd[-1]
        rec["shell_cmd"] = shell_cmd
        # Only the final segment actually runs the tool (`pip install ... && python ...`)
        tool_cmd = shell_cmd.split("&&")[-1].strip()
        argv = shlex.split(tool_cmd, posix=True)
        rec["container_argv"] = list(argv)

        # Map container paths -> host paths using the backend's OWN mounts.
        unmounted: list[str] = []
        mapped: list[str] = []
        for arg in argv:
            if arg in mounts:
                mapped.append(mounts[arg])
            elif arg.startswith("/app/"):
                # referenced inside the container but never mounted in
                unmounted.append(arg)
                mapped.append(arg)
            else:
                mapped.append(arg)
        rec["unmounted_refs"] = unmounted
        if unmounted:
            # Exactly what the real container would do: python can't open it.
            msg = f"python: can't open file {unmounted[0]!r}: No such file or directory"
            return _FakeProc(b"", msg.encode(), 2)

        if mapped and mapped[0] == "python":
            mapped[0] = sys.executable
        completed = subprocess.run(mapped, capture_output=True)
        return _FakeProc(completed.stdout, completed.stderr, completed.returncode)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake)
    return rec


def _run_docker(impl: Path, params: dict, monkeypatch, *, vault_root: Path):
    rec = _install_fake_docker(monkeypatch)
    backend = DockerBackend(vault_root=vault_root)
    result = asyncio.run(backend.execute(impl, json.dumps(params), timeout=60))
    return result, rec


def _write(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


# A module-style tool whose EFFECT is observable: it writes a marker file.
_EFFECT_TOOL = """\
TOOL_META = {'name': 'effect_tool'}

def run(**kwargs):
    import pathlib
    pathlib.Path(kwargs['out']).write_text('THE EFFECT HAPPENED', encoding='utf-8')
    return {'success': True, 'data': {'wrote': kwargs['out']}, 'error': None}
"""


# ── the silent no-op ─────────────────────────────────────────────────────────

class TestModuleStyleToolActuallyRuns:
    def test_module_style_tool_produces_its_effect(self, tmp_path, monkeypatch):
        """THE bug: run() never fired on the Docker backend, so nothing happened."""
        impl = _write(tmp_path, "effect_tool.py", _EFFECT_TOOL)
        marker = tmp_path / "effect.txt"

        res, _ = _run_docker(impl, {"out": str(marker)}, monkeypatch,
                             vault_root=tmp_path)

        assert marker.exists(), (
            "module-style tool did NOT produce its effect through the Docker "
            f"backend — run() was never called (error={res.error!r})"
        )
        assert marker.read_text(encoding="utf-8") == "THE EFFECT HAPPENED"
        assert res.success, res.error
        assert res.parsed.get("data") == {"wrote": str(marker)}

    def test_docker_matches_local_on_the_same_tool(self, tmp_path, monkeypatch):
        """Convergence: the two backends must agree on a module-style tool."""
        impl = _write(tmp_path, "effect_tool.py", _EFFECT_TOOL)
        d_marker = tmp_path / "d.txt"
        l_marker = tmp_path / "l.txt"

        d_res, _ = _run_docker(impl, {"out": str(d_marker)}, monkeypatch,
                               vault_root=tmp_path)
        # LocalBackend must NOT see the patched create_subprocess_exec; it uses
        # subprocess.run in a thread, so it is unaffected either way.
        l_res = asyncio.run(LocalBackend(vault_root=tmp_path).execute(
            impl, json.dumps({"out": str(l_marker)}), timeout=60))

        assert l_marker.exists(), "positive control: LocalBackend must produce the effect"
        assert d_marker.exists(), "DockerBackend diverged from LocalBackend"
        assert d_res.success == l_res.success is True
        assert d_res.parsed.get("data", {}).keys() == l_res.parsed.get("data", {}).keys()


# ── the false pass ───────────────────────────────────────────────────────────

class TestNoFalsePass:
    def test_module_level_output_cannot_mask_an_uncalled_run(self, tmp_path, monkeypatch):
        """The reachable FALSE PASS.

        A module-style tool that also prints at import time defeats the W6.2
        empty-stdout guard: pre-fix this returned success=True with run()
        never called and no effect on disk.
        """
        impl = _write(tmp_path, "banner_tool.py", (
            "import json\n"
            "print(json.dumps({'success': True, 'note': 'module-level banner'}))\n"
            "TOOL_META = {'name': 'banner_tool'}\n"
            "def run(**kwargs):\n"
            "    import pathlib\n"
            "    pathlib.Path(kwargs['out']).write_text('REAL', encoding='utf-8')\n"
            "    return {'success': True, 'data': {'real': True}}\n"
        ))
        marker = tmp_path / "real.txt"

        res, _ = _run_docker(impl, {"out": str(marker)}, monkeypatch,
                             vault_root=tmp_path)

        # Either the body ran, or we failed — never "success" with no effect.
        assert not (res.success and not marker.exists()), (
            "FALSE PASS: reported success=True while run() never executed "
            f"(parsed={res.parsed!r})"
        )
        assert marker.exists(), "run() must be invoked, not shadowed by the banner"
        assert res.parsed.get("data") == {"real": True}, (
            "the module-level banner was parsed as the result instead of run()'s "
            f"return value: {res.parsed!r}"
        )

    def test_body_that_cannot_run_is_reported_as_failure(self, tmp_path, monkeypatch):
        """ANTI-FALSE-PASS PIN.

        A module that defines neither a callable ``run`` nor produces output
        cannot have done anything. That MUST be a failure, and the error must
        say the body did not run — not a generic 'no output'.

        Mutation target: the no-run guard in tool_runner_script.main().
        """
        impl = _write(tmp_path, "inert_tool.py",
                      "TOOL_META = {'name': 'inert'}\nX = 1\n")

        res, _ = _run_docker(impl, {}, monkeypatch, vault_root=tmp_path)

        assert res.success is False, "an inert body must never report success"
        blob = (res.error or "") + json.dumps(res.parsed)
        assert "run(" in blob or "no run" in blob.lower(), (
            "the failure must name the cause (no callable run()), got: "
            f"error={res.error!r} parsed={res.parsed!r}"
        )
        assert "inert_tool" in blob, "the failure must name the offending tool"

    def test_inert_body_fails_identically_on_local(self, tmp_path, monkeypatch):
        """The same diagnostic on the LocalBackend — one shared contract."""
        impl = _write(tmp_path, "inert_tool.py",
                      "TOOL_META = {'name': 'inert'}\nX = 1\n")
        res = asyncio.run(LocalBackend(vault_root=tmp_path).execute(
            impl, "{}", timeout=60))
        assert res.success is False
        blob = (res.error or "") + json.dumps(res.parsed)
        assert "run(" in blob or "no run" in blob.lower()


# ── W6.6: a failure must always carry a reason ───────────────────────────────

class TestFailuresAreExplained:
    """A failure with ``error=None`` is a green-tick's quieter cousin: the
    operator is told something went wrong but not what. That was the shape
    for any crash that wrote only to stderr."""

    def test_crash_with_only_stderr_still_explains_itself(self):
        from systemu.runtime.tool_sandbox import _parse_execution_stdout

        success, parsed, err = _parse_execution_stdout(
            "", 2, "tool.py", "python: can't open file '/app/tool_runner.py'")
        assert success is False
        assert err, "a non-zero exit with no result must not be a reasonless failure"
        assert "exited 2" in err
        assert "tool_runner.py" in err, "the stderr detail must reach the operator"

    def test_crash_with_no_output_at_all_still_explains_itself(self):
        from systemu.runtime.tool_sandbox import _parse_execution_stdout

        success, _, err = _parse_execution_stdout("", 9, "tool.py", "")
        assert success is False
        assert err and "exited 9" in err

    def test_a_tool_that_returned_a_payload_keeps_its_own_message(self):
        """Do NOT overwrite a result the tool actually returned."""
        from systemu.runtime.tool_sandbox import _parse_execution_stdout

        success, parsed, err = _parse_execution_stdout(
            '{"success": false, "error": "no such place"}', 0, "tool.py", "noise")
        assert success is False
        assert err is None, f"synthesised message clobbered the tool's own: {err!r}"
        assert parsed["error"] == "no such place"

    def test_bare_false_payload_is_not_overwritten(self):
        from systemu.runtime.tool_sandbox import _parse_execution_stdout

        success, parsed, err = _parse_execution_stdout(
            '{"success": false}', 0, "tool.py", "noise")
        assert success is False
        assert err is None, "a tool that spoke for itself must not be second-guessed"

    def test_docker_surfaces_the_stderr_detail_when_the_process_dies(self, tmp_path, monkeypatch):
        """End-to-end: a body that dies WITHOUT printing a result envelope.

        ``os._exit`` skips the runner's exception handler, so stdout stays
        empty and the only diagnostic is on stderr — the exact shape that
        used to yield success=False with error=None. The stderr text itself
        must reach the operator, not just *some* message: asserting merely
        that ``error`` is truthy lets the backend drop stderr entirely and
        still pass.
        """
        impl = _write(tmp_path, "dies_hard.py", (
            "import sys, os\n"
            "sys.stderr.write('DIAGNOSTIC_ONLY_ON_STDERR')\n"
            "sys.stderr.flush()\n"
            "os._exit(7)\n"
        ))
        res, _ = _run_docker(impl, {}, monkeypatch, vault_root=tmp_path)

        assert res.success is False
        assert res.stdout.strip() == "", "precondition: no result envelope on stdout"
        assert res.error, "docker failure carried no reason at all"
        assert "DIAGNOSTIC_ONLY_ON_STDERR" in res.error, (
            "the stderr detail never reached the operator — the backend is "
            f"not threading stderr into the parser. error={res.error!r}"
        )
        assert "exited 7" in res.error

    def test_local_surfaces_the_stderr_detail_too(self, tmp_path):
        """Same contract on the other executing backend."""
        impl = _write(tmp_path, "dies_hard.py", (
            "import sys, os\n"
            "sys.stderr.write('DIAGNOSTIC_ONLY_ON_STDERR')\n"
            "sys.stderr.flush()\n"
            "os._exit(7)\n"
        ))
        res = asyncio.run(LocalBackend(vault_root=tmp_path).execute(
            impl, "{}", timeout=60))
        assert res.success is False
        assert "DIAGNOSTIC_ONLY_ON_STDERR" in (res.error or ""), (
            f"LocalBackend dropped the stderr detail: error={res.error!r}"
        )


# ── positive controls: the emulator really executes the body ────────────────

class TestEmulatorIsHonest:
    """Guards against a test that passes because nothing ran.

    The repo has shipped tests that passed for the wrong reason; these prove
    the Docker code path really did execute the tool body.
    """

    def test_a_raising_tool_surfaces_its_exception(self, tmp_path, monkeypatch):
        impl = _write(tmp_path, "boom_tool.py",
                      "def run(**kwargs):\n    raise ValueError('boom from tool')\n")
        res, _ = _run_docker(impl, {}, monkeypatch, vault_root=tmp_path)
        assert res.success is False
        assert "boom from tool" in (res.error or "") + json.dumps(res.parsed)

    def test_params_reach_the_tool_body(self, tmp_path, monkeypatch):
        impl = _write(tmp_path, "echo_tool.py",
                      "def run(**kwargs):\n"
                      "    return {'success': True, 'echo': kwargs.get('x')}\n")
        res, _ = _run_docker(impl, {"x": "through-the-container"}, monkeypatch,
                             vault_root=tmp_path)
        assert res.success, res.error
        assert res.parsed.get("echo") == "through-the-container", (
            "params did not reach run() — the body may not have executed"
        )

    def test_script_style_tool_keeps_the_old_contract(self, tmp_path, monkeypatch):
        """Script-style (module-level print, argparse --params) still works."""
        impl = _write(tmp_path, "script_tool.py", (
            "import argparse, json\n"
            "ap = argparse.ArgumentParser()\n"
            "ap.add_argument('--params', default='{}')\n"
            "args, _ = ap.parse_known_args()\n"
            "print(json.dumps({'success': True, 'echo': json.loads(args.params).get('x')}))\n"
        ))
        res, _ = _run_docker(impl, {"x": "argv"}, monkeypatch, vault_root=tmp_path)
        assert res.success, res.error
        assert res.parsed.get("echo") == "argv"

    def test_unmounted_container_path_is_caught_by_the_emulator(self, tmp_path, monkeypatch):
        """Meta-test: the emulator fails a command referencing an unmounted path.

        Without this, a backend that forgot to mount the runner would appear
        to pass because the emulator silently resolved the path on the host.
        """
        rec = _install_fake_docker(monkeypatch)
        impl = _write(tmp_path, "t.py", "def run(**k):\n    return {'success': True}\n")
        asyncio.run(DockerBackend(vault_root=tmp_path).execute(
            impl, "{}", timeout=10))
        assert rec["unmounted_refs"] == [], (
            "the backend's command references container paths it never mounted: "
            f"{rec['unmounted_refs']}"
        )


# ── the shared contract: backends must not drift apart again ────────────────

class TestBackendsShareTheRunnerContract:
    def test_docker_command_invokes_the_runner_not_the_impl(self, tmp_path, monkeypatch):
        impl = _write(tmp_path, "t.py", "def run(**k):\n    return {'success': True}\n")
        _, rec = _run_docker(impl, {}, monkeypatch, vault_root=tmp_path)

        argv = rec["container_argv"]
        assert argv[0] == "python"
        assert "tool_runner" in argv[1], (
            "DockerBackend must execute the shared runner, not the tool file "
            f"directly; container argv was {argv!r}"
        )
        # the impl is an ARGUMENT to the runner, never argv[1]
        assert "/app/tool.py" in argv[2:], f"impl not passed to the runner: {argv!r}"

    def test_runner_is_mounted_into_the_container(self, tmp_path, monkeypatch):
        impl = _write(tmp_path, "t.py", "def run(**k):\n    return {'success': True}\n")
        _, rec = _run_docker(impl, {}, monkeypatch, vault_root=tmp_path)

        runner_mounts = [c for c in rec["mounts"] if "tool_runner" in c]
        assert runner_mounts, f"runner not mounted; mounts={rec['mounts']!r}"
        host = Path(rec["mounts"][runner_mounts[0]])
        assert host.name == "tool_runner_script.py" and host.exists(), (
            f"runner mount does not point at the real runner: {host}"
        )

    def test_runner_is_mounted_read_only(self, tmp_path, monkeypatch):
        impl = _write(tmp_path, "t.py", "def run(**k):\n    return {'success': True}\n")
        _, rec = _run_docker(impl, {}, monkeypatch, vault_root=tmp_path)
        argv = rec["argv"]
        # only the value that FOLLOWS a -v is a mount spec (the shell command
        # also mentions the runner, and it is not a mount)
        specs = [argv[i + 1] for i, a in enumerate(argv)
                 if a == "-v" and i + 1 < len(argv) and "tool_runner" in argv[i + 1]]
        assert specs and all(s.endswith(":ro") for s in specs), (
            f"runner must be mounted read-only, got {specs!r}"
        )

    # NOTE: the source-level DRIFT PIN lives in
    # tests/test_backend_runner_drift_pin.py — it reads module source via
    # inspect.getsource, which conftest auto-tags ``source_sensitive`` for the
    # WHOLE module. Keeping it here would deselect every behavioural pin above
    # from the edit-safe gate (`pytest -m "not source_sensitive"`).
