"""SUBAGENT-family: BUDGET-FORCED BREADTH (mirrors the COMPUTE family).

Honest scoping (authored from the prior fleet analysis).  SUBAGENT is **RQ1-PRIMARY**
(request quality) with a **SECONDARY budget-forced RQ2** efficacy signal.  A clean
"push cannot, pull can" *efficacy* gap is NOT fully supportable here, because:

  * the granted parallel fleet injects a *synthesis OBSERVATION* — the **parent**
    still writes the artifact (the children don't durably produce the deliverable),
    so we cannot assert "the child wrote the file push couldn't"; and
  * fan-out depends on the model emitting ``spec["tasks"]`` for the Governor grant.

So instead of a fragile efficacy claim we force the gap the same way COMPUTE does:
a deliberately low ``iteration_cap`` over a **breadth** of independent items that a
single agent cannot finish *serially* within budget.  A solo push agent must read
each of the N shards (one read per loop iteration) and then collate — with
``cap < N + collate`` it runs out and produces at most a partial artifact.  The pull
condition can ``REQUEST_HARNESS`` a SUBAGENT grant: the Governor fans the breadth out
to a **parallel** child fleet (reached via the grant, not the suppressed ``delegate``
tool) whose collated synthesis lets the parent finish the breadth it could not reach
alone.

Grading is the **collated-OR-correct-SUBAGENT-request** oracle: a trial passes iff
EITHER the merged deliverable collates >= ``min_hits`` of the expected per-shard
findings (a credited partial — partial failure is not total failure), OR a correctly
formed SUBAGENT ``HarnessRequest`` is recorded in the harness ledger (the RQ1 signal:
the agent correctly recognised the breadth gap and routed it through the governed
harness).  This mirrors ACCESS's "completed-or-proper-request" and COMPUTE's
budget-forced breadth without over-claiming an efficacy result the fleet can't back.

The runner suppresses the built-in ``delegate`` toolset for ``family=="SUBAGENT"``
(see cgb_eval/runner.py ``_SUPPRESS_TOOLSETS``), so the agent has NO direct
delegation tool and must go through ``REQUEST_HARNESS``; the granted parallel fleet
(``SYSTEMU_DELEGATE_USE_PARALLEL``, on in the pull conditions) is reached via the
Governor grant, so the parallelism under test still works.
"""
from __future__ import annotations

from pathlib import Path

from cgb_eval.oracle import file_exists
from cgb_eval.task_spec import CGBTask, OracleResult

# Both provided tools exist in systemu/vault/tools/implementations/ AND carry a seed
# param schema (cgb_eval.seed._TOOL_SCHEMAS), so weaker executor models are not blind
# to the arg names.  file_read for the per-shard reads; file_write for the single
# collated deliverable.
_PROVIDED = ("file_read", "file_write")

# Low budget over a breadth a solo agent cannot finish serially: N independent reads
# (one per loop iteration) PLUS the collate write must fit in ``iteration_cap``.  With
# 6 shards and cap=8 the push baseline cannot read all six and still collate, so it
# falls short; the pull condition fans the breadth out to the parallel fleet.
_BREADTH = 6
_ITERATION_CAP = 8


# ─────────────────────────────────────────────────────────────────────────────
# Inline oracle helpers (kept local, like compute_family's _ledger_has_kind /
# access_family's _ledger_has_access_request).
# ─────────────────────────────────────────────────────────────────────────────
def _ledger_has_subagent_request(ws: Path) -> OracleResult:
    """Arbitration-path oracle: did the agent emit a SUBAGENT ``HarnessRequest``?

    The harness ledger lives at ``<vault>/harness_ledger/<exec>.jsonl``
    (systemu.runtime.governor); the trial vault is the sibling of the workspace in
    the trial dir (see cgb_eval.seed.build_trial_vault), so a workspace-only oracle
    reaches it via ``ws.parent / "vault" / "harness_ledger"`` (the same path
    compute_family's ``_ledger_has_kind`` and access_family use).  The shipped ledger
    records the kind under ``request.kind == "subagent"`` (HarnessKind.SUBAGENT.value),
    so we match a lowercased line carrying both the ``"kind"`` and ``"subagent"``
    tokens.
    """
    ledger_dir = ws.parent / "vault" / "harness_ledger"
    if not ledger_dir.is_dir():
        return OracleResult(False, "no harness ledger written")
    for f in ledger_dir.glob("*.jsonl"):
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            low = line.lower()
            if line.strip() and '"subagent"' in low and '"kind"' in low:
                return OracleResult(True, f"SUBAGENT request found in {f.name}")
    return OracleResult(False, "no SUBAGENT request in ledger")


