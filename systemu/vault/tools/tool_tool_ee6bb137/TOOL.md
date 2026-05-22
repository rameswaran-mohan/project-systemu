---
name: read_excel_sheet
tool_type: file_operation
status: deployed
enabled: true
dependencies:
  - openpyxl
---

# read_excel_sheet

## Description

Read data from an Excel .xlsx file and return headers and rows

## Parameters

- path (string, optional): Path to the .xlsx file
- sheet_name (string, default: ): Sheet name (first sheet if empty)
- sheet_index (integer, default: 0): Sheet index if sheet_name empty

## Returns

- success (boolean)
- headers (array)
- rows (array)
- row_count (integer)
- error (string)

## Implementation Notes

_No implementation notes yet._
