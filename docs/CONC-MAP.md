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
| **R-P1 resolve audit** `messaging/resolve_audit.jsonl` | **only** `decision_bridge.resolve_from_channel` → telegram thread | `APPEND` | **single ✓** | LOW |
| **R-P3a cost ledger** (in-process `costing._LEDGER`, keyed by execution_id) | **only** `llm_router._record_usage_safe` → the calling LLM-call thread (contextvar-propagated into the sync worker) | in-process `_LEDGER_LOCK` | **single ✓** (router token-capture hook) | LOW (in-memory, not durable; no cross-proc surface) |
| `command_approvals.json` | gate handlers, resume rail, sandbox → many threads | **LOCK+A** | singleton lock ✓ (in-proc) | LOW |
| `dep_approvals.json` | CLI + dashboard + installer | **LOCK+A** | ⚠ `approve_and_install` builds a **2nd store w/ its own lock** | LOW-MED |
| `affinity_log.json` / `rejection_store.json` / `tool_metrics.json` / `capabilities/_usage.json` | termination / UI / exec threads | **LOCK+A** | lock ✓ (in-proc) | LOW |
| `granted_roots.json` | *(no live writer today; read-only)* | `A`, unlocked | none yet | LOW (until a grant UI wires) |
| R-SEC1 `dashboard_auth.json` / `dashboard_session.secret` | `set_passphrase` (CLI) / boot (dashboard thread) | `A` | single ✓ | LOW |
| `.credentials.json` / `.env` (gate mode) | NiceGUI settings handlers | **PLAIN (not atomic)** | single-surface | LOW (corruption-capable) |

The **★ pinned single/known-writer stores** (ExecutionSnapshot, OnTheTable, fatigue-metrics
resolution side, R-P1 audit) are enforced by the test. The atomic-write invariant is enforced on
ExecutionSnapshot, table_store, command_approvals, metrics_store, dashboard_auth.

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
