---
name: create_excel_sheet
tool_type: file_operation
status: deployed
enabled: true
dependencies:
  - openpyxl
---

# create_excel_sheet

## Description

Create an Excel .xlsx file with headers and data rows

## Parameters

- output_path (string, optional): Path for the output .xlsx file
- sheet_name (string, default: Sheet1): Worksheet name
- headers (array, default: []): Column header labels
- rows (array, default: []): List of data rows
- overwrite (boolean, default: True): Overwrite if exists

## Returns

- success (boolean)
- output_path (string)
- error (string)

## Implementation Notes

_No implementation notes yet._
