"""DockerBackend must work when SYSTEMU ITSELF is containerised (W6.7).

The ``docker-sandbox`` compose profile runs systemu in a container and gives
it the HOST's Docker daemon through a mounted ``/var/run/docker.sock``.  That
splits one assumption the backend used to make silently:

    docker run -v <SOURCE>:<TARGET> ...

``<SOURCE>`` is resolved by the DAEMON, in the DAEMON's filesystem namespace.
The client that composed the command never resolves it.  On bare metal the two
namespaces coincide and nobody notices.  Under the compose profile they do
not, and every path systemu can name is a path the host daemon cannot see:

  * the tool  — ``/data/vault/...``, backed by the ``vault_data`` NAMED VOLUME
    (no host path exists for it at all); and
  * the runner — ``/app/systemu/runtime/backend/tool_runner_script.py``, baked
    into the systemu image, present in no filesystem the daemon can reach.

So the container received nothing usable where the tool and the runner were
meant to be, and the profile could never execute a tool.  This predates the
runner mount: the TOOL mount had the same defect from the start.

The pins below fix the deployment in place.  They emulate a daemon whose
filesystem is a SEPARATE namespace from the client's, which is the part the
sibling packet's emulator could not express — it resolved every mount against
the one machine running the tests, so a client-only path looked fine.

What real Docker does with a bind source the daemon cannot find is left
UNDECIDED here on purpose, and both possibilities are exercised
(``_MissingSource``): the documented ``-v`` behaviour is to create the source
as an empty directory, but a daemon that hard-errors is equally acceptable to
these tests.  The invariant does not depend on the answer, and after the fix
neither does the backend — it stops asking the daemon to resolve client paths
at all.
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


# ── how a daemon reacts to a bind source it cannot see ──────────────────────

# tarfile's extraction ``filter`` landed after this project's 3.10 floor; use
# it where available so the emulator does not emit a DeprecationWarning.
_EXTRACT_KW = {"filter": "data"} if sys.version_info >= (3, 12) else {}


class _MissingSource:
    EMPTY_DIR = "empty_dir"   # documented `-v` behaviour: create it, empty
    DAEMON_ERROR = "error"    # `--mount` behaviour / stricter daemons


def _payload_guard(shell_cmd: str, croot: Path):
    """Evaluate the backend's ``[ -f X ] && [ -f Y ] || { echo …; exit N; }``
    payload-arrival guard against the real container filesystem.

    Returns the ``(stdout, stderr, rc)`` the shell would produce if the guard
    trips, or ``None`` if there is no such guard or it passes. Deliberately
    parses the CONDITION, not just the ``echo``: a guard that was deleted must
    stop protecting anything.
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
    where the streamed payload arrives — on stdin, as the real container
    receives it."""

    def __init__(self, run) -> None:
        self._run, self.returncode = run, None

    async def communicate(self, input=None):  # noqa: A002 - matches asyncio API
        out, err, self.returncode = self._run(input)
        return out, err


def _static(out: bytes, err: bytes, rc: int) -> _FakeProc:
    return _FakeProc(lambda _input: (out, err, rc))


def _split_mount(spec: str) -> tuple[str, str]:
    """``source:target[:opts]`` -> (source, target), Windows drive-letter aware."""
    parts = spec.split(":")
    if len(parts) >= 3 and len(parts[0]) == 1 and parts[0].isalpha():
        return parts[0] + ":" + parts[1], parts[2]
    return parts[0], parts[1]


def _is_named_volume(source: str) -> bool:
    """A bare volume NAME — resolved by the daemon against its own volume
    store, so it is correct in every deployment.  Anything with a path
    separator or a drive letter is a bind mount and must be resolvable in the
    DAEMON's filesystem."""
    return not ("/" in source or "\\" in source
                or (len(source) > 1 and source[1] == ":"))


