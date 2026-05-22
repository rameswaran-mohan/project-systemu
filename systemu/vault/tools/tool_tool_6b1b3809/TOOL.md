---
name: web_extract_text
tool_type: browser_action
status: deployed
enabled: true
dependencies:
  - playwright
---

# web_extract_text

## Description

Extract visible text content from a web page or a specific element using a CSS selector

## Parameters

- url (string, optional): URL to load
- selector (string, default: body): CSS selector (default body)

## Returns

- success (boolean)
- text (string)
- error (string)

## Implementation Notes

Use playwright sync_playwright with an existing page. If selector is provided, use page.locator(selector).all_inner_texts() and join with newlines. If attribute is provided, use page.locator(selector).get_attribute(attribute). If no selector, use page.inner_text('body'). Catch playwright errors and return error.
