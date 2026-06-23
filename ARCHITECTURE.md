# Architecture

A reference for contributors and operators who want to understand
how the pieces fit together.  Pair this with the
[`docs/getting-started.md`](docs/getting-started.md) walkthrough for
hands-on context.

---

## Two components, one pipeline

```
┌─────────────────────────┐         ┌──────────────────────────────────┐
│      Sharing-On         │         │           Systemu                │
│  (capture + analysis)   │ ──────▶ │  (autonomous Shadow runtime)     │
│                         │         │                                  │
│  CLI: `sharing_on`      │         │  Dashboard + Supervisor + Queue  │
└─────────────────────────┘         └──────────────────────────────────┘
```

- **Sharing-On** is the capture engine.  It records what you do on
  your computer — window switches, file changes, clipboard events,
  screenshots, browser navigation — and uses an LLM to turn the
  recording into a structured `instructions.md`.
- **Systemu** is the runtime.  It refines `instructions.md` into a
  **Scroll** (structured intent + objectives), assigns it to a
  **Shadow** agent, dispatches the Shadow through the Supervisor,
  and surfaces live progress in a NiceGUI dashboard.

The two components live in the same repository but are deliberately
separable — Systemu can ingest Scrolls that didn't come from
Sharing-On, and Sharing-On's `instructions.md` is a useful artefact
even outside the Systemu pipeline.

---

## End-to-end pipeline

```
You perform a task on your computer
            │
            ▼
   Sharing-On records events + screenshots
            │
            ▼
   Tier 3 LLM groups events into a structured
   instructions.md
            │
            ▼
   Scroll Refinery (Tier 1 LLM) turns
   instructions.md into a Scroll with intent
   + objectives + tags
            │
            ▼
   Operator approves the Scroll in the dashboard
   (or auto-approval if enabled)
            │
            ▼
   Activity Extractor identifies the required
   skill + tools
            │
            ▼
   Shadow Decision selects an existing specialist
   Shadow or synthesises a new one for the activity
            │
            ▼
   Supervisor enqueues the Shadow's execution
            │
            ▼
   Worker dequeues, ShadowRuntime executes:
     - load identity + memory + tool registry
     - iterate: think → tool call → observe
     - report completion, partial, or failure
            │
            ▼
   Flywheel records the outcome; Memory
   Consolidator promotes lessons into Shadow /
   Elder memory
```

Every stage emits events on a shared EventBus; the dashboard
subscribes for live progress and the Supervisor uses the same bus to
coordinate retries and orphan recovery.

---

## Deployment modes

Three modes, picked by `./install.sh --mode <name>`.  See the
README for the comparison table.

### `local`

```
┌──────────────────────────────┐
│ Host (your laptop)           │
│                              │
│  .venv/python                │
│   ├─ daemon (NiceGUI + sched)│
│   └─ worker (Huey/SQLite)    │
│                              │
│  systemu/vault/*.json        │
│  data/systemu.db (queue)     │
└──────────────────────────────┘
```

Zero external services.  Best for single-machine dev and personal
use.  Daemon + worker run as detached host subprocesses tracked by
PID files (or window titles on Windows).

### `docker-local`

```
┌── docker-compose --profile local ──────────────────┐
│                                                    │
│  systemu-dashboard-local  ─┐                       │
│  systemu-worker-local      ├── shared volumes      │
│  postgres-local           ─┘                       │
│                                                    │
│  Vault state in Postgres                           │
│  Huey broker on SQLite (worker-local persistent)   │
└────────────────────────────────────────────────────┘
```

Hobbyist self-host on one box.  Same logic as `local` mode but
behind a single `docker compose up` and with Postgres for durability.

### `docker-enterprise`

```
┌── docker-compose --profile enterprise ─────────────────────┐
│                                                            │
│  systemu-dashboard ─┐                                      │
│  systemu-worker × N │                                      │
│  postgres           ├─ shared network                      │
│  redis              ┘                                      │
│                                                            │
│  Vault state in Postgres                                   │
│  Supervisor priority queue in Redis (durable)              │
│  Huey broker in Redis (cross-worker job queue)             │
└────────────────────────────────────────────────────────────┘
```

Production / multi-host.  Worker replica count is configurable
(`WORKER_REPLICAS`).  Redis is the source of truth for the
Supervisor's priority queue and the Huey broker — orphan recovery
re-claims rows after a container restart.

---

## Module map