def _install_fake_docker(monkeypatch, *, daemon_visible_roots, missing_source,
                         container_root: Path, deliver_payload: bool = True):
    """Emulate ``docker run`` against a daemon with its OWN filesystem.

    ``daemon_visible_roots`` is what the daemon can resolve.  Pass ``[]`` for
    the containerised deployment (the daemon shares nothing with the client)
    and ``[Path(anchor)]`` for bare metal (they share everything).
    """
    rec: dict = {}

    async def _fake(*cmd, stdin=None, stdout=None, stderr=None, **kwargs):
        rec["argv"] = list(cmd)
        assert cmd[0] == "docker", f"expected a docker invocation, got {cmd[0]!r}"

        croot = container_root
        croot.mkdir(parents=True, exist_ok=True)

        # ── resolve every -v the way the DAEMON would ───────────────────────
        mounts, binds = {}, {}
        for i, arg in enumerate(cmd):
            if arg == "-v" and i + 1 < len(cmd):
                source, target = _split_mount(cmd[i + 1])
                mounts[target] = source
                if _is_named_volume(source):
                    (croot / target.lstrip("/")).mkdir(parents=True, exist_ok=True)
                    continue
                binds[target] = source
                src = Path(source)
                visible = any(
                    str(src).lower().startswith(str(r).lower())
                    for r in daemon_visible_roots
                )
                if visible and src.exists():
                    dest = croot / target.lstrip("/")
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(src.read_bytes())
                elif missing_source == _MissingSource.DAEMON_ERROR:
                    msg = (f"docker: Error response from daemon: invalid mount "
                           f"config: bind source path does not exist: {source}")
                    return _static(b"", msg.encode(), 125)
                else:
                    # What `-v` really does: materialise the source as an empty
                    # DIRECTORY, then mount that. The container gets a directory
                    # where a file was meant to be.
                    (croot / target.lstrip("/")).mkdir(parents=True, exist_ok=True)
        rec["mounts"], rec["binds"] = mounts, binds

        shell_cmd = cmd[-1]
        rec["shell_cmd"] = shell_cmd

        def _run(payload: bytes | None):
            # ── deliver the streamed payload, if the backend sends one ──────
            rec["payload_bytes"] = len(payload or b"")
            m = re.search(r"tar\s+-xf\s+-\s+-C\s+([^\s;]+)", shell_cmd)
            rec["extract_dir"] = m.group(1) if m else None
            if m and payload and deliver_payload:
                dest = croot / m.group(1).lstrip("/")
                dest.mkdir(parents=True, exist_ok=True)
                with tarfile.open(fileobj=io.BytesIO(payload), mode="r") as tar:
                    tar.extractall(dest, **_EXTRACT_KW)  # noqa: S202 - our own archive
            elif m and not deliver_payload:
                # Emulate a delivery that silently produced nothing.
                (croot / m.group(1).lstrip("/")).mkdir(parents=True, exist_ok=True)

            # ── run the final python invocation ────────────────────────────
            pm = re.search(r"(?:exec\s+)?python\s+(/\S+)\s+(/\S+)\s+--params\s+(.*)$",
                           shell_cmd)
            if pm is None:
                return b"", b"emulator: no python invocation in the command", 2
            runner_c, tool_c = pm.group(1), pm.group(2)
            params = shlex.split(pm.group(3), posix=True)[0]

            # The backend's payload-arrival guard, if it has one, runs BEFORE
            # python. Evaluate the actual `[ -f X ]` test the command carries —
            # firing on the mere presence of an `echo` would let a backend
            # delete the guard and still look guarded.
            guard = _payload_guard(shell_cmd, croot)
            if guard is not None:
                rec["missing_in_container"] = ["<caught by guard>"]
                return guard

            missing = [p for p in (runner_c, tool_c)
                       if not (croot / p.lstrip("/")).is_file()]
            rec["missing_in_container"] = missing
            if missing:
                # No guard caught it — exactly what the container would say.
                target = croot / missing[0].lstrip("/")
                if target.is_dir():
                    msg = f"python: can't find '__main__' module in '{missing[0]}'"
                else:
                    msg = (f"python: can't open file '{missing[0]}': "
                           f"[Errno 2] No such file or directory")
                return b"", msg.encode(), 1

            argv = [sys.executable,
                    str(croot / runner_c.lstrip("/")),
                    str(croot / tool_c.lstrip("/")),
                    "--params", params]
            completed = subprocess.run(argv, capture_output=True)
            return completed.stdout, completed.stderr, completed.returncode

        return _FakeProc(_run)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake)
    return rec


_EFFECT_TOOL = """\
TOOL_META = {'name': 'effect_tool'}

def run(**kwargs):
    import pathlib
    pathlib.Path(kwargs['out']).write_text('THE EFFECT HAPPENED', encoding='utf-8')
    return {'success': True, 'data': {'wrote': kwargs['out']}, 'error': None}
"""


