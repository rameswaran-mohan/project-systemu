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
| **ExecutionSnapshot** `data/audit/exec_*/resume_snapshot.json` | shadow loop (`shadow_runtime`), `supervisor.resume_after_grant`, `resume_on_decision._dispatch_resume` → shadow thread + APScheduler + EventBus-thread; **+ CLI / `army execute` / Huey-worker (cross-proc)** | `A` + process-local `_lock` | **per-execution_id (1 logical run)** | **HIGH — the R-A12 target** |
| `decisions` (`decisions/index.json` + `<id>.json`) | `OperatorDecisionQueue.post/resolve`, reconcilers (`jobs`), inbox handler, `resume_on_decision` stamp, **CLI proc**, **telegram-gateway thread** (R-P1) | `A`, **unlocked index RMW** | multi | **HIGH** |
| Fatigue metrics `metrics/metrics.json` | `incr` at gate creation (exec thread) vs `record_resolution` (dashboard/telegram/CLI) | `A`, **unlocked RMW** | multi | MED |
| vault collection indexes (`scrolls`/`activities`/`tools`/`skills`/`shadows`/`notifications`) | many across `pipelines/`+`runtime/`+`scheduler/`+`interface/` (+ reconciler↔loop combos: `save_tool`, `save_activity`) | `A`, **unlocked index RMW** | multi | MED |
| `dashboard_lockout.json` (R-SEC1) | concurrent NiceGUI login threads | `A`, **unlocked RMW** | multi | MED (security-relevant undercount) |
| `schedules/*.json` + index | schedule job `mark_fired/missed` | `A`, unlocked RMW | APScheduler | LOW-MED |
| **OnTheTable** `table/items.json` | **only** `table_reconciler.reconcile_once` (60s) | `A` | **single ✓** | LOW |
| **R-P1 resolve audit** `messaging/resolve_audit.jsonl` | **only** `decision_bridge.resolve_from_channel` → telegram thread | `APPEND` | **single ✓** | LOW |
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

1. **ExecutionSnapshot** — 3 in-tree writer paths + cross-process (Huey/`army execute`). Guarded
   only by a **process-local** `_lock` + atomic replace; the reconciler↔EventBus dual-trigger has
   a TOCTOU on the best-effort `resume_dispatched` flag. Safe **today** because resume writes
   happen only while a run is *parked* (per-execution_id → one logical owner at a time).
2. **`decisions`** — telegram + dashboard + reconcilers + runtime + CLI; unlocked `index.json`
   RMW and an unlocked `queue.resolve` get→mutate→save with a status TOCTOU.
3. **Fatigue `metrics.json`** — exec-thread create vs UI/telegram/CLI resolve, unlocked RMW.
4. **vault collection indexes** — unlocked RMW; reconciler↔loop combos drop index headers.
5. **`dashboard_lockout.json`** — concurrent login threads undercount failures (weakens the gate).
6. **Cross-process false-safety** — every `threading.Lock` store (command/dep approvals, metrics,
   affinity, tool_metrics) is unprotected against the CLI/`army execute`/Huey-worker processes;
   only `os.replace` atomicity holds there.

---

## The R-A12 precondition (why this gate exists)

R-A12 adds `external_wait_reconciler`, a **new background writer on `ExecutionSnapshot`** (a
`pending_waits` field that does not exist yet). It would become a **4th** concurrent snapshot
writer, and in Huey mode it runs in a *different process* from the shadow loop — where the
process-local `_lock` gives **no** protection. Before it ships, R-A12 must:

1. **Add it to the writer allowlist** in `test_conc_map_writer_ownership.py` (the test will fail
   until then) and to the table above — the conscious review checkpoint.
2. **Preserve the per-execution_id parked-run invariant**: the reconciler may write a snapshot
   only while its run is *not live* (parked), so the run's own loop is not concurrently writing
   the same file. Assert/enforce this, don't assume it.
3. **Close the cross-process gap** for the `pending_waits` write path (an OS file lock or a
   single-owner discipline for that field), since `_lock` is process-local and Huey/CLI writers
   exist. At minimum, document that `pending_waits` is written by exactly one owner.

Full CONC-MAP enforcement (locks on hot-spots #2–#5) stays a precondition of R-P2 watchers and
the R-W4 world gardener, unchanged.
