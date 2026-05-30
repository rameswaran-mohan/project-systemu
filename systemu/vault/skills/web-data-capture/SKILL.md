---
name: web-data-capture
description: Proficiency in fetching, rendering, and capturing data from web pages
  using programmatic browser automation
metadata:
  category: browser
  proficiency_level: intermediate
  required_tools:
  - web_screenshot
  - web_read
---

# web_data_capture

## Description

Proficiency in fetching, rendering, and capturing data from web pages using programmatic browser automation

## Procedural Instructions

To capture web data: 1) Identify the target URL and data type needed (readable text or a visual capture). 2) Use web_screenshot for visual captures or charts where exact pixel representation matters — it renders the page in a headless browser and saves a full-page image. 3) Use web_read to pull the page's readable text and links; it fetches the page directly and escalates to a headless browser automatically for JS-heavy or single-page apps, so tables and dynamic content are included in the extracted text. 4) Verify the captured content is complete and matches the expected data before storing or using it downstream.

## Required Tools

- web_screenshot
- web_read

## Evidence Scrolls

_No evidence scrolls._