def _write(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


# ── the invariant that makes the backend deployment-portable ────────────────

class TestNoClientPathIsHandedToTheDaemon:
    """THE structural pin. Every ``-v`` source must be a NAMED VOLUME.

    A bind mount whose source is a path from the composing process's own
    filesystem is only correct when the client and the daemon share a
    filesystem — true on bare metal, false under ``docker-sandbox``. Naming a
    volume is correct in both, because the daemon resolves names against its
    own volume store.
    """

    def test_every_bind_source_is_a_named_volume(self, tmp_path, monkeypatch):
        impl = _write(tmp_path, "t.py", "def run(**k):\n    return {'success': True}\n")
        rec = _install_fake_docker(
            monkeypatch, daemon_visible_roots=[Path(tmp_path.anchor)],
            missing_source=_MissingSource.EMPTY_DIR,
            container_root=tmp_path / "croot")
        asyncio.run(DockerBackend(vault_root=tmp_path).execute(
            impl, "{}", timeout=30))

        assert rec["binds"] == {}, (
            "DockerBackend handed the daemon a path from its OWN filesystem: "
            f"{rec['binds']!r}. Under the docker-sandbox profile the daemon "
            "runs on the host and cannot resolve container paths, so this "
            "mount silently does not arrive."
        )

    def test_the_pip_cache_is_still_a_named_volume(self, tmp_path, monkeypatch):
        """Positive control: the emulator does see mounts, so the assertion
        above is not passing because nothing was inspected."""
        impl = _write(tmp_path, "t.py", "def run(**k):\n    return {'success': True}\n")
        rec = _install_fake_docker(
            monkeypatch, daemon_visible_roots=[Path(tmp_path.anchor)],
            missing_source=_MissingSource.EMPTY_DIR,
            container_root=tmp_path / "croot")
        asyncio.run(DockerBackend(vault_root=tmp_path).execute(
            impl, "{}", timeout=30))

        assert any("/root/.cache/pip" == t for t in rec["mounts"]), (
            f"expected the pip cache mount to survive; mounts={rec['mounts']!r}"
        )


# ── the deployment that was broken ──────────────────────────────────────────

@pytest.mark.parametrize("missing_source",
                         [_MissingSource.EMPTY_DIR, _MissingSource.DAEMON_ERROR])
class TestContainerisedDeploymentExecutesTools:
    """systemu in a container, driving the host daemon over the socket."""

    def test_the_tool_body_actually_runs(self, tmp_path, monkeypatch, missing_source):
        impl = _write(tmp_path, "effect_tool.py", _EFFECT_TOOL)
        marker = tmp_path / "effect.txt"
        rec = _install_fake_docker(
            monkeypatch,
            daemon_visible_roots=[],          # daemon shares NOTHING with us
            missing_source=missing_source,
            container_root=tmp_path / "croot")

        res = asyncio.run(DockerBackend(vault_root=tmp_path).execute(
            impl, json.dumps({"out": str(marker)}), timeout=30))

        assert marker.exists(), (
            "the tool produced no effect when systemu is containerised — the "
            "body never ran. The daemon could not resolve the paths systemu "
            f"gave it. (error={res.error!r}, missing_in_container="
            f"{rec.get('missing_in_container')!r})"
        )
        assert res.success, res.error
        assert res.parsed.get("data") == {"wrote": str(marker)}

    def test_a_run_that_cannot_execute_never_reports_success(
            self, tmp_path, monkeypatch, missing_source):
        """ANTI-FALSE-SUCCESS PIN — the sibling packet's exact shape.

        If delivery fails, the body did not run. That must surface as a
        failure that says so, never as a green tick.
        """
        impl = _write(tmp_path, "effect_tool.py", _EFFECT_TOOL)
        marker = tmp_path / "effect.txt"
        _install_fake_docker(
            monkeypatch, daemon_visible_roots=[], missing_source=missing_source,
            container_root=tmp_path / "croot",
            deliver_payload=False)            # payload silently lost

        res = asyncio.run(DockerBackend(vault_root=tmp_path).execute(
            impl, json.dumps({"out": str(marker)}), timeout=30))

        assert not marker.exists(), "precondition: the body must not have run"
        assert res.success is False, (
            "FALSE SUCCESS: reported success while the tool never executed"
        )
        assert res.error, "a failure that carries no reason is not actionable"

    def test_the_failure_says_the_tool_did_not_run(
            self, tmp_path, monkeypatch, missing_source):
        """The message must name the tool AND be specific about the cause —
        a generic failure sends the operator to debug a healthy tool."""
        impl = _write(tmp_path, "effect_tool.py", _EFFECT_TOOL)
        _install_fake_docker(
            monkeypatch, daemon_visible_roots=[], missing_source=missing_source,
            container_root=tmp_path / "croot", deliver_payload=False)

        res = asyncio.run(DockerBackend(vault_root=tmp_path).execute(
            impl, "{}", timeout=30))

        blob = (res.error or "") + json.dumps(res.parsed or {})
        assert "effect_tool" in blob, f"failure does not name the tool: {blob!r}"
        assert "did NOT run" in blob or "did not run" in blob, (
            f"failure does not say the body never executed: {blob!r}"
        )


# ── bare metal must keep working — it is the default deployment ─────────────

class TestBareMetalStillWorks:
    def test_tool_runs_when_client_and_daemon_share_a_filesystem(
            self, tmp_path, monkeypatch):
        impl = _write(tmp_path, "effect_tool.py", _EFFECT_TOOL)
        marker = tmp_path / "bare.txt"
        _install_fake_docker(
            monkeypatch,
            daemon_visible_roots=[Path(tmp_path.anchor)],   # same machine
            missing_source=_MissingSource.EMPTY_DIR,
            container_root=tmp_path / "croot")

        res = asyncio.run(DockerBackend(vault_root=tmp_path).execute(
            impl, json.dumps({"out": str(marker)}), timeout=30))

        assert marker.exists(), f"bare-metal deployment regressed: {res.error!r}"
        assert res.success, res.error


# ── the payload itself ──────────────────────────────────────────────────────

class TestPayloadCarriesBothFiles:
    def test_runner_and_tool_both_travel_to_the_container(
            self, tmp_path, monkeypatch):
        """Replaces the old ``runner is mounted`` pin: same requirement (the
        shared runner must REACH the container), mechanism that survives both
        deployments."""
        impl = _write(tmp_path, "t.py", "def run(**k):\n    return {'success': True}\n")
        rec = _install_fake_docker(
            monkeypatch, daemon_visible_roots=[],
            missing_source=_MissingSource.EMPTY_DIR,
            container_root=tmp_path / "croot")
        asyncio.run(DockerBackend(vault_root=tmp_path).execute(
            impl, "{}", timeout=30))

        assert rec["payload_bytes"] > 0, "nothing was streamed to the container"
        assert rec["extract_dir"], "the command never unpacks the payload"
        assert rec["missing_in_container"] == [], (
            "a path the command references never arrived in the container: "
            f"{rec['missing_in_container']!r}"
        )

    def test_payload_contains_the_real_shared_runner(self, tmp_path):
        """The delivered runner must be the SAME file LocalBackend executes —
        a copy that drifted would re-open the W6.1 no-op bug."""
        from systemu.runtime.backend.docker import _RUNNER_SRC, _build_payload

        impl = _write(tmp_path, "t.py", "def run(**k):\n    return {'success': True}\n")
        with tarfile.open(fileobj=io.BytesIO(_build_payload(impl)), mode="r") as tar:
            names = tar.getnames()
            runner_bytes = tar.extractfile("tool_runner.py").read()
            tool_bytes = tar.extractfile("impl/t.py").read()

        assert sorted(names) == ["impl/t.py", "tool_runner.py"], names
        assert runner_bytes == _RUNNER_SRC.read_bytes(), (
            "the delivered runner is not the shared tool_runner_script.py"
        )
        assert tool_bytes == impl.read_bytes()

    def test_payload_is_delivered_read_only(self, tmp_path):
        """The bind mounts were ``:ro``; delivery must not quietly relax that."""
        from systemu.runtime.backend.docker import _build_payload

        impl = _write(tmp_path, "t.py", "def run(**k):\n    return {'success': True}\n")
        with tarfile.open(fileobj=io.BytesIO(_build_payload(impl)), mode="r") as tar:
            modes = {m.name: m.mode for m in tar.getmembers()}

        for name, mode in modes.items():
            assert not (mode & 0o222), f"{name} is writable in the container: {mode:o}"
