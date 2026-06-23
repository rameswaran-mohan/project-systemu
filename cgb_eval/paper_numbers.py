"""Combine all five per-model CGB result files and emit EVERY number paper section 5
needs (RQ1-RQ5), with bootstrap CIs and a POOLED exact-McNemar test.

Reads cgb_results/v09344_<model>.jsonl for the five scored models, prints a human
report, and writes cgb_results/paper_numbers.json.  Stdlib + cgb_eval.analysis only.

Run:  python -m cgb_eval.paper_numbers
"""
from __future__ import annotations

import json
from pathlib import Path
from statistics import mean
from typing import Dict, List

from cgb_eval.analysis import (bootstrap_ci, mcnemar_exact_p, summarize,
                               _summarize_pull_decision)

MODELS = ["gemini_3_flash", "deepseek_v4_pro", "gpt_5_4", "claude_opus_4_8", "glm_5_2"]
FAMILIES = ["tool", "skill", "access", "compute", "subagent", "mcp"]
CONDS = ["push", "pull", "pull_min_governance"]


def _load_all() -> List[dict]:
    recs: List[dict] = []
    for m in MODELS:
        p = Path(f"cgb_results/v0941_{m}.jsonl")
        if p.exists():
            recs += [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()
                     if l.strip()]
    return recs


def _fam(r: dict) -> str:
    # family is stored UPPERCASE on the record (task.family); normalize to the
    # lowercase keys used in FAMILIES / task_id prefixes.
    return (r.get("family") or r["task_id"].split("-")[0]).lower()


def _succ_ci(sub: List[dict]):
    """k/n and a bootstrap CI on the success proportion."""
    vals = [1.0 if r["oracle_passed"] else 0.0 for r in sub]
    k = int(sum(vals)); n = len(vals)
    rate = (k / n) if n else 0.0
    ci = bootstrap_ci(vals) if n > 1 else (rate, rate)
    return k, n, rate, ci


def _pooled_mcnemar(recs: List[dict], cond_b: str = "pull", cond_a: str = "push"):
    """Pool discordant pairs across ALL (task, model, trial); one exact McNemar.

    b = cond_b-only successes, c = cond_a-only successes.  With breadth-first single
    trials the per-(task,model) test is degenerate (n<=1), so the POOLED test is the
    only one with power; we report it as the aggregate and disclose the pooling.
    """
    idx = {(r["task_id"], r["model_profile"], r["condition"], r["trial"]): r
           for r in recs}
    pairs = {(r["task_id"], r["model_profile"], r["trial"]) for r in recs}
    b = c = both = neither = n_pairs = 0
    recovered = 0; push_fail = 0
    for (task, model, t) in pairs:
        pa = idx.get((task, model, cond_a, t))
        pb = idx.get((task, model, cond_b, t))
        if not pa or not pb:
            continue
        n_pairs += 1
        ap, bp = bool(pa["oracle_passed"]), bool(pb["oracle_passed"])
        if bp and not ap:
            b += 1
        elif ap and not bp:
            c += 1
        elif ap and bp:
            both += 1
        else:
            neither += 1
        if not ap:                       # push failed → eligible for recovery
            push_fail += 1
            if bp:
                recovered += 1
    return {
        "cond_b": cond_b, "cond_a": cond_a, "n_pairs": n_pairs,
        "b_only_b": b, "c_only_a": c, "both": both, "neither": neither,
        "mcnemar_p": mcnemar_exact_p(b, c),
        "push_fail": push_fail, "recovered": recovered,
        "recovery_rate": (recovered / push_fail) if push_fail else 0.0,
    }


