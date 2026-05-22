#!/usr/bin/env python3
"""detect_language_from_extension — Detect programming language from a file extension using a built-in mapping dictionary.

Parameters (via run() kwargs):
  filename (str, required): Filename with extension (e.g., 'main.py')

Returns (dict):
  success (bool): True if the operation succeeded.
  language (str): Detected programming language name (e.g., 'Python', 'JavaScript', 'Unknown').
  error (str|None): Error message on failure, None on success.
"""
from __future__ import annotations
import os

TOOL_META = {
    "name": "detect_language_from_extension",
    "tool_type": "python_function",
    "dependencies": [],
}

# Hardcoded mapping of file extensions to programming language names
_EXTENSION_MAP = {
    ".py": "Python",
    ".pyi": "Python",
    ".ipynb": "Jupyter Notebook",
    ".js": "JavaScript",
    ".ts": "TypeScript",
    ".java": "Java",
    ".go": "Go",
    ".rb": "Ruby",
    ".rs": "Rust",
    ".c": "C",
    ".cpp": "C++",
    ".h": "C/C++ Header",
    ".cs": "C#",
    ".swift": "Swift",
    ".kt": "Kotlin",
    ".sh": "Shell",
    ".yaml": "YAML",
    ".yml": "YAML",
    ".json": "JSON",
    ".md": "Markdown",
    ".txt": "Text",
    ".html": "HTML",
    ".css": "CSS",
    ".sql": "SQL",
    ".dockerfile": "Dockerfile",
    ".tf": "Terraform",
}


def run(filename: str) -> dict:
    """Detect programming language from a file extension using a built-in mapping dictionary.

    Returns:
        success (bool): True if the operation succeeded.
        language (str): Detected programming language name (e.g., 'Python', 'JavaScript', 'Unknown').
        error (str|None): Error message on failure, None on success.
    """
    if not filename or not isinstance(filename, str):
        return {"success": False, "language": "", "error": "filename must be a non-empty string"}

    try:
        # Extract the file extension (e.g., '.py') and convert to lowercase
        ext = os.path.splitext(filename)[1].lower()

        # Look up the extension in the mapping; return 'Unknown' if not found
        language = _EXTENSION_MAP.get(ext, "Unknown")

        return {"success": True, "language": language, "error": None}
    except Exception as exc:
        return {"success": False, "language": "", "error": str(exc)}
