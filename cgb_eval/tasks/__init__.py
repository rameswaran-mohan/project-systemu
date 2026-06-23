"""Task registry across families (22 tasks; spec §5.2 'focused' scope + the
v0.9.34 MCP attachment family)."""
from typing import List

from cgb_eval.task_spec import CGBTask
from cgb_eval.tasks.tool_family import TOOL_TASKS
from cgb_eval.tasks.skill_family import SKILL_TASKS
from cgb_eval.tasks.access_family import ACCESS_TASKS
from cgb_eval.tasks.compute_family import COMPUTE_TASKS
from cgb_eval.tasks.subagent_family import SUBAGENT_TASKS
from cgb_eval.tasks.mcp_family import MCP_TASKS

ALL_TASKS: List[CGBTask] = [
    *TOOL_TASKS, *SKILL_TASKS, *ACCESS_TASKS, *COMPUTE_TASKS, *SUBAGENT_TASKS,
    *MCP_TASKS,
]
