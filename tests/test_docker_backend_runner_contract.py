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
fake ``docker run`` builds the container filesystem view from the backend's
OWN ``-v`` mounts AND from the tar payload it streams on stdin, then executes
the exact command the backend composed. A container path referenced by the
command but never delivered by either route is therefore a test failure, not
a silent pass.

W6.7 moved the tool and the runner from bind mounts to a streamed payload,
because a bind SOURCE is resolved by the daemon and systemu can be
containerised (see ``test_docker_backend_deployment_portability``). The
emulator follows: it now unpacks the payload as the container would. The
honesty property is unchanged — delivery is checked, not assumed.
"""
from __future__ import annotations

import asyncio
import io
import json
import re
import shlex
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from systemu.runtime.backend.docker import DockerBackend
from systemu.runtime.backend.local import LocalBackend


# ── container emulator ───────────────────────────────────────────────────────

def _payload_guard(shell_cmd: str, croot: Path):
    """Evaluate the backend's ``[ -f X ] && [ -f Y ] || { echo …; exit N; }``
    payload-arrival guard against the real container filesystem.

    Returns what the shell would produce if the guard trips, else ``None``.
    Parses the CONDITION rather than the ``echo``, so a deleted guard stops
    protecting anything.
    """
    g = re.search(
        r"\{\s*((?:\[\s*-f\s+\S+\s*\]\s*(?:&&)?\s*)+);\s*\}\s*\|\|\s*"
        r"\{\s*echo\s+'(\S+)[^']*'\s*>&2;\s*exit\s+(\d+);\s*\}",
        shell_cmd)
    if g is None:
        return None
    guarded = [p.strip("'\"") for p in re.findall(r"\[\s*-f\s+(\S+?)\s*\]", g.group(1))]
    if all((croot / p.lstrip("/")).is_file() for p in guarded):
        return None
    return b"", f"{g.group(2)} payload check failed".encode(), int(g.group(3))


class _FakeProc:
    """The container. Its work happens in ``communicate`` because that is
    where the streamed payload actually arrives — on stdin, exactly as the
    real container receives it."""

    def __init__(self, run) -> None:
        self._run, self.returncode = run, None

    async def communicate(self, input=None):  # noqa: A002 - matches asyncio API
        out, err, self.returncode = self._run(input)
        return out, err


def _static(out: bytes, err: bytes, rc: int) -> _FakeProc:
    return _FakeProc(lambda _input: (out, err, rc))


# tarfile's extraction ``filter`` landed after this project's 3.10 floor; use
# it where available so the emulator does not emit a DeprecationWarning.
_EXTRACT_KW = {"filter": "data"} if sys.version_info >= (3, 12) else {}


def _parse_mount(spec: str) -> tuple[str, str]:
    """``host:container[:ro]`` -> (host, container), Windows drive-letter aware."""
    parts = spec.split(":")
    if len(parts) >= 3 and len(parts[0]) == 1 and parts[0].isalpha():
        return parts[0] + ":" + parts[1], parts[2]
    return parts[0], parts[1]


def _install_fake_docker(monkeypatch, croot: Path, *,
                         deliver_payload: bool = True) -> dict:
    """Patch asyncio.create_subprocess_exec to emulate `docker run`.

    ``croot`` is the container's root directory: everything the container can
    see must have been put there by a mount or by the streamed payload.
    ``deliver_payload=False`` emulates a delivery that silently produced
    nothing, so the tests can prove the check is not vacuous.
    Returns a recorder dict populated on each call.
    """
    rec: dict = {}

    async def _fake(*cmd, stdin=None, stdout=None, stderr=None, **kwargs):
        rec["argv"] = list(cmd)
        assert cmd[0] == "docker", f"expected a docker invocation, got {cmd[0]!r}"
        croot.mkdir(parents=True, exist_ok=True)

        mounts: dict[str, str] = {}
        for i, arg in enumerate(cmd):
            if arg == "-v" and i + 1 < len(cmd):
                host, container = _parse_mount(cmd[i + 1])
                mounts[container] = host
                src = Path(host)
                if src.is_file():
                    dest = croot / container.lstrip("/")
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(src.read_bytes())
        rec["mounts"] = mounts

        shell_cmd = cmd[-1]
        rec["shell_cmd"] = shell_cmd

        def _run(payload: bytes | None):
            # The streamed payload arrives on stdin; the container unpacks it
            # before running anything.
            rec["payload_bytes"] = len(payload or b"")
            m = re.search(r"tar\s+-xf\s+-\s+-C\s+([^\s;]+)", shell_cmd)
            rec["extract_dir"] = m.group(1) if m else None
            rec["payload_names"] = []
            if m and payload and deliver_payload:
                dest = croot / m.group(1).lstrip("/")
                dest.mkdir(parents=True, exist_ok=True)
                with tarfile.open(fileobj=io.BytesIO(payload), mode="r") as tar:
                    rec["payload_names"] = tar.getnames()
                    tar.extractall(dest, **_EXTRACT_KW)  # noqa: S202 - our own archive

            # The final segment runs the tool; `exec` is a shell builtin, not argv.
            pm = re.search(
                r"(?:exec\s+)?(python)\s+(/\S+)\s+(/\S+)\s+--params\s+(.*)$",
                shell_cmd)
            assert pm, f"no python invocation found in the command: {shell_cmd!r}"
            argv = [pm.group(1), pm.group(2), pm.group(3),
                    "--params", shlex.split(pm.group(4), posix=True)[0]]
            rec["container_argv"] = list(argv)

            # Anything the command names must actually BE in the container —
            # whether a mount or the payload put it there.
            undelivered = [a for a in (pm.group(2), pm.group(3))
                           if not (croot / a.lstrip("/")).is_file()]
            rec["unmounted_refs"] = undelivered

            # The backend's payload-arrival guard runs BEFORE python. Evaluate
            # the actual `[ -f X ]` condition, not the mere presence of an
            # `echo` — otherwise deleting the guard still looks guarded.
            guard = _payload_guard(shell_cmd, croot)
            if guard is not None:
                return guard
            if undelivered:
                msg = (f"python: can't open file {undelivered[0]!r}: "
                       f"No such file or directory")
                return b"", msg.encode(), 2

            mapped = [sys.executable,
                      str(croot / pm.group(2).lstrip("/")),
                      str(croot / pm.group(3).lstrip("/")),
                      "--params", argv[4]]
            completed = subprocess.run(mapped, capture_output=True)
            return completed.stdout, completed.stderr, completed.returncode

        return _FakeProc(_run)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake)
    return rec


def _run_docker(impl: Path, params: dict, monkeypatch, *, vault_root: Path):
    croot = Path(vault_root) / "_container_root"
    rec = _install_fake_docker(monkeypatch, croot)
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

    def test_undelivered_container_path_is_caught_by_the_emulator(self, tmp_path, monkeypatch):
        """Meta-test: the emulator fails a command referencing a path that
        never arrived in the container.

        Without this, a backend that forgot to ship the runner would appear
        to pass because the emulator silently resolved the path on the host.
        """
        impl = _write(tmp_path, "t.py", "def run(**k):\n    return {'success': True}\n")
        _, rec = _run_docker(impl, {}, monkeypatch, vault_root=tmp_path)
        assert rec["unmounted_refs"] == [], (
            "the backend's command references container paths it never "
            f"delivered: {rec['unmounted_refs']}"
        )

    def test_the_emulator_really_would_catch_a_lost_payload(self, tmp_path, monkeypatch):
        """Positive control FOR the meta-test above: suppress delivery and the
        emulator must notice. An always-empty ``unmounted_refs`` would make
        the previous assertion vacuous."""
        impl = _write(tmp_path, "t.py", "def run(**k):\n    return {'success': True}\n")
        croot = tmp_path / "_container_root"
        rec = _install_fake_docker(monkeypatch, croot, deliver_payload=False)
        res = asyncio.run(DockerBackend(vault_root=tmp_path).execute(
            impl, "{}", timeout=10))
        assert rec["unmounted_refs"], (
            "the emulator did not notice a completely undelivered payload — "
            "its delivery check is vacuous"
        )
        assert res.success is False


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
        assert argv[2].endswith("/t.py"), f"impl not passed to the runner: {argv!r}"

    def test_the_tool_keeps_its_real_name_inside_the_container(self, tmp_path, monkeypatch):
        """The runner names failures after ``basename(impl_path)``.

        Delivering every tool as a fixed ``tool.py`` made every in-container
        diagnostic say "tool.py" and never name the tool that actually broke.
        The previous emulator hid this: it mapped the container path back to
        the host file, so the runner saw the real name in tests and a generic
        one in production.
        """
        impl = _write(tmp_path, "weather_lookup.py",
                      "def run(**k):\n    return {'success': True}\n")
        _, rec = _run_docker(impl, {}, monkeypatch, vault_root=tmp_path)

        assert rec["container_argv"][2].endswith("/weather_lookup.py"), (
            "the tool lost its identity on the way into the container: "
            f"{rec['container_argv']!r}"
        )

    def test_the_runner_reaches_the_container(self, tmp_path, monkeypatch):
        """The shared runner must ARRIVE, by whatever mechanism.

        Was ``test_runner_is_mounted_into_the_container``: it pinned the
        bind mount specifically, which is the thing W6.7 had to remove (a
        bind source is resolved by the daemon, and systemu can be
        containerised). The requirement is unchanged — the runner must be
        in the container, and it must be the real shared one. Payload-level
        detail (identity, read-only mode) is pinned in
        ``test_docker_backend_deployment_portability``.
        """
        impl = _write(tmp_path, "t.py", "def run(**k):\n    return {'success': True}\n")
        _, rec = _run_docker(impl, {}, monkeypatch, vault_root=tmp_path)

        assert "tool_runner.py" in rec["payload_names"], (
            f"runner never reached the container; delivered={rec['payload_names']!r}"
        )
        assert rec["unmounted_refs"] == [], (
            f"the command names a path that never arrived: {rec['unmounted_refs']!r}"
        )
        landed = tmp_path / "_container_root" / "app" / "tool_runner.py"
        from systemu.runtime.backend.docker import _RUNNER_SRC
        assert landed.read_bytes() == _RUNNER_SRC.read_bytes(), (
            "what arrived is not the real shared tool_runner_script.py"
        )

    # NOTE: the source-level DRIFT PIN lives in
    # tests/test_backend_runner_drift_pin.py — it reads module source via
    # inspect.getsource, which conftest auto-tags ``source_sensitive`` for the
    # WHOLE module. Keeping it here would deselect every behavioural pin above
    # from the edit-safe gate (`pytest -m "not source_sensitive"`).
