---
name: web_screenshot
tool_type: browser_action
status: deployed
enabled: true
dependencies:
  - playwright
---

# web_screenshot

## Description

Render a URL in a headless browser and capture a full-page screenshot.

## Parameters

- url (string): Full URL to render
- output_path (string, default: ): Where to save the PNG (optional)

## Returns

- success (boolean)
- image_path (string)
- tier_used (string)
- error (string)

## Implementation Notes

Routes to systemu.runtime.web.browser_pool.BrowserPool.screenshot (headless chromium, full_page=True, wait_until='networkidle'). Returns missing_packages=['playwright-chromium'] while chromium is still installing.
