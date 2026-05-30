---
name: web_read
tool_type: web
status: deployed
enabled: true
dependencies:
  - playwright
---

# web_read

## Description

Read a web page and return readable title, text, and links. Uses a fast HTTP fetch first and escalates to a headless browser for JavaScript/SPA pages.

## Parameters

- url (string): Full URL to read

## Returns

- success (boolean)
- title (string)
- text (string)
- links (array) — List of {url, text}
- tier_used (string) — 'fetch' (T0 httpx) or 'browser' (T2 chromium)
- error (string)

## Implementation Notes

T0: httpx fetch + pure-Python readability via systemu.runtime.web.fetch_core. If the page looks like a JS/SPA shell, escalate to systemu.runtime.web.browser_pool.BrowserPool.render_html. Returns missing_packages=['playwright-chromium'] while chromium is still installing.
