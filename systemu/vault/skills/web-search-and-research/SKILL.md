---
name: web-search-and-research
description: Ability to search the web, evaluate results, and extract relevant information
  for a research objective
metadata:
  category: browser
  proficiency_level: intermediate
  required_tools:
  - web_search
  - web_read
  - fetch_html
---

# web_search_and_research

## Description

Ability to search the web, evaluate results, and extract relevant information for a research objective

## Procedural Instructions

To research a topic: 1) Formulate a specific search query — prefer factual, concise terms over natural language. 2) Call web_search with the query and review the returned results (title, URL, snippet). 3) Navigate to the most relevant result using web_read to get the full page text (it fetches the page and falls back to a headless browser for JS-heavy pages automatically). 4) If the snippet is sufficient, avoid fetching the full page. 5) Cross-reference from at least 2 sources when accuracy is critical.

## Required Tools

- web_search
- web_read
- fetch_html

## Evidence Scrolls

_No evidence scrolls._
