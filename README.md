<p align="center">
  <img src="systemu/interface/assets/logo.png" alt="Systemu" width="128" height="128">
</p>

<h1 align="center">Systemu</h1>

> **A personal AI workforce you instruct in plain language or by showing it —
> and that grows the capabilities it needs to finish the job, under your
> governance.**
>
> Ask a quick question and get an answer in seconds. Hand a whole task to a
> chat and an AI specialist runs it end-to-end. Or record a task on screen
> once and replay it forever. However you instruct it, when the agent hits
> something it lacks mid-run — a tool that doesn't exist, a skill it wasn't
> given, a file it can't read — it doesn't fail and it doesn't fake it. It
> **requests** the missing capability, and an always-on **Governor** grants,
> denies, or escalates the request by risk. Every action gated, logged, and
> local. Self-provisioning, made safe.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.20816383.svg)](https://doi.org/10.5281/zenodo.20816383)

**Three ways to put it to work:**

- 💬 **Ask** — a quick question in Chat comes back in seconds (plan-first, and
  honest when an answer is only partial).
- 🗣️ **Delegate** — describe a whole task in plain language; an AI specialist
  runs it end-to-end through the governed pipeline.
- 🎬 **Demonstrate** — record a task on screen once; Systemu turns it into a
  reusable workflow you replay in one click.

Tell it or show it — verbal or visual. Either way, the agent assembles the
capabilities it needs as it goes, under your approval.

## Why Systemu is different

Most automation is **frozen at design time.** RPA scripts break when a
selector moves; agent frameworks can only use the tools you wired up in
advance. But the capability an agent *actually* needs is usually discovered
mid-task — a tool that doesn't exist yet, a skill it wasn't given, a file
it can't read. The system guesses the toolkit up front, and the agent —
the one actually doing the work — can't ask for more.

Systemu inverts that. Instead of the system **pushing** a fixed harness to
the agent, the running agent **pulls** the capabilities it lacks at runtime,
and an always-on **Governor** arbitrates every request by risk — auto-granting
the safe, escalating the rest to you. The agent assembles its own harness,
just-in-time, under governance. We named the pattern **Reverse-Harness** and
[built a benchmark for it](#the-reverse-harness-pattern).

It starts where RPA starts — record a task once — and goes where RPA can't:
the agent grows to finish the job. Built for work that has consequences:

* **Governed self-provisioning** — forging a tool, attaching an MCP server,
  reading a secret, spawning sub-agents: each is a *request* the Governor
  grants, denies, or escalates. High-risk requests always land as one card
  in your Inbox with a plain-English summary and a safe default.
* **Local-first** — your recordings, workflows, memory, and results live
  in a vault on your machine. API keys are never typed into the browser.
* **Honest by construction** — tool results are verified (a no-output call
  is a failure, not a phantom success), outcomes report file paths you can
  open, and "couldn't do it" is never dressed up as done.

---

## The Reverse-Harness pattern

<p align="center">
  <img src="docs/assets/reverse-harness.svg" alt="The Reverse-Harness loop: a running agent hits a capability gap, issues a REQUEST_HARNESS pull, the Governor arbitrates by risk (low auto-grants, medium goes to an off-path judge, high escalates to the operator Inbox), the grant is materialised across six families, leased and logged, and the run resumes." width="760">
</p>

Classic agent harnesses are **push**: the system decides, at design time,
which tools and permissions an agent gets. Reverse-Harness flips it to
**pull** — the running agent proposes the capabilities it needs and a
governance layer arbitrates them live. `REQUEST_HARNESS` becomes a
first-class loop verb, the inverse of `TOOL_CALL`: *"provision a capability
I lack"* vs *"use one I have."*

```
   PUSH  (classic harness)            PULL  (Reverse-Harness)
   design time · fixed                runtime · just-in-time
   ──────────────────────            ──────────────────────────
   system picks the tools             agent hits a gap mid-task
            │                                    │
            ▼                                    ▼
   agent is frozen with              REQUEST_HARNESS:
   whatever it was handed            "provision what I lack"
            │                                    │
            ▼                                    ▼
   gap at runtime → it fails         Governor arbitrates by risk
                                     (grant · deny · escalate)
                                                 │
                                                 ▼
                                     capability leased + logged,
                                     revocable → the run continues
```

It generalizes capability acquisition from one class (tools, at design time)
to **six families the Governor arbitrates at runtime:**

| Family | The agent requests… | Default gate |
|---|---|---|
| **Tool** | a new executable tool (forge), or reuse of an existing one | reuse auto-grants; **new code escalates** |
| **Skill** | a procedure (`SKILL.md`) — new or reused | reuse low-risk; new text → review |
| **Access** | reading a file / resource / secret | whitelisted read low; **write / secret / network escalates** |
| **Compute** | more iterations / think-budget | within ceiling low; **over ceiling escalates** |
| **Sub-agent** | a bounded fleet of parallel child agents | depth + budget clamped; beyond → escalate |
| **MCP** | attaching a Model Context Protocol server | re-attach low; **new server escalates** (SSRF-guarded, tool-hash-pinned) |

The arbitration rests on two ideas we think are non-obvious:

1. **A self-requested capability is *more* dangerous than a pre-provisioned
   one** — the agent chose it — so it is gated *more* strictly, not less.
2. **Judgment can only ever downgrade toward safe.** When an ambiguous
   request needs an LLM judge, the judge may deny or escalate — it can
   **never** grant beyond policy or "open a hole." A judge fault fails to
   *escalation*, not to *grant*.

Every grant is **leased and logged**: minted on grant, written to a per-run
**decision-audit ledger** with its outcome, and revocable in one click —
each self-built capability carries the provenance of the run that made it.
We distilled the whole thing into **six reusable patterns** —
Pull-Provisioning, the `REQUEST_HARNESS` verb, Risk-Tiered Arbitration,
Attributed & Revocable Self-Grants, Off-Path Judgment, and a Provenance
Ledger — and benchmarked it (see [Evidence](#evidence)).

### What it looks like

You hand a folder of scanned invoices to a chat: *"pull the totals into a
spreadsheet."* The specialist starts — and finds it has no PDF-table
extractor. Instead of failing, it requests one:

1. `REQUEST_HARNESS{ kind: tool — "extract a table from a PDF" }`
2. New code is **HIGH risk**, so the Governor escalates: one card lands in
   your Inbox — *"forge `pdf_table_extract`? [view code] · [approve] · [deny]."*
3. You approve. Systemu writes the tool, **dry-runs it** to prove it works,
   deploys it with an *agent-built* badge, and the run resumes — now holding
   the capability it lacked thirty seconds ago.
4. Next time, the tool already exists: no request, no gate, instant.

Notice the gap → request → govern → grow. That's the whole loop.

## How Systemu grows itself

Self-provisioning isn't a one-off. Capabilities the agent acquires persist
(attributed and revocable), and the system keeps making them better:

- **Forge tools on demand** — a missing tool is written (spec → code),
  **dry-run-validated behind a gate**, then deployed as a first-class,
  reusable tool. You see exactly what the agent built itself, with an
  *agent-built* badge, and can revoke it anytime.
- **Recalibrate what's inadequate** — when a tool or skill keeps failing,
  the runtime diagnoses it and either repairs it in place or forks a
  specialized version — re-validated before it ships.
- **Evolve over time** — an evolution engine reviews real runs and
  *proposes* improvements (merge duplicate specialists, upgrade a persona
  with a discovered skill). Proposals land in your Inbox; nothing
  auto-applies.
- **Remember** — episodic memory captures what each run learned, a curator
  consolidates skills over time (archive, never delete), and a capability
  ledger tracks what actually works.

Every one of these is **governed**: forging, recalibration, and evolution
surface as approval cards by default. The agent gets more capable; you stay
in control of what it keeps.

## Evidence

Reverse-Harness is being **validated, not just asserted.** Our Capability-Gap
Benchmark puts it through tasks that are *impossible without acquiring a missing
capability*, across the six families and multiple frontier models, in three
conditions: a **frozen-harness baseline** (no pull), **governed pull** (the full
Governor), and **pull without the LLM judge** — graded by an **external oracle**,
never the system's own verifier (which would be circular).

It targets the load-bearing question of self-provisioning that, to our
knowledge, no prior benchmark isolates: **does the agent know when it's blocked,
and does it request the *right* capability?** — pull-decision precision/recall,
request appropriateness (premature / wasted / unused), governance cost (the
deterministic-vs-LLM split), and per-family efficacy.

Two properties hold **by construction**, independent of any run:

- **Bounded safety.** Every high-risk request escalates regardless of
  configuration; a judge fault escalates; a Governor failure can only ever deny
  or escalate — never grant. These are verified as explicit safety properties,
  not hoped-for behavior.
- **Cost-disciplined governance.** Deterministic policy resolves the easy
  majority for free; the LLM judge is reserved for genuinely ambiguous cases.

The headline result: across **179 trials** over **5 models / 5 vendors**, a
frozen-harness baseline succeeds on **6%** of gap-bearing tasks and governed pull
on **61%**, recovering **~60%** of the baseline's failures at modest cost. The full
results — the recognition rate and request-outcome taxonomy, governance cost, and
the bounded-safety verification — are in the preprint.

📄 **Preprint:** [*Reverse-Harness: Design Patterns for Runtime, Agent-Initiated
Capability Provisioning under Governance*](docs/Reverse-Harness-preprint.pdf) —
Rameswaran Mohan, 2026. Preprint, not yet peer-reviewed; licensed
[CC&nbsp;BY&nbsp;4.0](https://creativecommons.org/licenses/by/4.0/). Every number
is reproducible from [`cgb_eval/`](cgb_eval) and [`cgb_results/`](cgb_results) via
`python -m cgb_eval.paper_numbers`.

**Cite (DOI):** [10.5281/zenodo.20816383](https://doi.org/10.5281/zenodo.20816383)

```bibtex
@misc{mohan2026reverseharness,
  title  = {Reverse-Harness: Design Patterns for Runtime, Agent-Initiated
            Capability Provisioning under Governance},
  author = {Mohan, Rameswaran},
  year   = {2026},
  note   = {Preprint},
  doi    = {10.5281/zenodo.20816383},
  url    = {https://doi.org/10.5281/zenodo.20816383}
}
```

## Quick start

```bash
pip install systemu
```

In your chosen working directory:

```bash
sharing_on init           # seeds the starter catalog (41 tools, idempotent)
sharing_on setup          # pick your LLM provider + model preset, store keys securely
sharing_on daemon start
```

`sharing_on setup` walks you through choosing a provider (OpenRouter, Google,
OpenAI, Anthropic, or a local Ollama) per tier and stores the keys in a local
`.env` — entered hidden, never echoed, never typed into a browser. Skip it and
`daemon start` runs the same flow on first launch.

Open <http://localhost:8765>. A short setup wizard and guided tour take it from
there: confirm your models, say who you are, run a starter task — then hit
**Record** and teach it something real.

**The one-page guide:** [OPERATOR-SOP.md](OPERATOR-SOP.md) — the
record → approve → run → results loop, what each approval card means, and
a troubleshooting table. New to the vocabulary?
[docs/glossary.md](docs/glossary.md) maps Systemu terms to industry ones.

Docker (Postgres-backed) and enterprise (Redis-scaled) modes:

```bash
git clone <this repo> && cd <repo>
python install.py --mode docker-local     # or docker-enterprise
```

## What's in the box

- **Sharing-On** (`sharing_on`) — the capture engine: records screenshots,
  window switches, file changes, and input while you demonstrate a task,
  then turns the recording into accurate plain-English instructions.
- **Systemu runtime** — executes workflows through AI **Shadow** agents
  (specialists created per job, with your approval), a curated 41-tool
  registry that works out of the box, MCP connector support, episodic
  memory, and an evolution engine that proposes improvements from real runs.
  A Reverse-Harness Governor arbitrates the capabilities a running agent asks
  for, writes a per-run decision-audit trail, and — opt-in — can fan a
  decomposed goal out to a bounded fleet of parallel sub-agents.
- **Bring your own model** — choose a provider per tier: OpenRouter, Google,
  OpenAI, Anthropic (native SDK), or a local **Ollama** (keyless, on-device).
  Presets — budget / balanced / quality — set the cost/quality dial in one
  keystroke; `sharing_on setup` or Settings stores the keys.
- **The dashboard** — a command center: **Home · Work · Shadows · Build ·
  Insights · Settings**, a persistent *Needs you* + *Live* rail, and one
  Decisions Inbox where every approval lands. Quick tasks answer in
  seconds from Chat; recorded workflows re-run in one click.

**📚 More:**
[Getting Started](docs/getting-started.md) ·
[Architecture](ARCHITECTURE.md) ·
[User Guide](USER_GUIDE.md) ·
[Contributing](CONTRIBUTING.md)

---

## How it works

```
You perform a task on your computer
          │
          ▼
Sharing-On records: screenshots, window switches,
  file changes, clipboard, process events
          │
          ▼
Intent extractor (Tier-2 LLM) infers what you
  actually wanted — written to intent.json, not
  inferred from the click sequence              (v0.6.0)
          │
          ▼
Scroll refiner turns the intent + abstracted
  steps into a structured Scroll with objectives
          │
          ▼
Pre-flight scroll validator (opt-in) checks
  satisfiability + intent-vs-tool fit;          (v0.4.0 + v0.6.0)
  surfaces a side-by-side remediation card
  with a proposed_revision when blocked         (v0.6.0)
          │
          ▼
Activity extractor selects tools and skills
  via data-flow reasoning (schemas in headers,
  not just keyword name match)                  (v0.6.0)
          │
          ▼
Missing tools forged with intent context →
  dry-run validation gate (Gate 3.5)            (v0.5.0)
          │
          ▼
Shadow decision picks an existing specialist OR
  creates a new one, scoring on semantic intent
  match plus skill/tool ID overlap              (v0.6.0)
          │
          ▼
Supervisor dispatches the Shadow.  Intelligent
  Supervisor (opt-in) intervenes between
  iterations with bounded actions including
  RECALIBRATE_TOOL / RECALIBRATE_SKILL when
  capabilities are structurally inadequate
          │
          ▼
Reverse-Harness Governor arbitrates capability
  requests the running Shadow PULLs — a missing
  tool, a dependency, an escalation, or a fan-out
  to parallel sub-agents (opt-in).  Under the
  default risk-tiered gate mode it auto-grants
  low-risk requests and escalates the rest to the
  Decisions Inbox; on approval the run resumes.
  Every iteration's decision is written to a
  per-run decision-audit ledger
          │
          ▼
Dashboard shows live progress, results,
  per-shadow + per-tool metrics, memory, and the
  Decisions Inbox for every operator gate
```

A deeper walkthrough of every stage lives in
[`ARCHITECTURE.md`](ARCHITECTURE.md).

---

## Dashboard

The web dashboard (default <http://localhost:8765>) is organised as a
six-spine command center. The left sidebar has exactly six entries:

| Spine | Route | What it holds |
|---|---|---|
| **Home** | `/` | Overview — stat cards, the workflow pipeline, and the live activity feed |
| **Work** | `/work` | The workflow-centric view; Scrolls + Activities fold in here |
| **Shadows** | `/shadows` | The Shadow roster (agent personas) and their per-shadow memory |
| **Build** | `/tools` | Tool registry (with an *agent-built* filter for tools Systemu forged itself); Skills and Evolution proposals fold in here |
| **Insights** | `/insights` | Memory, the capability flywheel, and the event stream (tabbed) |
| **Settings** | `/settings` | LLM tier config, the gate-mode dial, and approval defaults |

Two surfaces are present on **every** page:

- **Right rail** — a persistent panel showing what *Needs you* (a glance
  at pending gates) and *Live* (a feed of in-flight runs). On narrow
  viewports it collapses to a "Needs you (N)" badge in the header.
- **Decisions Inbox** (`/inbox`) — the single place every approval gate
  lands as one unified card: scroll-approval, dependency, tool-forge,
  evolution, harness-escalation, and recovery gates. **Approve executes**
  — approving a card runs the same action the CLI would (e.g. approving a
  scroll triggers activity extraction).

### Gate modes

Settings exposes a gate-mode dial that controls how the runtime handles
approval gates:

| Mode | Behaviour |
|---|---|
| **Risk-tiered** (default) | The Governor auto-grants low-risk requests and escalates the rest to the Inbox |
| **Approve-only** | Every gate waits for the operator |
| **Bypass** | Auto-grants every gate **except** the safety floor (dependency/recovery gates) — dev/test only |

A safety **floor** keeps dependency and recovery gates interactive even
under Bypass unless explicitly disabled. The same dial is available from
the CLI via `sharing_on decisions mode`.

> **Legacy URLs still work.** `/army` redirects to `/shadows`;
> `/systemu-chat`, `/memory`, `/flywheel`, and `/notifications` redirect
> into their merged tabs. The old `/workshop` route is gone — its scroll
> rebuild is now an in-place dialog on the Scrolls view.

---

## Prerequisites

### Resource minimums (verified during the manual smoke run)

| Resource    | `local`        | `docker-local` | `docker-enterprise`              |
|-------------|----------------|----------------|----------------------------------|
| CPU cores   | 2              | 2              | 4                                |
| Free RAM    | 4 GB           | 6 GB           | 8 GB (Redis + Postgres + workers)|
| Free disk   | 2 GB           | 4 GB           | 6 GB                             |
| Network     | LLM API access | + Postgres     | + Redis                          |

### Software

| Requirement   | Version | Notes |
|---------------|---------|-------|
| Python        | 3.10+ (3.12 tested) | Required for all modes |
| pip           | latest  | `pip install --upgrade pip` |
| Git           | 2.30+   | Required for `./install.sh` |
| Docker        | 24+ / Desktop 4.x | Required for `docker-local` and `docker-enterprise` |
| Node.js + npm | 18+     | Optional — only for the Chrome capture extension |

### OS support

| OS                  | Native capture | docker-* modes  |
|---------------------|----------------|-----------------|
| Windows 10 / 11     | ✅ verified    | ✅ verified     |
| macOS 13+           | ⚠️ partial     | ✅              |
| Ubuntu 22.04+       | ⚠️ needs `xdotool xclip` | ✅      |

**Linux capture extras:**

```bash
sudo apt install xdotool xclip      # Debian / Ubuntu
sudo dnf install xdotool xclip      # Fedora
```

### LLM access

Systemu calls models in three tiers and you choose a **provider per tier** — mix
and match, or use one everywhere. You need credentials for **at least one** of:

- [OpenRouter](https://openrouter.ai) (free tier works) — the default; one key reaches many models
- [Google AI Studio](https://aistudio.google.com) (free)
- [OpenAI](https://platform.openai.com)
- [Anthropic](https://console.anthropic.com) — native SDK (install the `anthropic` extra)
- A local [Ollama](https://ollama.com) instance on `:11434` — keyless, on-device

`sharing_on setup` collects the keys (hidden entry, stored in `.env`) and the
Settings page lets you switch providers, models, and the budget / balanced /
quality preset anytime.

---

## Install from source (Docker & enterprise modes)

Installed with `pip` above? You're done — this section is only for running the
Docker / enterprise stacks or hacking on the code. Full walkthrough lives in
[docs/getting-started.md](docs/getting-started.md). The headline:

```bash
git clone https://github.com/rameswaran-mohan/project-systemu.git
cd project-systemu
./install.sh        # Linux/macOS    (or  install.bat  on Windows)
./start.sh          # Linux/macOS    (or  start.bat    on Windows)
```

`install.sh` asks which deployment mode you want and sets everything up. Three options:

| Mode | What you get | Best for |
|---|---|---|
| **local** | Native venv. Daemon + worker run as detached subprocesses. SQLite vault + Huey-SQLite broker. | Single-machine dev / personal use. |
| **docker-local** | docker-compose. Postgres vault + Huey-SQLite broker. One worker container. | Hobbyist self-hosting on one box. |
| **docker-enterprise** | docker-compose. Postgres vault + Redis broker. N worker containers (scale via `WORKER_REPLICAS`). | Production / multi-host. |

The dashboard runs at [http://localhost:8765](http://localhost:8765) in every mode.
`./stop.sh` (or `stop.bat`) shuts everything down cleanly.

To re-run installer after changing your mind: `./install.sh` will detect the existing
install and offer **reconfigure** / **upgrade-deps** / **quit**.

To upgrade an existing install to the latest release: `./update.sh`
(or `update.bat`). It stops the daemon, `git pull --ff-only`s, reinstalls deps,
runs alembic migrations, and restarts. Pass `--yes` / `/y` for non-interactive
CI / cron usage. Refuses on a dirty working tree.

### Non-interactive install (CI / automation)

```bash
./install.sh --mode docker-enterprise --non-interactive \
    --pg-password=hunter2 --redis-password=hunter3 \
    --worker-replicas=4 \
    --openrouter-key=sk-... --google-key=AIza...
```

### Record a workflow (optional)

After `./start.sh`:

```bash
sharing_on record --name "My workflow"
# Press Ctrl+C when done — Systemu converts the recording into a Scroll
```

> **Windows note (v0.7.3):** Use **Ctrl+C** directly in the same terminal where
> `sharing_on record` is running. Sending SIGINT from another process via
> `kill -INT <pid>` (e.g. from Git Bash or a background script) may not
> deliver the signal to the Python child reliably — the session may stop
> without writing its final `end_time`, leaving `session.json` looking
> half-complete. Events in `events.db` are still complete and the session
> is fully usable by `sharing_on analyze`.

### Export a recorded workflow as a portable Agent Skill

Once a recording has been analyzed, one command turns it into a portable
[Anthropic Agent Skill](https://www.anthropic.com/news/skills) bundle that
any Agent-Skills-compatible runtime (Claude Code, Cursor, etc.) can load:

```bash
sharing_on capture export-skill ./captures/<your_session_dir> \
           --output ./my-skill
# -> ./my-skill/<kebab-name>/SKILL.md
```

Validate the bundle with `skills-ref validate ./my-skill/<kebab-name>`.

### Legacy / advanced Docker profiles

The original profiles are still in `docker-compose.yml` for backwards compatibility:

```bash
docker compose up systemu                          # legacy file backend
docker compose --profile docker-sandbox up systemu-docker   # tool sandbox
```

---

## Migrating from a pre-pivot install

If you already have a JSON-vault deployment from before the holistic-enterprise
pivot and want to move to **docker-local** or **docker-enterprise**, run the
one-shot migration tool after spinning up the new Postgres:

```bash
# 1. Start the new stack so Postgres is up + tables created
./install.sh --mode docker-enterprise --skip-pull --pg-password=<your-pg> --redis-password=<your-redis>
docker compose --profile enterprise up -d postgres
alembic upgrade head     # creates tables in the new Postgres

# 2. Dry-run — see what would migrate
python -m systemu.migrations.json_to_db \
    --source ./systemu/vault --dry-run

# 3. Run for real
python -m systemu.migrations.json_to_db \
    --source ./systemu/vault \
    --target "postgresql://systemu:<pg-password>@localhost:5432/systemu"
```

The migration is **idempotent** — re-running it after fixing any errors leaves
already-migrated rows untouched.  See `systemu/migrations/json_to_db.py` for
the source list (scrolls, shadows, tools, skills, activities, evolutions,
chat history).

For Redis topologies beyond the default standalone (TLS, Sentinel, custom CA),
see [`docs/redis-topologies.md`](docs/redis-topologies.md).

---

## Configuration

Every setting lives in your `.env` file — copy [`.env.example`](.env.example)
(each variable is documented inline) as a starting point, or let
`sharing_on setup` and the dashboard **Settings** page write them for you. The
ones you'll actually touch:

| Variable | Default | What it does |
|---|---|---|
| **API key** (one of) | — | `OPENROUTER_API_KEY` (default, many models), `GOOGLE_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` (needs `systemu[anthropic]`), or `OLLAMA_URL` (local, keyless). At least one. |
| `SYSTEMU_MODEL_PRESET` | `budget` | Cost/quality dial: `budget` \| `balanced` \| `quality`. Override any tier with `SYSTEMU_TIER{1,2,3}_MODEL`; an explicit tier model always wins. |
| `SYSTEMU_STORAGE` | `sqlite` (local) | `file` \| `sqlite` \| `postgres` — set by `install.py` per mode. |
| `SYSTEMU_DASHBOARD_PORT` | `8765` | Dashboard port. |
| `SYSTEMU_OUTPUT_DIR` | `~/Documents` | Where agent-generated files land. |
| `SYSTEMU_NON_INTERACTIVE` | `false` | Auto-pick the safe default in every approval prompt (dev/CI only). |
| `SYSTEMU_DELEGATE_USE_PARALLEL` | `false` | Opt in to parallel sub-agent fan-out for granted SUBAGENT requests. |

The full set — per-tier models + providers, queue/Redis, Docker host binds,
the Intelligent-Supervisor budget knobs, pre-flight validators, recalibration
auto-approve, persona dials, and capture intervals — is documented inline in
[`.env.example`](.env.example) and editable from the **Settings** page.

---

## Storage Modes

`install.py` writes `SYSTEMU_STORAGE=sqlite` to `.env` for `local` mode and `postgres` for `docker-local` / `docker-enterprise`. The in-process default when no env is set is `file` (kept for backward compat with pre-v0.3 installs).

### `SYSTEMU_STORAGE=sqlite` (default for `local` mode)

- SQLite database at `SYSTEMU_DATABASE_URL`, e.g. `sqlite:///./data/systemu.db`
- Durable task queue with crash recovery + orphan requeue
- Dashboard and worker run as separate processes
- Alembic migrations run automatically on first start
- Recommended for single-machine deployments

### `SYSTEMU_STORAGE=postgres` (default for `docker-local` / `docker-enterprise`)

- PostgreSQL backend (managed by docker-compose)
- Multi-machine / multi-worker deployments
- Same Alembic migrations as SQLite

### `SYSTEMU_STORAGE=file` (legacy)

- State stored as JSON files in `systemu/vault/`
- Zero external dependencies
- Kept for backward compatibility; use the migration tool below to move to SQLite or Postgres

**Migrating from file → SQLite or Postgres:**

```bash
SYSTEMU_STORAGE=sqlite SYSTEMU_DATABASE_URL=sqlite:///./data/systemu.db \
  python -m systemu.migrations.json_to_db --source ./systemu/vault --dry-run
```

See the `Migrating from a pre-pivot install` section above for the Postgres path.

---

## Project Structure

```
project-systemu/
├── sharing_on/                         — Capture engine + analyser
│   ├── collectors/                       — Screen, clipboard, file, window monitors
│   ├── analyzer/                         — Step detector, narrative generator
│   │   ├── intent_extractor.py             — v0.6.0 Tier-2 pre-pass that infers
│   │   │                                     outcome-oriented intent before the
│   │   │                                     narrative LLM runs (intent.json)
│   │   └── prompts/                        — Analyzer prompt library
│   ├── output/                           — instructions.md renderer
│   └── cli.py                            — `sharing_on` command entry point
│
├── systemu/                            — Systemu runtime
│   ├── core/                             — Pydantic models (Shadow, Scroll,
│   │                                       Activity, Tool, Skill, Objective…)
│   ├── pipelines/                        — Stage 1→6 transformations
│   │   ├── scroll_refiner.py               — Stage 2 — intent + objectives
│   │   ├── scroll_validator.py             — Pre-flight intent-aware check
│   │   ├── scroll_remediator.py            — v0.6.0 side-by-side fix card
│   │   ├── activity_extractor.py           — Stage 3 — schema-aware extraction
│   │   ├── skill_validator.py              — v0.6.0 GUI-codification check
│   │   ├── skill_recalibrator.py           — v0.6.0 re-author instructions_md
│   │   ├── tool_forge.py                   — Spec → code → save (Gate 1/2)
│   │   ├── tool_dry_run.py                 — v0.5.0 Gate 3.5 validation
│   │   ├── tool_recalibrator.py            — v0.5.0 bump-vs-fork pipeline
│   │   ├── tool_inadequacy_diagnosis.py    — v0.5.0 supervisor diagnosis
│   │   ├── shadow_decision.py              — Stage 5 — intent-aware tiebreak
│   │   ├── refinery.py                     — Post-execution memory consolidation
│   │   ├── evolution_engine.py             — Long-term shadow/skill evolution
│   │   ├── memory_consolidator.py          — Tiered memory consolidation
│   │   ├── cross_shadow_patterns.py        — Promotion of recurring lessons
│   │   └── workshop_module.py              — Operator-driven scroll/shadow edit
│   ├── runtime/                          — Shadow ReAct loop + Supervisor
│   │   ├── shadow_runtime.py               — Per-shadow execute loop
│   │   ├── supervisor.py                   — Activity queue + worker pool
│   │   ├── execution_mind.py               — Intelligent Supervisor (v0.4.0)
│   │   ├── execution_snapshot.py           — v0.5.1 true snapshot resume
│   │   ├── failure_classifier.py           — 10-category failure taxonomy
│   │   ├── tool_metrics.py / shadow_metrics.py — per-id telemetry
│   │   ├── affinity_log.py                 — Activity-shadow routing memory
│   │   ├── inadequacy_tracker.py           — Cross-shadow tool-inadequacy clustering
│   │   ├── rejection_store.py              — Operator-feedback learning
│   │   ├── governor.py                      — Reverse-Harness Governor (arbitrate + materialise capability PULLs)
│   │   ├── harness_arbiter.py               — Deterministic GRANT/DENY/ESCALATE policy
│   │   ├── subagent_fleet.py / subagent_harness.py — opt-in parallel child fan-out + partial-success collation
│   │   ├── decision_audit.py                — per-iteration decision ledger (executions/<id>/decision_audit.jsonl)
│   │   ├── gate_mode_settings.py            — Gate-mode dial (bypass / risk-tiered / approve-only) + floor
│   │   ├── tool_sandbox.py                 — Subprocess / docker / wsl / ssh exec
│   │   └── tool_registry.py                — Runtime tool loader
│   ├── interface/                        — NiceGUI dashboard + REST API
│   │   ├── pages/                          — Home, Work, Shadows, Build, Insights, Settings, Inbox, Chat
│   │   ├── command/                         — Shared command layer (Inbox queue, gates, verbs)
│   │   └── cli_commands.py                  — Systemu CLI groups (scrolls/army/tools/skills/decisions/…)
│   ├── messaging/                        — Optional Telegram gateway
│   ├── prompts/                          — Tier-1/2/3 prompt library
│   ├── queue/                            — In-process / SQLite / Redis priority queues
│   ├── storage/sqlite/                   — SQLite + Postgres vault (SQLAlchemy)
│   ├── vault/                            — File-based vault + starter pack
│   │   ├── tools/                          — Starter tool implementations
│   │   ├── shadow_army/                    — Starter Shadow configurations
│   │   └── skills/                         — Starter SKILL.md files (Anthropic
│   │                                         Agent Skills Standard compatible)
│   ├── scheduler/                        — Daemon + recurring jobs
│   └── worker.py                         — Background worker entry point
│
├── alembic/versions/                   — DB schema migrations (0001–0010)
├── extension/                          — Chrome extension for web-event capture
├── docs/                               — Architecture, getting-started, messaging
├── tests/                              — pytest suite
├── docker-compose.yml
├── Dockerfile
├── install.py / install.sh / install.bat
├── start.sh / start.bat / stop.sh / stop.bat
└── .env.example
```

---

## sharing_on Capture

sharing_on records what you do and produces:

```
captures/
└── my_task_cap_YYYYMMDD_HHMMSS/
    ├── instructions.md       ← Step-by-step workflow guide
    ├── session.json          ← Session metadata
    ├── events.db             ← Raw captured events
    └── assets/               ← Screenshots embedded in instructions.md
```

The `instructions.md` is converted into a Systemu **Scroll** when you submit the capture to the dashboard.

**Privacy:** keystrokes are NOT recorded; clipboard auto-redacts secrets; no data leaves your machine until the LLM analysis step.

---

## CLI reference

Everything in the dashboard is also driven from the `sharing_on` CLI.
Run `sharing_on --help` (or `sharing_on <group> --help`) for the full
surface; the headline groups:

| Command | Purpose |
|---|---|
| `sharing_on record` / `analyze` | Capture a workflow / re-analyze a recorded session |
| `sharing_on init` | Seed the working-directory vault from the bundled starter catalog |
| `sharing_on setup` | Pick the LLM provider + model preset per tier and store keys securely (hidden entry → `.env`); auto-runs on first `daemon start` if unconfigured |
| `sharing_on daemon start` / `stop` / `status` | Run the background daemon + web dashboard |
| `sharing_on doctor <id>` | Diagnose pending gates/blockers for a scroll/activity/shadow/tool (`--apply` to auto-fix) |
| `sharing_on scrolls list` / `show` / `refine` / `approve` | Manage Scrolls (refined SOPs) |
| `sharing_on army list` / `show` / `awaken` / `execute` | Manage and run Shadows |
| `sharing_on tools list` / `forge` / `dry-run` / `enable` / `recalibrate` | Manage the tool registry + its forge gates |
| `sharing_on skills list` / `export` / `deprecate` | Manage Skills (export to a portable Agent Skill) |
| `sharing_on evolve run` / `show-pending` / `apply` | Run and apply the Evolution Engine |
| `sharing_on decisions list` / `mode` / `resolve` | The Decisions Inbox from the terminal; `mode` sets the gate-mode dial |
| `sharing_on chat submit` / `history` | Run a free-text task through the full pipeline |
| `sharing_on settings show` / `set` | Inspect / write allow-listed configuration |
| `sharing_on session` · `capability` · `skill` · `user` | Inspect episodic memory, the capability ledger, bundled skills, and your profile |

---

## Development

```bash
# Install with dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Apply database migrations
alembic upgrade head

# Generate a new migration after model changes
alembic revision --autogenerate -m "describe_change"
```

---

## Contributing

Pull requests are welcome — from humans **and** AI agents.  See
[`CONTRIBUTING.md`](CONTRIBUTING.md) for the contribution flow,
including the explicit guidelines for AI-authored PRs.

* Report bugs / suggest features → [issue tracker](https://github.com/rameswaran-mohan/project-systemu/issues)
* Security disclosures → [`SECURITY.md`](SECURITY.md)
* Community expectations → [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md)
* Release notes → [`release-notes/`](release-notes/) — one file per version, written at release time

---

## Project status

**Pre-1.0** — current release **v0.9.62**. It's used daily, but APIs and
behavior can still change. Full per-version history lives in
[`release-notes/`](release-notes/).

**The arc so far:** a capture engine + three deployment modes → the Intelligent
Supervisor and tool-readiness pipeline (forge → dry-run → recalibrate) →
intent-aware extraction → the Decisions Inbox + gate-mode dial → the
**Reverse-Harness Governor**: capability-pull arbitration across all six
families, scoped leases, a per-run decision-audit ledger, pip-first onboarding,
per-tier providers (OpenRouter/Google/OpenAI/Anthropic/Ollama), MCP connectors,
and opt-in parallel sub-agent fan-out.

### What's next

The next-phase work is open for design.  Likely candidates (not yet scheduled):

- Auto-recalibration without operator approval for low-risk **skill** patterns (telemetry-gated promotion)
- The remaining harness provisioners — SKILL / ACCESS / COMPUTE (TOOL ships; SUBAGENT fan-out ships opt-in; ACCESS isolation is currently Docker-only / future work)
- Recursive sub-agent decomposition (today's fleet is one level deep)
- Multi-tenant deployment + per-operator vaults
- Hosted catalog of community-contributed tools / skills

If you want to contribute, [`CONTRIBUTING.md`](CONTRIBUTING.md) is the contribution flow.

---

## Troubleshooting

The fixes for what new users hit most:

- **Windows — `start.bat` prints "the system cannot find the drive specified":**
  cosmetic stderr from a stale `PATH` entry (an old mapped network drive). It
  doesn't affect startup — remove the dead entry from PATH.
- **Linux — capture records empty events:** `pynput` needs X11, but Ubuntu/Fedora
  default to Wayland. Log in with an Xorg session (the daemon, dashboard, and
  tools work fine on Wayland — only capture is affected) and
  `sudo apt install xdotool xclip`.
- **macOS — capture is empty:** grant **Accessibility** + **Screen Recording** to
  your terminal in System Settings → Privacy & Security, then restart the daemon.
- **`HTTP 401 from OpenRouter`:** the key is mistyped, revoked, or lacks model
  access — generate a fresh one at <https://openrouter.ai/keys>.
- **Daemon 500s with `no such column`:** the DB schema is behind the code —
  `./update.sh` (or re-running the installer) applies the migrations.

More environment-specific fixes — corporate proxy, Apple Silicon / Rosetta,
Docker host binds, older Python — are in [`USER_GUIDE.md`](USER_GUIDE.md). For
anything else, the [issue tracker](https://github.com/rameswaran-mohan/project-systemu/issues).

---

## License

MIT — see [`LICENSE`](LICENSE).
