---
name: spreadsheet-automation
description: Ability to create and read Excel spreadsheets with structured data
metadata:
  category: file_ops
  proficiency_level: intermediate
  required_tools:
  - create_excel_sheet
  - read_excel_sheet
---

# spreadsheet_automation

## Description

Ability to create and read Excel spreadsheets with structured data

## Procedural Instructions

To work with Excel files: 1) Use create_excel_sheet to build a new workbook — always provide headers as a list of strings first. 2) Pass rows as a list-of-lists matching the header column order. 3) Use read_excel_sheet to inspect existing files — specify sheet_name for multi-sheet workbooks. 4) When updating an existing sheet, read it first, modify the data in memory, then write a new file with overwrite=True. 5) Numeric values in rows will be stored as numbers; ensure data types match the intended column type.

## Required Tools

- create_excel_sheet
- read_excel_sheet

## Evidence Scrolls

_No evidence scrolls._
