"""DockerBackend — run the tool in an ephemeral Docker container.

Mirrors ``ToolSandbox._execute_docker``.  The container image is
``python:3.11-slim``; the tool file is mounted read-only and any
``extra_packages`` are pip-installed inside the container before the
tool runs.  A named volume caches pip downloads across runs.

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
module-level output was parsed as the tool's "result".  The runner is
mounted read-only alongside the tool; it is stdlib-only by design, so it
needs nothing installed in the image.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

# The shared out-of-process entrypoint, mounted into the container.  Same file
# LocalBackend invokes — the two backends MUST NOT drift apart (a divergence
# here is how module-style tools became silent no-ops under docker).
_RUNNER_SRC = Path(__file__).parent / "tool_runner_script.py"

# Container-side paths.
_C_TOOL   = "/app/tool.py"
_C_RUNNER = "/app/tool_runner.py"


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

        # W6.1: invoke the RUNNER with the tool as its argument — never the
        # tool file directly.  The runner imports the module and calls
        # run(**params); script-style tools keep the old contract.
        shell_cmd = (
            f"{pip_step}python {_C_RUNNER} {_C_TOOL} --params {quoted_params}"
        )

        cmd = [
            "docker", "run", "--rm",
            "-v", f"{impl_path.absolute()}:{_C_TOOL}:ro",
            "-v", f"{_RUNNER_SRC.absolute()}:{_C_RUNNER}:ro",
            "-v", f"{self.pip_cache_volume}:/root/.cache/pip",
            self.image,
            "sh", "-c", shell_cmd,
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout,
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
                    "[DockerBackend] execution timed out for '%s'", impl_path.name,
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

        success, parsed, parse_error = _parse_execution_stdout(
            stdout, exit_code, impl_path.name, stderr,
        )
        return ToolResult(
            success=success, stdout=stdout, stderr=stderr, parsed=parsed,
            exit_code=exit_code,
            error=parse_error or (parsed.get("error") if not success else None),
        )