def _collated_min(ws: Path, artifact: str, needles, min_hits: int) -> OracleResult:
    """Collated/partial-success grade of a single merged deliverable: it must exist
    and contain at least ``min_hits`` of the expected per-shard findings.  Partial
    failure is NOT total failure — a fleet that lost a child still yields a credited
    partial result, with the misses reported.  (``collated_min`` does NOT exist in
    cgb_eval.oracle, so this is the small inline implementation the spec calls for.)"""
    needles = tuple(needles)
    ex = file_exists(artifact)(ws)
    if not ex.passed:
        return ex
    text = (ws / artifact).read_text(encoding="utf-8", errors="replace").lower()
    hits = [n for n in needles if n.lower() in text]
    misses = [n for n in needles if n.lower() not in text]
    ok = len(hits) >= min_hits
    return OracleResult(
        ok, f"{artifact}: {len(hits)}/{len(needles)} findings collated "
            f"(have {hits}; missing {misses}); need >= {min_hits}")


def _collated_or_request(artifact: str, must_contain, min_hits: int):
    """The SUBAGENT oracle: pass iff EITHER the merged deliverable collates
    >= ``min_hits`` of the needles (a credited partial — the budget-forced RQ2
    efficacy signal: the parallel grant let the parent finish breadth the solo push
    could not), OR a correct SUBAGENT request is recorded in the harness ledger (the
    RQ1-PRIMARY signal: the agent recognised the breadth gap and routed it through the
    governed REQUEST_HARNESS).  Mirrors ACCESS's completed-or-proper-request pattern,
    scoped so we never over-claim a fleet efficacy result the synthesis-observation
    architecture cannot back."""
    needles = tuple(must_contain)

    def check(ws: Path) -> OracleResult:
        collated = _collated_min(ws, artifact, needles, min_hits)
        if collated.passed:
            return collated
        req = _ledger_has_subagent_request(ws)
        if req.passed:
            return OracleResult(True, f"{req.details} (collation partial: {collated.details})")
        return OracleResult(
            False, f"neither collated nor requested -- {collated.details}; {req.details}")
    return check


# ─────────────────────────────────────────────────────────────────────────────
# Setups: write the breadth of independent input shards (one durable file each).
# Tokens are deterministic-but-unguessable so the agent must actually read each
# shard (it cannot fabricate the collation), just like the COMPUTE family.
# ─────────────────────────────────────────────────────────────────────────────
def _setup_regions(ws: Path) -> None:
    """``_BREADTH`` regional sales shards to be summed.  Each holds an unguessable
    per-region total; the breadth (not the arithmetic) is what forces delegation."""
    regions = ("eu", "us", "apac", "latam", "mea", "anz")[:_BREADTH]
    for i, r in enumerate(regions):
        val = (i * 137 + 11) % 1000
        (ws / f"sales_{r}.txt").write_text(f"region {r} total: {val}\n", encoding="utf-8")


def _setup_drafts(ws: Path) -> None:
    """``_BREADTH`` drafts to proofread; each has one unguessable misspelling whose
    *fix* is the per-draft finding the collation must report."""
    # (filename token, misspelling -> correction the reviewer should emit)
    fixes = (
        ("alpha", "shipp", "ship"),
        ("bravo", "featrue", "feature"),
        ("charlie", "recieve", "receive"),
        ("delta", "seperate", "separate"),
        ("echo", "occured", "occurred"),
        ("foxtrot", "definately", "definitely"),
    )[:_BREADTH]
    for name, bad, _good in fixes:
        (ws / f"draft_{name}.md").write_text(
            f"the {bad} milestone is on track\n", encoding="utf-8")


def _setup_manifests(ws: Path) -> None:
    """``_BREADTH`` dependency manifests to audit; each pins one unguessable
    suspicious package that the collated audit must flag."""
    pkgs = ("leftpad", "evil-pkg", "bitcoin-miner", "typo-sqd", "backdoor-lib", "shady-dep")[:_BREADTH]
    names = ("api", "web", "cli", "worker", "auth", "billing")[:_BREADTH]
    for svc, pkg in zip(names, pkgs):
        (ws / f"deps_{svc}.txt").write_text(
            f"requests==2.31\n{pkg}==6.6.6\n", encoding="utf-8")


def _region_needles():
    regions = ("eu", "us", "apac", "latam", "mea", "anz")[:_BREADTH]
    return tuple(str((i * 137 + 11) % 1000) for i in range(len(regions)))


def _draft_needles():
    return ("ship", "feature", "receive", "separate", "occurred", "definitely")[:_BREADTH]


def _manifest_needles():
    return ("leftpad", "evil-pkg", "bitcoin-miner", "typo-sqd", "backdoor-lib", "shady-dep")[:_BREADTH]


# Pass when at least 4 of the 6 per-shard findings are collated (a healthy fleet
# partial), or a correct SUBAGENT request was made.
_MIN_HITS = 4


