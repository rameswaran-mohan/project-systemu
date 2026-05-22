#!/usr/bin/env python3
"""Create an Excel .xlsx file with provided headers and row data."""
from __future__ import annotations

from pathlib import Path

TOOL_META = {
    "name": "create_excel_sheet",
    "tool_type": "file",
    "dependencies": ["openpyxl"],
}


def run(**kwargs) -> dict:
    output_path: str = kwargs.get("output_path", "")
    sheet_name: str = kwargs.get("sheet_name", "Sheet1") or "Sheet1"
    headers: list = kwargs.get("headers", []) or []
    rows: list = kwargs.get("rows", []) or []
    overwrite: bool = bool(kwargs.get("overwrite", True))

    if not output_path:
        return {"success": False, "output_path": "", "error": "output_path is required"}

    try:
        import openpyxl

        out = Path(output_path).expanduser()

        if out.exists() and not overwrite:
            return {"success": False, "output_path": str(out), "error": "File already exists"}

        out.parent.mkdir(parents=True, exist_ok=True)

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = sheet_name

        if headers:
            ws.append(headers)

        for row in rows:
            ws.append(list(row))

        wb.save(str(out))

        return {"success": True, "output_path": str(out), "error": None}

    except Exception as exc:
        return {"success": False, "output_path": "", "error": str(exc)}
