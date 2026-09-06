# CONC-MAP v1 — durable-store writer-ownership map

**Purpose (DEC-10 / SEQ-2).** A one-page map of *who writes which durable store, from which
process/thread, and what serializes concurrent writes*. It is the **hard precondition of
R-A12's `external_wait_reconciler`** — a new background writer on `ExecutionSnapshot`, the
highest-risk store. New standing daemons/reconcilers and new durable state must update this
map (and pass the concurrency review) before they ship.

**Enforced by** `tests/test_conc_map_writer_ownership.py`: a source-scan that pins the
single-writer / known-writer sets below. Adding a concurrent writer to a pinned store **fails
CI** until this map is updated — that failure *is* the review checkpoint.

> **Scope of v1:** the writers that exist at v0.9.63. Full CONC-MAP enforcement (locks on the
> multi-writer hot-spots) remains a precondition of R-P2 watchers / R-W4 gardener, unchanged.

---

## Process / thread topology (the substrate)

Default deployment = `SYSTEMU_STORAGE=file` → **one daemon OS process**, many threads sharing
in-process singletons (`EventBus`, `AppState`, `Supervisor`, `Command/DepApprovalStore`) and one
on-disk vault:

| Thread | Role | Home |
|---|---|---|
| `main` | keep-alive | daemon proc |
| APScheduler pool (~10) | all reconciler/sweep jobs | daemon proc |
| `supervisor-dispatcher` / `-heartbeat` | queue drain + stuck watchdog | daemon proc |
| `shadow-<id>` (1/run) | **the execution loop — writes ExecutionSnapshot** | daemon proc (or Huey worker proc) |
| `systemu-dashboard` | NiceGUI/uvicorn request handlers | daemon proc |
| `telegram-gateway` | long-poll; button-tap + `/answer` handlers | daemon proc |
| (EventPusher) | fires on the *publishing* thread (no own thread) | — |

**Separate processes that also write the same vault:** the CLI (`sharing_on decisions resolve`),
the scheduled-execute child (`army execute`), and — under `SYSTEMU_QUEUE=huey` — `python -m
systemu.worker` (which runs the shadow loop, hence the **snapshot writer**, in its own process).

**Serialization reality:** in-process `threading.Lock` + `os.replace` atomicity guard *threads
within the daemon*. **Nothing serializes across processes** — `os.replace` prevents torn/corrupt
files but not lost updates on read-modify-write patterns. There is no OS file lock anywhere.

---

## Writer-ownership table

Legend — **Ser.**: `LOCK+A` = in-process lock + atomic replace · `A` = atomic replace only
(no lock) · `APPEND` = plain append · `PLAIN` = non-atomic rewrite. **Risk** reflects
multi-writer × missing-serialization.

