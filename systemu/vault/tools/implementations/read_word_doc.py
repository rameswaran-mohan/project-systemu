#!/usr/bin/env python3
"""Read text content from a .docx Word document."""
from __future__ import annotations

from pathlib import Path

TOOL_META = {
    "name": "read_word_doc",
    "tool_type": "file",
    "dependencies": ["python-docx"],
}


def run(**kwargs) -> dict:
    path: str = kwargs.get("path", "")

    if not path:
        return {"success": False, "text": "", "paragraph_count": 0, "error": "path is required"}

    try:
        from docx import Document

        p = Path(path).expanduser()

        if not p.exists():
            return {"success": False, "text": "", "paragraph_count": 0, "error": f"File not found: {p}"}

        doc = Document(str(p))
        paragraphs = [para.text for para in doc.paragraphs]
        text = "\n".join(paragraphs)

        return {"success": True, "text": text, "paragraph_count": len(paragraphs), "error": None}

    except Exception as exc:
        return {"success": False, "text": "", "paragraph_count": 0, "error": str(exc)}
