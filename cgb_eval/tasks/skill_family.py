"""SKILL-family: the gap is an *arbitrary procedure* the agent cannot infer.

Design note (REAL gaps, not synthetic). SKILL is the INDIRECT family: granting a
SKILL request persists a ``SKILL.md`` to the vault and surfaces only its PATH as
an observation (the body is NOT injected — see
``shadow_runtime._apply_materialised_grant``: ``"Skill provisioned: <path>. ..."``).
The agent must then read that file and apply it. For this to be a genuine
capability gap (and not a synthetic one the LLM fakes from the goal), the needed
procedure carries an ARBITRARY, externally-specified convention the model cannot
guess from the goal text: the rule lives ONLY in a ``spec.md`` the setup writes,
never in the goal. The expected pull flow is: read ``spec.md`` → codify it as a
skill (``REQUEST_HARNESS`` kind=skill, persisted as ``SKILL.md``) → apply it. The
oracle COMPUTES the exact expected output deterministically from the inputs, so a
loose substring guess cannot pass.

RQ scoping (honest). RQ1 (does the agent recognise the gap and request a SKILL)
and "persist-then-load" (was a ``SKILL.md`` actually materialised and applied)
are the PRIMARY signals here. RQ2 (recovery efficacy) is WEAK for SKILL: unlike
TOOL/COMPUTE, the push baseline is not categorically blocked — it can also read
``spec.md`` and, in principle, apply the rule by hand. We therefore do not claim
strong RQ2 efficacy for this family; the value is that the arbitrary convention
makes guessing implausible and makes "did a skill get persisted+loaded" a real,
measurable event rather than a synthetic restatement of the goal. One task
(``skill-04``) adds a 2-arg ``(ws, vault)`` oracle clause that additionally
checks a ``SKILL.md`` was actually persisted under ``<vault.root>/skills/``, to
instrument persist-then-load directly.
"""
from __future__ import annotations

import csv
import io
from pathlib import Path

from cgb_eval.oracle import (
    Check, all_of, file_contains, file_exists, json_field_equals,
)
from cgb_eval.task_spec import CGBTask, OracleResult

_PROVIDED = ("file_read", "file_write")


# ─────────────────────────── inline helpers ────────────────────────────────
def not_file_contains(rel: str, needle: str) -> Check:
    """Pass iff ``rel`` exists and does NOT contain ``needle`` (case-insensitive).

    Used for "keep-these-unchanged / must-NOT-leak" clauses: the redaction or
    filter rule from spec.md requires certain tokens to be absent from the
    output, which a model that ignored the procedure would leave in place."""
    def check(ws: Path) -> OracleResult:
        p = ws / rel
        if not p.is_file():
            return OracleResult(False, f"{rel} MISSING")
        body = p.read_text(encoding="utf-8", errors="replace").lower()
        ok = needle.lower() not in body
        return OracleResult(ok, f"{rel} {'omits' if ok else 'STILL CONTAINS'} {needle!r}")
    return check


