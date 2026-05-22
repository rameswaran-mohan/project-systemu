---
name: web_extract_table
tool_type: browser_action
status: deployed
enabled: true
dependencies:
  - playwright
---

# web_extract_table

## Description

Extract an HTML table from a web page and return it as headers and rows

## Parameters

- url (string, optional): URL containing the table
- table_index (integer, default: 0): 0-indexed table number
- selector (string, default: table): CSS selector to find tables within

## Returns

- success (boolean)
- headers (array)
- rows (array)
- error (string)

## Implementation Notes

Use playwright sync_playwright with an existing page. Use page.locator(selector).evaluate() to run JS that extracts table headers (th) and rows (tr > td). Return headers array and rows array of arrays. Catch playwright errors and return error.
