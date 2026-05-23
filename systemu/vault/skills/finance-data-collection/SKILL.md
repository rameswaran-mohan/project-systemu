---
name: finance-data-collection
description: Expert-level proficiency in collecting financial data from web sources
  and organizing it into structured documents
metadata:
  category: finance
  proficiency_level: expert
  required_tools:
  - web_screenshot
  - web_extract_text
  - web_extract_table
  - fetch_json
  - create_word_doc
  - create_excel_sheet
---

# finance_data_collection

## Description

Expert-level proficiency in collecting financial data from web sources and organizing it into structured documents

## Procedural Instructions

For financial data collection: 1) Prefer structured data (JSON APIs, HTML tables) over screenshots when available — use web_extract_table for tabular data like index compositions or OHLCV data. 2) For charts and visual representations, use web_screenshot with a selector targeting the chart element. 3) Organize collected data into Excel (create_excel_sheet) for numerical analysis or Word (create_word_doc) for reports. 4) Always timestamp outputs using today's date in the naming convention observed from the scroll's observed_preferences. 5) Validate that captured prices/values are current — check for staleness indicators on the source page.

## Required Tools

- web_screenshot
- web_extract_text
- web_extract_table
- fetch_json
- create_word_doc
- create_excel_sheet

## Evidence Scrolls

_No evidence scrolls._
