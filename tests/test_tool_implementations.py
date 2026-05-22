"""Tests for starter pack tool implementations.

Each test calls run() directly and verifies return shape.
Tests requiring external network or heavy deps are marked with appropriate marks.
"""
import importlib
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

IMPL_DIR = Path(__file__).parent.parent / "systemu" / "vault" / "tools" / "implementations"
sys.path.insert(0, str(IMPL_DIR))


def _load(name):
    """Import a tool implementation module by name."""
    spec = importlib.util.spec_from_file_location(name, IMPL_DIR / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ─── file_read ────────────────────────────────────────────────────────────────

class TestFileRead:
    def test_read_existing_file(self, tmp_path):
        f = tmp_path / "hello.txt"
        f.write_text("Hello, world!", encoding="utf-8")
        mod = _load("file_read")
        result = mod.run(path=str(f))
        assert result["success"] is True
        assert result["content"] == "Hello, world!"
        assert result["size_bytes"] == len("Hello, world!")

    def test_nonexistent_file(self):
        mod = _load("file_read")
        result = mod.run(path="/nonexistent/file.txt")
        assert result["success"] is False
        assert result["error"]

    def test_has_tool_meta(self):
        mod = _load("file_read")
        assert hasattr(mod, "TOOL_META")
        assert mod.TOOL_META["name"] == "file_read"


# ─── file_write ───────────────────────────────────────────────────────────────

class TestFileWrite:
    def test_write_creates_file(self, tmp_path):
        out = tmp_path / "output.txt"
        mod = _load("file_write")
        result = mod.run(path=str(out), content="Test content")
        assert result["success"] is True
        assert out.exists()
        assert out.read_text() == "Test content"

    def test_write_creates_parent_dirs(self, tmp_path):
        out = tmp_path / "a" / "b" / "c.txt"
        mod = _load("file_write")
        result = mod.run(path=str(out), content="nested")
        assert result["success"] is True
        assert out.read_text() == "nested"

    def test_no_overwrite_fails(self, tmp_path):
        out = tmp_path / "existing.txt"
        out.write_text("original")
        mod = _load("file_write")
        result = mod.run(path=str(out), content="new", overwrite=False)
        assert result["success"] is False
        assert out.read_text() == "original"  # unchanged


# ─── file_append ──────────────────────────────────────────────────────────────

class TestFileAppend:
    def test_append_to_existing(self, tmp_path):
        f = tmp_path / "log.txt"
        f.write_text("line1\n")
        mod = _load("file_append")
        result = mod.run(path=str(f), content="line2\n")
        assert result["success"] is True
        assert f.read_text() == "line1\nline2\n"

    def test_creates_if_not_exists(self, tmp_path):
        f = tmp_path / "new.txt"
        mod = _load("file_append")
        result = mod.run(path=str(f), content="first line\n")
        assert result["success"] is True
        assert f.read_text() == "first line\n"


# ─── file_list_dir ────────────────────────────────────────────────────────────

class TestFileListDir:
    def test_list_all(self, tmp_path):
        (tmp_path / "a.txt").write_text("a")
        (tmp_path / "b.txt").write_text("b")
        (tmp_path / "c.py").write_text("c")
        mod = _load("file_list_dir")
        result = mod.run(path=str(tmp_path))
        assert result["success"] is True
        assert result["count"] == 3

    def test_glob_pattern(self, tmp_path):
        (tmp_path / "a.txt").write_text("a")
        (tmp_path / "b.md").write_text("b")
        mod = _load("file_list_dir")
        result = mod.run(path=str(tmp_path), pattern="*.txt")
        assert result["success"] is True
        assert result["count"] == 1

    def test_nonexistent_dir(self):
        mod = _load("file_list_dir")
        result = mod.run(path="/nonexistent/dir/")
        assert result["success"] is False


# ─── file_copy ────────────────────────────────────────────────────────────────

class TestFileCopy:
    def test_copy_file(self, tmp_path):
        src = tmp_path / "src.txt"
        src.write_text("source content")
        dst = tmp_path / "dst.txt"
        mod = _load("file_copy")
        result = mod.run(src=str(src), dst=str(dst))
        assert result["success"] is True
        assert dst.read_text() == "source content"

    def test_no_overwrite(self, tmp_path):
        src = tmp_path / "src.txt"
        src.write_text("new")
        dst = tmp_path / "dst.txt"
        dst.write_text("original")
        mod = _load("file_copy")
        result = mod.run(src=str(src), dst=str(dst), overwrite=False)
        assert result["success"] is False
        assert dst.read_text() == "original"


# ─── file_delete ──────────────────────────────────────────────────────────────

class TestFileDelete:
    def test_delete_existing(self, tmp_path):
        f = tmp_path / "delete_me.txt"
        f.write_text("bye")
        mod = _load("file_delete")
        result = mod.run(path=str(f))
        assert result["success"] is True
        assert not f.exists()

    def test_delete_nonexistent(self):
        mod = _load("file_delete")
        result = mod.run(path="/nonexistent/file.txt")
        assert result["success"] is False


# ─── compress_files / extract_archive ─────────────────────────────────────────

class TestArchive:
    def test_compress_and_extract(self, tmp_path):
        # Create some files
        src1 = tmp_path / "a.txt"
        src2 = tmp_path / "b.txt"
        src1.write_text("file a")
        src2.write_text("file b")
        archive = tmp_path / "test.zip"
        out_dir = tmp_path / "extracted"
        out_dir.mkdir()

        compress_mod = _load("compress_files")
        result = compress_mod.run(
            output_path=str(archive),
            files=[str(src1), str(src2)],
        )
        assert result["success"] is True
        assert archive.exists()
        assert result["file_count"] == 2

        extract_mod = _load("extract_archive")
        result2 = extract_mod.run(archive_path=str(archive), output_dir=str(out_dir))
        assert result2["success"] is True
        assert result2["file_count"] == 2

    def test_compress_directory(self, tmp_path):
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        for i in range(3):
            (src_dir / f"file{i}.txt").write_text(f"content {i}")
        archive = tmp_path / "dir.zip"
        mod = _load("compress_files")
        result = mod.run(output_path=str(archive), include_dir=str(src_dir))
        assert result["success"] is True
        assert result["file_count"] == 3


# ─── parse_json ───────────────────────────────────────────────────────────────

class TestParseJson:
    def test_parse_json_string(self):
        mod = _load("parse_json")
        result = mod.run(input='{"key": "value", "num": 42}')
        assert result["success"] is True
        assert result["data"]["key"] == "value"
        assert result["data"]["num"] == 42

    def test_parse_json_file(self, tmp_path):
        f = tmp_path / "data.json"
        f.write_text('{"items": [1, 2, 3]}')
        mod = _load("parse_json")
        result = mod.run(input=str(f), mode="file")
        assert result["success"] is True
        assert result["data"]["items"] == [1, 2, 3]

    def test_invalid_json(self):
        mod = _load("parse_json")
        result = mod.run(input="{not valid json}", mode="string")
        assert result["success"] is False

    def test_auto_mode_tries_json_first(self):
        mod = _load("parse_json")
        result = mod.run(input='["a", "b"]', mode="auto")
        assert result["success"] is True
        assert result["data"] == ["a", "b"]


# ─── format_date ──────────────────────────────────────────────────────────────

class TestFormatDate:
    def test_standard_conversion(self):
        mod = _load("format_date")
        result = mod.run(date_str="2026-05-04", input_format="%Y-%m-%d", output_format="%m/%d/%Y")
        assert result["success"] is True
        assert result["result"] == "05/04/2026"

    def test_custom_format(self):
        mod = _load("format_date")
        result = mod.run(date_str="04052026", input_format="%d%m%Y", output_format="%d-%m-%Y")
        assert result["success"] is True
        assert result["result"] == "04-05-2026"

    def test_invalid_date(self):
        mod = _load("format_date")
        result = mod.run(date_str="not-a-date")
        assert result["success"] is False


# ─── run_command ──────────────────────────────────────────────────────────────

class TestRunCommand:
    def test_echo_command(self):
        mod = _load("run_command")
        # Use echo — works on both Windows and Unix via shell=True
        result = mod.run(command="echo hello")
        assert result["success"] is True
        assert result["return_code"] == 0
        assert "hello" in result["stdout"].lower() or "hello" in result["stdout"]

    def test_failing_command(self):
        mod = _load("run_command")
        result = mod.run(command="this_command_does_not_exist_xyz_abc")
        assert result["success"] is False or result["return_code"] != 0

    def test_timeout(self):
        mod = _load("run_command")
        result = mod.run(command="ping -n 10 127.0.0.1" if os.name == "nt" else "sleep 10", timeout=1)
        assert result["success"] is False


# ─── create_word_doc / read_word_doc ──────────────────────────────────────────

class TestWordDoc:
    def test_create_and_read(self, tmp_path):
        pytest.importorskip("docx", reason="python-docx not installed")
        out = tmp_path / "test.docx"
        create_mod = _load("create_word_doc")
        result = create_mod.run(
            output_path=str(out),
            title="Test Document",
            body_text="This is the body text.",
        )
        assert result["success"] is True
        assert out.exists()

        read_mod = _load("read_word_doc")
        result2 = read_mod.run(path=str(out))
        assert result2["success"] is True
        assert "Test Document" in result2["text"] or "body text" in result2["text"]
        assert result2["paragraph_count"] >= 1

    def test_create_overwrite_false(self, tmp_path):
        pytest.importorskip("docx", reason="python-docx not installed")
        out = tmp_path / "existing.docx"
        mod = _load("create_word_doc")
        mod.run(output_path=str(out), body_text="original")
        result = mod.run(output_path=str(out), body_text="new", overwrite=False)
        assert result["success"] is False


# ─── create_excel_sheet / read_excel_sheet ────────────────────────────────────

class TestExcel:
    def test_create_and_read(self, tmp_path):
        pytest.importorskip("openpyxl", reason="openpyxl not installed")
        out = tmp_path / "data.xlsx"
        create_mod = _load("create_excel_sheet")
        result = create_mod.run(
            output_path=str(out),
            sheet_name="Report",
            headers=["Name", "Value", "Date"],
            rows=[["Alpha", 42, "2026-05-04"], ["Beta", 99, "2026-05-05"]],
        )
        assert result["success"] is True
        assert out.exists()

        read_mod = _load("read_excel_sheet")
        result2 = read_mod.run(path=str(out), sheet_name="Report")
        assert result2["success"] is True
        assert result2["headers"] == ["Name", "Value", "Date"]
        assert result2["row_count"] == 2
        assert result2["rows"][0][0] == "Alpha"


# ─── image_resize ─────────────────────────────────────────────────────────────

class TestImageResize:
    def test_resize_png(self, tmp_path):
        PIL = pytest.importorskip("PIL", reason="Pillow not installed")
        from PIL import Image
        # Create a 200x100 image
        img = Image.new("RGB", (200, 100), color="red")
        src = tmp_path / "src.png"
        img.save(src)

        out = tmp_path / "resized.png"
        mod = _load("image_resize")
        result = mod.run(input_path=str(src), output_path=str(out), width=100, height=0)
        assert result["success"] is True
        assert result["width"] == 100
        assert result["height"] == 50  # aspect ratio maintained

    def test_both_dimensions_zero_fails(self, tmp_path):
        pytest.importorskip("PIL", reason="Pillow not installed")
        mod = _load("image_resize")
        result = mod.run(input_path="/nonexistent.png", output_path="/out.png", width=0, height=0)
        assert result["success"] is False


# ─── Tool META validation ─────────────────────────────────────────────────────

class TestToolMeta:
    REQUIRED_TOOLS = [
        "web_screenshot", "web_extract_text", "web_extract_table",
        "fetch_json", "fetch_html", "download_file", "web_search",
        "file_read", "file_write", "file_append", "file_list_dir",
        "file_copy", "file_delete", "compress_files", "extract_archive",
        "create_word_doc", "read_word_doc", "create_excel_sheet", "read_excel_sheet",
        "take_screenshot", "clipboard_read", "clipboard_write",
        "notify_desktop", "run_command", "parse_json", "format_date", "image_resize",
        "launch_application", "close_application", "keyboard_shortcut",
        "type_text", "browser_navigate",
    ]

    @pytest.mark.parametrize("tool_name", REQUIRED_TOOLS)
    def test_tool_has_meta(self, tool_name):
        mod = _load(tool_name)
        assert hasattr(mod, "TOOL_META"), f"{tool_name} missing TOOL_META"
        assert "name" in mod.TOOL_META
        assert "tool_type" in mod.TOOL_META
        assert "dependencies" in mod.TOOL_META

    @pytest.mark.parametrize("tool_name", REQUIRED_TOOLS)
    def test_tool_has_run_function(self, tool_name):
        mod = _load(tool_name)
        assert hasattr(mod, "run"), f"{tool_name} missing run()"
        assert callable(mod.run)
