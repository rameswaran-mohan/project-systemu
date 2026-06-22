# Getting Started

A five-minute walkthrough to take you from an empty checkout to a
Shadow agent executing your first workflow.

> **TL;DR**: clone → install → start → open dashboard → submit chat.
> The whole thing runs locally with one external dependency (an
> LLM API key).

---

## 1. Prerequisites (one-time)

You need:

- **Python 3.10+** (3.12 recommended).  Check with `python --version`.
- **An LLM API key.**  A free OpenRouter key works:
  [openrouter.ai](https://openrouter.ai).  A Google AI Studio key
  unlocks the Gemini tiers (also free): [aistudio.google.com](https://aistudio.google.com).
- **Git.**

On Linux you'll also want `xdotool` and `xclip` for window /
clipboard capture:

```bash
sudo apt install xdotool xclip      # Debian / Ubuntu
sudo dnf install xdotool xclip      # Fedora
```

That's it for `local` mode.  For `docker-local` or
`docker-enterprise` you'll need Docker Desktop or Docker Engine 24+.

---

## 2. Clone and install

```bash
git clone https://github.com/rameswaran-mohan/project-systemu.git
cd project-systemu

./install.sh        # Linux / macOS
# or
install.bat         # Windows
```

The installer is interactive — it asks which deployment mode you
want and prompts for your API keys.  For a quick first run, pick
**local** and paste your OpenRouter key when asked.

If you'd rather run non-interactively (CI, scripts, repeatable
setup):

```bash
./install.sh --mode local --non-interactive \
    --openrouter-key=sk-or-v1-... \
    --google-key=AIza...
```

The installer:

1. Creates `.venv/` and installs Python dependencies.
2. Writes `.env` with your keys and mode-specific defaults.
3. Runs `alembic upgrade head` to create the SQLite vault.
4. Seeds the vault with starter tools, shadows, and skills.

---

## 3. Start the stack

```bash
./start.sh          # Linux / macOS
start.bat           # Windows
```

You should see:

```
[INFO] Starting daemon (NiceGUI + scheduler) ...
[INFO] Daemon PID 12345
[INFO] Starting worker (Huey consumer) ...
[INFO] Worker PID 12346

 Dashboard:  http://localhost:8765/
 Logs:       logs/daemon.log  &  logs/worker.log
 Stop:       ./stop.sh
```

Open the dashboard at <http://localhost:8765/>.

---

## 4. Your first Shadow execution

The fastest way to verify everything is hooked up: submit a chat task.

1. In the dashboard sidebar, click **Systemu Chat**.
2. Type a short prompt — e.g. *"Tell me two facts about the moon."*
3. Choose **Run now** (or leave it on **Queue** to exercise the
   Supervisor path).
4. Click **▶ SUBMIT**.

You'll see live progress in the event feed: the chat pipeline picks
a Shadow, the runtime loops through its iterations, and the final
response appears in the chat history.

If you don't see output within ~30 s, check:

- `logs/daemon.log` for dashboard / pipeline errors
- `logs/worker.log` for runtime errors
- That your OpenRouter key in `.env` is valid (an "Invalid API key"
  message in the worker log is the most common first-run issue)

---

## 5. Record a workflow (optional)

Once the chat path works, the more interesting flow is recording a
real task and replaying it via a Shadow:

```bash
sharing_on record --name "My first workflow"
```

Do whatever you'd normally do on your computer.  Press `Ctrl+C` when
you're done.  Sharing-On will analyse the recording (this takes
~30 s for short tasks) and produce a Scroll that lands at
`/scrolls` in the dashboard with status `pending_approval`.

Click **✓ APPROVE** to advance it through the pipeline:

1. Activity Extractor figures out which skill and tools are needed.
2. Shadow Decision picks (or synthesises) the right specialist.
3. The Activity becomes visible at `/activities` with status
   `assigned`.
4. Submit it through **Systemu Chat** to dispatch a real execution.

The first time you do this, expect to iterate — Scrolls are LLM
output and may need a quick edit in the Workshop tab.

### Recording in docker modes (v0.6.6+)

`sharing_on record` runs on the host — it has to, because the desktop
being captured *is* the host.  The auto-spawned `analyze` then needs to
hand off the resulting Scroll to the vault, which in docker modes lives
in a Postgres container.

- **docker-local** publishes Postgres on `127.0.0.1:5432` by default
  (`SYSTEMU_DB_BIND=127.0.0.1:5432` in `.env`).  This is loopback-only —
  the same security boundary as the dashboard's already-exposed port
  8765.  The host's `analyze` connects to `127.0.0.1:5432`, the Scroll
  lands in Postgres, and you see it on `/scrolls` as usual.
  - To opt out on a shared-user host, copy
    `docker-compose.override.yml.example` to `docker-compose.override.yml`
    and uncomment the snippet that removes the `ports:` section from
    `postgres-local`.
- **docker-enterprise** does NOT publish Postgres by default — production
  deployments should keep the database on the docker-internal network
  only.  The capture-and-record flow from a host is not the intended
  pattern for enterprise; use the dashboard chat instead, or explicitly
  set `SYSTEMU_DB_BIND=127.0.0.1:5432` and add a `ports:` block to the
  `postgres` service via a `docker-compose.override.yml` if you want
  ad-hoc host access for development.

If your Scroll never appears on the dashboard after capture in docker
mode, the host's `analyze` couldn't reach the container's Postgres —
check `SYSTEMU_DB_BIND` and the `analysis.log` in the capture directory.

---

## 6. Stopping

```bash
./stop.sh           # Linux / macOS
stop.bat            # Windows
```

`stop.sh` shuts the daemon + worker (or the compose stack) cleanly.
Your vault data persists across restarts.

---

## What next?

- **Browse the Workshop** (`/workshop`) to see the tools, shadows,
  and skills that shipped with the install.  Each is editable.
- **Read `USER_GUIDE.md`** for operator-level guidance.
- **Read `ARCHITECTURE.md`** if you want to understand how the
  pieces fit together.
- **Open an issue** if anything didn't work — the
  [bug template](../.github/ISSUE_TEMPLATE/bug.yml) has the fields
  we need to diagnose first-run problems quickly.