def fixed_width_lines_match(input_rel: str, output_rel: str,
                            widths=(12, 8, 10), pad: str = " ") -> Check:
    """Deterministic SKILL oracle: recompute the fixed-width export of a CSV and
    require the output to reproduce it EXACTLY, in order.

    The fixed-width *format* (column order, per-column widths, padding side,
    rounding) is the arbitrary convention documented only in ``spec.md`` — it is
    not stated in the goal, so it cannot be inferred. The export rule used here
    (and written verbatim into ``spec.md`` by the setup):

      * skip the CSV header row;
      * for each data row, emit ``name``, ``qty``, ``price`` in that order;
      * ``name`` left-justified in ``widths[0]`` (truncated if longer);
      * ``qty`` right-justified in ``widths[1]``;
      * ``price`` formatted to EXACTLY two decimals, right-justified in
        ``widths[2]``;
      * columns concatenated with NO separator; one line per row.

    The oracle recomputes every expected line from the actual CSV, so the answer
    is unguessable; only a byte-faithful fixed-width export passes."""
    def _expected_lines(src: Path):
        rows = list(csv.reader(io.StringIO(
            src.read_text(encoding="utf-8", errors="replace"))))
        out = []
        for row in rows[1:]:  # skip header
            if len(row) < 3 or not any(c.strip() for c in row):
                continue
            name, qty, price = row[0].strip(), row[1].strip(), row[2].strip()
            try:
                price_s = f"{float(price):.2f}"
            except ValueError:
                price_s = price
            line = (name[:widths[0]].ljust(widths[0], pad)
                    + qty.rjust(widths[1], pad)
                    + price_s.rjust(widths[2], pad))
            out.append(line)
        return out

    def check(ws: Path) -> OracleResult:
        src, out = ws / input_rel, ws / output_rel
        if not out.is_file():
            return OracleResult(False, f"{output_rel} MISSING")
        if not src.is_file():
            return OracleResult(False, f"{input_rel} MISSING (cannot compute expected)")
        expected = _expected_lines(src)
        got = [ln for ln in out.read_text(encoding="utf-8", errors="replace")
               .splitlines() if ln.strip("\r\n")]
        if got == expected:
            return OracleResult(True, f"{output_rel} matches {len(expected)} fixed-width lines")
        for i, exp in enumerate(expected):
            if i >= len(got) or got[i] != exp:
                actual = got[i] if i < len(got) else "<missing>"
                return OracleResult(
                    False, f"{output_rel} line {i} = {actual!r} (want {exp!r})")
        return OracleResult(False, f"{output_rel} has {len(got)} lines (want {len(expected)})")
    return check


def skill_md_persisted(vault) -> OracleResult:
    """2-arg clause helper: a ``SKILL.md`` was materialised under the vault.

    The Governor SKILL provisioner writes ``<vault.root>/skills/<name>/SKILL.md``
    (governor._provision_skill → auto_skill_extractor.persist_skill_candidate).
    ``vault`` is None when called by the 1-arg registry test on an empty
    workspace — that correctly fails (no skill was persisted)."""
    if vault is None:
        return OracleResult(False, "no vault (no SKILL.md persisted)")
    root = getattr(vault, "root", None)
    if root is None:
        return OracleResult(False, "vault has no root")
    hits = list(Path(root).glob("skills/*/SKILL.md"))
    return OracleResult(bool(hits),
                        f"SKILL.md persisted: {[p.parent.name for p in hits]}"
                        if hits else "no SKILL.md under <vault>/skills/")


# ─────────────────────────────── setups ────────────────────────────────────
# Each setup writes the INPUT plus a spec.md whose composite rule is the ONLY
# place the arbitrary convention appears (it is deliberately absent from goals).

def _setup_release_notes(ws: Path) -> None:
    (ws / "spec.md").write_text(
        "# Release-Note Bump Procedure (apply EXACTLY)\n\n"
        "Read commits.txt (one `type: subject` per line). Classify each commit\n"
        "by its prefix and bump the version from the CURRENT version 2.4.7 by\n"
        "PRECEDENCE (the single highest-ranked category present wins the whole\n"
        "release; lower categories do NOT also bump):\n\n"
        "  1. `breaking:`  -> MAJOR bump (x+1.0.0)  [highest precedence]\n"
        "  2. `feat:`      -> MINOR bump (x.y+1.0)\n"
        "  3. `fix:`       -> PATCH bump (x.y.z+1)  [lowest precedence]\n\n"
        "Commits with any other prefix (e.g. `chore:`, `docs:`) are IGNORED for\n"
        "version bumping AND are NOT counted.\n\n"
        "Write release.json with keys: next_version (string), breaking (int\n"
        "count), feat (int count), fix (int count). Counts are of COUNTED\n"
        "commits only (ignored prefixes contribute 0).\n",
        encoding="utf-8")
    # 1 breaking, 2 feat, 2 fix, 2 ignored -> precedence = breaking => MAJOR
    # next_version = 3.0.0 ; counts breaking=1 feat=2 fix=2
    (ws / "commits.txt").write_text(
        "fix: guard against empty vault\n"
        "feat: add CSV export\n"
        "chore: bump deps\n"
        "breaking: drop legacy v1 API\n"
        "feat: dark mode dashboard\n"
        "docs: update readme\n"
        "fix: correct off-by-one in pager\n",
        encoding="utf-8")


