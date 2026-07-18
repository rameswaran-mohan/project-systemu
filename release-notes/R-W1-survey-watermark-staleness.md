# R-W1 — survey watermark + read-side staleness

The world-model fact store only ever adds or confirms; nothing removes. So a service you
disconnected, or a folder whose access you revoked, stayed in the store looking exactly
as current as everything else. This makes the store **honest about its own freshness**
without introducing deletion.

## The shape

Rather than have a writer mark facts stale, each populate records a **survey watermark**
— what that survey actually *covered*: which fact kinds it produced, which granted roots
it walked, and whether the per-run file cap truncated it. Staleness is then derived
**read-side**, comparing a fact's `last_confirmed` against that coverage.

Three honest verdicts:

- **confirmed** — re-seen by the latest survey.
- **unconfirmed** — the survey covered this fact's scope and did *not* re-see it. The
  "this may be gone" signal.
- **not surveyed** — the survey didn't cover this kind or scope, so absence is *not*
  evidence. This is the case a naive "not re-seen ⇒ stale" rule gets wrong.

## Why not just mark facts stale on non-observation

That was the obvious design, and it's wrong here, for three reasons:

1. **It would break the write-only guarantee.** Staling facts from cross-run evidence
   means the populator must read the whole store — the exact thing the guard test forbids,
   in the same release that ships it.
2. **Non-observation is weak evidence.** The survey is scope-varying: roots are
   grant-scoped, sources have timeouts, files are capped per run. A slow or narrowed run
   would mass-stale perfectly valid facts.
3. **It pre-empts belief revision**, which is deliberately sequenced into a later program
   with the machinery to do it properly (contradicting evidence, propagation, logging).

So the coverage rule is deliberately **conservative**: a kind counts as surveyed only if
it produced at least one entry, because an empty slice is indistinguishable from one that
timed out. The cost is that staleness is *under*-reported (if every service vanishes at
once, the kind simply reads "not surveyed"). That's the safe direction — **a fact is
never called stale on evidence we don't actually have.**

## What review caught — every one a false-stale

Adversarial review found **three real defects, two reproduced against actual files on
disk**, all pointing the dangerous way (calling a live fact "may be gone"). The root
cause was single: coverage was being *inferred from how many facts we produced* instead
of *reported by the surveyor*, and only the surveyor knows what it stopped walking.

- **Truncated listings were invisible.** The surveyor emits only the top-N files per root
  (and stops walking a huge tree entirely), so any root with more files than that is
  truncated on every run — yet nothing said so. Twenty files, all present on disk, were
  reported as possibly gone. The surveyor now reports truncation directly, including its
  traversal cap.
- **An unreadable root read as "empty".** A vanished network mount or a transient
  permission error still emits a row (so the planner sees the grant) with an empty
  listing — and that was being taken as coverage, staling everything under it. A root now
  counts as covered only if it actually yielded entries.
- **Prefix collision on root containment.** A raw string prefix meant `C:/Radiology/…`
  counted as inside root `C:/R`. Containment is now by path component, matching the rule
  the confinement layer already uses. (The original test passed for the wrong reason —
  it compared different drives.)

Two more, both also false-stale: timestamps were compared as **strings**, so a differing
UTC offset or a naive stamp inverted the comparison (a *newer* fact read as stale) —
they're parsed as instants now; and a watermark row missing its timestamp defaulted to
*read-time now*, which is newer than every fact and would have staled the whole store in
one read — the field is required, so such a row is skipped instead. A never-confirmed
fact now reads "unknown" rather than "may be gone".

## Notes

The facts and the watermark share **one survey instant**. They're a single observation
event, and taking two timestamps meant a fact confirmed microseconds before the watermark
read as older than the survey that had just confirmed it — every fact showed "unconfirmed"
on the run that confirmed it. Caught by the end-to-end test.

Watermarks are bounded on disk and picked by timestamp rather than write order.
`record_survey` writes a separate file and never loads `facts.json`, so the populator
remains a non-reader of the store and the guard is untouched. Nothing here reads the store
for a bind or plan decision.

Full suite green.
