---
name: summarize-page
description: "Fetch a web page and write a 3-paragraph summary to a markdown file."
version: 1.0.0
platforms: [linux, macos, windows]
metadata:
  systemu:
    tags: [summarization, web]
    related_skills: []
prerequisites:
  commands: []
requires_toolsets: [file, web]
fallback_for_toolsets: []
---

# Summarize Page — Recipe

## When to Use

User asks "summarize this URL" or wants a markdown summary of a web page.

## When NOT to Use

- The URL is behind a paywall (the request will get a partial page).
- The URL points at a PDF or video (use a different extractor).

## Procedure

1. Call `web_extract` with the URL the user provided. If it returns `error_type="anti_bot_blocked"`, suggest the user provide a different URL.
2. Extract the visible text from the response body.
3. Write a 3-paragraph summary to `{default_output_dir}/<slug-of-url>.md`.
4. Reply with the file location.

## Pitfalls

- Some pages are mostly JS — the extracted content may be near-empty. Detect and warn.
- The URL slug should be deterministic.

## Verification

- The output markdown file exists, ≥150 chars
- Chat reply names the file
