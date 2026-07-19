"""DockerBackend — run the tool in an ephemeral Docker container.

Mirrors ``ToolSandbox._execute_docker``.  The container image is
``python:3.11-slim``; the tool implementation and the shared runner are
delivered into the container and any ``extra_packages`` are pip-installed
inside the container before the tool runs.  A named volume caches pip
downloads across runs.

Why ``--network host`` is omitted: it's a no-op on Docker Desktop
(containers run inside a VM, not on the host network) and creates a
false security impression.  Docker's default bridge network works on
every platform.

W6.1 parity (see ``tool_runner_script``): this backend executes the tool
through the SHARED runner, exactly like LocalBackend, rather than running
the implementation file as a script.  It previously ran
``python /app/tool.py`` directly — but every curated vault tool is
module-style (``TOOL_META`` + ``run()``, no ``__main__`` block), so the
container defined the functions and exited: exit 0, no effect, and any
module-level output was parsed as the tool's "result".

W6.7 — payload DELIVERY, not bind mounts.  The runner and the tool are
streamed into the container as a tar on stdin.  They used to be bind
mounted from the composing process's own filesystem, which cannot work
when that process is itself containerised:

    docker run -v <SOURCE>:<TARGET> ...

``<SOURCE>`` is resolved by the DAEMON, in the DAEMON's filesystem
namespace — never by the client that typed the command.  Under the
``docker-sandbox`` compose profile systemu runs in a container and drives
the HOST daemon through a mounted ``/var/run/docker.sock``, so the only
paths it can name are its own container's:

  * the tool  — ``/data/vault/...``, backed by the ``vault_data`` NAMED
    VOLUME, which has no host path at all; and
  * the runner — ``/app/systemu/runtime/backend/tool_runner_script.py``,
    baked into the systemu image by ``COPY . .``, so it exists in no
    filesystem the host daemon can see.

Neither exists on the host, and ``-v``'s short syntax does not reject a
missing source — the daemon CREATES it, as an empty directory.  The
container therefore received an empty directory where the runner and the
tool should have been, and no tool could execute under this profile.
That is a pre-existing defect of the tool mount; it predates the runner
mount and is not specific to it.

Streaming the payload removes the daemon-side path resolution entirely,
so there is nothing left to resolve wrongly.  The same code path is
correct on bare metal (where client and daemon happen to share a
filesystem) and under the compose profile (where they do not) — one code
path, no deployment branch, and therefore nothing for the two
deployments to drift apart on.  The pip cache stays a NAMED VOLUME: names
are resolved by the daemon against its own volume store, which is
deployment-independent by construction.

Delivery failure is never silent.  The in-container preamble verifies
that both files landed and exits with a distinctive, reserved code if
they did not, so a payload problem is reported as a payload problem
rather than blamed on the tool.
"""

from __future__ import annotations

import asyncio
import io
import logging
import shlex
import tarfile
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

# The shared out-of-process entrypoint, delivered into the container.  Same
# file LocalBackend invokes — the two backends MUST NOT drift apart (a
# divergence here is how module-style tools became silent no-ops under docker).
_RUNNER_SRC = Path(__file__).parent / "tool_runner_script.py"

# Container-side paths.  The implementation keeps its REAL filename inside the
# container, in its own directory so it can never collide with the runner.
# The runner reports failures as ``basename(impl_path)``; delivering the tool
# as a fixed ``tool.py`` made every in-container diagnostic say "tool.py"
# instead of naming the tool that actually failed — the operator-facing half
# of the same "which thing broke?" problem W6.6 fixed for stderr.
_C_DIR      = "/app"
_C_IMPL_DIR = "/app/impl"
_C_RUNNER   = "/app/tool_runner.py"


def _container_tool_path(impl_path) -> str:
    """Where the implementation lands inside the container."""
    return f"{_C_IMPL_DIR}/{Path(impl_path).name}"

# Reserved exit codes for the delivery preamble, paired with a sentinel the
# preamble writes to stderr.  BOTH are required to classify a run as a
# delivery failure: an exit code alone is ambiguous, because a tool is free
# to exit 90 itself, and misreading a tool failure as a delivery failure
# would send the operator hunting the wrong problem — the same class of harm
# the generic messages caused.
_DELIVERY_SENTINEL    = "__SYSTEMU_DELIVERY_FAIL__"
_EXIT_UNPACK_FAILED   = 90
_EXIT_PAYLOAD_MISSING = 91

_DELIVERY_EXIT_HELP = {
    _EXIT_UNPACK_FAILED: (
        "could not unpack the tool payload inside the container — the tool "
        "did NOT run"
    ),
    _EXIT_PAYLOAD_MISSING: (
        "the tool payload did not arrive inside the container — the tool "
        "did NOT run"
    ),
}


