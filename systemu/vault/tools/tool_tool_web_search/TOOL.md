---
name: web_search
tool_type: web
status: deployed
enabled: true
dependencies:
  []
---

# web_search

## Description

Search the web and return results with title, URL, and snippet. Multi-provider: keyed (Brave/Serper) preferred when an API key is set, free DuckDuckGo-lite fallback otherwise.

## Parameters

- query (string): Search query
- max_results (integer, default: 5): Maximum number of results to return

## Returns

- success (boolean)
- results (array) — List of {title, url, snippet}
- provider (string)
- degraded (boolean)
- error (string)

## Implementation Notes

Routes to systemu.runtime.web.search_providers.search. Keyed providers (Brave via SYSTEMU_BRAVE_API_KEY, Serper via SYSTEMU_SERPER_API_KEY) are preferred; DuckDuckGo-lite is the free floor. degraded=True when the free provider was used.
