---
name: browser_navigate
tool_type: browser_action
status: deployed
enabled: true
dependencies:
  - playwright
---

# browser_navigate

## Description

Navigate a headless browser to a URL and wait for the page to fully load

## Parameters

- url (string, optional): URL to navigate to

## Returns

- success (boolean)
- page_title (string)
- final_url (string)
- error (string)

## Implementation Notes

Use playwright sync_playwright with Chromium headless. Create a browser context and page. Call page.goto(url, wait_until=wait_until, timeout=timeout). Return page.title() and page.url. Catch playwright TimeoutError and return error. Close browser context after navigation.
