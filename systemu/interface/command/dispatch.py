"""The one dispatch contract (spec §4.1).

Both the Click CLI and the NiceGUI controls call dispatch(). It replaces the
13 hand-built [sys.executable, '-m', 'sharing_on', ...] argv sites in
systemu/interface (see grounding notes). argv is built in ONE place here.
  • in-process (CLI, CI):  runs the registered verb, returns CommandResult.
  • stream=True (dashboard): spawns the verb as a subprocess via JobManager
    and returns a CommandResult whose stream_ref is the Job.id.

JobManager wiring (verified against systemu/interface/jobs.py):
  • accessor:  JobManager.get() — classmethod singleton.
  • start_job(name, job_type, cmd, cwd, on_cancel=None, output_dir=None,
              dedup_key="") -> Job, where job_type="execute" arms the
              subprocess→EventBus bridge (SYSTEMU_EVENT_BRIDGE_FILE).
  • returns a Job dataclass exposing `.id`.
The reference signature matched reality, so no adaptation was required.
"""
from __future__ import annotations

import os
import sys
from typing import Callable, Dict, List

from systemu.interface.command.result import CommandResult, CommandStatus

_IN_PROCESS_VERBS: Dict[str, Callable] = {}


def _job_manager():
    from systemu.interface.jobs import JobManager
    return JobManager.get()


def _dashboard_origin() -> str:
    """Resolve the dashboard's HTTP origin from the same env the dashboard
    stamps at startup (dashboard.run_dashboard sets SYSTEMU_DASHBOARD_PORT and
    SYSTEMU_DASHBOARD_ORIGIN; SYSTEMU_DASHBOARD_HOST is the bind host).

    Used to tag the spawned recorder so it can drop captures of systemu's own
    dashboard chrome (v0.9.32 Item 2, Layer 1). A 0.0.0.0 bind is rewritten to
    localhost so the value matches what the browser actually loads.

    R-B5: the resolution now lives in ``runtime.capture_exclusion`` and this
    delegates to it. Both callers are answering the same question — "which origin
    is our own UI?" — and the recorder self-filter (v0.9.32) and the §5.10.b#6
    capture exclusion must never end up with two different answers.
    """
    from systemu.runtime.capture_exclusion import dashboard_origin
    return dashboard_origin()


def _verb_to_argv(verb: str, args: List[str]) -> List[str]:
    return [sys.executable, "-m", "sharing_on", *verb.split(), *args]


def dispatch(verb: str, args: List[str], *, cwd: str = ".", vault=None,
             stream: bool = False, job_type: str = "execute",
             dedup_key: str = "") -> CommandResult:
    """Run a verb either in-process or, when stream=True, as a JobManager subprocess.

    job_type names the job's semantic type (e.g. "evolve", "approve", "refine").
    It defaults to "execute", which arms the subprocess→EventBus bridge; migrated
    dashboard shell-outs pass their original job_type to preserve semantics.
    """
    if stream:
        cmd = _verb_to_argv(verb, args)
        # v0.9.32 Item 2: tag the spawned recorder with the dashboard origin so
        # WebExtensionCollector can drop captures of systemu's own UI (Layer 1).
        # This is the SINGLE spawn site every record job passes through, so the
        # env is set in exactly one place; JobMan.start_job → _child_env copies
        # os.environ into the child.
        os.environ["SYSTEMU_DASHBOARD_ORIGIN"] = _dashboard_origin()
        try:
            job = _job_manager().start_job(
                name=f"{verb} {' '.join(args)}".strip(),
                job_type=job_type,
                cmd=cmd, cwd=cwd, dedup_key=dedup_key,
            )
        except Exception as exc:
            return CommandResult(status=CommandStatus.ERROR,
                                 summary=f"Failed to dispatch {verb!r}: {exc}")
        return CommandResult(status=CommandStatus.OK,
                             summary=f"Dispatched {verb} (streaming).",
                             stream_ref=job.id)

    fn = _IN_PROCESS_VERBS.get(verb)
    if fn is None:
        return CommandResult(status=CommandStatus.ERROR,
                             summary=f"Unknown verb {verb!r}.")
    return fn(args, vault=vault)


# ── Default in-process verb registry ─────────────────────────────────────────
# Adapters map the dispatch list-args convention -> each verb's real signature,
# and look the verb up at CALL time so monkeypatching verbs.* still works.
def _adapt_tools_enable(args, *, vault) -> CommandResult:
    from systemu.interface.command import verbs
    if not args:
        return CommandResult(status=CommandStatus.ERROR, summary="tools enable requires a tool_id.")
    return verbs.tools_enable(args[0], vault=vault)


_IN_PROCESS_VERBS["tools enable"] = _adapt_tools_enable
