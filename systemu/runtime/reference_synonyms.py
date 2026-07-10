# systemu/runtime/reference_synonyms.py
"""R-A11a §5.4 — a deterministic phrase-class → file-extension hint table.

Pure data + one lookup; NO I/O, NO state. Seeded to align with the vocabulary in
``tool_registry._PARAM_SYNONYMS`` (do not fork it — extend the same intent)."""
from __future__ import annotations

# phrase-class token → the extensions it implies (lower-case, dot-prefixed)
_SYNONYM_EXTS: dict[str, frozenset[str]] = {
    "sheet": frozenset({".xlsx", ".xls", ".csv"}),
    "spreadsheet": frozenset({".xlsx", ".xls", ".csv"}),
    "workbook": frozenset({".xlsx", ".xls"}),
    "deck": frozenset({".pptx"}),
    "slides": frozenset({".pptx"}),
    "presentation": frozenset({".pptx"}),
    "doc": frozenset({".docx"}),
    "document": frozenset({".docx"}),
    "letter": frozenset({".docx"}),
    "memo": frozenset({".docx"}),
    "report": frozenset({".docx", ".pdf"}),
    "resume": frozenset({".pdf", ".docx"}),
    "cv": frozenset({".pdf", ".docx"}),
    "pdf": frozenset({".pdf"}),
    "notebook": frozenset({".ipynb"}),
}


def synonym_exts(token) -> frozenset[str]:
    """Extensions implied by a phrase-class ``token`` (case-insensitive); empty when
    unknown/blank/non-str (defensive — a caller may pass None)."""
    if not isinstance(token, str) or not token:
        return frozenset()
    return _SYNONYM_EXTS.get(token.strip().lower(), frozenset())
