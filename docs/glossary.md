# Glossary

Systemu uses some distinctive vocabulary. Here are the industry-standard terms:

| Systemu term | Industry term | What it means |
|---|---|---|
| **Shadow** | Agent persona / Specialist agent | A long-lived agent with its own identity, memory, and tool set |
| **Scroll** | Workflow spec / Intent doc | A structured representation of "what the user wants done" |
| **Elder** | Cross-agent shared memory | Memory promoted from one Shadow to all (governance-gated) |
| **Refinery** | Memory consolidator / Lesson extractor | The post-execution pipeline that distills lessons from runs |
| **Forge** | Tool synthesis | LLM-generated tool implementations |
| **Sharing-On** | Activity recorder | The capture engine that records your computer actions |
| **Vault** | Project state store | The on-disk + DB representation of scrolls / shadows / tools / skills |
| **Supervisor** | Control plane | Bounded-action overseer with cost ledger |

The lore is intentional — it gives the system an identity. The terms above let you map it to what you already know.

## Dashboard page aliases (v0.7.2)

The v0.7.2 sidebar consolidation merged a few pages — old names still
work as deep links, but the sidebar shows the consolidated parent:

| Old label / URL | New location |
|---|---|
| **Systemu Chat** (`/systemu-chat`) | `Chat` → `Live Events` tab (`/chat?tab=live`) |
| **Memory** (`/memory`) | `Insights` → `Memory` tab (`/insights?tab=memory`) |
| **Flywheel** (`/flywheel`) | `Insights` → `Flywheel` tab (`/insights?tab=flywheel`) |
| **Notifications** (`/notifications`) | `Insights` → `Events` tab (`/insights?tab=events`) |
| **Shadow Army** (`/army`) | Just **Shadows** in the sidebar — URL unchanged |

Every legacy URL above resolves via a redirect handler, so screenshots,
notification emails, and recovery-panel "Fix URL" links from earlier
versions still land in the right place.
