#!/usr/bin/env python3
"""Subprocess entrypoint for tool implementations (W6.1).

Invoked by LocalBackend as:

    python tool_runner_script.py <impl_path> --params '<json>'

Why this exists: W2.2 made forged tools run out-of-process, executing the
implementation file directly as a script. But the curated vault tool pack is
MODULE-style — ``TOOL_META`` + ``run(**kwargs)`` with no ``__main__`` block —
so direct execution defined the functions and exited: exit 0, empty stdout,
reported as success. Every such tool was a silent no-op on the dashboard.

This runner restores the in-process semantics inside the subprocess boundary:

  * load the implementation module from its file path,
  * if it defines a callable ``run``, call ``run(**params)`` and print the
    JSON result (the sandbox parses the LAST stdout line),
  * if it doesn't (script-style tools, the old contract), the module-level
    code already executed during load and printed its own JSON — print
    nothing extra. ``sys.argv`` is rewritten first so script-style tools
    that argparse ``--params`` see exactly the argv they always did.

DELIBERATELY stdlib-only: the child process must not depend on ``systemu``
being importable (restricted env, arbitrary CWD, editable vs wheel installs).
Errors print a JSON error envelope and exit non-zero so the sandbox reports
an honest failure with the exception text.

W6.5 — no silent no-ops.  If the module defines no callable ``run`` AND
printed nothing while loading, then nothing ran: there is no result and no
effect.  That case now prints a SPECIFIC error naming the cause and exits
non-zero, rather than relying on the caller to infer failure from empty
stdout.  The caller's generic "no output" guard
(``_parse_execution_stdout``) still backstops it, but a generic message
sent an operator hunting the wrong problem.

Detection uses a pass-through stdout proxy (it delegates every write to the
real stream, so buffering and fd-level behaviour are unchanged).  A tool
whose ONLY output comes from a child process's inherited fd 1 — and which
also defines no ``run`` — is therefore reported as a failure rather than a
pass.  That direction is deliberate: over-strict is recoverable, a silent
false success is not.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys


class _CountingStdout:
    """Pass-through proxy for ``sys.stdout`` that records whether anything
    was written.  Delegates everything else to the wrapped stream."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.wrote = False

    def write(self, s):
        if s:
            self.wrote = True
        return self._inner.write(s)

    def writelines(self, lines):
        for line in lines:
            self.write(line)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("impl_path")
    parser.add_argument("--params", default="{}")
    args = parser.parse_args()

    try:
        params = json.loads(args.params or "{}")
    except json.JSONDecodeError as exc:
        print(json.dumps({"success": False, "error": f"invalid params JSON: {exc}"}))
        return 2
    if not isinstance(params, dict):
        print(json.dumps({"success": False,
                          "error": f"params must be a JSON object, got {type(params).__name__}"}))
        return 2

    # Old-contract compatibility: script-style tools parse --params from THEIR
    # argv at module level. Present the argv shape they were always given.
    sys.argv = [args.impl_path, "--params", args.params]

    spec = importlib.util.spec_from_file_location("_systemu_tool_impl", args.impl_path)
    if spec is None or spec.loader is None:
        print(json.dumps({"success": False,
                          "error": f"could not load tool module: {args.impl_path}"}))
        return 2
    module = importlib.util.module_from_spec(spec)

    # W6.5: watch whether loading the module produced any output, so a body
    # that did nothing at all can be reported as such instead of passing.
    probe = _CountingStdout(sys.stdout)
    sys.stdout = probe
    try:
        spec.loader.exec_module(module)  # script-style tools run + print here
    finally:
        sys.stdout = probe._inner

    run = getattr(module, "run", None)
    if callable(run):
        result = run(**params)
        # default=str: tolerate Paths/datetimes etc. in tool results rather
        # than turning a successful run into a serialization crash.
        # Printed LAST so it wins over any module-level output: the sandbox
        # parses the final stdout line, and an import-time banner must never
        # be mistaken for the tool's result.
        print(json.dumps(result, default=str))
        return 0

    if not probe.wrote:
        # Nothing to call and nothing printed — the body did not run. Say so.
        name = os.path.basename(args.impl_path)
        print(json.dumps({
            "success": False,
            "error": (
                f"tool body did not run: '{name}' defines no callable "
                f"run(**params) and produced no output while loading, so "
                f"nothing was executed and no result exists. Expected either "
                f"a module-style tool (define run(**params) returning a JSON "
                f"object) or a script-style tool (print a JSON result at "
                f"module level)."
            ),
            "error_type": "tool_body_did_not_run",
        }))
        return 4

    # Script-style: module-level code already printed its own JSON result.
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 — honest failure envelope for ANY crash
        print(json.dumps({"success": False,
                          "error": f"{type(exc).__name__}: {exc}"}))
        raise SystemExit(3)
