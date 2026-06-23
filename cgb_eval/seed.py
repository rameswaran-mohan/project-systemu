"""Build an isolated, gap-bearing seed vault for one trial.

Gap injection = the goal requires a capability whose tool is simply absent from
the vault.  The push condition has no recourse; the pull condition can
``REQUEST_HARNESS``.  An identical Scroll/Activity across conditions = a
controlled A/B comparison.

Mirrors the directory scaffold + save calls from ``tests/test_shadow_runtime.py``
(the ``tmp_vault`` / ``runtime_setup`` fixtures).
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from systemu.core.models import (
    Activity, Objective, Scroll, Shadow, ShadowStatus, Tool, ToolStatus, ToolType,
)
from systemu.vault.vault import Vault

from cgb_eval.task_spec import CGBTask

REPO_ROOT = Path(__file__).resolve().parents[1]
STARTER_IMPLS = REPO_ROOT / "systemu" / "vault" / "tools" / "implementations"

_VAULT_SUBDIRS = [
    "scrolls", "activities", "shadow_army", "skills", "tools/implementations",
    "evolutions", "notifications", "executions",
]
_INDEXED = ["scrolls", "activities", "shadow_army", "skills", "tools", "evolutions"]

# Param schemas for the starter tools the benchmark provides. The seed registers
# tools with these so the executor LLM isn't blind to their parameters (an empty
# schema forces the model to guess arg names, which derails weaker models).
_TOOL_SCHEMAS = {
    "file_read": {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Path to the file to read."}},
        "required": ["path"],
    },
    "file_write": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to write."},
            "content": {"type": "string", "description": "Text content to write."},
        },
        "required": ["path", "content"],
    },
    "file_append": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to append to (created if absent)."},
            "content": {"type": "string",
                        "description": "Text to append verbatim; include a trailing "
                                       "newline to separate lines."},
        },
        "required": ["path", "content"],
    },
}


@dataclass
class BuiltTrial:
    vault: Vault
    vault_dir: str
    shadow: Shadow
    activity: Activity
    workspace: Path


def _scaffold_vault_dir(root: Path) -> None:
    for sub in _VAULT_SUBDIRS:
        (root / sub).mkdir(parents=True, exist_ok=True)
    for idx in _INDEXED:
        (root / idx / "index.json").write_text("[]", encoding="utf-8")
    (root / "global_memory.jsonl").write_text("", encoding="utf-8")


def build_trial_vault(task: CGBTask, trial_dir: Path) -> BuiltTrial:
    """Construct a fresh vault + workspace for ``task`` under ``trial_dir``.

    Copies ONLY the tools named in ``task.provided_tools`` — the withheld
    capability is deliberately absent.  Writes an identical Scroll/Activity
    regardless of condition; runs ``task.setup`` to lay down input artifacts.
    """
    # Resolve to ABSOLUTE up front. The goal embeds the workspace path, and the
    # v0.9.34 v2 file tools re-root the agent's path under _root=output_dir(=the
    # workspace) via safe_resolve. A cwd-relative workspace path then DOUBLE-joins
    # ('.../workspace/cgb_results/.../workspace/in.txt' -> not found) whenever the
    # trial dir is itself relative (the real run path). An absolute path is
    # unambiguous under any _root. (Same double-join class as the impl_path fix
    # below; see test_workspace_path_in_goal_resolves_under_v2_output_dir_root.)
    trial_dir = Path(trial_dir).resolve()
    vault_dir = trial_dir / "vault"
    workspace = trial_dir / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    _scaffold_vault_dir(vault_dir)
    vault = Vault(str(vault_dir))

    # Copy ONLY the provided starter tools — the withheld capability is absent.
    for name in task.provided_tools:
        src = STARTER_IMPLS / f"{name}.py"
        if not src.is_file():
            raise FileNotFoundError(f"starter tool not found: {src}")
        dst = vault_dir / "tools" / "implementations" / f"{name}.py"
        shutil.copyfile(src, dst)
        # Store the path the way the forge does (tool_forge.py) and the way
        # tool_sandbox resolves it: relative to the vault root's PARENT, with
        # forward slashes (e.g. "vault/tools/implementations/file_read.py").
        # str(dst) would be cwd-relative and, when the trial dir is itself
        # relative (the real pilot/full-run workdir), double-joins under
        # vault_root.parent -> "Implementation not found".
        impl_rel = dst.relative_to(vault_dir.parent).as_posix()
        vault.save_tool(Tool(
            id=f"tool_{name}", name=name, description=f"CGB provided tool {name}",
            tool_type=ToolType.PYTHON_FUNCTION, status=ToolStatus.DEPLOYED,
            enabled=True,
            implementation_path=impl_rel,
            parameters_schema=dict(_TOOL_SCHEMAS.get(name, {})),
        ))

    shadow = Shadow(
        id="cgb_shadow", name="CGB Shadow", description="Benchmark executor",
        system_prompt="You are a capable task executor.", status=ShadowStatus.AWAKENED,
    )
    vault.save_shadow(shadow)

    goal = task.format_goal(workspace)
    scroll = Scroll(
        id=f"scroll_{task.task_id}", name=task.task_id, source_session_id="cgb",
        raw_instructions_path="", narrative_md=goal,
        raw_request=goal,  # authoritative goal (v0.9.7 Pillar A)
        objectives=[Objective(id=1, goal=goal, success_criteria=task.success_criteria)],
    )
    vault.save_scroll(scroll)

    activity = Activity(
        id=f"act_{task.task_id}", name=task.task_id, scroll_id=scroll.id,
        required_tool_ids=[f"tool_{n}" for n in task.provided_tools],
        required_skill_ids=[], assigned_shadow_id=shadow.id,
    )
    vault.save_activity(activity)

    task.setup(workspace)
    return BuiltTrial(vault=vault, vault_dir=str(vault_dir), shadow=shadow,
                      activity=activity, workspace=workspace)
