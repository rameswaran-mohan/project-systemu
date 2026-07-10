# tests/test_ra11a_reference_synonyms.py
from systemu.runtime.reference_synonyms import synonym_exts

def test_sheet_maps_to_spreadsheet_exts():
    assert ".xlsx" in synonym_exts("sheet")
    assert ".csv" in synonym_exts("spreadsheet")

def test_deck_maps_to_pptx():
    assert synonym_exts("deck") == frozenset({".pptx"})

def test_doc_and_report_map_to_docx():
    assert ".docx" in synonym_exts("letter")
    assert ".docx" in synonym_exts("report")

def test_unknown_token_is_empty_not_crash():
    assert synonym_exts("wombat") == frozenset()
    assert synonym_exts("") == frozenset()
    assert synonym_exts(None) == frozenset()   # defensive

def test_case_insensitive():
    assert synonym_exts("SHEET") == synonym_exts("sheet")
