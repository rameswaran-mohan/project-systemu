# R-W1 (W-A slice-2a) — populate the world model from the live inventory

Slice-1 built an inert fact substrate. Slice-2a is its first wiring into a live run —
a **write-only populator** that projects the §5.1 situational-inventory report into the
`FactStore` after each survey, so the world model is non-empty from the operator's
actual setup (visible via `sharing-on world` / the `world.query` views).

## The boundary (why this is safe to ship without the full 4-lens)

Slice-2a is deliberately the **non-trust-critical** half of slice-2:

- **No planner-input change.** The populator only *reads* the report and writes the
  store; it never mutates `_report` / `context._situation_report`, so the open-world
  planner prompt is byte-identical.
- **No silent-bind enablement.** No bind/plan/resolve module reads the store — the
  §5.3 binder is untouched. A fact written here **cannot** seed a silent bind. The
  store is a read-only *observability* surface today. (A test pins that
  `requirement_binder` / `open_world_planner` / `reference_resolver` / `supervisor` /
  `situational_inventory` do not reference the world model.)
- **Fail-safe.** It runs inside the survey's swallow-all try/except *and* is itself
  defensive per-entry, so a malformed entry is skipped and any failure returns 0 —
  the run is left exactly as it is today (a smaller world, never a broken one).

The trust-critical half — teaching the §5.3 binder to read the store under the AC1
"content_derived can't silent-bind" assertion — stays **deferred to a 4-lens-gated
slice**.

## What it maps

Each SituationReport entry already carries a valid `origin_class` (set by the inventory
builder from the *source kind*), copied verbatim as the Fact's honest **provenance**:

| entry | → Fact |
|-------|--------|
| `ConnectedService` | `service`, value = URL, `operator` |
| `CapabilityRef` | `capability`, value = tool_id, `systemu_authored` |
| `RootSurvey.salient[*]` | `data_location`, value = path, `content_derived` |
| `credentials[*]` | `credential_ref`, value = **name only** (never a secret), `operator` |

**Provenance ≠ bind-taint.** The stored `origin_class` is who-asserted-it, not a
silent-bind clearance. When a future slice teaches the binder to read the store it must
re-derive conservative bind-taint (as `requirement_binder._entry_origin` already does —
always `content_derived` for an inventory value), never trusting this field.

## Also fixed here

- The slice-1 residual (unbounded `source_chain` growth) becomes real with a per-run
  populator, so `put_fact` now dedups the chain by `(source_kind, ref)`: re-observing a
  fact from the same source every run confirms it in place (updating `last_confirmed`)
  without growing the chain. A genuinely distinct source is still appended.
- **Bulk `put_facts`** — one load + one save per batch. Calling `put_fact` in a loop
  rewrote the whole store once per fact (O(N²) disk work); the per-run populate is now
  O(N), and it runs **off the event loop** (`to_thread`) under its own timeout, matching
  the survey stage's non-blocking contract.
- **A bound on one run's file-derived facts** — `data_location` is the churny kind (a
  busy root re-mints path facts) and slice-2a has no removal yet, so a single run's
  contribution is capped rather than silently unbounded.
- **A stronger no-read guard** — the invariant that nothing reads the store for a
  decision is now enforced by scanning *every* runtime module for the read surface
  (not a hardcoded shortlist), and `shadow_runtime` — the write host, and the most
  likely place a future read would appear — is pinned to reference only the populator.

## Deferred (W-D / later slices)

Belief revision + a gardener (WM-3/WM-13): today a fact **persists even after its
source disappears** (a disconnected service stays in the store), surfaced honestly via
`last_confirmed` but not yet auto-expired. The report-as-view inversion, the §5.3
binder AC1 assertion, and the §5.5 discovery negative-fact loop remain slice-2b+.

## Review

Adversarially reviewed against the trust contract: **no critical or high defect** — the
change is genuinely additive, the planner's input is byte-identical, the taint copy
cannot launder (each entry's `origin_class` is set from the *source kind*, never from
content), credentials are names-only, and no module reads the store. The review's four
findings — the O(N²) write path, the unbounded churny kind, and two gaps in the no-read
guard — are all folded in above. Stale-fact accumulation across runs is a deliberate
W-D deferral (belief revision + gardener): with no reader, a stale fact can neither
mis-bind nor mis-plan; it only shows in the observability view with an honest
`last_confirmed`.

Full edit-safe suite green. Held on `feat/rw1-world-model-slice2`.
