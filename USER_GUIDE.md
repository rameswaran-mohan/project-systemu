# Systemu — User Guide

> Complete reference for installation, configuration, and operation.

---

## Table of Contents

1. [What Is Systemu?](#1-what-is-systemu)
2. [Prerequisites](#2-prerequisites)
3. [Installation](#3-installation)
4. [Configuration](#4-configuration)
5. [Starting the Application](#5-starting-the-application)
6. [The Web Dashboard](#6-the-web-dashboard)
7. [Recording a Task](#7-recording-a-task)
8. [Scrolls](#8-scrolls)
9. [Shadow Army](#9-shadow-army)
10. [Tools](#10-tools)
11. [Activities](#11-activities)
12. [Skills](#12-skills)
13. [Evolutions](#13-evolutions)
14. [Daemon](#14-daemon)
15. [UC Test Runner](#15-uc-test-runner)
16. [Benchmarks](#16-benchmarks)
17. [Project Structure](#17-project-structure)
18. [Troubleshooting](#18-troubleshooting)
19. [Quick Start (5 Minutes)](#19-quick-start-5-minutes)

---

## 1. What Is Systemu?

Systemu is a two-layer automation framework that runs on your local machine.

**Layer 1 — Sharing-On** records what you do on your computer (mouse clicks, keyboard input, window focus changes, browser navigation, file changes) and uses an LLM to generate a structured step-by-step guide (`instructions.md`) from the recording.

**Layer 2 — Systemu** refines that recording into a reusable **Scroll** (a structured SOP), optionally awakens an autonomous **Shadow** agent persona, and lets the Shadow re-execute the workflow on your behalf using real browser access, file operations, and generated tools.

**The pipeline:**
```
Record → Analyze → Scroll → Approve → Shadow → Execute
```

---

## 2. Prerequisites

| Requirement | Version | Notes |
|:------------|:--------|:------|
| Python | 3.10+ | Check: `python --version` |
| pip | Included with Python | |
| OpenRouter API key | Required | Free at openrouter.ai |
| Docker Desktop | Optional | Only for Docker sandbox mode |
| Playwright Chromium | Optional | Only for UC test runner |

---

## 3. Installation

### 3.1 Windows (one-time)

```bat
setup.bat
```

Creates `.venv`, installs all dependencies from `requirements.txt`, and registers the `sharing_on` CLI.

### 3.2 Linux / macOS (one-time)

```bash
./setup.sh
```

### 3.3 Manual Setup (if scripts fail)

```bash
python -m venv .venv

# Windows
.venv\Scripts\activate

# Linux / macOS
source .venv/bin/activate

pip install -r requirements.txt
pip install -e .
```

### 3.4 Install Playwright (UC test runner only)

```bash
python -m playwright install chromium
```

### 3.5 Verify

```bash
sharing_on info
sharing_on --version
```

---

## 4. Configuration

### 4.1 Create Your .env File

```bat
copy .env.example .env        # Windows
cp .env.example .env          # Linux / macOS
```

Open `.env` in any text editor and set your values.

### 4.2 Required

| Variable | Example | Description |
|:---------|:--------|:------------|
| `OPENROUTER_API_KEY` | `sk-or-v1-abc123` | Your OpenRouter API key. Get a free one at openrouter.ai. |

### 4.3 LLM Model Tiers (optional — defaults work fine)

| Variable | Default | Purpose |
|:---------|:--------|:--------|
| `SYSTEMU_TIER1_MODEL` | `gemini-3.1-flash-lite-preview` | Deep reasoning — scroll refinement, shadow decisions, evolution |
| `SYSTEMU_TIER2_MODEL` | `gemini-3.1-flash-lite-preview` | Structured output / code — tool forge, execution planning |
| `SYSTEMU_TIER3_MODEL` | `z-ai/glm-4.5-air:free` | Fast / cheap — event-to-instruction formatting |
| `SHARING_ON_MODEL` | `z-ai/glm-4.5-air:free` | Model used during the Record → Analyze step |
| `GOOGLE_API_KEY` | *(empty)* | Optional. If set, Tier 1 & 2 route to Google AI Studio instead of OpenRouter. |

Model names follow the format `provider/model-name`, e.g. `openai/gpt-4o-mini`, `anthropic/claude-haiku-4-5`. Any model on OpenRouter works.

### 4.4 Capture Settings

| Variable | Default | Description |
|:---------|:--------|:------------|
| `SHARING_ON_SCREENSHOT_INTERVAL` | `3` | Seconds between automatic screenshots |
| `SHARING_ON_SCREENSHOT_WIDTH` | `1280` | Maximum screenshot width in pixels |

### 4.5 Systemu Behaviour

| Variable | Default | Description |
|:---------|:--------|:------------|
| `SYSTEMU_AUTO_APPROVE_SCROLLS` | `false` | Skip the human approval step for scrolls. Dev/CI only. |
| `SYSTEMU_AUTO_FORGE_TOOLS` | `false` | Auto-enable generated tools without code review. **DANGEROUS — never use in production.** |
| `SYSTEMU_VAULT_DIR` | `systemu/vault` | Path to vault storage root (relative to project root) |
| `SYSTEMU_OUTPUT_DIR` | `~/Documents` | Where Shadow-generated output files are saved |
| `SYSTEMU_TOOL_BACKEND` | `local` | Tool execution backend: `local` \| `docker` \| `ssh` \| `wsl` |
| `SYSTEMU_DOCKER_TOOL_TIMEOUT` | `300` | Per-tool timeout when `SYSTEMU_TOOL_BACKEND=docker` |

### 4.6 Storage Backend

| Variable | Default | Description |
|:---------|:--------|:------------|
| `SYSTEMU_STORAGE` | `file` | `file` (default) or `sqlite` |
| `SYSTEMU_DATABASE_URL` | *(empty)* | SQLite connection string, e.g. `sqlite:///data/systemu.db`. Only used when `SYSTEMU_STORAGE=sqlite`. |

---

## 5. Starting the Application

### 5.1 Native Mode (recommended)

```bat
start.bat          # Windows
./start.sh         # Linux / macOS
```

Activates `.venv`, starts the daemon, and launches the NiceGUI dashboard.

Open your browser to: **http://localhost:8765**

### 5.2 Docker Tool Sandbox Mode

Each generated tool runs in an isolated Docker container. Docker Desktop must be running first.

```bat
start_docker.bat       # Windows
./start_docker.sh      # Linux / macOS
```

Dashboard is still at **http://localhost:8765**.

### 5.3 Docker Compose (multi-container)

```bash
# Single container, file-based storage
docker compose up

# SQLite backend with separate worker process
docker compose --profile sqlite up

# Scale workers
docker compose --profile sqlite up --scale systemu-worker=2

# Docker-in-Docker tool isolation
docker compose --profile docker-sandbox up systemu-docker
```

### 5.4 Stopping

```bash
sharing_on daemon stop
```

Or press `Ctrl+C` in the terminal running the daemon.

---

## 6. The Web Dashboard

**URL:** http://localhost:8765

### Sidebar Navigation

| Section | Purpose |
|:--------|:--------|
| Overview | Stat cards and live activity feed |
| Chat | Direct chat with Systemu |
| Scrolls | View, approve, and manage refined SOPs |
| Shadow Army | Agent personas — create, awaken, assign, monitor |
| Activities | Bundles of Scrolls + required Skills / Tools |
| Tools | Generated tools — status, enable / disable |
| Skills | Abstract proficiencies extracted from recordings |
| Memory | Shadow long-term memory consolidation |
| Evolutions | Proposed vault improvements awaiting approval |
| Notifications | Pending decisions — tool approval, scroll approval |
| Settings | LLM tier config, model selection, auto-approve toggles |

**Key buttons:**
- **Record Session** — start a new capture
- **Active Tasks** — live count and dropdown of running Shadows

---

## 7. Recording a Task

### 7.1 Basic

```bash
sharing_on record --name "Deploy app to staging"
```

Sharing-On captures mouse clicks, keyboard input, window focus changes, and browser navigation silently in the background. Press `Ctrl+C` when done. Analysis runs automatically.

### 7.2 All Options

| Flag | Example | Description |
|:-----|:--------|:------------|
| `--name, -n` | `--name "Setup DB"` | Session name. Prompted interactively if omitted. |
| `--watch, -w` | `--watch ./src` | Directory to watch for file changes. Repeatable. |
| `--output, -o` | `--output ./my-caps` | Output directory. Default: `captures/<name>_<timestamp>/` |
| `--screenshots` | *(flag)* | Enable periodic screenshot capture. Off by default. |
| `--screenshot-interval` | `--screenshot-interval 5` | Seconds between screenshots. Default: 3. |
| `--model` | `--model openai/gpt-4o` | Override LLM model for this session only. |
| `--no-analyze` | *(flag)* | Skip LLM analysis. Save raw events only. |
| `--debug` | *(flag)* | Enable verbose debug logging. |

### 7.3 Examples

```bash
# With file watching and screenshots
sharing_on record \
  --name "Configure nginx" \
  --watch ./config \
  --screenshots \
  --screenshot-interval 5

# Capture only — skip LLM step
sharing_on record --name "Quick capture" --no-analyze

# Re-analyze a session captured earlier
sharing_on analyze ./captures/quick_capture_cap_20260509_140000/
```

### 7.4 What Gets Saved

```
captures/
└── deploy_app_to_staging_cap_20260509_140000/
    ├── session.json        ← metadata (name, platform, start/end time)
    ├── events.db           ← all raw events (SQLite)
    ├── instructions.md     ← generated step-by-step guide
    ├── analysis.log        ← LLM analysis output
    └── assets/             ← screenshots referenced in instructions.md
```

---

## 8. Scrolls

A Scroll is the structured, reusable SOP that Systemu creates from a recording.

**Components:**
- **Intent** — the overall goal in one sentence
- **Narrative** — human-readable prose description
- **Objectives** — decomposed sub-goals, each with a success criterion
- **Action Blocks** — step-by-step actions (legacy format)

### Commands

```bash
sharing_on scrolls list
sharing_on scrolls list --status pending_approval
sharing_on scrolls show <scroll_id>
sharing_on scrolls refine ./captures/my_session_cap_20260509/
sharing_on scrolls refine ./captures/my_session_cap_20260509/ --auto   # skip approval gate
sharing_on scrolls approve <scroll_id>
```

### Statuses

| Status | Meaning |
|:-------|:--------|
| `draft` | Initial state after capture |
| `refined` | LLM has processed it into structured format |
| `pending_approval` | Waiting for your approval |
| `approved` | Reviewed and approved |
| `linked` | Linked to an Activity — ready for Shadow execution |
| `evolved` | Updated by the evolution engine |

---

## 9. Shadow Army

A Shadow is an autonomous agent persona that executes Scrolls on your behalf.

### Traits (0–100 each)

| Trait | Description |
|:------|:------------|
| Creativity | Propensity to try novel or lateral approaches |
| Professionalism | Adherence to conventions and standards |
| Techie | Depth of technical knowledge applied |
| Thinking | Internal reasoning depth before acting |

### Commands

```bash
sharing_on army list
sharing_on army list --status active
sharing_on army show <shadow_id>

sharing_on army awaken \
  --name "CI/CD Specialist" \
  --creativity 60 \
  --professionalism 85 \
  --techie 80 \
  --thinking 70

sharing_on army execute <shadow_id> <scroll_id>
sharing_on army execute <shadow_id> <scroll_id> --dry-run   # plan only, no real actions
```

### Statuses

| Status | Meaning |
|:-------|:--------|
| `created` | Exists but not yet awakened |
| `awakened` | Ready to accept assignments |
| `active` | Currently executing a scroll |
| `retired` | No longer in use |

---

## 10. Tools

Tools are callable capabilities generated by the tool forge from what a recording showed was needed.

### Commands

```bash
sharing_on tools list
sharing_on tools list --status deployed

sharing_on tools forge \
  --name "Fetch Ethereum Gas Price" \
  --context "Use etherscan.io or Infura JSON-RPC to get current gas price in gwei"
```

### Statuses

| Status | Meaning |
|:-------|:--------|
| `proposed` | Spec generated — awaiting your code review |
| `forged` | Code generated — ready for testing |
| `tested` | Tests passed |
| `deployed` | Enabled and available to Shadows |
| `upgraded` | Updated by the evolution engine |

> **Security gate:** A tool must be explicitly enabled after your code review before any Shadow can use it. `SYSTEMU_AUTO_FORGE_TOOLS=true` bypasses this — never use it in production.

### Tool Types

| Type | Description |
|:-----|:------------|
| `python_function` | Pure Python with pip dependencies |
| `cli_command` | Shell command wrapper |
| `browser_action` | Playwright browser automation |
| `api_call` | HTTP call to an external service |
| `file_operation` | File read / write / transform |

---

## 11. Activities

An Activity bundles a Scroll with the Skills and Tools needed to execute it. It is the unit assigned to a Shadow.

### Statuses

| Status | Meaning |
|:-------|:--------|
| `pending` | Waiting for tool / skill requirements to be met |
| `in_progress` | Shadow is currently executing |
| `completed` | Finished successfully |
| `failed` | Execution failed — check the execution log |
| `partial` | Some steps done, tool forge pending |
| `unassigned` | No Shadow has the required skills yet |

---

## 12. Skills

Skills are abstract procedural proficiencies extracted from recordings. A Shadow must have the required skills to be assigned an Activity.

```bash
sharing_on skills list
sharing_on skills list --category "Web Automation"
```

---

## 13. Evolutions

The evolution engine proposes improvements to the vault — better scroll objectives, merged tools, skill upgrades.

```bash
sharing_on evolve run            # propose new evolutions
sharing_on evolve show-pending   # view pending evolutions
sharing_on evolve apply <evolution_id>
```

---

## 14. Daemon

The daemon is the background service managing the event queue, scroll refinement, Shadow execution, and the web dashboard.

```bash
sharing_on daemon start                  # background
sharing_on daemon start --port 9000      # custom port
sharing_on daemon start --foreground     # foreground (Docker / debugging)
sharing_on daemon stop
sharing_on daemon status
```

Logs: `systemu/vault/daemon.log`

```bash
# Show current config (API keys masked)
sharing_on settings show
```

---

## 15. UC Test Runner

Runs three pre-built end-to-end use cases with headless Playwright. Exercises the full capture → analyze → scroll pipeline automatically.

| UC | Domain | Script |
|:---|:-------|:-------|
| UC1 | CI/CD Failure Investigation (GitHub Actions) | `playwright_uc1.py` |
| UC2 | DeFi Wallet Risk Assessment (Etherscan) | `playwright_uc2.py` |
| UC3 | Clinical Trial Eligibility Screener (ClinicalTrials.gov) | `playwright_uc3.py` |

```bash
# Run all three
python run_uc_tests.py

# Run a single UC
python playwright_uc1.py
python playwright_uc2.py
python playwright_uc3.py
```

Output goes to `captures/uc1_cicd_...`, `captures/uc2_defi_...`, `captures/uc3_clinical_...`.

> **Port conflict check:** sharing_on must not already be running on port 49494.
> ```bat
> netstat -ano | findstr :49494
> taskkill /PID <pid> /F
> ```

---

## 16. Benchmarks

```bash
python benchmark.py
```

Results saved to `benchmark_results.json`. Measures:

1. SQLite write throughput
2. Step detection latency (realistic mixed-timing events)
3. DB read latency at scale (100 → 10,000 rows)
4. Real pipeline timings from UC session logs
5. Memory footprint of EventStore
6. Write comparison: per-file vs JSONL append vs SQLite WAL (cold + warm runs)

---

## 17. Project Structure

| Path | Purpose |
|:-----|:--------|
| `sharing_on/` | Recording daemon and CLI |
| `sharing_on/cli.py` | All CLI entry points |
| `sharing_on/config.py` | Config dataclass — loads from `.env` |
| `sharing_on/events/` | EventStore, models, collectors |
| `sharing_on/analyzer/` | LLM analysis, step detection, unifier |
| `systemu/core/` | Models, LLM router, supervisor |
| `systemu/vault/` | Vault interface and storage |
| `systemu/interface/` | NiceGUI dashboard, CLI commands |
| `systemu/scheduler/` | Daemon and task queue |
| `captures/` | All recorded sessions |
| `systemu/vault/` | Scrolls, Shadows, Tools, Skills, Activities |
| `run_uc_tests.py` | UC end-to-end test runner |
| `benchmark.py` | Performance benchmarks |
| `playwright_helper.py` | Headless browser event injection |
| `playwright_uc1/2/3.py` | UC Playwright automation scripts |
| `setup.bat` / `setup.sh` | One-time installation |
| `start.bat` / `start.sh` | Start daemon — native mode |
| `start_docker.bat` / `start_docker.sh` | Start daemon — Docker sandbox mode |
| `.env` | Your local config — **never commit this** |
| `.env.example` | Template — copy to `.env` |

---

## 18. Troubleshooting

| Problem | Solution |
|:--------|:--------|
| `sharing_on` command not found | Run `pip install -e .` from project root with `.venv` activated |
| `OPENROUTER_API_KEY not set` | Add key to `.env`. Must start with `sk-or-v1-` |
| Dashboard not loading at `:8765` | `netstat -ano \| findstr :8765` (Windows) — another process may own the port |
| Port 49494 already in use | Previous session didn't exit cleanly. `netstat -ano \| findstr :49494` then `taskkill /PID <pid> /F` |
| Docker tool fails immediately | Ensure Docker Desktop is running before starting the daemon |
| `NiceGUI` import error | `pip install nicegui` in the active `.venv` |
| Playwright errors | `python -m playwright install chromium` |
| Recording captures 0 events | On macOS: grant Accessibility + Input Monitoring in System Settings → Privacy |
| LLM returns prose instead of JSON | The repair retry handles this automatically. If it persists, try a different `SYSTEMU_TIER1_MODEL` |
| Scroll stuck at `pending_approval` | `sharing_on scrolls approve <id>` or set `SYSTEMU_AUTO_APPROVE_SCROLLS=true` in `.env` for dev use |

---

## 19. Quick Start (5 Minutes)

**Step 1 — Install (one-time)**
```bat
setup.bat
```

**Step 2 — Configure**
```bat
copy .env.example .env
```
Edit `.env` and add your `OPENROUTER_API_KEY`.

**Step 3 — Start**
```bat
start.bat
```
Open **http://localhost:8765** in your browser.

**Step 4 — Record a task**
```bash
sharing_on record --name "My First Task"
```
Perform your task, then press `Ctrl+C`.

**Step 5 — Approve the scroll**
```bash
sharing_on scrolls list --status pending_approval
sharing_on scrolls approve <scroll_id>
```

**Step 6 — (Optional) Awaken a Shadow and execute**
```bash
sharing_on army awaken --name "My Shadow" --techie 70 --professionalism 80
sharing_on army execute <shadow_id> <scroll_id>
```
