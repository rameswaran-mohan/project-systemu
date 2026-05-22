#!/usr/bin/env python3
"""write_csv_file — Write a list of dictionaries to a CSV file.

Parameters (via run() kwargs):
  output_path (str, required): Path to save the CSV file.
  data (list[dict], required): List of dictionaries representing rows.

Returns (dict):
  success (bool): True if the operation succeeded.
  error (str|None): Error message on failure, None on success.
"""
from __future__ import annotations
import csv
import os
from pathlib import Path

TOOL_META = {
    "name": "write_csv_file",
    "tool_type": "file_operation",
    "dependencies": [],
}


def run(output_path: str, data: list) -> dict:
    """Write a list of dictionaries to a CSV file."""
    if not output_path:
        output_path = os.path.join(os.getenv("SYSTEMU_OUTPUT_DIR", "."), "output.csv")

    if not data or not isinstance(data, list):
        return {"success": False, "error": "data must be a non-empty list of dictionaries"}

    try:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        fieldnames = list(data[0].keys())

        with open(path, mode="w", newline="", encoding="utf-8") as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(data)

        return {"success": True, "error": None}
    except Exception as exc:
        return {"success": False, "error": str(exc)}
