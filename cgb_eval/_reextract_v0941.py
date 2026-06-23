"""Re-extract pull_decision for the v0.9.41 run from the PERSISTED per-trial vaults
with the CURRENT taxonomy code (cgb_eval.pull_decision), so every cell's
pull-failure taxonomy is computed uniformly.

WHY: the v0.9.41 run was resumed across a pull_decision.py edit (the first-class
cap_exceeded + granted_na additions), so the run-time-captured pull_decision in the
jsonl is a two-version patchwork — only the cells finished after the edit carry
cap_exceeded/granted_na. The governor harness_ledger IS persisted under each pull
cell's vault/, so we can recompute the whole taxonomy from disk and overwrite the
records uniformly. This also makes the §5 taxonomy reproducible from the published
artifact (anyone can re-derive it from the vaults).

PUSH cells keep pull_decision=None (no governor → not a pull run; analysis skips them).
Backs up each jsonl to <file>.prereextract.bak and prints a before/after diff.

Run:  python -m cgb_eval._reextract_v0941
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Dict, List

from cgb_eval.pull_decision import extract_pull_decision

MODELS = ["gemini_3_flash", "deepseek_v4_pro", "gpt_5_4", "claude_opus_4_8", "glm_5_2"]
_SUM_KEYS = ("requests", "blocked_iters", "blocked_then_pulled", "premature", "wasted",
             "unused_grant", "cap_exceeded", "granted_na", "granted_used",
             "granted_unused", "decided_by_det", "decided_by_llm")


def _fam(rec: dict) -> str:
    return (rec.get("family") or rec["task_id"].split("-")[0]).lower()


def _trial_dir(model: str, rec: dict) -> Path:
    return Path(f"cgb_results/v0941_{model}_work") / (
        f"{rec['task_id']}__{rec['condition']}__{rec['model_profile']}__t{rec['trial']}")


def _sum(recs: List[dict], key: str) -> int:
    tot = 0
    for r in recs:
        pd = r.get("pull_decision")
        if pd:
            tot += int(pd.get(key, 0) or 0)
    return tot


def main() -> None:
    grand_before = {k: 0 for k in _SUM_KEYS}
    grand_after = {k: 0 for k in _SUM_KEYS}
    for model in MODELS:
        f = Path(f"cgb_results/v0941_{model}.jsonl")
        if not f.exists():
            print(f"  (skip {model}: no jsonl)")
            continue
        recs = [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]
        before = {k: _sum(recs, k) for k in _SUM_KEYS}

        n_reext = n_noledger = n_missing = 0
        for r in recs:
            if r["condition"] == "push":
                r["pull_decision"] = None          # push is not a pull run
                continue
            td = _trial_dir(model, r)
            vault = td / "vault"
            if not vault.is_dir():
                n_missing += 1
                # leave the run-time record's pull_decision intact (best available)
                continue
            r["pull_decision"] = extract_pull_decision(vault, _fam(r))
            n_reext += 1
            if not (vault / "harness_ledger").is_dir():
                n_noledger += 1

        after = {k: _sum(recs, k) for k in _SUM_KEYS}
        bak = f.with_suffix(".jsonl.prereextract.bak")
        if not bak.exists():
            shutil.copy2(f, bak)
        f.write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")

        print(f"\n=== {model}: re-extracted {n_reext} non-push cells "
              f"({n_noledger} had no ledger, {n_missing} vault missing) ===")
        print(f"  {'key':22s} {'before':>7s} {'after':>7s}")
        for k in _SUM_KEYS:
            mark = "" if before[k] == after[k] else "  <-- changed"
            print(f"  {k:22s} {before[k]:7d} {after[k]:7d}{mark}")
            grand_before[k] += before[k]
            grand_after[k] += after[k]

    print("\n=== GRAND TOTAL (all models) ===")
    print(f"  {'key':22s} {'before':>7s} {'after':>7s}")
    for k in _SUM_KEYS:
        mark = "" if grand_before[k] == grand_after[k] else "  <-- changed"
        print(f"  {k:22s} {grand_before[k]:7d} {grand_after[k]:7d}{mark}")


if __name__ == "__main__":
    main()
