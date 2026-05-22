# Messaging gateway

Run Systemu from a chat app.  Submit tasks, approve scrolls, see
execution results — all without opening the dashboard.

Today the supported platform is **Telegram**.  Slack and Discord are
straightforward follow-ups on the same `Gateway` abstraction.

The gateway is **opt-in**: leave the env vars unset and the daemon
behaves exactly as it did before.  Set the bot token and an allowlist
and the gateway boots alongside the dashboard.

---

## Why Telegram first

- **Free** bot creation, no OAuth flow.
- **Long-polling** — works from behind NAT.  You don't need a public
  webhook URL or a reverse proxy in front of your home network.
- Solid Python SDK with inline keyboards, file uploads, message edits.
- Markdown rendering matches the dashboard's chat history format.

---

## Setup (one-time)

### 1. Create a bot

Talk to [@BotFather](https://t.me/BotFather) on Telegram and run
`/newbot`.  Follow the prompts; at the end BotFather hands you a
**bot token** that looks like `1234567890:ABCdef…`.

### 2. Find your Telegram user ID

The bot will only accept messages from user IDs on its allowlist —
so you need to know yours.  Easiest way: message [@userinfobot](https://t.me/userinfobot)
and it replies with your numeric ID.

### 3. Add the env vars

In your `.env`:

```env
SHARING_ON_TELEGRAM_BOT_TOKEN=1234567890:ABCdef...
SHARING_ON_TELEGRAM_ALLOWED_USER_IDS=12345,67890   # your ID, plus anyone else
```

The allowlist is comma-separated.  Refusing to start with an empty
allowlist is deliberate — leaking a bot token shouldn't give the
world access to your runtime.

### 4. Install the SDK

```bash
pip install 'python-telegram-bot>=20'
```

The SDK is an **optional** dependency — the gateway gracefully degrades
to "not installed" if it's missing.

### 5. Restart the daemon

```bash
./stop.sh && ./start.sh
```

Look for `[Dashboard] Telegram gateway started.` in `logs/daemon.log`.
Now send `/help` to your bot.

---

## Commands

| Command | What it does |
|---|---|
| `/chat <prompt>` | Submit a task.  Routes through the Supervisor exactly like the dashboard's "Queue" mode. |
| `/status` | Show queue depth and the list of currently running submissions. |
| `/scrolls` | List the 10 most recent scrolls + their status. |
| `/activities` | List the 10 most recent activities + their status. |
| `/shadows` | List your Shadow Army. |
| `/approve <scroll_id>` | Equivalent of clicking ✓ APPROVE on the Scrolls page. |
| `/reject <scroll_id>` | Reject a pending scroll. |
| `/help` | Show this list. |

Plain-text messages without a leading `/` are treated as `/chat
<message>`.  So you can just type "tell me two facts about the moon"
and the bot submits a chat task.

---

## Push notifications (inverse direction)

The gateway pushes messages to you on:

- Shadow execution complete (status + execution ID for follow-up).
- Pending approval (with inline ✓ / ✗ buttons).
- Watchdog fires (Shadow stuck — re-queued).
- Tool forge requires review (when `SYSTEMU_AUTO_FORGE_TOOLS=false`).

Pushes go to the user(s) on the allowlist.  Single-user setups (the
common case) just get every notification at the one chat.

---

## Security

The gateway treats Telegram as **untrusted transport**:

1. **Strict user-ID allowlist.**  Any message from a user not on the
   allowlist is rejected with "Unauthorised".  No per-message ACL —
   the allowlist is the single decision.
2. **Approval gates are preserved.**  `/chat` submissions go through
   the same Supervisor queue + approval flow as dashboard submissions.
   The bot is a thin operator surface, never a privilege escalation.
3. **No filesystem or shell access from chat.**  There's no `/run`
   command — anything that would access the host directly is a
   dashboard-only path.
4. **Bot token is a secret.**  Don't commit `.env`.  Rotate via
   BotFather (`/revoke`) if you suspect leakage.

A leaked bot token still costs you nothing as long as your
allowlist is tight: an attacker who joins your bot gets "Unauthorised"
on every message.  Treat the allowlist as the real security boundary.

---

## Disabling

Either delete `SHARING_ON_TELEGRAM_BOT_TOKEN` from `.env`, or set it
to an empty string and restart the daemon.  The gateway is dormant
again and the dashboard behaves exactly as before.

---

## Slack / Discord (planned)

Slack and Discord ship in a follow-up PR.  They reuse the same
`Gateway` protocol and command parser from
`systemu/messaging/gateway.py`, so each new platform is a single new
module — no changes to the daemon or command handlers.

When that lands you'll be able to wire any combination of platforms
on the same daemon and push notifications go to all of them at once.

---

## Troubleshooting

**"Telegram gateway not configured" in daemon log**
The bot token env var is empty.  Set it and restart.

**"bot token present but allowlist empty"**
You set the token but not the allowlist.  Add at least your own
user ID to `SHARING_ON_TELEGRAM_ALLOWED_USER_IDS`.

**"python-telegram-bot not installed — gateway is dormant"**
The optional dependency isn't installed.  `pip install
'python-telegram-bot>=20'` and restart.

**Bot says "Unauthorised" on every message**
Your user ID isn't on the allowlist.  Check `/start` interactions —
your user ID is in the URL of `https://t.me/<bot_handle>?start=…`,
or message [@userinfobot](https://t.me/userinfobot).
