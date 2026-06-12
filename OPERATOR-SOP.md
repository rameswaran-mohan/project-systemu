# Systemu — Operator's Standard Operating Procedure

One page. How to work with your assistant, what to expect, and what to do
when something needs you. (The in-app tour covers the same ground —
Settings → Help → Replay the tour.)

## The one clear path

1. **Install & set up** — the installer asks for your OpenRouter key
   (validated on the spot), a model preset, and an output folder. First
   launch opens the **welcome wizard** (your name, city, timezone) and a
   two-minute tour. Keys are never typed into the browser.
2. **Ask, or teach:**
   * **Quick task** (Chat → the box at the top): plain-English one-shot
     asks — "make a CSV of …", "list the files in …". Answers in seconds;
     produced files are listed under the answer.
   * **Teach a workflow** (＋New → Record session): do the task once
     yourself — Systemu watches, writes step-by-step instructions, and
     proposes a repeatable workflow. *This is the superpower.*
3. **Approve** — anything that needs your yes lands in the **Inbox** and
   on the **Needs-you badge** (top right) — proposals from recordings,
   new-specialist suggestions, dependency installs, risky steps. Every
   card says what approving actually does, with the safe default marked.
4. **Watch it run** — Work shows every workflow with live stage chips
   (CAPTURE → SCROLL → ACTIVITY → EXECUTION → DONE); the **Live** rail
   streams each step (expand the arrow for the tool's reasoning and
   results).
5. **Get your results** — the **Status** button lists recent tasks with
   their outcome message and the exact file paths produced. Files land in
   the output folder you chose at setup.

## Where things live

| You want | Go to |
|---|---|
| Ask something now | Chat (composer is at the top) |
| Teach a repeatable job | ＋New → Record session |
| Approve / answer questions | Inbox, or the Needs-you badge |
| See workflows + their status | Work |
| Your specialists ("shadows") | Shadows |
| Tools (each OFF until you enable it) | Build |
| Outcomes + file paths | Status button (any page) |
| Models, connections, Telegram, trust | Settings |
| Replay the tour | Settings → Help |

## When it asks for your yes (and why)

* **"Approve scroll"** — a recorded/refined workflow wants to become
  runnable. Review the summary; Approve extracts the steps.
* **"New Shadow Recommended"** — no existing specialist fits; Awaken
  creates one (its tools still need your enablement).
* **Dependency install** — a tool needs a Python package. Approve once;
  it's remembered.
* **Destructive actions** (deleting, overwriting outside the output
  folder, risky commands) are **auto-denied** when nobody's watching —
  the agent is told to find a safer route or stop honestly.

## Troubleshooting

| Symptom | Likely cause → what to do |
|---|---|
| "No API key found" on the welcome screen | Add `OPENROUTER_API_KEY=…` to `.env` next to the app, save, click **Re-check** |
| A task failed with a tool error | Open the Live row's arrow for the real error; web tools need their package approved once (Inbox) |
| Nothing seems to need you, but a workflow says PENDING_APPROVAL | Open Work → **Review & Approve** on the row |
| A run sits "unassigned" | Check the Inbox — a "New Shadow Recommended" question is waiting |
| Results missing | Status button → the task row shows the produced file paths and the output folder |
| Slow model stalls get cancelled | Raise `SYSTEMU_STUCK_THRESHOLD_S` (default 300) in `.env` for slow/preview models |
| "N daemon processes are running" banner | Run `sharing_on daemon stop --all`, then start one daemon |

## Safety model (the short version)

Your data stays in your local vault. Every tool is OFF until you enable
it. Approvals are per-action with safe defaults; dependency installs and
destructive recovery can never be silently auto-approved. Auto-allowed
steps (if you loosen the gate mode) still leave an audit row in the Inbox
history.
