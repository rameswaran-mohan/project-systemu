# R-W1 (W-A slice-1) — the World Model v2 fact substrate

The greenfield foundation of the World Model program (§5.11, the successor to the
§5.1 Situational Inventory). This is **slice-1: the substrate only** — a durable,
provenance-immutable fact store with a query API. Nothing in the run loop reads or
writes it yet, so the agent behaves **identically** when it is absent or empty (the
§5.11.f risk-5 invariant holds trivially). The payoff wiring is slice-2.

## What it is

A pure module `systemu/runtime/world_model.py`:

- **WM-1 — the universal `Fact`** `{fact_id, kind, value, origin_class, confidence,
  last_confirmed, source_chain}`. `origin_class` is the same immutable taint axis as
  everywhere else (`operator | systemu_authored | content_derived`); `source_chain`
  is an append-only list of provenance steps. `kind` is an **open** vocabulary — an
  unknown kind is stored and fenced, never refused.
- **WM-2 — negative knowledge** (`NegativeFact` `{scope, probes, recorded_at, ttl}`):
  "searched and NOT found" as a first-class, **expiring** fact, so a handoff can cite
  *what* was searched and *when*, and an identical goal within the TTL can skip the
  re-search. Absence expires on a short absolute default (6h); the relative "faster
  than presence" comparison goes live when W-D confidence-decay lands.
- **WM-4 — `world.query`** — a deterministic view family (`find_services`,
  `what_can(verb, target_class)`, `find_data`, `about`, `provenance`) over the store.
  `what_can` reuses the R-CAP1 slot canonicalizer — it's the fact-store expression of
  the `find_tools` seed (CAP-9). Views return full `Fact` objects (they carry the
  taint/verification signal a slice-2 fence/binder needs).
- A **read-only** `sharing-on world` CLI: summarise the store, or query `about` a
  host/app/account. (Empty until slice-2 populates it — a smaller world, never broken.)

## Trust properties (the point of a world model)

- **Taint never launders (E1, the one soundness rule).** `put_fact` on an existing
  `fact_id` with a different `origin_class` is **refused** (`ImmutableProvenanceError`).
  `confidence`/`last_confirmed` update; `source_chain` is append-only (existing steps
  are never dropped). Mirrors `Requirement.value_origin` ("copied, never recomputed").
- **The world model describes; it never authorizes.** `Fact.taint_permits_silent_bind`
  is a taint-only, *necessary-not-sufficient* advisory (content_derived → False) — the
  §5.3 binder (slice-2) is the sole silent-bind authority and additionally requires
  sufficient confidence/verification for the effect class.
- **Never-subtract binds the STORE (E3).** The raw `query_facts` is uncapped by
  default, so no fact is ever silently hidden; a view may rank/trim for context
  because any trimmed fact stays reachable via `query_facts`/`about`/`get`.
- **No secret values (E6)**; facts hold ids/names/paths only. **Fail-open expiry** — a
  corrupt negative-fact timestamp reads as expired (re-search), never suppresses a
  search forever.

## Acceptance criteria — the slice-1 ↔ slice-2 split (tracked, not dropped)

| AC | slice-1 (this release) | slice-2 |
|----|------------------------|---------|
| **AC1** fact schema | the 4 fields + immutable `origin_class` (update-path) | the `content_derived`-can't-silent-bind **assertion** at the §5.3 binder |
| **AC2** negative knowledge | store shape (what/when) + TTL expiry | the §5.5 discovery skip-within-TTL + handoff-cites loop |
| **AC4** queryable | the `world.query` API + never-subtract at the store | the planner retrieving OUTSIDE its initial ranked view |

## Deferred (documented, owned by later slices/releases)

Slice-2: the §5.1-report-as-view inversion, the §5.3 binder AC1 assertion, the
discovery negative-fact write/read loop, and populating the store from the live
inventory (the negative-fact **writer** must stamp `systemu_authored`, never accept a
`content_derived` "absent" — a denial-of-discovery guard); and a corrupt-store
hardening (quarantine `facts.json` to `.corrupt-<ts>` rather than discard, so a
provenance store never loses facts without a trace). W-D: WM-5 WorldGraph + edges,
WM-3 belief revision, WM-13 gardener confidence-decay + a `source_chain` cap. So
slice-1 facts are **flat** (no edges) and there is no decay/contradiction logic yet.

## Provenance of this build

Grounded by a code survey (the W-A dependency chain — §5.1 inventory, G2 GrantedRoots,
the fact precedents — is fully merged on mainline; the substrate is greenfield, not
blocked). Confirmatory spec-review of §5.11.a/.b returned BUILD-READY-WITH-EDITS; all
seven edits (E1–E7) folded in. Adversarially reviewed — the core invariants (E1
immutability, E3 never-subtract, WM-2 fail-open, E5 read-only) verified sound; one MED
defense-in-depth defect fixed before ship (the `origin_class` field was unvalidated and
the taint advisory was a blacklist that failed OPEN → now a construction-time validator
rejects an out-of-vocab taint value and the advisory is a fail-closed whitelist), plus
`kind` pinned immutable on the update path and the dedup id widened to 64 bits; two LOW
items (corrupt-store quarantine, `source_chain` growth cap) deferred as noted above.
Full edit-safe suite green. Held on `feat/rw1-world-model`.