def _build_payload(impl_path: Path) -> bytes:
    """Tar the runner + the implementation into an in-memory archive.

    Read from the composing process's OWN filesystem — the one place both
    files are guaranteed to be readable — and shipped over the Docker API,
    so the daemon never has to resolve a client-side path.
    """
    buf = io.BytesIO()
    impl_arc = _container_tool_path(impl_path)[len(_C_DIR) + 1:]
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for src, arcname in ((_RUNNER_SRC, "tool_runner.py"),
                             (Path(impl_path), impl_arc)):
            data = Path(src).read_bytes()
            info = tarfile.TarInfo(arcname)
            info.size = len(data)
            info.mode = 0o444  # read-only, as the bind mounts were
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class DockerBackend:
    """Run the tool in an ephemeral Python container."""

    def __init__(
        self,
        *,
        vault_root: Path,
        image: str = "python:3.11-slim",
        pip_cache_volume: str = "systemu_pip_cache",
        max_output_bytes: int = 65_536,
    ) -> None:
        self.vault_root       = Path(vault_root)
        self.image            = image
        self.pip_cache_volume = pip_cache_volume
        self.max_output       = max_output_bytes

    async def execute(
        self,
        impl_path: Path,
        params_json: str,
        *,
        timeout: int,
        extra_packages: Optional[List[str]] = None,
    ):
        # Lazy import to avoid the circular ToolSandbox ↔ backend dependency.
        from systemu.runtime.tool_sandbox import ToolResult, _parse_execution_stdout

        quoted_params = shlex.quote(params_json)
        if extra_packages:
            unique = list(dict.fromkeys(extra_packages))
            pip_step = f"pip install -q {shlex.join(unique)} && "
            logger.debug("[DockerBackend] installing %s before tool run", unique)
        else:
            pip_step = ""

        try:
            payload = _build_payload(impl_path)
        except OSError as exc:
            return ToolResult(
                success=False,
                error=(f"could not read the tool payload for "
                       f"'{Path(impl_path).name}': {exc} — the tool did NOT run"),
            )

        c_tool = shlex.quote(_container_tool_path(impl_path))

        # W6.7: unpack the streamed payload, then PROVE it landed before
        # running anything.  Without the check, a delivery failure would
        # surface as whatever python said about a missing file, which reads
        # like a broken tool.
        preamble = (
            f"{{ mkdir -p {_C_DIR} && tar -xf - -C {_C_DIR}; }} || {{ "
            f"echo '{_DELIVERY_SENTINEL} could not unpack the tool payload' >&2; "
            f"exit {_EXIT_UNPACK_FAILED}; }}; "
            f"{{ [ -f {_C_RUNNER} ] && [ -f {c_tool} ]; }} || {{ "
            f"echo '{_DELIVERY_SENTINEL} the tool payload did not arrive' >&2; "
            f"exit {_EXIT_PAYLOAD_MISSING}; }}; "
        )

        # W6.1: invoke the RUNNER with the tool as its argument — never the
        # tool file directly.  The runner imports the module and calls
        # run(**params); script-style tools keep the old contract.
        shell_cmd = (
            f"{preamble}{pip_step}"
            f"exec python {_C_RUNNER} {c_tool} --params {quoted_params}"
        )

        # Only the pip cache stays a mount, and it is a NAMED VOLUME: the
        # daemon resolves the name against its own volume store, so it is
        # correct whether or not the client shares the daemon's filesystem.
        cmd = [
            "docker", "run", "--rm", "-i",
            "-v", f"{self.pip_cache_volume}:/root/.cache/pip",
            self.image,
            "sh", "-c", shell_cmd,
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(payload), timeout=timeout,
                )
                stdout    = stdout_b.decode(errors="replace")[: self.max_output]
                stderr    = stderr_b.decode(errors="replace")[: self.max_output]
                exit_code = proc.returncode
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                    await proc.communicate()
                except ProcessLookupError:
                    pass
                logger.warning(
                    "[DockerBackend] execution timed out for '%s'",
                    Path(impl_path).name,
                )
                return ToolResult(
                    success=False, error="Docker execution timed out",
                    timed_out=True, exit_code=-1,
                )
        except FileNotFoundError:
            return ToolResult(
                success=False,
                error=(
                    "Docker not found — install Docker Desktop or "
                    "set SYSTEMU_TOOL_BACKEND=local"
                ),
            )
        except Exception as exc:
            logger.error("[DockerBackend] Docker process failed: %s", exc)
            return ToolResult(success=False, error=str(exc))

        # A delivery failure is NOT a tool failure — say which one it was, and
        # say plainly that the body never ran.  Reported before the stdout
        # parser gets a chance to describe it in the tool's terms.
        if exit_code in _DELIVERY_EXIT_HELP and _DELIVERY_SENTINEL in stderr:
            detail = (stderr or "").replace(_DELIVERY_SENTINEL, "").strip()
            msg = (
                f"{Path(impl_path).name}: {_DELIVERY_EXIT_HELP[exit_code]}"
                + (f" — {detail[-500:]}" if detail else "")
            )
            logger.error("[DockerBackend] %s", msg)
            return ToolResult(
                success=False, stdout=stdout, stderr=stderr,
                exit_code=exit_code, error=msg,
            )

        success, parsed, parse_error = _parse_execution_stdout(
            stdout, exit_code, Path(impl_path).name, stderr,
        )
        return ToolResult(
            success=success, stdout=stdout, stderr=stderr, parsed=parsed,
            exit_code=exit_code,
            error=parse_error or (parsed.get("error") if not success else None),
        )
