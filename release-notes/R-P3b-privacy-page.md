# R-P3b (slice 3) — "What leaves this machine": an honest privacy page

The transparency half of R-P3b: a `/privacy` page that states, in plain language, the
machine's *actual* egress reality — no marketing, no "100% local" overclaim. It
follows the spec's interim-honesty rule (§15.7): until a private-compute mode exists,
"local-first" means **custody** (your vault), **verification** (receipts), and a clear
boundary — **not** zero egress.

## What it says (truthfully)

- **Model calls** — the headline: your prompts *and any file excerpts you give the
  agent* transit the actual network destination (**openrouter.ai** for the default
  models, Anthropic for native claude) to be processed. Locality is judged **per
  tier** — it only says "nothing leaves" when *every* model tier is local (ollama); a
  local tier-1 with a remote tool-forge/formatting tier is honestly reported as still
  leaking.
- **Outbound network** — truthful pre- *and* post-S2 (AC6): today there's no OS-level
  egress jail, so the honest boundary is the forged-network hard-DENY (a forged tool
  that declares network access is refused unless you approve it); once an OS jail
  exists, the page reports it.
- **The agent's tools** — a completeness note most privacy pages skip, with the
  *default* third parties named: a web fetch is relayed through **r.jina.ai** (which
  sees the URL and page body), search goes to **DuckDuckGo**, place lookups to
  **OpenStreetMap**, and any connected MCP servers reach their own hosts.
- **Secrets at rest** — the OS secret store (DPAPI / Keychain / SecretService), with a
  plaintext-file fallback visibly **flagged**.
- **Custody** — your vault stays local; a money-move is credited only via independent
  verification (a hardened read-back), never the tool's self-report.

## How it's built

A pure, deterministic `privacy_report()` (unit-tested — the page is a thin renderer
over it, token classes only). Rendered at `/privacy`, linked from Settings. Truthful
across fixtures pre- and post-S2 and local- vs remote-model (AC6). Accuracy is the
whole point, so the claims are scoped to what's actually present on this build — no
forward-references to features that live on other branches. The truthfulness audit
caught (and this fixes) two real trust failures before ship: a plaintext-file
credential fallback that had been rendering as "OS-encrypted / OK", and a tier-1-only
locality check that could claim "nothing leaves" while other tiers still egressed — as
well as naming the actual destination (openrouter.ai, not the model vendor) and the
default third-party relays.

## Deferred

The ledger-backed "what actually left today" egress summary (an `iter_rows` consumer,
lands when the action ledger merges) and the UX-11 run timeline.
