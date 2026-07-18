# R-W1 (W-A slice-2b) — pin the silent-bind invariant generically

## The finding that reshaped this slice

Slice-2b was scoped as "assert at the §5.3 binder that a `content_derived` fact can
never silent-bind" (spec AC1, second clause). Grounding it first showed **that
assertion already exists and is load-bearing**:

- `requirement_binder._needs_ask()` *is* AC1(b): a `content_derived` requirement is
  forced into the ask bundle unconditionally — even at confidence 1.0. The confidence
  threshold governs *have-vs-resolvable* and cannot rescue a tainted value into silence.
- It is a real park, not bookkeeping: a non-empty ask bundle builds a scope card, goes
  through the Governor, snapshots, and **suspends the run**.
- Taint cannot be laundered *into* the binder: `_entry_origin()` derives taint from the
  **source kind** and never reads an entry's self-declared `origin_class`; the file and
  run-context sources clamp regardless of score; an unknown origin fail-clamps at
  construction.
- It is already pinned by five binder tests.

So building it as scoped would have re-implemented a shipped security control. This is
the codebase's repeatedly-earned lesson firing again: **a plan-named gap dissolved on
grounding.** What was genuinely missing was different — see below.

## What this slice actually does: make the invariant un-regressable

The existing tests pin AC1(b) for *specific known sources*. Nothing pinned the **general
rule** — so a future bind source that stamped a trusted origin at high confidence would
create the very silent-bind AC1(b) forbids, and every existing test would still pass.

- **A behavioural pin** — `_needs_ask` forces an ask for `content_derived` at any
  confidence, and only a trusted-axis value already at `have` may bind silently.
- **A structural allowlist** — enumerate the bind pipeline and assert that *only* the
  four allowlisted sources can emit a trusted (silent-bind-capable) origin. Each is
  backed by data systemu genuinely trusts (the current tool call's own params, the
  operator's credential store, facts from the operator's own prompt, systemu's catalog)
  — never tool output or file bytes. **A new source that stamps a trusted origin now
  fails the suite until a human admits it deliberately** — the review gate we want on
  anything silent-bind-capable.
- **A clamp pin + an anti-laundering pin** — the two always-untrusted sources must not
  acquire a trusted path, and `_entry_origin` must keep refusing a claimed
  `origin_class` (forged or snapshot-rehydrated).

## A hole closed in slice-2a's own guard

Slice-2a's "nothing reads the store" guard listed read symbols to grep for — and
**omitted `about`, `provenance`, `all_negatives`**, so a future reader calling `about()`
would have passed silently. A symbol blocklist can't work here anyway: the read API
includes generic names (`get`) that cannot be grepped without false positives. The guard
now gates on **module reference** — a fixed allowlist of modules, each with a stated
role — so a reader added anywhere trips regardless of which call it uses.

It also now scans the **whole package**, not just `runtime/`. The narrower scan quietly
rested on "every decision path lives under `systemu/runtime/`" — true today, but
unpinned, and exactly the kind of premise that stops being true without anyone noticing.

## Deferred — deliberately, with the traps recorded

**Never trust a stored fact's `origin_class` or `confidence` at bind time.** The
populator copies each entry's declared origin, and the service model's default is
`operator` for *every* service; it also stamps `confidence=1.0` on every fact. Trusting
either would flip an inventory value from *ask* to *silent* — a straight regression of
the untrusted-content rule. A fact's origin is honest **provenance**, never a bind-taint
clearance.

Still ahead, in order: (1) an **observability-only** bind-candidate metric — measure
what the store *would* have proposed, and how much of it fails a fresh granted-root
re-gate — to earn evidence before anything binds; (2) only if that shows value, an
**ask-only** world-model bind (hard-coded untrusted origin, confidence capped below the
silent threshold so it fails the taint *and* confidence gates independently, opaque
ref, every path re-gated fail-closed) — payoff is `missing → resolvable`, never a silent
bind. Also deferred: the report-as-view inversion, and the per-key taint map for values
that reach a tool call *through the LLM* (a real, documented residual, but it changes
bind behaviour toward more asks and needs its own slice and regression suite).

## A note on which gate tier checks this

The structural allowlist source-inspects the bind pipeline, so it is auto-tagged
`source_sensitive` and runs in the full tier. The **behavioural** pins are deliberately
written without source inspection so they are *not* auto-tagged — the core silent-bind
assertion is therefore checked by the edit-safe gate on every run, not only pre-push.
(Written as one module first, which quietly pulled the load-bearing behavioural pins out
of the edit-safe tier; splitting them fixed that.)

Test-only — zero production code, zero behaviour change. Full edit-safe suite green.
Held on `feat/rw1-world-model-slice2`.
