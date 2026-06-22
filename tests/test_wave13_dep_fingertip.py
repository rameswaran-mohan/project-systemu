"""W13.2 — dependencies at the fingertip.

(a) The packages forged tools most commonly request ship with the core
install (pandas, beautifulsoup4, lxml, geopy, python-dateutil) — the most
frequent approval interruption disappears.
(b) Any package that still needs approval becomes a ONE-CLICK needs-you
card — including from the quick lane, which previously only errored.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from systemu.vault.vault import Vault

REPO = Path(__file__).resolve().parent.parent
COMMON = ("pandas", "beautifulsoup4", "lxml", "geopy", "python-dateutil")


class TestCommonPackagesBundled:
    def test_common_forge_deps_ship_with_install(self):
        pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
        deps_block = pyproject.split("dependencies = [", 1)[1].split("]", 1)[0]
        shipped = {re.split(r"[<>=!~;\[]", line.strip().strip('",'))[0].lower()
                   for line in deps_block.splitlines()
                   if line.strip().startswith('"')}
        missing = [p for p in COMMON if p not in shipped]
        assert missing == [], f"common forge deps not bundled: {missing}"


class TestQuickLaneDepGate:
    def test_blocked_dep_enqueues_a_needs_you_gate(self, tmp_path):
        """THE fingertip contract: a quick-lane tool blocked on a package
        puts an Approve & install card in the queue, immediately."""
        from systemu.core.models import Tool, ToolStatus, ToolType
        from systemu.core.utils import generate_id
        from systemu.interface.command.inbox import InboxQueue
        from systemu.pipelines.quick_task import run_quick_task

        for sub in ["tools/implementations", "elder", "notifications"]:
            (tmp_path / sub).mkdir(parents=True, exist_ok=True)
        (tmp_path / "tools" / "index.json").write_text("[]", encoding="utf-8")
        vault = Vault(str(tmp_path))
        impl = tmp_path / "tools" / "implementations" / "needs_pkg.py"
        impl.write_text("def run(**kwargs):\n    return {'success': True}\n",
                        encoding="utf-8")
        vault.save_tool(Tool(
            id=generate_id("tool"), name="needs_pkg", description="t",
            tool_type=ToolType.PYTHON_FUNCTION, status=ToolStatus.DEPLOYED,
            enabled=True, implementation_path=str(impl),
            forged_by_systemu=True, parameter_names=["x"]))

        class _BlockedSandbox:
            async def execute_tool(self, *a, **k):
                from types import SimpleNamespace
                return SimpleNamespace(
                    success=False,
                    error="Tool 'needs_pkg' needs operator approval to "
                          "install: superpkg.",
                    parsed={"success": False,
                            "error_type": "dependency_install_pending_approval",
                            "missing_packages": ["superpkg"]},
                    exit_code=-1)

        script = iter([
            {"action": "TOOL_CALL", "tool": "needs_pkg",
             "params": {"x": 1}, "reasoning": "try"},
            {"action": "ANSWER", "answer_md": "blocked, told the operator"},
        ])

        def llm(*, system, user, config=None):
            return next(script)

        res = run_quick_task("do the thing", None, vault, llm_json=llm,
                             sandbox=_BlockedSandbox())
        assert res.status == "success"
        rows = list(InboxQueue(vault).list_descriptors())
        descriptors = [r[1] if isinstance(r, tuple) else r for r in rows]
        assert any("superpkg" in str(getattr(d, "dedup", "")) or
                   "superpkg" in str(getattr(d, "title", ""))
                   for d in descriptors), \
            "the blocked package must be one click away in needs-you"

    def test_error_message_points_at_needs_you(self):
        from systemu.pipelines import quick_task
        src = inspect.getsource(quick_task)
        assert "Needs-you" in src, \
            "the model (and chat history) must say where the button is"


class TestQuickLaneHonesty:
    """W13.6: a 'could not complete' answer must never report success."""

    def _run(self, tmp_path, answer_action):
        from systemu.pipelines.quick_task import run_quick_task
        for sub in ["tools/implementations", "elder"]:
            (tmp_path / sub).mkdir(parents=True, exist_ok=True)
        (tmp_path / "tools" / "index.json").write_text("[]", encoding="utf-8")
        vault = Vault(str(tmp_path))
        return run_quick_task("x", None, vault,
                              llm_json=lambda **k: answer_action)

    def test_completed_false_reports_partial(self, tmp_path):
        res = self._run(tmp_path, {
            "action": "ANSWER", "completed": False,
            "answer_md": "Could not complete — missing location."})
        assert res.status == "partial"

    def test_default_stays_success(self, tmp_path):
        res = self._run(tmp_path, {"action": "ANSWER", "answer_md": "done"})
        assert res.status == "success"

    def test_prompt_demands_the_verdict(self):
        from systemu.core.utils import load_prompt
        text = load_prompt("quick_task.md")
        assert '"completed"' in text and "Never dress a failure" in text