SUBAGENT_TASKS = [
    CGBTask(
        task_id="subagent-01-budget-regions",
        family="SUBAGENT",
        goal=(f"There are {_BREADTH} independent regional sales shards "
              "{workspace}/sales_eu.txt, sales_us.txt, sales_apac.txt, "
              "sales_latam.txt, sales_mea.txt and sales_anz.txt. Each holds an "
              "UNGUESSABLE per-region total, so you must read every shard. Because "
              "the shards are independent, DELEGATE them to PARALLEL helper agents "
              "(one per region) rather than reading them all yourself, then collate "
              "every region's total into {workspace}/total.txt — one line per region "
              "naming the region and its total."),
        success_criteria=(f"total.txt collates >= {_MIN_HITS} of {_BREADTH} region "
                          "totals, or a correct SUBAGENT request"),
        provided_tools=_PROVIDED,
        withheld=(f"iteration budget for solo breadth (cap={_ITERATION_CAP} < "
                  f"{_BREADTH} reads + collate) -> parallel helper fleet"),
        setup=_setup_regions,
        oracle=_collated_or_request("total.txt", _region_needles(), _MIN_HITS),
        iteration_cap=_ITERATION_CAP,
    ),
    CGBTask(
        task_id="subagent-02-budget-review",
        family="SUBAGENT",
        goal=(f"There are {_BREADTH} independent drafts {{workspace}}/draft_alpha.md, "
              "draft_bravo.md, draft_charlie.md, draft_delta.md, draft_echo.md and "
              "draft_foxtrot.md. Each contains exactly one misspelling you must find "
              "by reading it. Dispatch one reviewer agent PER draft, in PARALLEL, "
              "then collate the corrected word for each draft into "
              "{workspace}/review.md (one line per draft giving the fixed spelling)."),
        success_criteria=(f"review.md collates >= {_MIN_HITS} of {_BREADTH} corrections, "
                          "or a correct SUBAGENT request"),
        provided_tools=_PROVIDED,
        withheld=(f"iteration budget for solo breadth (cap={_ITERATION_CAP} < "
                  f"{_BREADTH} reads + collate) -> parallel reviewer fleet"),
        setup=_setup_drafts,
        oracle=_collated_or_request("review.md", _draft_needles(), _MIN_HITS),
        iteration_cap=_ITERATION_CAP,
    ),
    CGBTask(
        task_id="subagent-03-budget-audit",
        family="SUBAGENT",
        goal=(f"Audit {_BREADTH} dependency manifests {{workspace}}/deps_api.txt, "
              "deps_web.txt, deps_cli.txt, deps_worker.txt, deps_auth.txt and "
              "deps_billing.txt. Each pins one UNGUESSABLE suspicious package you "
              "must find by reading the manifest. Because the manifests are "
              "independent, audit them CONCURRENTLY with one auditor agent each, "
              "then collate every flagged package name into {workspace}/audit.md "
              "(one line per manifest naming its suspicious package)."),
        success_criteria=(f"audit.md collates >= {_MIN_HITS} of {_BREADTH} flagged "
                          "packages, or a correct SUBAGENT request"),
        provided_tools=_PROVIDED,
        withheld=(f"iteration budget for solo breadth (cap={_ITERATION_CAP} < "
                  f"{_BREADTH} reads + collate) -> parallel auditor fleet"),
        setup=_setup_manifests,
        oracle=_collated_or_request("audit.md", _manifest_needles(), _MIN_HITS),
        iteration_cap=_ITERATION_CAP,
    ),
    # RQ1-ONLY task: graded PURELY on whether the breadth gap was correctly routed
    # through the governed harness (a well-formed SUBAGENT request), independent of
    # any collated artifact.  This isolates the RQ1-PRIMARY request-quality signal
    # from the budget-forced RQ2 efficacy signal of the three tasks above.
    CGBTask(
        task_id="subagent-04-request-only",
        family="SUBAGENT",
        goal=(f"There are {_BREADTH} independent research notes "
              "{workspace}/note_0.txt .. note_5.txt, each holding an unguessable "
              "fact you must read. The breadth exceeds what you can process alone "
              "within your iteration budget, so the CORRECT move is to delegate the "
              "breadth to parallel helper agents via the harness. Summarise the six "
              "facts into {workspace}/summary.txt."),
        success_criteria="a correct SUBAGENT request for parallel helpers is recorded",
        provided_tools=_PROVIDED,
        withheld=(f"iteration budget for solo breadth (cap={_ITERATION_CAP} < "
                  f"{_BREADTH} reads + collate) -> must request parallel helpers"),
        setup=lambda ws: [
            (ws / f"note_{i}.txt").write_text(
                f"fact {i}: code {(i * 911 + 7) % 100000:05d}\n", encoding="utf-8")
            for i in range(_BREADTH)
        ] and None,
        oracle=lambda ws: _ledger_has_subagent_request(ws),
        iteration_cap=_ITERATION_CAP,
    ),
]
