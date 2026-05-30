---
name: web_act
tool_type: browser_action
status: deployed
enabled: true
dependencies:
  - playwright
---

# web_act

## Description

Drive a web page to accomplish an instruction using accessibility-tree-first interaction (click/type/read) in a bounded LLM loop.

## Parameters

- url (string): Full URL to open
- instruction (string): Natural-language goal to accomplish on the page
- max_steps (integer, default: 8): Maximum interaction steps

## Returns

- success (boolean)
- result (string)
- steps (array) — Trace of executed actions
- error (string)

## Implementation Notes

Opens the URL in a pooled headless context and runs systemu.runtime.web.act_loop.run_act_loop against an a11y-tree page adapter. Refuses to type into password-named fields. Honors domain allow/deny policy. Returns missing_packages=['playwright-chromium'] while chromium is still installing.