```
sharing_on/                     — Capture engine
    collectors/                   ─ Screen, clipboard, file, window monitors
    analyzer/                     ─ Step detector + instructions.md generator (Tier 3 LLM)
    cli.py                        ─ `sharing_on` console entry point

systemu/
    core/                         ─ Pydantic models (Shadow, Scroll, Activity, Tool, …)
    runtime/
        supervisor.py             ─ Engine that dispatches Shadows
        shadow_runtime.py         ─ Per-execution loop (think → tool → observe)
        tool_sandbox.py           ─ Tool runner — delegates to a ToolBackend
        backend/                  ─ Pluggable ToolBackend implementations
                                    (local + docker today; ssh + wsl stubs
                                    reserved for v0.4)
        workflow_tracker.py       ─ In-memory map of in-flight workflows to
                                    pipeline stages; warms from vault on boot,
                                    incremental updates via EventBus
    pipelines/
        scroll_refiner.py         ─ Tier 1: instructions.md → Scroll
        activity_extractor.py     ─ Tier 1: Scroll → Activity + skill + tools
        shadow_decision.py        ─ Tier 1: Activity → specialist or fallback Shadow
        tool_forge.py             ─ Tier 2: synthesise missing tools
        memory_consolidator.py    ─ Promote per-execution lessons into Shadow / Elder memory
        evolution_engine.py       ─ Review failures + propose changes
    interface/
        dashboard.py              ─ NiceGUI root + sidebar
        pages/                    ─ Per-route page modules (overview, scrolls, …)
        components/               ─ Reusable widgets composed into pages
                                    (workflow_pipeline, learning_curves,
                                    memory_status, skills_snapshot,
                                    pending_tools)
    messaging/                    ─ Chat-platform gateways (Telegram today;
                                    Slack + Discord planned).  Shared
                                    Gateway protocol + command parser;
                                    each platform is one module.
    queue/
        in_memory.py              ─ Supervisor's in-process priority queue
        sqlite_priority.py        ─ Durable SQLite queue (local / docker-local)
        redis_priority.py         ─ Durable Redis queue (docker-enterprise)
        huey_app.py               ─ Huey app (sqlite or redis broker)
    storage/
        file/                     ─ JSON file vault (legacy)
        sqlite/                   ─ SQLite vault
        postgres/                 ─ Postgres vault
    vault/                        ─ Starter tools, shadows, skills, scrolls (shipped data)
    worker.py                     ─ Huey consumer entry point
    migrations/                   ─ JSON → DB one-shot migration tool
```

---

## Storage backends

| Mode | Vault | Supervisor queue | Huey broker |
|---|---|---|---|
| `local` | SQLite (or JSON file in legacy mode) | SQLite | SQLite |
| `docker-local` | Postgres | SQLite | SQLite |
| `docker-enterprise` | Postgres | Redis (durable) | Redis |

The `SqliteVault` auto-seeds from the shipped JSON starter content on
first boot — operators don't see an empty dashboard after install.
The migration tool (`python -m systemu.migrations.json_to_db`) lets
pre-pivot installs move from JSON to Postgres without losing data.

---

## Memory layer

Five tiers, named to extend the project's existing vocabulary.  See
[`docs/memory-model.md`](docs/memory-model.md) for the full
contract; the short version:

- **Identity** — `Shadow.identity_block` + `Shadow.accumulated_voice`
  (planned for a follow-up PR — today the existing single
  `system_prompt` field stands in for both).
- **Active Context** — `ExecutionContext` + scratchpad, cleared at
  end of execution.
- **Shadow Memory** — `SHADOW_MEMORY.md` + per-shadow
  `memory_buffer.jsonl`, consolidated by the Memory Consolidator.
- **Elder Memory** — `ELDER_MEMORY.md` + `elder/memory_buffer.jsonl`,
  cross-Shadow operator-level preferences.
- **Archive** — Scroll repository + completed executions + capture
  sessions, fetched on demand via `LOAD_RESOURCE`.

Three write-contract rules prevent split state:

1. Single source of truth per claim type — each claim lives in
   exactly one tier.
2. Writes always go to the most-specific tier; promotion happens
   only via the consolidator.
3. Reads cascade narrow → broad (Active → Shadow → Elder → Archive).

Use `Vault.append_shadow_memory_buffer(...)` /
`Vault.append_elder_buffer(...)` to write — these are the only
sanctioned writers for new code; they stamp tier provenance and
reject cross-tier writes.

---

## Security model

Single-operator deployment by default:

- Dashboard binds to `localhost` and is unauthenticated.  Exposing it
  to a network requires the operator to put it behind their own
  auth layer.
- LLM outputs (Scrolls, tool specs, decisions) are untrusted data.
  Every code-generating pipeline routes through an approval gate
  (`/scrolls`, `/notifications`, `/tools` "pending" tab).
- The Tool Sandbox isolates tool execution.  Two backends today
  (in-process subprocess and Docker sidecar); the roadmap adds an
  explicit `ToolBackend` protocol with `ssh` and `wsl` backends in a
  future phase.
- `SECURITY.md` describes responsible disclosure and the threat
  surface in scope.

---

## Where to look next

- **First-run walkthrough** → [`docs/getting-started.md`](docs/getting-started.md)
- **Operator reference** → [`USER_GUIDE.md`](USER_GUIDE.md)
- **Redis topologies** → [`docs/redis-topologies.md`](docs/redis-topologies.md)
- **Migration from pre-pivot installs** → [README → Migrating from a pre-pivot install](README.md#migrating-from-a-pre-pivot-install)
- **Contribution flow** → [`CONTRIBUTING.md`](CONTRIBUTING.md)