| Store | Writer(s) → home thread | Ser. | Owner | Risk |
|---|---|---|---|---|
| **ExecutionSnapshot** `data/audit/exec_*/resume_snapshot.json` | shadow loop (`shadow_runtime`), `supervisor.resume_after_grant`, `resume_on_decision._dispatch_resume`, **`jobs.external_wait_reconciler` (R-A12a — 4th writer, PARKED-runs only)** → shadow thread + APScheduler + EventBus-thread; **+ CLI / `army execute` / Huey-worker (cross-proc)** | `A` + process-local `_lock` | **per-execution_id (1 logical run)** | **HIGH — the R-A12 target** |
| `decisions` (`decisions/index.json` + `<id>.json`) | `OperatorDecisionQueue.post/resolve`, reconcilers (`jobs`), inbox handler, `resume_on_decision` stamp, **CLI proc**, **telegram-gateway thread** (R-P1) | `A`, **unlocked index RMW** | multi | **HIGH** |
| Fatigue metrics `metrics/metrics.json` | `incr` at gate creation (exec thread) vs `record_resolution` (dashboard/telegram/CLI) | `A`, **unlocked RMW** | multi | MED |
| **S4 shadow meter** `metrics/metrics.json` (`s4_shadow` bucket) | **only** `shadow_runtime` credit-seam meter branch → shadow exec thread (record-only; R-A13b-1) | `A` | **single ✓** (same writer thread as `incr`) | LOW |
| vault collection indexes (`scrolls`/`activities`/`tools`/`skills`/`shadows`/`notifications`) | many across `pipelines/`+`runtime/`+`scheduler/`+`interface/` (+ reconciler↔loop combos: `save_tool`, `save_activity`) | `A`, **unlocked index RMW** | multi | MED |
| `dashboard_lockout.json` (R-SEC1) | concurrent NiceGUI login threads | `A`, **unlocked RMW** | multi | MED (security-relevant undercount) |
| `schedules/*.json` + index | schedule job `mark_fired/missed` | `A`, unlocked RMW | APScheduler | LOW-MED |
| **OnTheTable** `table/items.json` | **only** `table_reconciler.reconcile_once` (60s) | `A` | **single ✓** | LOW |
| **CapabilitySlots index** `capabilities/capability_index.json` (R-CAP1) | **only** the daemon's `capability_reconciler` job (60s) → `capability_index.reconcile_index` | `A` (derive-only, no RMW) | **single ✓** (read-only callers use `find_tools(live=True)` — derives in memory, never writes) | LOW |
| **OnTheTable curation sidecars** `table/tombstones.json` + `table/pins.json` (T2a) + `table/operator_items.json` (T2b) | **only** the `/table` page (`interface/pages/table.py` — `add/remove_tombstone`, `set_pin`, `add_operator_item`) | `A` | **single ✓** (UI-owned; the reconciler only READS them in `project()`) | LOW |
| **OnTheTable consult sidecar** `table/consulted_items.json` (T3 / R-B3) | **only** `table_consult.commit` ← the `/table` consult panel (`interface/pages/table.py`) | `A` | **single ✓** (UI-owned, one nicegui thread; the reconciler only READS it in `project()`) | LOW |
| **OnTheTable proposal sidecar** `table/proposed_items.json` (T3 / R-B3) | **only** `table_store.add_proposed_item` ← `table_consult.propose` ← the `table_propose` registry tool, on a shadow execution thread | **LOCK+A** (`table_store._PROPOSED_LOCK` around the read-modify-write, then the atomic write) | genuinely multi-writer: **concurrent RUNS** are threads of one daemon and can propose at once. The lock makes the RMW serial, so the `MAX_PROPOSED_ITEMS` cap holds and no proposal is lost. NOT covered: a second daemon process (same exposure as `items.json`) | LOW | **only** `decision_bridge.resolve_from_channel` → telegram thread | `APPEND` | **single ✓** | LOW |
| **R-A13.5 ask corpus** `audit/ask_corpus.jsonl` | **only** the shadow exec loop at the ask point (`shadow_runtime` → `replay_metrics.record_ask`) | `APPEND` (serialized: process lock + best-effort OS file lock, then `O_APPEND` fd + ONE `os.write`) | one writer per run, but concurrent RUNS appear as concurrent appenders. **Guaranteed:** no tearing and no lost rows. **NOT guaranteed:** cross-run row order, un-`fsync`'d durability | LOW |
| **R-A16 answer-linked ask corpus** `audit/ask_avoidable.jsonl` | **only** the two ANSWER chokepoints: the pre-loop B10 rail (`elicitation.surface_ask_bundle_requirement` → shadow exec thread) + the bundled scope card's answer-time join (`scheduler/jobs.reconcile_resolved_harness_grants` → daemon thread), both via `replay_metrics.record_ask_avoidable` | `APPEND` (serialized: process lock + best-effort OS file lock, then `O_APPEND` fd + ONE `os.write`) | 2 genuinely concurrent writers. **Guaranteed:** no interleaving/tearing, and no lost rows. **NOT guaranteed:** row ORDER across writers, and durability of an un-`fsync`'d row against an OS crash (observability-only; both acceptable). NOTE what this row used to claim: a buffered `open(p,"a")` is NOT loss-free — measured 8x150 concurrent appends landed ~1155/1200 rows, silently, no torn line and no exception. Bare `O_APPEND` is not sufficient either **on Windows**, where the CRT emulates it as seek-then-write (measured ~886/1200); hence the explicit lock | LOW |
| **R-QL1 quick-lane ask corpus** `audit/quick_lane_asks.jsonl` | **only** the quick lane's `ASK_USER` branch (`pipelines/quick_task.py` → `replay_metrics.record_quick_lane_ask`), on the submitting task's own thread | `APPEND` (serialized: process lock + best-effort OS file lock, then `O_APPEND` fd + ONE `os.write` — the same writer as the two corpora above) | one writer per run, but concurrent quick-lane RUNS are threads of one daemon and appear as concurrent appenders. **Guaranteed:** no interleaving/tearing and no lost rows. **NOT guaranteed:** cross-run row order, and durability of an un-`fsync`'d row against an OS crash (observability-only; both acceptable). DELIBERATELY a THIRD file, and the separation is a DEC-7 RULING, not taste: folding these rows into `ask_corpus.jsonl` would have `avoidable_ask_report`'s no-attempt proxy score every one of them into the numerator of a SHIPPED metric (absent attempt fields default to 0), and folding them into `ask_avoidable.jsonl` would put schema-path-less free-text asks into an ANSWER-LINKED corpus keyed by `Requirement.schema_path`. Records REFS ONLY — the question is written as `value_ref`'s keyed non-reversible HMAC and no vault key ⇒ no row; credential/secret-class asks are excluded by a DOUBLE guard (the caller's `secret_class` **and** the recorder's own `is_secret_ask_text`, either alone refusing). Any further writer needs a DEC-10 review | LOW |
| **R-P1 resolve audit** `messaging/resolve_audit.jsonl` | **only** `decision_bridge._audit` ← `resolve_from_channel`, on the telegram-gateway thread | `APPEND` (buffered `open(..,"a")` + one `write`) | **single ✓** — one gateway thread is the sole writer. Row is best-effort observability: the append is not lock-serialised and not `fsync`'d, so a second writer would reintroduce the ~4% silent-loss shape measured on `ask_avoidable.jsonl`. That is why the writer set is pinned by test. Previously enforced by the test with NO row here — added when the CONC-MAP row/test binding was made two-way | LOW |
| **World-model facts** `world_model/facts.json` (R-W1 / §5.11.a, R-W2 / §5.11.c) | **TWO** writers, both on the shadow exec thread's post-survey step (`asyncio.to_thread`, separately bounded), and SERIAL with respect to each other within a run: (1) `world_model_populator.populate_from_situation` → `FactStore.put_facts`; (2) **R-W2** `ambient_census.run_census` → `FactStore.put_facts`, plus `ambient_census.revoke_category` → `FactStore.purge_source_ref` from the operator's revoke surface | `A` (whole-file rewrite from one load) | genuinely multi-writer — **concurrent RUNS** are threads of one daemon and each surveys — but the ADD path is IDEMPOTENT-CONVERGENT, not an accumulating RMW: `put_facts` re-derives every row from `fact_id_for(kind, value)`, so two runs surveying the same setup (or censusing the same machine) produce the same rows. A lost update costs a re-confirmation on the next survey, never a lost FACT. **R-W2 adds the one DELETION path** (`purge_source_ref`, on consent revocation), which is NOT convergent: a census in flight could re-add rows a concurrent purge just removed. Mitigated at BOTH ends — `revoke_category` withdraws consent BEFORE purging, and `run_census` RE-CHECKS consent immediately before its write and discards a revoked category's facts (pinned by test) — shrinking the window from the whole probe to one read+save. NOT covered: a second daemon process | LOW |
| **World-model negatives** `world_model/negatives.json` (R-W1 WM-2) | **only** `world_model_discovery.record_discovery_miss` / `clear_discovery_miss` → `FactStore.put_negative` / `drop_negative`, at the discovery-before-forge seam on the shadow exec thread | `A`, **unlocked RMW** | multi (concurrent runs). A lost update drops a SUPPRESSION, which is the safe direction by construction: the cost is re-paying a search, never a missed one (that is the same argument `drop_negative` rests on) | LOW |
| **World-model survey watermarks** `world_model/surveys.json` (R-W1) | **only** `world_model_populator.populate_from_situation` → `FactStore.record_survey` (same call, same thread) | `A`, **unlocked RMW** (append-then-truncate to last 20) | multi (concurrent runs). A lost watermark degrades READ-SIDE staleness to a slightly older coverage record — `staleness_of` under-reports staleness, which is its documented safe direction | LOW |
| **R-P3a cost ledger** (in-process `costing._LEDGER`, keyed by execution_id) | **only** `llm_router._record_usage_safe` → the calling LLM-call thread (contextvar-propagated into the sync worker) | in-process `_LEDGER_LOCK` | **single ✓** (router token-capture hook) | LOW (in-memory, not durable; no cross-proc surface) |
| `command_approvals.json` | gate handlers, resume rail, sandbox → many threads | **LOCK+A** | singleton lock ✓ (in-proc) | LOW |
| `dep_approvals.json` | CLI + dashboard + installer | **LOCK+A** | ⚠ `approve_and_install` builds a **2nd store w/ its own lock** | LOW-MED |
| `affinity_log.json` / `rejection_store.json` / `tool_metrics.json` / `capabilities/_usage.json` | termination / UI / exec threads | **LOCK+A** | lock ✓ (in-proc) | LOW |
| **R-W2 census consent** `census_consent.json` (§5.11.c WM-7) | **only** `census_consent.CensusConsentStore._write` ← `grant` / `revoke` / `set_paused` and `mark_ran`. **P4-B2: the operator surface is now LIVE** — `systemu census grant\|revoke\|pause\|resume` (`sharing_on/cli.py` → `interface/cli_commands.py` `run_census_*`, CLI process, at most one operator at a time) reach those three mutators **through `ambient_census.grant_category` / `revoke_category` / `pause_category`**, so the CLI never constructs a `CensusConsentStore` and the mutable handle stays confined to `runtime/ambient_census.py` (the writer-ownership guard still allowlists that one file). `mark_ran` still comes from `ambient_census.run_census` on the shadow exec thread. The live surface makes the operator-vs-run race REAL rather than hypothetical: revoke from a terminal genuinely can interleave with a survey's `mark_ran` | **LOCK+A** (`census_consent._CONSENT_LOCK` around the whole read-modify-write, then the atomic write) | genuinely multi-writer: an operator `revoke` vs. a run's `mark_ran`. The lock is LOAD-BEARING, not hygiene — every mutator rewrites the WHOLE file from its own load, so unlocked, a `mark_ran` that loads before a `revoke` and writes after it puts the revoked grant BACK (verified reproducible by hand during R-W2), which in turn defeats `run_census`'s pre-write consent re-check. With the lock, membership-check and write are atomic and `mark_ran` never ADDs a row, so consent cannot be manufactured by a timestamp. Reads fail CLOSED (a broken file grants nothing). **AUTHENTICATED (format `version: 2`)**: the file carries an HMAC-SHA256 over its canonical `{version, grants}` body, keyed by `census_consent._consent_key` — derived by domain separation from this vault's `dashboard_auth.session_secret`, the same discipline `replay_metrics._ref_key` uses — so a writer that can place a file in the vault can no longer manufacture a grant, and `_write` REFUSES to write a file it cannot sign (an unsignable file would read back as unconsented and silently discard the operator's answer). **MIGRATION RULE: an unsigned `version: 1` file reads as UNCONSENTED — never grandfathered, never upgraded in place, never re-signed on read; re-granting is the only way back, and reading a v1 file leaves it byte-identical.** No derivable key ⇒ no valid grants ⇒ nothing scans. **The MAC key is now bound to the consent GENERATION as well as to the vault** (the `secrets/census_consent.epoch` anchor, own row below), so a copy of this file saved before a withdrawal no longer authenticates after it — a per-vault-only key made every signature valid forever, which was live and witnessed on v0.10.27. NOT covered: a second daemon process (same exposure as every side-store here) | LOW |
| **R-W2 census consent epoch** `secrets/census_consent.epoch` (§5.11.c WM-7) | **only** `census_consent._bump_consent_epoch` ← `CensusConsentStore.revoke`, i.e. the operator's own `systemu census revoke` in its own CLI process (reached through `ambient_census.revoke_category`, so the mutable handle stays confined exactly as it is for the consent file). The anchor is also CREATED — at generation 0, never advanced — by `_current_epoch_or_create` on the SIGNING path (`_write` → `_consent_key`), so a fresh install anchors itself on its first grant | **LOCK+A** — the bump happens INSIDE the same `census_consent._CONSENT_LOCK` hold as the consent-file rewrite it belongs to; the write itself is `mkstemp` + `os.replace` (0600 by inheritance) | **single ✓** — the consent store is its own sole writer. The writer-ownership guard keys on `_bump_consent_epoch(` rather than on a constructor, which makes it a REACHABILITY pin as well as an allowlist: delete the bump from `revoke` and the guard's "declared writer no longer calls this" half goes red. WHY IT EXISTS: the consent MAC was keyed per-vault only, so it answered "was this signed by THIS vault?" and nothing else — save `census_consent.json`, run `census revoke`, copy the saved bytes back, and `census status` reported GRANTED again while the next survey re-scanned the machine (witnessed e2e, v0.10.27; the operator's LAST ACT was a withdrawal). The epoch is an input to the MAC **KEY**, not a field in the signed body: a plaintext field would need its own comparison, and a second layer that checks something CHEAPER than the MAC is worse than no second layer (DEC-34) — it manufactures confidence while the MAC alone still decides. A consent file with NO readable anchor is UNCONSENTED, never grandfathered — the same rule the unsigned `version: 1` format gets. The bump MUST stay inside the lock: outside it, a `mark_ran` that loaded before the bump and wrote after it would re-sign the revoked grant into the NEW generation, a resurrection the epoch itself cannot catch. NOT covered (typed P under DEC-36): restoring the anchor alongside the consent file, restoring `secrets/dashboard_session.secret`, or DELETING the anchor and waiting for a later operator-typed grant to re-create generation 0 — all are writes to the KEY-DOMAIN files inside systemu's own trust domain, where a genuine signature can be minted directly. A further writer needs a DEC-10 review AND a consent review: advancing this counter invalidates every signature the operator currently holds, so anything but a withdrawal doing it silently revokes consent nobody withdrew | LOW |
| **GrantedRoots** `granted_roots.json` (G2 / spec sec 5.4 / HIGH-3) | **TWO** writers, both interactive CLI commands in their own short-lived processes, both operator-typed: `cli_commands.roots_grant` -> `GrantedRootsStore.grant` (R-A4 B1b, gated on a printed-consequences y/N consent prompt that defaults to NO) and `cli_commands.roots_revoke` -> `GrantedRootsStore.revoke` (B1a). Before B1a this row read *(no live writer today; read-only)* and that was literally true. The other two handle holders are READ-ONLY and must stay so: `situational_inventory.build_roots` (the S3 root survey) and `requirement_binder` (resolver source #1's confinement re-gate) | **LOCK+A** -- `granted_roots._exclusive` (process-wide `_ROOTS_WRITE_LOCK` plus a best-effort OS lock on a SIDECAR `granted_roots.lock`) wraps the whole load-modify-replace in both mutators; the write itself is mkstemp + `os.replace`. The lock CANNOT sit on `granted_roots.json` itself: `os.replace` swaps the inode out from under any handle held on it, so that lock would exclude nothing, and on Windows an open handle on the destination makes the replace fail outright | **LOW, and the lock is the reason** -- not the writer count. B1a's row predicted MED at the second writer; the second writer arrived WITH the lock, so the prediction is closed rather than realised. Unlocked, both mutators rewrite the whole file from their own load, and the direction that matters is REVOKE: a lost removal leaves GRANTED a folder the operator withdrew, which no re-run repairs because the operator already believes it is gone (reproduced by test, not assumed -- the two race tests in tests/test_granted_roots.py fail on an unlocked store). NOT covered: cross-process exclusion is best-effort by design, degrading to the in-process guarantee rather than raising. A BACKGROUND or UI writer is still a DEC-10 review: it would make concurrent writes routine rather than a human running two shells, and a grant that no operator typed is a different question from a lock | LOW |
| R-SEC1 `dashboard_auth.json` / `dashboard_session.secret` | `set_passphrase` (CLI) / boot (dashboard thread) | `A` | single ✓ | LOW |
| R-UTL1 `secrets/api_token.json` (U-1a) | **only** `dashboard_auth.mint_api_token` ← `cli doctor --make-api-token` (CLI proc) | `A` (reuses `_write_secret_file`) | **single ✓** (the request path only READS it) | LOW |
| **U-12 Outbox** `<root>/Outbox/<yyyy-mm-dd>-<slug>/` (R-UTL1) | **only** `outbox.write_outbox_for_run` ← the two LANE TERMINALS (`pipelines/direct_task.py`, `pipelines/quick_task.py`) → the submitting task's own thread | `A` per file (tmp + `os.replace`); `.done` written LAST as the folder-complete marker | genuinely multi-writer — **concurrent RUNS** are threads of one daemon — but each run writes its OWN uniquely-named folder (`_unique_dir` collide-check), so two writers never share a path and no lock is needed. NOT covered: a second daemon process racing the same `_unique_dir` check (same exposure as `items.json`) | LOW |
| **2a growth snapshot** `<root>/growth_snapshot.json` | **only** `growth_snapshot.growth_delta_line` <- the Home growth card (`interface/pages/console.py` `_build_growth_card`) -> NiceGUI request threads | `A` (tmp + `os.replace`) | UI-owned, ONE entry point. Genuinely multi-writer in the small: two Home renders in two tabs are two request threads and both can find the week due. The payload is DERIVED (a fresh `growth_counts` read + the stamping time), never an accumulating read-modify-write, so a lost update costs at most a duplicated week boundary and never a count. The private `_write_growth_snapshot` has no caller outside its module (pinned by test), so the writer set cannot be widened without appearing in the allowlist. NOT covered: a second daemon process (same exposure as `items.json`) | LOW |
| **P2d first-run funnel** `<root>/funnel.json` | **only** `funnel.mark_milestone` <- the six first-run call sites: `interface/pages/welcome.py` (page render + `finalize_onboarding`), `pipelines/quick_task.py` + `pipelines/direct_task.py` (submission hook + success terminal), `interface/wishes.py` (`add_wish`), `interface/dashboard.py` (the record dialog) -> nicegui request threads + the submitting task's own thread | `A`, **unlocked RMW** | genuinely multi-writer -- concurrent runs/clicks are threads of one daemon -- but the store is FIRST-WRITE-WINS over a CLOSED six-name vocabulary, so a write only ever ADDS a key that was absent, never edits one. Two writers racing the SAME milestone lose nothing (same key, values milliseconds apart); two racing DIFFERENT milestones can drop one row, and the cost is one table cell reading "not yet" until that call site runs again -- which for five of the six is every task or page render. Deliberately UNLOCKED: the exposure is a cosmetic under-count on a read-only Insights table, never a lost fact, a lost artifact or a widened gate. Reads are filtered, not trusted: a hand-edited sidecar cannot introduce a seventh milestone. NOT covered: a second daemon process (same exposure as `items.json`) | LOW |
| `.credentials.json` / `.env` (gate mode) | NiceGUI settings handlers | **PLAIN (not atomic)** | single-surface | LOW (corruption-capable) |

The **★ pinned single/known-writer stores** (ExecutionSnapshot, OnTheTable, fatigue-metrics
resolution side, R-P1 audit) are enforced by the test. The atomic-write invariant is enforced on
ExecutionSnapshot, table_store, command_approvals, metrics_store, dashboard_auth, outbox,
world_model, census_consent, growth_snapshot, funnel and granted_roots - i.e. every store in
`_ATOMIC_WRITE_STORES` (a hand-maintained enumeration goes stale silently: it named five while
the test enforced eight, was corrected to nine while the test enforced ten, and is re-synced
here at eleven. Read the set in the test, not the count in this sentence).

---

## Multi-writer hot-spots (ranked)

1. **ExecutionSnapshot** — 4 in-tree writer paths (incl. R-A12a `external_wait_reconciler`) +
   cross-process (Huey/`army execute`). Guarded only by a **process-local** `_lock` + atomic
   replace; the reconciler↔EventBus dual-trigger has a TOCTOU on the best-effort
   `resume_dispatched` flag. Safe **today** because every snapshot write (resume rail +
   `external_wait_reconciler`) happens only while a run is *parked* (per-execution_id → one logical
   owner at a time): the reconciler skips any run the supervisor reports live, and stamps
   `dispatched` BEFORE re-submit (at-most-once).
2. **`decisions`** — telegram + dashboard + reconcilers + runtime + CLI; unlocked `index.json`
   RMW and an unlocked `queue.resolve` get→mutate→save with a status TOCTOU.
3. **Fatigue `metrics.json`** — exec-thread create vs UI/telegram/CLI resolve, unlocked RMW.
4. **vault collection indexes** — unlocked RMW; reconciler↔loop combos drop index headers.
5. **`dashboard_lockout.json`** — concurrent login threads undercount failures (weakens the gate).
6. **Cross-process false-safety** — every `threading.Lock` store (command/dep approvals, metrics,
   affinity, tool_metrics) is unprotected against the CLI/`army execute`/Huey-worker processes;
   only `os.replace` atomicity holds there.

---

## The R-A12 precondition (SATISFIED — R-A12a)

R-A12a added `external_wait_reconciler` (`scheduler/jobs.py`), a **new background writer on
`ExecutionSnapshot`** (the `pending_waits` field, schema v6). It is the **4th** concurrent snapshot
writer, and in Huey mode it runs in a *different process* from the shadow loop — where the
process-local `_lock` gives **no** protection. The three preconditions are now met:

1. ✅ **Added to the writer allowlist** in `test_conc_map_writer_ownership.py` (and the table
   above) — the conscious DEC-10 review checkpoint. The guardrail was observed failing on the
   unlisted `write_snapshot` caller, then satisfied.
2. ✅ **Per-execution_id parked-run invariant enforced, not assumed**: `external_wait_reconciler`
   calls `_run_is_live(supervisor, activity_id)` (scans the supervisor `_running` set + pending
   queue) and skips the ENTIRE run — writing no snapshot — for any run reported live, so it never
   races the run's own loop. A CANCELLED run's waits are expired (no resubmit). Covered by
   `tests/test_ra12a_external_wait_reconciler.py::test_live_run_wait_not_touched` +
   `::test_cancelled_run_wait_is_noop`.
3. ⚠️ **Cross-process gap — mitigated by single-owner discipline, not an OS lock.** `pending_waits`
   is written by exactly one owner *at a time* because writes happen only on PARKED runs and
   `dispatched` is stamped+persisted BEFORE the re-submit (at-most-once — a crash after the stamp
   never double-submits; `::test_stamp_before_submit_idempotency`). The residual: the daemon's
   `_running` set does NOT see a run executing in a *separate* Huey/`army execute` process, so the
   liveness check is process-local. This is acceptable for retry waits (a wait exists only because
   the run already FAILED and is genuinely parked awaiting a delayed retry — it is not executing
   anywhere until re-submitted); a true OS file lock on `pending_waits` remains the belt-and-braces
   fix, deferred with the hot-spot #6 cross-process work (R-P2 / R-W4).

Full CONC-MAP enforcement (locks on hot-spots #2–#5) stays a precondition of R-P2 watchers and
the R-W4 world gardener, unchanged.
