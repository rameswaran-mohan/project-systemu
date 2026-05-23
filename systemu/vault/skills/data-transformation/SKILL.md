---
name: data-transformation
description: Ability to parse, format, and transform data between different representations
metadata:
  category: data
  proficiency_level: intermediate
  required_tools:
  - parse_json
  - format_date
---

# data_transformation

## Description

Ability to parse, format, and transform data between different representations

## Procedural Instructions

For data transformation: 1) Use parse_json in auto mode — it handles both raw JSON strings and file paths transparently. 2) Use format_date to convert date strings between formats — common input formats: %Y-%m-%d, %m/%d/%Y, %d-%m-%Y. 3) When transforming large datasets, prefer processing in memory using Python logic rather than multiple tool calls. 4) Validate transformed data against the expected output_type before writing to file. 5) For CSV data, use file_read to get the content then parse with stdlib csv module in a run_command or within a Python tool.

## Required Tools

- parse_json
- format_date

## Evidence Scrolls

_No evidence scrolls._
