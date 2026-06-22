"""W6 — the subprocess tool-runner contract fix (the 41-tool silent no-op).

Field bug (spa/parking tasks): W2.2 routes forged tools out-of-process, and
LocalBackend executed the implementation as a script:

    python <impl.py> --params '{...}'

But ALL 41 curated vault tools are module-style — ``TOOL_META`` + ``run()``,
no ``__main__`` block — so the script defined its functions and exited:
exit 0, empty stdout, and ``_parse_execution_stdout`` defaulted empty output
to SUCCESS. Every web/file tool on the dashboard was a silent no-op that
*reported* success, which also blinded the stuck-loop governor (the
same-tool-failure trigger needs ``success=False``) and corrupted objective
crediting.

These tests pin the new contract:
  * W6.1 — a runner imports the impl and calls ``run(**params)``, printing
    the JSON result; script-style tools (old contract) keep working.
  * W6.2 — exit 0 + empty stdout is a FAILURE, not a success.
  * W6.4 — a REAL module-style vault tool (parse_json, offline) executes
    through the full ToolSandbox subprocess path and returns a payload.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from systemu.runtime.backend.local import LocalBackend
from systemu.runtime.tool_sandbox import ToolSandbox, _parse_execution_stdout


def _execute(impl: Path, params: dict, *, vault_root: Path, timeout: int = 40):
    backend = LocalBackend(vault_root=vault_root)
    return asyncio.run(backend.execute(impl, json.dumps(params), timeout=timeout))


# ── W6.1: module-style tools actually execute ────────────────────────────────
class TestModuleStyleTools:
    def _impl(self, tmp_path: Path, body: str) -> Path:
        p = tmp_path / "vault" / "tools" / "implementations" / "mod_tool.py"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
        return p

    def test_run_result_reaches_parsed(self, tmp_path):
        impl = self._impl(tmp_path, (
            "TOOL_META = {'name': 'mod_tool'}\n"
            "def run(**kwargs):\n"
            "    return {'success': True, 'data': {'echo': kwargs.get('x')}, 'error': None}\n"
        ))
        res = _execute(impl, {"x": "hello"}, vault_root=tmp_path / "vault")
        assert res.success, f"module-style tool must execute, got error={res.error!r}"
        assert res.parsed.get("data") == {"echo": "hello"}

    def test_run_returning_failure_is_failure(self, tmp_path):
        impl = self._impl(tmp_path, (
            "def run(**kwargs):\n"
            "    return {'success': False, 'data': None, 'error': 'no such place'}\n"
        ))
        res = _execute(impl, {}, vault_root=tmp_path / "vault")
        assert res.success is False
        assert "no such place" in (res.error or "")

    def test_run_raising_is_failure_with_error(self, tmp_path):
        impl = self._impl(tmp_path, (
            "def run(**kwargs):\n"
            "    raise ValueError('boom from tool')\n"
        ))
        res = _execute(impl, {}, vault_root=tmp_path / "vault")
        assert res.success is False
        assert "boom from tool" in (res.error or "") + json.dumps(res.parsed)
        assert res.exit_code != 0

    def test_non_serializable_result_does_not_crash(self, tmp_path):
        impl = self._impl(tmp_path, (
            "def run(**kwargs):\n"
            "    return {'success': True, 'data': {'p': __import__('pathlib').Path('x')}}\n"
        ))
        res = _execute(impl, {}, vault_root=tmp_path / "vault")
        assert res.success, res.error
        assert res.parsed.get("data", {}).get("p") == "x"


# ── W6.1: script-style tools (the OLD contract) keep working ─────────────────
class TestScriptStyleTools:
    def test_module_level_print_still_parsed(self, tmp_path):
        impl = tmp_path / "script_tool.py"
        impl.write_text(
            "import json\nprint(json.dumps({'success': True, 'echo': 'script'}))\n",
            encoding="utf-8",
        )
        res = _execute(impl, {}, vault_root=tmp_path)
        assert res.success, res.error
        assert res.parsed.get("echo") == "script"

    def test_script_reading_params_via_argv_still_works(self, tmp_path):
        # Old contract: the impl receives ['--params', json] on ITS OWN argv.
        impl = tmp_path / "argv_tool.py"
        impl.write_text(
            "import argparse, json\n"
            "ap = argparse.ArgumentParser()\n"
            "ap.add_argument('--params', default='{}')\n"
            "args, _ = ap.parse_known_args()\n"
            "p = json.loads(args.params)\n"
            "print(json.dumps({'success': True, 'echo': p.get('x')}))\n",
            encoding="utf-8",
        )
        res = _execute(impl, {"x": "via-argv"}, vault_root=tmp_path)
        assert res.success, res.error
        assert res.parsed.get("echo") == "via-argv"


# ── W6.2: truth-in-results — empty output is NOT success ────────────────────
class TestEmptyOutputIsFailure:
    def test_parse_helper_rejects_empty_stdout_on_exit_zero(self):
        success, parsed, err = _parse_execution_stdout("", 0, "x.py")
        assert success is False
        assert "no output" in (err or "").lower()

    def test_whitespace_only_stdout_is_failure(self):
        success, _, err = _parse_execution_stdout("  \n ", 0, "x.py")
        assert success is False and "no output" in (err or "").lower()

    def test_nonzero_exit_still_failure_without_duplicate_error(self):
        success, _, _ = _parse_execution_stdout("", 3, "x.py")
        assert success is False

    def test_impl_with_no_run_and_no_output_is_failure(self, tmp_path):
        # The exact field shape: definitions only, nothing printed.
        impl = tmp_path / "noop_tool.py"
        impl.write_text("TOOL_META = {'name': 'noop'}\nX = 1\n", encoding="utf-8")
        res = _execute(impl, {}, vault_root=tmp_path)
        assert res.success is False
        assert "no output" in (res.error or "").lower()


# ── W6.4: a REAL vault tool through the FULL sandbox subprocess path ────────
class TestRealVaultToolSubprocess:
    def test_parse_json_executes_with_payload(self, tmp_path):
        repo_impl = (Path(__file__).resolve().parent.parent
                     / "systemu" / "vault" / "tools" / "implementations"
                     / "parse_json.py")
        assert repo_impl.exists()
        sandbox = ToolSandbox(tmp_path)
        res = asyncio.run(sandbox.execute_tool(
            str(repo_impl),
            {"input": '{"spa": "found", "price": "cheap"}', "mode": "string"},
            force_subprocess=True, timeout=40,
        ))
        assert res.success, f"real vault tool no-op'd in subprocess mode: {res.error!r}"
        assert res.parsed.get("data") == {"spa": "found", "price": "cheap"}, \
            "the W2.2 regression: empty parsed payload reported as success"


# ── W6.3: the stuck ask reports ALL attempted tools ──────────────────────────
class TestToolsTriedTracking:
    def _runtime(self):
        # Bare instance — only the stuck-counter state is exercised.
        from systemu.runtime.shadow_runtime import ShadowRuntime
        rt = ShadowRuntime.__new__(ShadowRuntime)
        rt._iters_since_obj_credit = 0
        rt._same_tool_fail_streak = {}
        rt._tools_since_credit = set()
        return rt

    def test_successful_but_useless_calls_are_reported(self):
        """THE field shape: fetch_json 'succeeded' 3x with empty payloads —
        the ask said 'Tools tried: (none)' because only failures counted."""
        rt = self._runtime()
        for _ in range(3):
            rt._update_stuck_counters(action="TOOL_CALL", tool_name="fetch_json",
                                      tool_success=True, credited_obj_id=None)
        assert rt._tools_tried_since_credit() == ["fetch_json"]

    def test_failed_calls_still_reported(self):
        rt = self._runtime()
        rt._update_stuck_counters(action="TOOL_CALL", tool_name="web_search",
                                  tool_success=False, credited_obj_id=None)
        assert rt._tools_tried_since_credit() == ["web_search"]

    def test_objective_credit_resets_the_report(self):
        rt = self._runtime()
        rt._update_stuck_counters(action="TOOL_CALL", tool_name="fetch_json",
                                  tool_success=True, credited_obj_id=None)
        rt._update_stuck_counters(action="TOOL_CALL", tool_name="fetch_json",
                                  tool_success=True, credited_obj_id=1)
        assert rt._tools_tried_since_credit() == []

    def test_mixed_tools_sorted_union(self):
        rt = self._runtime()
        rt._update_stuck_counters(action="TOOL_CALL", tool_name="web_search",
                                  tool_success=True, credited_obj_id=None)
        rt._update_stuck_counters(action="TOOL_CALL", tool_name="fetch_json",
                                  tool_success=False, credited_obj_id=None)
        assert rt._tools_tried_since_credit() == ["fetch_json", "web_search"]