def main() -> None:
    recs = _load_all()
    print(f"=== loaded {len(recs)} trials across {len({r['model_profile'] for r in recs})} models ===\n")

    out: Dict[str, object] = {"n_trials": len(recs)}

    # ---- overall summarize (RQ1 overall + by_condition efficacy/cost) ----------
    summ = summarize(recs)
    # JSON-safe copy: paired dict has tuple keys -> flatten to "task|model".
    summ_json = dict(summ)
    summ_json["paired"] = {f"{t}|{m}": v for (t, m), v in summ["paired"].items()}
    out["overall"] = summ_json

    # ---- RQ2 efficacy per condition: k/n, CI, + pooled recovery & McNemar -------
    print("### RQ2 — EFFICACY (per condition, all models pooled)")
    cond_eff = {}
    for cond in CONDS:
        sub = [r for r in recs if r["condition"] == cond]
        k, n, rate, ci = _succ_ci(sub)
        cond_eff[cond] = {"k": k, "n": n, "rate": rate, "ci": ci}
        print(f"  {cond:20s} {k:3d}/{n:3d} = {rate*100:5.1f}%  CI[{ci[0]*100:.1f},{ci[1]*100:.1f}]")
    pull_vs_push = _pooled_mcnemar(recs, "pull", "push")
    pmg_vs_push = _pooled_mcnemar(recs, "pull_min_governance", "push")
    out["rq2"] = {"cond_eff": cond_eff, "pull_vs_push": pull_vs_push, "pmg_vs_push": pmg_vs_push}
    print(f"  pull  vs push : pairs={pull_vs_push['n_pairs']} b(pull-only)={pull_vs_push['b_only_b']} "
          f"c(push-only)={pull_vs_push['c_only_a']} both={pull_vs_push['both']} neither={pull_vs_push['neither']} "
          f"McNemar p={pull_vs_push['mcnemar_p']:.2e}  recovery={pull_vs_push['recovered']}/{pull_vs_push['push_fail']}="
          f"{pull_vs_push['recovery_rate']*100:.1f}%")
    print(f"  pmin  vs push : pairs={pmg_vs_push['n_pairs']} b={pmg_vs_push['b_only_b']} c={pmg_vs_push['c_only_a']} "
          f"McNemar p={pmg_vs_push['mcnemar_p']:.2e}  recovery={pmg_vs_push['recovery_rate']*100:.1f}%\n")

    # ---- RQ3 cost per condition ------------------------------------------------
    print("### RQ3 — COST (per condition)")
    for cond, s in summ["by_condition"].items():
        lo, hi = s["token_ci"]
        print(f"  {cond:20s} tok_mean={s['token_mean']:.0f} CI[{lo:.0f},{hi:.0f}] calls={s['llm_calls_mean']:.1f}")
    pd_all = summ["pull_decision"]
    print(f"  decided_by  deterministic={pd_all['decided_by_det']}  llm={pd_all['decided_by_llm']}\n")

    # ---- RQ1 overall + per family ----------------------------------------------
    print("### RQ1 — PULL-DECISION QUALITY (overall + per family; pull conditions only)")
    print(f"  OVERALL: runs={pd_all['n_pull_runs']} reqs={pd_all['requests']} "
          f"prec={pd_all['precision']:.2f} rec={pd_all['recall']:.2f} "
          f"prem={pd_all['premature_rate']:.2f} wasted={pd_all['wasted_rate']:.2f} "
          f"cap={pd_all['cap_exceeded_rate']:.2f} "
          f"unused={pd_all['unused_grant_rate']:.2f} used={pd_all['used_grant_rate']:.2f}")
    rq1_fam = {}
    for fam in FAMILIES:
        sub = [r for r in recs if _fam(r) == fam and r["condition"] != "push"]
        pdf = _summarize_pull_decision(sub)
        rq1_fam[fam] = pdf
        print(f"  {fam:9s} runs={pdf['n_pull_runs']:2d} reqs={pdf['requests']:3d} "
              f"prec={pdf['precision']:.2f} rec={pdf['recall']:.2f} "
              f"prem={pdf['premature_rate']:.2f} wasted={pdf['wasted_rate']:.2f} "
              f"cap={pdf['cap_exceeded_rate']:.2f} "
              f"unused={pdf['unused_grant_rate']:.2f} used={pdf['used_grant_rate']:.2f}")
    out["rq1_overall"] = pd_all
    out["rq1_family"] = rq1_fam
    print()

    # ---- RQ5 per-family efficacy (push vs pull vs pmg) + per-family recovery ----
    print("### RQ5 — PER-FAMILY EFFICACY (k/n by condition; CI on pull) + recovery")
    fam_eff = {}
    for fam in FAMILIES:
        row = {}
        for cond in CONDS:
            sub = [r for r in recs if _fam(r) == fam and r["condition"] == cond]
            k, n, rate, ci = _succ_ci(sub)
            row[cond] = {"k": k, "n": n, "rate": rate, "ci": ci}
        fam_recs = [r for r in recs if _fam(r) == fam]
        rec_pp = _pooled_mcnemar(fam_recs, "pull", "push")
        row["recovery"] = rec_pp
        fam_eff[fam] = row
        pu = row["pull"]
        print(f"  {fam:9s} push {row['push']['k']}/{row['push']['n']:<2d}  "
              f"pull {pu['k']}/{pu['n']:<2d}={pu['rate']*100:5.1f}% CI[{pu['ci'][0]*100:.0f},{pu['ci'][1]*100:.0f}]  "
              f"pmg {row['pull_min_governance']['k']}/{row['pull_min_governance']['n']}  "
              f"recov {rec_pp['recovered']}/{rec_pp['push_fail']} b={rec_pp['b_only_b']} c={rec_pp['c_only_a']}")
    out["rq5_family_eff"] = fam_eff
    print()

    # ---- RQ1 recognition rate (robust): correct-kind request fired / pull runs --
    # (recomputed here from the audits so the paper number is reproducible)
    from pathlib import Path as _P
    fam_runs = {f: 0 for f in FAMILIES}
    fam_reqruns = {f: 0 for f in FAMILIES}
    fam_correct = {f: 0 for f in FAMILIES}
    for wd in _P("cgb_results").glob("v0941_*_work"):
        for td in wd.iterdir():
            if not td.is_dir():
                continue
            parts = td.name.split("__")
            if len(parts) < 4 or parts[1] == "push":
                continue
            fam = parts[0].split("-")[0]
            if fam not in fam_runs:
                continue
            ex = td / "vault" / "executions"
            if not ex.is_dir():
                continue
            fam_runs[fam] += 1
            requested = correct = False
            for exd in ex.iterdir():
                aud = exd / "decision_audit.jsonl"
                if not aud.is_file():
                    continue
                for ln in aud.read_text(encoding="utf-8", errors="replace").splitlines():
                    if not ln.strip():
                        continue
                    try:
                        r = json.loads(ln)
                    except Exception:
                        continue
                    if r.get("is_request_harness"):
                        requested = True
                        k = r.get("harness_kind")
                        if k and k.lower() == fam:
                            correct = True
            if requested:
                fam_reqruns[fam] += 1
            if correct:
                fam_correct[fam] += 1
    print("### RQ1 — RECOGNITION (correct-kind request fired / pull runs) [ROBUST]")
    rq1_recog = {}
    tot_runs = tot_correct = 0
    for fam in FAMILIES:
        n = fam_runs[fam]; c = fam_correct[fam]
        rq1_recog[fam] = {"runs": n, "req_runs": fam_reqruns[fam], "correct_kind": c,
                          "recognition": (c / n) if n else 0.0}
        tot_runs += n; tot_correct += c
        print(f"  {fam:9s} runs={n:2d} req-firing={fam_reqruns[fam]:2d} correct-kind={c:2d} "
              f"recognition={100*c/n if n else 0:5.1f}%")
    rq1_recog["overall"] = {"runs": tot_runs, "correct_kind": tot_correct,
                            "recognition": (tot_correct / tot_runs) if tot_runs else 0.0}
    print(f"  OVERALL    runs={tot_runs} correct-kind={tot_correct} "
          f"recognition={100*tot_correct/tot_runs:.1f}%")
    out["rq1_recognition"] = rq1_recog
    print()

    # ---- Per-model breakdown (for prose) --------------------------------------
    print("### PER-MODEL (push vs pull success)")
    per_model = {}
    for m in MODELS:
        row = {}
        for cond in CONDS:
            sub = [r for r in recs if r["model_profile"] == m and r["condition"] == cond]
            k, n, rate, _ = _succ_ci(sub)
            row[cond] = {"k": k, "n": n, "rate": rate}
        per_model[m] = row
        print(f"  {m:18s} push {row['push']['k']}/{row['push']['n']:<2d}  "
              f"pull {row['pull']['k']}/{row['pull']['n']:<2d}  "
              f"pmg {row['pull_min_governance']['k']}/{row['pull_min_governance']['n']}")
    out["per_model"] = per_model

    Path("cgb_results/paper_numbers.json").write_text(
        json.dumps(out, indent=2, default=str), encoding="utf-8")
    print("\n-> wrote cgb_results/paper_numbers.json")


if __name__ == "__main__":
    main()
