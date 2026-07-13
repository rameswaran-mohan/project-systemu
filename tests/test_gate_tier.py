"""GATE-TIER (DEC-14) — the edit-safe test subset.

Verifies the conftest auto-tagger's load-bearing contract: tests that read source
via ``inspect.getsource`` are detected as ``source_sensitive`` (and everything else
is not), so ``pytest -m "not source_sensitive"`` is a subset you can run WHILE a
subagent edits source files, without spurious getsource-mismatch failures.
"""
from __future__ import annotations

from conftest import module_text_is_source_sensitive


def test_detects_getsource_usage():
    assert module_text_is_source_sensitive("import inspect\ninspect.getsource(foo)") is True
    assert module_text_is_source_sensitive("x = getsource(bar)") is True


def test_plain_test_is_not_source_sensitive():
    assert module_text_is_source_sensitive("def test_x():\n    assert 1 + 1 == 2") is False
    assert module_text_is_source_sensitive("") is False
    assert module_text_is_source_sensitive(None) is False       # defensive


def test_auto_tagger_ran_and_flagged_this_module(request):
    # this file's own source contains "getsource(" (in the detection tests above),
    # so the conftest auto-tagger must have marked ITS collected items
    # `source_sensitive` — proving the pytest_collection_modifyitems hook ran and
    # detects by module content (not manual per-file marking).
    mine = [it for it in request.session.items if it.fspath == request.node.fspath]
    assert mine and all(
        any(m.name == "source_sensitive" for m in it.iter_markers()) for it in mine)
