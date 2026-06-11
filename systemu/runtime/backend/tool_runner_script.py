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
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys


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
    spec.loader.exec_module(module)  # script-style tools run + print here

    run = getattr(module, "run", None)
    if callable(run):
        result = run(**params)
        # default=str: tolerate Paths/datetimes etc. in tool results rather
        # than turning a successful run into a serialization crash.
        print(json.dumps(result, default=str))
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
