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

Render a URL in a headless browser and capture a screenshot of the page or a specific element

## Parameters

- url (string, optional): Full URL to render
- selector (string, default: ): CSS selector to screenshot (optional, full page if empty)
- output_path (string, default: ): Where to save the PNG (optional)

## Returns

- success (boolean)
- image_path (string)
- error (string)

## Implementation Notes

Use playwright sync_playwright with Chromium headless. Call page.goto(url, wait_until='networkidle'). If selector is provided, use page.locator(selector).screenshot(path=output_path). Otherwise page.screenshot(path=output_path). Return base64 if no output_path. Catch playwright TimeoutError and return error.
