"""Verify the v0.9.37 Bug 11 fix: do the SUSPEND families now reconcile to
granted_used/unused with a real pull_failure_category (not invisible)?  ~6 trials.

For each pull trial, dump every request-outcome event's outcome + category and the
attempts_before on the request, across BOTH execution ledgers in the vault.
"""
import json
from pathlib import Path

from cgb_eval.runner import run_trial
from cgb_eval.tasks import ALL_TASKS

T = {t.task_id: t for t in ALL_TASKS}
WD = Path("cgb_results/_smoke_v0937_work")


def inspect(model: str, task_id: str, cond: str, trial: int):
    td = WD / f"{task_id}__{cond}__{model}__t{trial}"
    vault = td / "vault"
    ex = vault / "executions"
    n_exec = len([d for d in ex.iterdir() if d.is_dir()]) if ex.is_dir() else 0
    mint = rev = 0
    outcomes = []  # (outcome, category)
    led = vault / "harness_ledger"
    if led.is_dir():
        for f in led.glob("*.jsonl"):
            for ln in f.read_text(encoding="utf-8", errors="replace").splitlines():
                if not ln.strip():
                    continue
                try:
                    r = json.loads(ln)
                except Exception:
                    continue
                et = r.get("event_type")
                if et == "lease-mint":
                    mint += 1
                elif et == "lease-revoke":
                    rev += 1
                elif et == "request-outcome":
                    outcomes.append((r.get("outcome"), r.get("pull_failure_category")))
    ab = []
    if ex.is_dir():
        for exd in ex.iterdir():
            aud = exd / "decision_audit.jsonl"
            if aud.is_file():
                for ln in aud.read_text(encoding="utf-8", errors="replace").splitlines():
                    if not ln.strip():
                        continue
                    try:
                        r = json.loads(ln)
                    except Exception:
                        continue
                    if r.get("is_request_harness"):
                        ab.append(r.get("harness_attempts_before"))
    return n_exec, mint, rev, outcomes, ab


def main() -> None:
    plan = [("tool-01-sha256", "pull", 0),
            ("mcp-01-lookup3", "pull", 0),
            ("subagent-01-budget-regions", "pull", 0),
            ("subagent-01-budget-regions", "pull", 1),
            ("access-01-policy-read-low", "pull", 0)]  # control
    print("=== v0.9.37 Bug-11 SMOKE: do suspend families reconcile to granted_*? (model=gemini) ===")
    for tid, cond, tr in plan:
        rec = run_trial(T[tid], cond, "gemini_3_flash", tr, workdir_root=WD)
        n_exec, mint, rev, outs, ab = inspect("gemini_3_flash", tid, cond, tr)
        print(f"\n{tid} {cond}: oracle={rec.oracle_passed} status={rec.runtime_status} n_exec={n_exec}")
        print(f"   lease-mint={mint} lease-revoke={rev} attempts_before={ab}")
        print(f"   request-outcomes (outcome, category): {outs if outs else 'NONE'}")
    print("\n=== WANT: mcp/subagent/tool show request-outcome with outcome=granted_used/unused (not NONE, not escalate_unresolved) ===")


if __name__ == "__main__":
    main()
