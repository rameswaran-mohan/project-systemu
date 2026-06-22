from pathlib import Path
import textwrap


def test_scan_tool_deps_finds_dependencies_from_implementations(tmp_path):
    from install import scan_tool_deps

    impl_dir = tmp_path / "implementations"
    impl_dir.mkdir()
    (impl_dir / "fetch_json.py").write_text(textwrap.dedent("""\
        # deps: requests>=2.31
        import requests
    """))
    (impl_dir / "create_word_doc.py").write_text(textwrap.dedent("""\
        # deps: python-docx
        from docx import Document
    """))

    deps = scan_tool_deps(impl_dir)
    assert set(deps) == {"requests>=2.31", "python-docx"}


def test_scan_tool_deps_dedupes_across_files(tmp_path):
    from install import scan_tool_deps
    impl_dir = tmp_path / "impl"; impl_dir.mkdir()
    (impl_dir / "a.py").write_text("# deps: requests\n")
    (impl_dir / "b.py").write_text("# deps: requests\n# deps: lxml\n")
    deps = scan_tool_deps(impl_dir)
    assert sorted(deps) == ["lxml", "requests"]


def test_scan_tool_deps_handles_comma_separated_list(tmp_path):
    from install import scan_tool_deps
    impl_dir = tmp_path / "impl"; impl_dir.mkdir()
    (impl_dir / "a.py").write_text("# deps: requests, python-docx, lxml\n")
    deps = scan_tool_deps(impl_dir)
    assert set(deps) == {"requests", "python-docx", "lxml"}


def test_scan_tool_deps_empty_dir(tmp_path):
    from install import scan_tool_deps
    impl_dir = tmp_path / "empty"; impl_dir.mkdir()
    assert scan_tool_deps(impl_dir) == []
