"""DEC-34 -- the optional_deps module docstring must describe the probe it ships.

The module docstring said availability is "NEVER CACHED" and that the probe
"reads ``importlib.metadata`` on every call". D-3 (e2e 0.10.31) then shipped a
per-distribution, process-lifetime memo in ``is_installed`` with
``clear_probe_cache`` as its reset seam. A false assertion about what the code
does is a defect of its own (DEC-34), so the docstring now says what is
cached, for how long, and how it is cleared -- and this pins it.
"""
from __future__ import annotations

from systemu.runtime import optional_deps as od


def _doc() -> str:
    return od.__doc__ or ""


def test_the_module_no_longer_claims_it_never_caches():
    doc = _doc()
    assert "NEVER CACHED" not in doc
    assert "on every call" not in doc


def test_the_module_describes_the_memo_and_how_to_clear_it():
    doc = _doc()
    assert "memo" in doc.lower()
    assert "clear_probe_cache" in doc
    assert "is_installed" in doc
    # what is cached, and for how long
    assert "per distribution" in doc.lower()
    assert "process lifetime" in doc.lower()
    # the bound the code enforces is the bound the docstring names
    assert str(od._MEMO_CAP) in doc


def test_the_memo_the_docstring_describes_is_real():
    """Guard against the docstring being right about a memo that is gone."""
    od.clear_probe_cache()
    try:
        od.is_installed("definitely-not-a-real-distribution-xyz")
        assert od._INSTALLED_MEMO.get("definitely-not-a-real-distribution-xyz") is False
        od.clear_probe_cache()
        assert od._INSTALLED_MEMO == {}
    finally:
        od.clear_probe_cache()
