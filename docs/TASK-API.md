# The local task API (R-UTL1 / U-1a)

A minimal HTTP intake so systemu can be driven without opening the dashboard —
by a script, a shortcut, or the browser extension's "Send to systemu".

It is a thin front door, not a second engine: submissions route through
`pipelines.direct_task.submit_chat_task`, the same helper the Telegram `/chat`
handler calls, which in turn uses the ordinary lane entry points. There is no
separate executor, no separate gate path, and no privileged mode.

## Getting a token

```
sharing_on doctor --make-api-token
```

The token is printed **once** and stored only as a SHA-256 hash under
`<vault>/secrets/api_token.json`. It cannot be recovered. Running the command
again mints a new token and **revokes the previous one** — that is the revoke
path.

Authentication accepts **either** an authenticated dashboard session (R-SEC1)
**or** `Authorization: Bearer <token>`. The bearer check runs in the route
handler itself, not only in the dashboard's route-guard middleware, because that
middleware is a no-op when no dashboard passphrase is configured — the right
call for a loopback dashboard, the wrong one for an endpoint an extension posts
to.

Budget: **30 requests per minute per principal** (sliding window). Over budget
returns `429`.

## `POST /api/tasks`

```jsonc
{
  "prompt": "string, required, 1..8000 chars",
  "lane": "quick | workflow",        // optional, default "workflow"
  "project_id": "string",            // optional, <=128 chars, recorded only
  "source_page": {                   // optional — see "The fence" below
    "url": "string",
    "title": "string",
    "selection": "string"            // <=20000 chars
  },
  "defer_until": "ISO-8601"          // REFUSED — see below
}
```

Responses:

| Status | Meaning |
|---|---|
| `202` | accepted — `{task_id, lane, submitted_via, status}` |
| `401` | no valid session and no valid bearer token |
| `422` | malformed body — the response carries the schema |
| `429` | over the 30/min budget |
| `500` | submission failed |
| `503` | the daemon is still starting up (no vault yet) |

`202`, not `200`: the pipeline makes several LLM calls before anything is
queued, so the request returns as soon as the task has an id and the work
continues on a background thread. Poll for the outcome.

### `defer_until` is refused, deliberately

The field is in the schema so the request contract will not change when it
starts working, but sending it returns `422`. The deferred-release job ships
with **R-UTL7 (Night Shift)** and does not exist yet. Accepting the field and
running the task immediately would silently do the opposite of what a caller
asking for "tonight" wants, so it refuses and says so.

## `GET /api/tasks/<task_id>`

Returns a projection over the run's chat-history record — no new store:

```jsonc
{
  "task_id": "2026-07-20T10:00:00.000001",
  "status": "success",
  "terminal": true,
  "lane": "workflow",
  "prompt": "...",
  "outcome": "...",
  "error": null,
  "files_produced": ["..."],
  "execution_id": "quick_123",
  "project_id": null,
  "submitted_via": "api",
  "source": "api:9c9b5adc0886",
  "origin": "chat"
}
```

`terminal` tells a poller when to stop. Non-terminal statuses include
`running`, `queued`, and the two that mean systemu is waiting on **you** —
`waiting_on_tools` and `pending_decision`. Those two do not resolve on their
own; a poller that treats them as "still working" will wait forever.

`origin` is `chat` for every submission here, because it is the live-event pane
partition key and the panes name a closed set — an unrecognised origin renders
in no pane at all. The surface that actually submitted the work is
`submitted_via` (`chat` | `api` | `extension` | `telegram`) with
`source` carrying the token fingerprint or Telegram user.

## The fence (U-1b)

When `source_page` is present the server composes the final prompt itself: the
operator's intent first and alone, then the page's URL/title/selection inside a
delimited block explicitly labelled untrusted, with any forged delimiter in the
page text neutralised first.

This is why the extension sends structured fields instead of a ready-made
prompt. If it composed the text client-side, page content and operator intent
would be indistinguishable on arrival, and a page reading "ignore previous
instructions…" would be one.

## Example

```bash
TOKEN=...   # from `sharing_on doctor --make-api-token`

TASK=$(curl -s -X POST http://localhost:8080/api/tasks \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Summarise this week'\''s invoices", "lane": "quick"}' \
  | python -c 'import sys,json; print(json.load(sys.stdin)["task_id"])')

curl -s "http://localhost:8080/api/tasks/$TASK" \
  -H "Authorization: Bearer $TOKEN"
```

## Where the results land

A completed run also writes its files back to the filesystem — see the Outbox
contract (U-12): `<vault>/Outbox/<yyyy-mm-dd>-<task-slug>/` with the artifacts,
a `receipt.html`, and a `.done` marker written last. A script can watch for
`.done` instead of polling this API at all.