def _setup_log_triage(ws: Path) -> None:
    (ws / "spec.md").write_text(
        "# Log-Triage Severity Scoring (apply EXACTLY)\n\n"
        "Read events.txt (one `LEVEL message` per line). Compute a weighted\n"
        "severity score. The WEIGHT-PER-OCCURRENCE depends on the level, and the\n"
        "trap is that rarer high-weight levels dominate frequent low-weight ones:\n\n"
        "  CRITICAL = 50 points each\n"
        "  ERROR    =  8 points each\n"
        "  WARN     =  2 points each\n"
        "  INFO     =  0 points each (ignored)\n\n"
        "score = sum over all lines of (weight for that line's level).\n"
        "Also report the dominant level = the level contributing the MOST total\n"
        "points (NOT the most frequent level). Ties broken by higher per-event\n"
        "weight.\n\n"
        "Write triage.json with keys: score (int) and dominant (string, the\n"
        "UPPERCASE level name).\n",
        encoding="utf-8")
    lines = []
    lines += ["INFO  started ok"] * 12          # 0 pts, most FREQUENT (the trap)
    lines += ["WARN  retry backoff"] * 9        # 18 pts
    lines += ["ERROR upstream 500"] * 4         # 32 pts
    lines += ["CRITICAL disk full"] * 1         # 50 pts -> dominant by POINTS
    # score = 0 + 18 + 32 + 50 = 100 ; dominant = CRITICAL (not INFO/WARN)
    (ws / "events.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _setup_fixed_width(ws: Path) -> None:
    (ws / "spec.md").write_text(
        "# Fixed-Width Inventory Export (apply EXACTLY)\n\n"
        "Read inventory.csv (header: name,qty,price). Skip the header. For each\n"
        "data row emit ONE line concatenating three columns with NO separators:\n\n"
        "  * name  : left-justified, width 12 (truncate if longer), pad spaces\n"
        "  * qty   : right-justified, width 8, pad spaces\n"
        "  * price : formatted to EXACTLY two decimals, then right-justified,\n"
        "            width 10, pad spaces\n\n"
        "Preserve CSV row order. Write the result to export.txt (one line per\n"
        "data row, trailing newline).\n",
        encoding="utf-8")
    (ws / "inventory.csv").write_text(
        "name,qty,price\n"
        "widget,9,3.5\n"
        "supergadget-deluxe,120,19.999\n"   # name truncated to 12; price -> 20.00
        "bolt,0,0.1\n",
        encoding="utf-8")


def _setup_redaction(ws: Path) -> None:
    (ws / "spec.md").write_text(
        "# PII Redaction Policy (apply EXACTLY)\n\n"
        "Read record.txt and write redacted.txt applying THIS policy verbatim:\n\n"
        "  * Replace every email address with the literal token [EMAIL].\n"
        "  * Replace every 10-digit phone number with the literal token [PHONE].\n"
        "  * KEEP-UNCHANGED clause: the order id (the token of the form\n"
        "    ORD-#####) and the public support address support@corp.example MUST\n"
        "    be preserved EXACTLY and must NOT be redacted, even though the\n"
        "    latter is an email. (This carve-out is the non-obvious part.)\n\n"
        "All other text is copied through unchanged.\n",
        encoding="utf-8")
    (ws / "record.txt").write_text(
        "Customer jane.doe@gmail.com placed order ORD-48217. "
        "Reach her at 5551234567. "
        "For help contact support@corp.example anytime.\n",
        encoding="utf-8")


# ─────────────────────────── oracle clause builders ────────────────────────
def _release_oracle() -> Check:
    return all_of(
        file_exists("release.json"),
        json_field_equals("release.json", "next_version", "3.0.0"),
        json_field_equals("release.json", "breaking", 1),
        json_field_equals("release.json", "feat", 2),
        json_field_equals("release.json", "fix", 2),
    )


def _triage_oracle() -> Check:
    return all_of(
        file_exists("triage.json"),
        json_field_equals("triage.json", "score", 100),
        json_field_equals("triage.json", "dominant", "CRITICAL"),
    )


def _fixed_width_oracle() -> Check:
    return all_of(
        file_exists("export.txt"),
        fixed_width_lines_match("inventory.csv", "export.txt"),
    )


def _redaction_oracle() -> Check:
    """1-arg artifact clauses + a 2-arg persist-then-load clause.

    Declaring ``vault=None`` keeps the oracle callable with a single argument
    (the registry test) while the runner — which dispatches on arity >= 2 —
    passes the real vault so the SKILL.md-persisted clause can fire."""
    artifact = all_of(
        file_exists("redacted.txt"),
        not_file_contains("redacted.txt", "jane.doe@gmail.com"),  # private email redacted
        not_file_contains("redacted.txt", "5551234567"),           # phone redacted
        file_contains("redacted.txt", "[EMAIL]"),
        file_contains("redacted.txt", "[PHONE]"),
        file_contains("redacted.txt", "ORD-48217"),                # kept-unchanged order id
        file_contains("redacted.txt", "support@corp.example"),     # kept-unchanged carve-out
    )

    def check(ws: Path, vault=None) -> OracleResult:
        a = artifact(ws)
        s = skill_md_persisted(vault)
        # Persist-then-load: require BOTH the correct artifact AND evidence a
        # SKILL.md was materialised. (When vault is None — the 1-arg registry
        # probe on an empty workspace — this fails, as it must.)
        return OracleResult(a.passed and s.passed, f"{a.details}; {s.details}")
    return check


# ─────────────────────────────── tasks ─────────────────────────────────────
SKILL_TASKS = [
    CGBTask(
        task_id="skill-01-release-notes",
        family="SKILL",
        goal=("Follow the documented release-note procedure in "
              "{workspace}/spec.md to process {workspace}/commits.txt. Apply the "
              "procedure exactly and write the result to {workspace}/release.json."),
        success_criteria="release.json matches the spec-computed bump + counts",
        provided_tools=_PROVIDED,
        withheld="the release-note bump-precedence procedure (skill)",
        setup=_setup_release_notes,
        oracle=_release_oracle(),
    ),
    CGBTask(
        task_id="skill-02-log-triage",
        family="SKILL",
        goal=("Follow the documented log-triage scoring procedure in "
              "{workspace}/spec.md to process {workspace}/events.txt, then write "
              "the computed result to {workspace}/triage.json."),
        success_criteria="triage.json score + dominant level match the spec weighting",
        provided_tools=_PROVIDED,
        withheld="the weighted log-triage scoring procedure (skill)",
        setup=_setup_log_triage,
        oracle=_triage_oracle(),
    ),
    CGBTask(
        task_id="skill-03-fixed-width",
        family="SKILL",
        goal=("Follow the documented fixed-width export procedure in "
              "{workspace}/spec.md to convert {workspace}/inventory.csv, writing "
              "the result to {workspace}/export.txt."),
        success_criteria="export.txt reproduces the exact spec-defined fixed-width layout",
        provided_tools=_PROVIDED,
        withheld="the fixed-width export format procedure (skill)",
        setup=_setup_fixed_width,
        oracle=_fixed_width_oracle(),
    ),
    CGBTask(
        task_id="skill-04-pii-redaction",
        family="SKILL",
        goal=("Follow the documented PII-redaction policy in {workspace}/spec.md "
              "to process {workspace}/record.txt, writing the redacted output to "
              "{workspace}/redacted.txt."),
        success_criteria=("redacted.txt obeys the redact/keep-unchanged policy AND "
                          "a SKILL.md was persisted (persist-then-load)"),
        provided_tools=_PROVIDED,
        withheld="the PII-redaction policy with keep-unchanged carve-outs (skill)",
        setup=_setup_redaction,
        oracle=_redaction_oracle(),
    ),
]
