"""create_word_doc gracefully handles a directory output_path by
appending a default filename derived from the title.

These tests exercise the real ``python-docx`` write path so they only run
when that dep is installed.  In CI environments without it, they skip
cleanly rather than fail — the tool's deployment-time dep approval is the
right place to ensure ``docx`` is available, not pytest setup."""
import pytest

# Skip the whole module when python-docx isn't installed. The schema
# description test (which doesn't touch python-docx) is also gated for
# simplicity — once an environment has the tool's deps, all four tests run.
pytest.importorskip(
    "docx",
    reason="python-docx not installed; create_word_doc dir-path tests "
           "exercise the real .docx write path. In dev, install via: "
           "pip install python-docx",
)


def test_create_word_doc_with_directory_path_writes_under_it(tmp_path):
    from systemu.vault.tools.implementations.create_word_doc import (
        create_word_doc,
    )
    out_dir = tmp_path / "outputs"
    out_dir.mkdir()

    result = create_word_doc(
        output_path=str(out_dir),
        title="Tokyo Weather 2026-05-21",
        sections=[{"heading": "Summary", "content": "Body text."}],
    )

    assert result.get("success") is True, f"got: {result}"
    written = list(out_dir.glob("*.docx"))
    assert len(written) == 1
    # Filename should reflect the title
    assert "Tokyo" in written[0].name


def test_create_word_doc_with_file_path_uses_it_directly(tmp_path):
    """Backward-compat: explicit file path still works."""
    from systemu.vault.tools.implementations.create_word_doc import (
        create_word_doc,
    )
    target = tmp_path / "report.docx"
    result = create_word_doc(
        output_path=str(target),
        title="x",
        sections=[{"heading": "h", "content": "c"}],
    )
    assert result.get("success") is True
    assert target.exists()


def test_create_word_doc_with_path_no_extension_adds_docx(tmp_path):
    """If output_path has no extension and isn't an existing dir, add .docx."""
    from systemu.vault.tools.implementations.create_word_doc import (
        create_word_doc,
    )
    target = tmp_path / "report"  # no .docx
    result = create_word_doc(
        output_path=str(target),
        title="x",
        sections=[{"heading": "h", "content": "c"}],
    )
    assert result.get("success") is True
    assert (tmp_path / "report.docx").exists()


def test_create_word_doc_schema_description_is_unambiguous():
    """The parameters_schema.description for output_path should explicitly
    mention BOTH file path and directory path as accepted forms."""
    from systemu.vault.tools.implementations.create_word_doc import (
        TOOL_SPEC,
    )
    desc = TOOL_SPEC["parameters_schema"]["properties"]["output_path"]["description"]
    assert "directory" in desc.lower()
    assert "file" in desc.lower()
