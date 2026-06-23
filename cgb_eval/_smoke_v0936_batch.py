"""Measure the v0.9.36 request-outcome reconciliation rate across the SUSPEND
families (tool/mcp/subagent, all HIGH-risk -> escalate -> resume) vs non-suspend
controls (access-LOW, compute). ~9 real trials, ~$1.50.

For each trial, reports lease-mint / lease-revoke / request-outcome counts + the
number of execution dirs (>1 => the run suspended and resumed). The question:
does reconciliation fire on the resumed-completion exit, per family?
"""
import json
from pathlib import Path

from cgb_eval.runner import run_trial
from cgb_eval.tasks import ALL_TASKS

T = {t.task_id: t for t in ALL_TASKS}
WD = Path("cgb_results/_smoke_v0936_batch_work")


def inspect(model: str, task_id: str, cond: str, trial: int):
    td = WD / f"{task_id}__{cond}__{model}__t{trial}"
    vault = td / "vault"
    ex = vault / "executions"
    n_exec = len([d for d in ex.iterdir() if d.is_dir()]) if ex.is_dir() else 0
    mint = rev = ro = 0
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
                    ro += 1
    return n_exec, mint, rev, ro


def main() -> None:
    plan = [
        ("tool-01-sha256", "pull", 0), ("tool-01-sha256", "pull", 1),
        ("mcp-01-lookup3", "pull", 0), ("mcp-01-lookup3", "pull", 1),
        ("subagent-01-budget-regions", "pull", 0), ("subagent-01-budget-regions", "pull", 1),
        ("access-01-policy-read-low", "pull", 0),   # LOW control (no suspend)
        ("compute-01-reproduce12", "pull", 0),      # control
    ]
    print("=== v0.9.36 RECONCILIATION RATE across suspend families (model=gemini) ===")
    print(f"{'task':28s} {'oracle':6s} {'n_exec':6s} {'mint':4s} {'revoke':6s} {'req-outcome':11s}")
    tally = {}
    for tid, cond, tr in plan:
        rec = run_trial(T[tid], cond, "gemini_3_flash", tr, workdir_root=WD)
        n_exec, mint, rev, ro = inspect("gemini_3_flash", tid, cond, tr)
        fam = tid.split("-")[0]
        d = tally.setdefault(fam, {"runs": 0, "minted": 0, "reconciled": 0, "suspended": 0})
        d["runs"] += 1
        if mint:
            d["minted"] += 1
        if ro:
            d["reconciled"] += 1
        if n_exec > 1:
            d["suspended"] += 1
        print(f"{tid:28s} {str(rec.oracle_passed):6s} {n_exec:<6d} {mint:<4d} {rev:<6d} {ro:<11d}")
    print("\n=== per-family tally (runs / minted-lease / reconciled / suspended) ===")
    for fam, d in tally.items():
        print(f"  {fam:9s} runs={d['runs']} minted={d['minted']} reconciled={d['reconciled']} suspended={d['suspended']}")
    print("\nIf suspend families (tool/mcp/subagent) show minted>0 but reconciled=0 -> the")
    print("cross-suspend reconciliation gap is real. If reconciled>0 -> the taxonomy CAN populate.")


if __name__ == "__main__":
    main()
