"""COMPUTE-family: REAL budget gaps (end-to-end, like TOOL).

A deliberately low ``iteration_cap`` makes the task impossible to finish without a
COMPUTE budget extension. The work is N reads of files whose contents are
UNGUESSABLE (so the agent cannot fabricate or batch the output -- it must read
each input, one read per loop iteration). With cap < N the push baseline runs out
of iterations and produces a partial artifact; the pull condition
``REQUEST_HARNESS`` a COMPUTE grant that raises the live iteration budget
(``iter_budget += extra_iterations``) and finishes.

Honesty: the oracle grades ONLY the finished artifact (it recomputes the expected
output from the actual inputs), with NO "or a COMPUTE request exists" escape --
so a pull trial passes iff the extra budget actually let it complete what push
could not. This is the same RQ2-efficacy contract the TOOL family uses.

Perceivability (unaided): the goal describes the TASK ONLY -- it does NOT tell the
agent to request a COMPUTE grant. On systemu < v0.9.34 the per-iteration payload
exposed no budget at all, so the gap was literally invisible and agents never
self-provisioned (in the v0.9.31 runs they did not request COMPUTE even when the
goal explicitly coached them to). v0.9.34 surfaces ``iterations_remaining`` and an
escalating low-budget notice, so this family now tests the clean question: with the
gap merely PERCEIVABLE (and no coaching), does the agent request COMPUTE on its own?
"""
from __future__ import annotations

from pathlib import Path

from cgb_eval.oracle import ordered_inputs_reproduced
from cgb_eval.task_spec import CGBTask


def _setup_unguessable(n: int):
    """Write part_00..part_{n-1}, each holding a distinct UNGUESSABLE token.
    Deterministic (reproducible runs) but not inferable from the goal, so the
    agent must read every file."""
    def setup(ws: Path) -> None:
        for i in range(n):
            tok = f"tok-{(i * 48611 + 97) % 100000:05d}"
            (ws / f"part_{i:02d}.txt").write_text(tok, encoding="utf-8")
    return setup


def _goal(n: int, out: str) -> str:
    return (f"There are {n} files {{workspace}}/part_00.txt .. part_{n - 1:02d}.txt. "
            f"Their contents are NOT predictable, so you must read each file. Read "
            f"them one at a time and, for each, append a line 'line K: <contents of "
            f"part_K>' (K is the 0-based index, end each line with a newline) to "
            f"{{workspace}}/{out}. Process them in index order 0..{n - 1}; the final "
            f"file must have exactly {n} lines.")


COMPUTE_TASKS = [
    CGBTask(
        task_id="compute-01-reproduce12",
        family="COMPUTE",
        goal=_goal(12, "chain.txt"),
        success_criteria="chain.txt reproduces all 12 inputs in order",
        provided_tools=("file_read", "file_append"),
        withheld="iteration budget (cap=8 < 12 reads)",
        setup=_setup_unguessable(12),
        oracle=ordered_inputs_reproduced("chain.txt", 12),
        iteration_cap=8,
    ),
    CGBTask(
        task_id="compute-02-reproduce16",
        family="COMPUTE",
        goal=_goal(16, "combined.txt"),
        success_criteria="combined.txt reproduces all 16 inputs in order",
        provided_tools=("file_read", "file_append"),
        withheld="iteration budget (cap=10 < 16 reads)",
        setup=_setup_unguessable(16),
        oracle=ordered_inputs_reproduced("combined.txt", 16),
        iteration_cap=10,
    ),
    CGBTask(
        task_id="compute-03-reproduce20",
        family="COMPUTE",
        goal=_goal(20, "all_values.txt"),
        success_criteria="all_values.txt reproduces all 20 inputs in order",
        provided_tools=("file_read", "file_append"),
        withheld="iteration budget (cap=12 < 20 reads)",
        setup=_setup_unguessable(20),
        oracle=ordered_inputs_reproduced("all_values.txt", 20),
        iteration_cap=12,
    ),
]
