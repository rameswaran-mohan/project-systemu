# R-P3b (slice 2a) — the action ledger: an exportable, tamper-evident record of what the agent did

The next R-P3b piece after spend caps: an **action ledger** — an inspectable,
append-ordered, SIEM-exportable record of every side-effectful thing the agent did,
built as a pure **projection** over already-persisted sources (gate decisions,
receipts, the action audit) with **no new write path** (RUL-7).

## What's in slice 2a (the projection core)

- **One row per committed effect** (AC1): the ledger's spine is the action-audit
  successes — one `effect` row each — each enriched with its run's durable receipt
  (**verified** iff the effect was independently machine-checked, else **claimed** —
  never conflated, DEC-13) and an `evidence_fingerprint` that survives later
  garbage-collection of the evidence body (AC5).
- **Secrets never reach the ledger** (AC4): params are MASK-redacted (the
  external-verifier redactor: `sk-…`/`ghp-…`/keyed secrets) at projection time, and
  only a one-way digest of the *masked* form is stored — so no token appears in a
  row, in the CSV, or in the JSONL export.
- **Byte-stable SIEM export** (AC3): a frozen 25-column CSV and a JSONL export share
  one canonical encoder, with fixed-width timestamps so re-exporting an unchanged
  range is byte-for-byte identical (`sha256(export) == sha256(export)`).
- **PII-out-of-chain, tamper-evidence-ready** (DEC-23 S-1 / CMP-2): raw/PII values
  live in a `raw_beside` field that the hash and both exporters skip; only digests
  are in the hashable body. The canonical + row-hash rule is **frozen and unit-tested
  now** (though not yet populating a live chain), so activating tamper-evident
  hash-chaining later is a **zero-migration** switch — and a lawful GDPR erasure
  (blanking `raw_beside`) leaves the hash unchanged.

## Hardened by adversarial review

The review caught (and this slice fixes) real compliance-relevant
defects the green suite couldn't see: a valid-JSON-but-non-object audit line
crashed the projection; the full projection returned a *silently empty* ledger on
the sqlite/postgres backend (now it raises loudly — a compliance export must never
look empty when it just isn't wired for that backend yet); `action.host` could leak
a raw request path / email into the export (now only a real hostname is emitted); and
a per-objective receipt was fanned onto every effect row, over-counting "verified"
(now it attaches once per objective).

Two honest residuals: `actor.origin`/`lane` are best-effort inferences from the
execution-id (the audit row carries no origin), and the export caller must pass the
same `data_dir` the run used for receipts (a mismatch silently downgrades rows to
"claimed"). Both documented; the real-origin field + a backend-agnostic full scan
are follow-ups.

## Grounded, not guessed

A scope pass against the real code caught three traps before a line was written: the
actual `coerce_origin` vocabulary (no fabricated `watcher:<id>` origins), the need to
normalize timestamps to fixed width (Python drops zero microseconds → breaks
byte-stability), and compact-vs-pretty canonical form. 17 unit tests pin the crux
(canonical determinism, fixed-width ts, hash determinism + PII-out-of-chain, MASK
end-to-end, one-row-per-execution, byte-stable export, robustness). Adversarially
reviewed.

## Deferred (companion slices)

The UI page + the "what leaves this machine" privacy page (AC6), the DEC-13 criteria
authoring/scoring path (the N-of-M half of AC7), the `resolved_via` channel stamp
(the one genuine additive write — CONC-MAP-registered when built), mapping a spend
halt into a ledger row (once the held caps work merges), and the **live chain
writer** (populates seq/prev_hash/row_hash durably — CMP-2, zero-migration because
the rule is frozen here).
