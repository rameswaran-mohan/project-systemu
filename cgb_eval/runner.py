"""Single-trial executor + resume-safe matrix runner with a per-model token cap.

``run_trial`` builds a gap-bearing vault, applies the condition+model env, runs
``ShadowRuntime.execute`` once (token-accounted), grades with the EXTERNAL oracle,
and — on pull conditions — attaches the RQ1 ``pull_decision`` instrumentation.

``run_matrix`` walks tasks x conditions x models x trials, appending one JSON line
per trial.  It is:
  * resume-safe — cells already present in the results file are skipped, so an
    interrupted overnight run continues where it left off; and
  * budget-bounded — it tracks cumulative tokens per model (seeded from any
    existing results on resume) and STOPS scheduling further trials for a model
    once it reaches PER_MODEL_TOKEN_BUDGET.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import tempfile
import time
import traceback
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional
from unittest.mock import patch as _patch

from cgb_eval.accounting import TokenLedger, patched_accounting
from cgb_eval.conditions import CONDITIONS, MODEL_PROFILES, applied_env
from cgb_eval.seed import build_trial_vault
from cgb_eval.task_spec import CGBTask

# Hard per-model spend cap (cumulative tokens across all of a model's trials).
# Once a model crosses this, the runner stops scheduling new trials for it.
PER_MODEL_TOKEN_BUDGET = 400_000

# Per-family built-in toolsets to SUPPRESS from the agent's catalog, so a
# capability the runtime injects by default cannot MASK the injected gap. The
# SUBAGENT gap is masked by the always-injected ``spawn_subagent`` (delegate
# toolset) exactly as ``write_file`` masked the TOOL gap; suppressing it forces
# delegation through the governed REQUEST_HARNESS. The granted parallel fleet
# (``SYSTEMU_DELEGATE_USE_PARALLEL``) is reached via the Governor grant, NOT this
# direct tool, so suppression does not disable the parallelism under test.
_SUPPRESS_TOOLSETS: Dict[str, set] = {
    "SUBAGENT": {"delegate"},
}


def _suppress_toolsets_catalog(suppress: set):
    """Build a drop-in for ``_build_llm_tool_catalog`` that filters out entries
    whose ``toolset`` is suppressed. Captures the real builder BEFORE the patch is
    installed, so it wraps (not recurses into) the original. One patch covers both
    the boot-time and per-iteration injection sites (both call this builder)."""
    from systemu.runtime import shadow_runtime as _sr
    _orig = _sr._build_llm_tool_catalog

    def _filtered(*a, **k):
        return [e for e in _orig(*a, **k) if e.get("toolset") not in suppress]
    return _filtered


@dataclass
class TrialRecord:
    task_id: str
    family: str
    condition: str
    model_profile: str
    trial: int
    runtime_status: str        # success | failure | partial | crash
    oracle_passed: bool        # EXTERNAL ground truth — RQ2 efficacy
    oracle_details: str
    tokens_in: int
    tokens_out: int
    tokens_total: int
    llm_calls: int
    duration_s: float
    error: Optional[str]
    # --- RQ1 pull-decision quality (PRIMARY), parsed from decision_audit.jsonl
    #     + the harness ledger; populated by cgb_eval.pull_decision.
    #     None on the push baseline (no pull instrumentation produced).
    pull_decision: Optional[dict] = None


def _record_key(rec: dict) -> tuple:
    return (rec["task_id"], rec["condition"], rec["model_profile"], rec["trial"])


def run_trial(task: CGBTask, condition: str, model_profile: str, trial: int,
              workdir_root: Optional[Path] = None,
              write_oracle_file: Optional[str] = None,
              model_operator_approval: bool = True) -> TrialRecord:
    """Run ONE trial and return its record.  Makes real LLM calls unless the
    caller patches ``systemu.runtime.shadow_runtime.llm_call_json`` /
    ``ToolSandbox.execute_tool`` (the unit-test path)."""
    env = {**CONDITIONS[condition], **MODEL_PROFILES[model_profile]}
    ledger = TokenLedger()
    t0 = time.perf_counter()
    status, error = "crash", None
    oracle_passed, oracle_details = False, "not evaluated"

    ctx = tempfile.TemporaryDirectory() if workdir_root is None else None
    if ctx:
        trial_dir = Path(ctx.name)
    else:
        trial_dir = workdir_root / f"{task.task_id}__{condition}__{model_profile}__t{trial}"
        # Clear any stale vault from a prior build's run of this SAME cell — otherwise
        # old ledgers coexist with the new ones and corrupt analysis (a stale pre-fix
        # request-outcome can read as a second grant). run_matrix is resume-safe
        # (already-done cells are skipped upstream), so this only fires on an
        # intentional re-run (remediation / smoke re-validation).
        if trial_dir.exists():
            import shutil
            shutil.rmtree(trial_dir, ignore_errors=True)
        trial_dir.mkdir(parents=True, exist_ok=True)

    pull_decision: Optional[dict] = None
    try:
        with applied_env(env):
            built = build_trial_vault(task, trial_dir)
            from sharing_on.config import Config
            # from_env (NOT Config()): resolves the OpenRouter key AND the
            # SYSTEMU_TIER*_MODEL overrides applied by applied_env above. A no-arg
            # Config() yields an empty key and the default deepseek model, so every
            # real trial would 401 on the wrong model.
            cfg = Config.from_env()
            cfg.vault_dir = built.vault_dir
            cfg.output_dir = str(built.workspace)

            from systemu.runtime.shadow_runtime import ShadowRuntime
            with ExitStack() as _stack:
                _stack.enter_context(patched_accounting(ledger))
                _stack.enter_context(_patch(
                    "systemu.runtime.shadow_runtime.MAX_ITERATIONS", task.iteration_cap))
                _suppress = _SUPPRESS_TOOLSETS.get(task.family)
                if _suppress:
                    _stack.enter_context(_patch(
                        "systemu.runtime.shadow_runtime._build_llm_tool_catalog",
                        _suppress_toolsets_catalog(_suppress)))
                runtime = ShadowRuntime(cfg, built.vault)
                result = asyncio.run(runtime.execute(built.shadow, built.activity))
                status = (result or {}).get("status", "crash")
                # Modeled operator approval (see cgb_eval/operator.py): a HIGH-risk
                # pull (e.g. forging new tool code) ESCALATES and SUSPENDS awaiting a
                # human. We model the approve + resume so recovery (RQ2) is
                # measurable; the escalation stays recorded in the ledger (RQ4). Only
                # the pull conditions can escalate, so push is untouched.
                if model_operator_approval and condition != "push":
                    from cgb_eval.operator import approve_and_resume_once
                    resumes = 0
                    # Cap aligned with systemu's per-run request cap (8): the modeled
                    # operator approves every escalation the runtime itself allows, so a
                    # genuine self-correction loop (forge -> see dry-run error -> re-forge)
                    # is not cut short by the eval before the runtime's own bound bites.
                    while "suspend" in str(status).lower() and resumes < 8:
                        rr = approve_and_resume_once(
                            runtime, built.shadow, built.activity, built.vault, cfg)
                        if rr is None:
                            break
                        result = rr
                        status = (result or {}).get("status", "crash")
                        resumes += 1
            status = (result or {}).get("status", "crash")

            if write_oracle_file is not None:  # unit-test hook only
                (built.workspace / "out.txt").write_text(write_oracle_file,
                                                         encoding="utf-8")
            # Arity dispatch: artifact graders take (workspace); graders that must
            # read the harness ledger / decision queue (ACCESS) take (workspace, vault).
            if len(inspect.signature(task.oracle).parameters) >= 2:
                verdict = task.oracle(built.workspace, built.vault)
            else:
                verdict = task.oracle(built.workspace)
            oracle_passed, oracle_details = verdict.passed, verdict.details

            # RQ1 (PRIMARY): only the pull conditions produce instrumentation.
            if condition != "push":
                from cgb_eval.pull_decision import extract_pull_decision
                pull_decision = extract_pull_decision(Path(built.vault_dir), task.family)
    except Exception:
        error = traceback.format_exc(limit=5)
    finally:
        if ctx:
            ctx.cleanup()

    return TrialRecord(
        task_id=task.task_id, family=task.family, condition=condition,
        model_profile=model_profile, trial=trial, runtime_status=status,
        oracle_passed=oracle_passed, oracle_details=oracle_details,
        tokens_in=ledger.input_tokens, tokens_out=ledger.output_tokens,
        tokens_total=ledger.total_tokens, llm_calls=ledger.calls,
        duration_s=round(time.perf_counter() - t0, 1), error=error,
        pull_decision=pull_decision,
    )


def _model_token_totals(records: Iterable[dict]) -> Dict[str, int]:
    """Cumulative tokens already spent per model (for resume + cap seeding)."""
    totals: Dict[str, int] = {}
    for r in records:
        m = r.get("model_profile")
        if m is None:
            continue
        totals[m] = totals.get(m, 0) + int(r.get("tokens_total", 0) or 0)
    return totals


def run_matrix(tasks: Iterable[CGBTask], conditions: Iterable[str],
               model_profiles: Iterable[str], trials: int,
               results_path: Path, workdir_root: Optional[Path] = None,
               per_model_token_budget: int = PER_MODEL_TOKEN_BUDGET,
               run_trial_fn: Callable[..., TrialRecord] = run_trial) -> int:
    """Run the full matrix, appending one JSON line per trial.

    Resume-safe: cells already present in ``results_path`` are skipped.
    Budget-bounded: once a model's cumulative tokens reach
    ``per_model_token_budget`` no further trials are scheduled for it.

    ``run_trial_fn`` is injectable so tests can drive the cap deterministically
    with a mocked accounting source.  Returns the number of NEW trials run.
    """
    results_path.parent.mkdir(parents=True, exist_ok=True)
    existing: set = set()
    existing_records: List[dict] = []
    if results_path.exists():
        for line in results_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rec = json.loads(line)
                existing.add(_record_key(rec))
                existing_records.append(rec)

    spent = _model_token_totals(existing_records)
    capped: set = {m for m, t in spent.items() if t >= per_model_token_budget}

    ran = 0
    for task in tasks:
        for cond in conditions:
            for prof in model_profiles:
                if prof in capped:
                    continue  # this model hit its token budget — skip remaining cells
                for t in range(trials):
                    if prof in capped:
                        break
                    key = (task.task_id, cond, prof, t)
                    if key in existing:
                        continue
                    rec = run_trial_fn(task, cond, prof, t, workdir_root=workdir_root)
                    with results_path.open("a", encoding="utf-8") as fh:
                        fh.write(json.dumps(asdict(rec)) + "\n")
                    ran += 1
                    spent[prof] = spent.get(prof, 0) + int(rec.tokens_total or 0)
                    print(f"[{ran}] {key} -> {rec.runtime_status} "
                          f"oracle={rec.oracle_passed} tok={rec.tokens_total} "
                          f"(model cum={spent[prof]}/{per_model_token_budget})")
                    if spent[prof] >= per_model_token_budget:
                        capped.add(prof)
                        print(f"[budget] model '{prof}' reached "
                              f"{spent[prof]} >= {per_model_token_budget} tokens — "
                              f"stopping further trials for it.")
    return ran
