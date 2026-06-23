"""Smoke test the v0.9.36 Bug 9/10 fix (real API; ~3 trials, ~$0.20).

Runs in ONE process so the cross-run MCP leak is exercised:
  1. access-01 pull  -> mints an access lease; expect lease-revoke + request-outcome
     now emitted (Bug 9) and harness_attempts_before populated (Bug 10).
  2. mcp-01 pull      -> attaches the 'lookup' server, completes; expect revoke +
     request-outcome + MCP teardown.
  3. mcp-02 push      -> SAME process, no grant; expect FAIL (the leaked tool from
     step 2 must be gone -> the teardown worked).
"""
import json
from pathlib import Path

from cgb_eval.runner import run_trial
from cgb_eval.tasks import ALL_TASKS

T = {t.task_id: t for t in ALL_TASKS}
WD = Path("cgb_results/_smoke_v0936_work")


def ledger_and_audit(model: str, task_id: str, cond: str):
    td = WD / f"{task_id}__{cond}__{model}__t0"
    vault = td / "vault"
    mint = rev = ro = 0
    outcomes = []
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
                    outcomes.append(r.get("outcome") or r.get("pull_failure_category"))
    ab_vals = []
    ex = vault / "executions"
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
                        ab_vals.append(r.get("harness_attempts_before"))
    return mint, rev, ro, outcomes, ab_vals


def main() -> None:
    plan = [("access-01-policy-read-low", "pull"),
            ("mcp-01-lookup3", "pull"),
            ("mcp-02-lookup4", "push")]
    print("=== v0.9.36 SMOKE (one process; tests Bug 9 reconcile/revoke/MCP-teardown + Bug 10 attempts_before) ===")
    for tid, cond in plan:
        rec = run_trial(T[tid], cond, "gemini_3_flash", 0, workdir_root=WD)
        m, rv, ro, outs, ab = ledger_and_audit("gemini_3_flash", tid, cond)
        print(f"\n{tid} {cond}: oracle={rec.oracle_passed} status={rec.runtime_status} tok={rec.tokens_total}")
        print(f"   ledger: lease-mint={m} lease-revoke={rv} request-outcome={ro} outcomes={outs}")
        print(f"   attempts_before on requests: {ab}")
    print("\n=== EXPECT: access/mcp pull -> revoke>=1 & request-outcome>=1 & attempts_before non-null;"
          " mcp-02 push -> oracle=False (no cross-run leak) ===")


if __name__ == "__main__":
    main()
