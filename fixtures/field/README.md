# `fixtures/field/` — the labelled field scenario corpus (DEC-11 / PLAN-13)

This directory is the **labelled SCENARIO corpus** the R-A13.5 resolver replay
runs against: checked-in JSON files, bounded, reviewed, versioned. Every release
from R-A9 onward is meant to contribute to it, and partner-reported gaps
(Track E1) land here.

> **⚠ "Corpus" is overloaded here.** This is a corpus of *scenarios*. It is **not**
> one of the append-only runtime JSONL corpora under `<vault>/audit/`
> (`ask_corpus.jsonl`, `taint_clamp_corpus.jsonl`, `ask_avoidable.jsonl`), and it
> is **never** `cgb_eval/` (that evaluation set is frozen for the paper). Nothing
> here is written at runtime.

## The two files that own this corpus

This README is a **pointer**, not the specification. Two committed artefacts are
authoritative; read them, not prose here:

1. **[`roster.json`](./roster.json) — the DENOMINATOR (DEC-27).** A flat list of
   the fixture filenames this corpus **must** contain. It is *independent of the
   directory glob on purpose*: the count of avoidable/necessary asks is only
   trustworthy if the set of scenarios behind it is fixed by something the glob
   cannot silently shrink. Adding or removing a fixture means editing `roster.json`
   **in the same commit** — `test_field_roster_matches_fixtures` turns any drift
   (a rostered file gone from disk, or an unrostered `*.json` present) into a red
   suite, and at replay time either direction is a hard failure that suppresses
   the rate and makes the run non-`definitive`.

2. **[`systemu/runtime/resolver_replay.py`](../../systemu/runtime/resolver_replay.py)
   — the HARNESS.** Its module docstring is the spec for: what a scenario file
   looks like (`schema_version: 1`), how each ask is scored (`avoidable` /
   `necessary` / `unassessable`), why some asks are structurally unassessable (the
   binder stores a keyed non-reversible digest, never a value), the
   never-record-a-credential rule (`answer_is_synthetic` is the only escape hatch),
   the `{root}` containment rule for every declared path, and how `definitive` and
   `rate` are *derived* — never stored — so no failure path can emit a healthy
   `0.0`.

## Adding a scenario

1. Write the JSON (minimal — one behaviour per scenario). Derive the `label` from
   the spec/AC intent, **not** from running the harness.
2. Add its filename to `roster.json`.
3. Run `python -m pytest tests/test_ra135_resolver_replay.py -q`.
4. If your label disagrees with the replay (`mislabelled` is non-empty),
   investigate which side is wrong before touching either — that disagreement is
   the corpus's entire value as a tripwire.

## ⚠ Boundary — this replay does NOT settle DEC-7

DEC-7 concerns the **quick lane's** ask cap (`quick_task.py:_ASK_USER_CAP = 3`).
Every scenario here replays `requirement_binder.compute_requirements` — the
**deep-lane** ask rail — and **no scenario here can score the quick lane**:

* `quick_task.py` emits its asks as LLM-chosen `ASK_USER` actions and **never
  calls the binder** (`compute_requirements` / `build_requirement_report` /
  `requirement_binder` appear nowhere in that file). Its asks are free-text
  questions, not schema leaves, so there is no requirement for a replay to score.
* The quick lane records **nothing** into `ask_corpus.jsonl` — the only
  `record_ask` call site is `shadow_runtime.py` (the deep lane) — so the
  `avoidable-ask` **proxy** does not observe it either.

So **neither** this replay **nor** the `avoidable_ask_report` proxy (whose own
docstring calls it a *non-definitive proxy* — accurately, and it is unchanged by
this packet) produces quick-lane data. Making DEC-7 decidable needs quick-lane
instrumentation first — and scoring those asks would need a mechanism other than
this one, because there is no schema leaf to bind. Do **not** read this corpus's
rate as a quick-lane measurement, and do not relabel the aggregator as a replay.

## ⚠ Unresolved (a spec question, not a bug): what belongs in the denominator

The harness scores **every ask a fixture declares**; §10 asks about asks that
**actually happen**, and `requirement_binder._needs_ask` binds a `have` result at
a TRUSTED origin **silently** (never asking). Several corpus asks sit on such
silent-bind leaves, so the headline rate is "of the asks this corpus declares",
not an estimate of the production avoidable-ask rate. Which population §10 means
is a semantic ruling (the DEC-25/DEC-26 pattern), recorded here, not decided in a
build packet.
