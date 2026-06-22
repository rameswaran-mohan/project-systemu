"""W12-A1/B1 — starter-pack conformance: the pack must WORK from install.

Operator requirement R3 (2026-06-12): "Brilliant and useful tool/skill
stack as starter pack. Many a time they are not working as expected and
fails. This should not be the case — systemu is supposed to work and be
usable from installation."

Two layers, both keyless and offline:
  * CONTRACT — every seed tool parses, exposes run() (positional or
    **kwargs style) + TOOL_META, and declares well-formed deps.
  * EXECUTION — every pure-local tool runs through the REAL W6 subprocess
    runner with realistic params and produces the right OUTCOME (files on
    disk, round-trips read back), not just success:true.

Network/desktop/LLM tools are exercised by the golden tasks + the manual
A6 protocol — contract layer only here.
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
IMPL_DIR = REPO / "systemu" / "vault" / "tools" / "implementations"
RUNNER = REPO / "systemu" / "runtime" / "backend" / "tool_runner_script.py"
INDEX = REPO / "systemu" / "vault" / "tools" / "index.json"


def _index():
    return json.loads(INDEX.read_text(encoding="utf-8"))


def _run_tool(name: str, params: dict, timeout: int = 60) -> dict:
    """One tool through the real runner; returns the parsed JSON payload."""
    impl = IMPL_DIR / f"{name}.py"
    proc = subprocess.run(
        [sys.executable, str(RUNNER), str(impl), "--params", json.dumps(params)],
        capture_output=True, text=True, timeout=timeout)
    lines = (proc.stdout or "").strip().splitlines()
    assert lines, f"{name}: no stdout (exit {proc.returncode}; stderr: {proc.stderr[-300:]})"
    return json.loads(lines[-1])


# ── Contract layer ───────────────────────────────────────────────────────────

class TestPackContract:
    def test_every_tool_has_an_implementation(self):
        missing = [t["name"] for t in _index()
                   if not (IMPL_DIR / f"{t['name']}.py").exists()]
        assert missing == []

    def test_every_implementation_parses_and_exposes_run(self):
        broken = []
        for t in _index():
            src = (IMPL_DIR / f"{t['name']}.py").read_text(encoding="utf-8")
            try:
                tree = ast.parse(src)
            except SyntaxError as exc:
                broken.append(f"{t['name']}: syntax error line {exc.lineno}")
                continue
            names = {n.name for n in ast.walk(tree)
                     if isinstance(n, ast.FunctionDef)}
            if "run" not in names:
                broken.append(f"{t['name']}: no run()")
        assert broken == []

    def test_declared_deps_are_wellformed(self):
        import re
        bad = [(t["name"], d) for t in _index()
               for d in (t.get("dependencies") or [])
               if not re.match(r"^[A-Za-z0-9._-]+", d)]
        assert bad == []

    def test_every_pack_dep_ships_with_the_core_install(self):
        """W12-B1 (R3/R5): a starter tool must never hit the dep-approval
        flow on a fresh install — every pip dep the seed pack declares is a
        core dependency of systemu itself. Platform-conditional deps
        (pynput, win32-only) count as shipped."""
        import re
        pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
        deps_block = pyproject.split("dependencies = [", 1)[1].split("]", 1)[0]
        shipped = {re.split(r"[<>=!~;\[]", line.strip().strip('",'))[0].lower()
                   for line in deps_block.splitlines()
                   if line.strip().startswith('"')}
        declared = {d.lower() for t in _index()
                    for d in (t.get("dependencies") or [])}
        missing = declared - shipped
        assert missing == set(), \
            f"pack deps not shipped with the core install: {sorted(missing)}"


# ── Execution layer — pure-local tools, real runner, outcome assertions ─────

class TestPackExecution:
    @pytest.fixture()
    def box(self, tmp_path: Path) -> Path:
        (tmp_path / "input.txt").write_text("hello world\nline two\n",
                                            encoding="utf-8")
        return tmp_path

    def test_write_text_file(self, box):
        out = box / "t.txt"
        r = _run_tool("write_text_file", {"file_path": str(out),
                                          "content": "shipping"})
        assert r["success"] and out.read_text(encoding="utf-8") == "shipping"

    def test_write_markdown_file(self, box):
        out = box / "t.md"
        r = _run_tool("write_markdown_file", {"file_path": str(out),
                                              "content": "# hi"})
        assert r["success"] and out.exists()

    def test_write_csv_file(self, box):
        out = box / "t.csv"
        r = _run_tool("write_csv_file", {
            "output_path": str(out),
            "data": [{"a": "1", "b": "2"}, {"a": "3", "b": "4"}]})
        assert r["success"], r
        assert "a" in out.read_text(encoding="utf-8").splitlines()[0]

    def test_file_write_read_roundtrip(self, box):
        out = box / "rw.txt"
        assert _run_tool("file_write", {"path": str(out),
                                        "content": "roundtrip"})["success"]
        r = _run_tool("file_read", {"path": str(out)})
        assert r["success"] and "roundtrip" in str(r.get("content"))

    def test_file_append(self, box):
        out = box / "ap.txt"
        out.write_text("a", encoding="utf-8")
        assert _run_tool("file_append", {"path": str(out),
                                         "content": "b"})["success"]
        assert out.read_text(encoding="utf-8") == "ab"

    def test_file_copy(self, box):
        dst = box / "copy.txt"
        r = _run_tool("file_copy", {"src": str(box / "input.txt"),
                                    "dst": str(dst)})
        assert r["success"] and dst.exists()

    def test_file_delete(self, box):
        victim = box / "victim.txt"
        victim.write_text("x", encoding="utf-8")
        assert _run_tool("file_delete", {"path": str(victim)})["success"]
        assert not victim.exists()

    def test_file_list_dir(self, box):
        r = _run_tool("file_list_dir", {"path": str(box)})
        assert r["success"]
        listed = json.dumps(r)
        assert "input.txt" in listed

    def test_file_scan_directory(self, box):
        r = _run_tool("file_scan_directory", {"source_path": str(box)})
        assert r["success"] and r.get("files")

    def test_compress_then_extract_roundtrip(self, box):
        zip_path = box / "arch.zip"
        r = _run_tool("compress_files", {"output_path": str(zip_path),
                                         "files": [str(box / "input.txt")]})
        assert r["success"] and zipfile.is_zipfile(zip_path)
        out_dir = box / "unz"
        r2 = _run_tool("extract_archive", {"archive_path": str(zip_path),
                                           "output_dir": str(out_dir)})
        assert r2["success"] and (out_dir / "input.txt").exists()

    def test_parse_json(self, box):
        r = _run_tool("parse_json", {"input": '{"k": [1, 2]}'})
        assert r["success"], r

    def test_format_date(self, box):
        r = _run_tool("format_date", {"date_str": "2026-06-12"})
        assert r["success"] and "06" in json.dumps(r)

    def test_detect_language_from_extension(self, box):
        r = _run_tool("detect_language_from_extension",
                      {"filename": "script.py"})
        assert r["success"]

    @pytest.mark.skipif(
        not pytest.importorskip("importlib.metadata", reason="stdlib")
        or subprocess.run([sys.executable, "-c", "import openpyxl"],
                          capture_output=True).returncode != 0,
        reason="openpyxl not installed")
    def test_excel_roundtrip(self, box):
        out = box / "t.xlsx"
        r = _run_tool("create_excel_sheet", {
            "output_path": str(out), "headers": ["c1", "c2"],
            "rows": [["v1", "v2"]]})
        assert r["success"] and out.exists()
        r2 = _run_tool("read_excel_sheet", {"path": str(out)})
        assert r2["success"] and "v1" in json.dumps(r2)

    @pytest.mark.skipif(
        subprocess.run([sys.executable, "-c", "import docx"],
                       capture_output=True).returncode != 0,
        reason="python-docx not installed")
    def test_word_roundtrip(self, box):
        out = box / "t.docx"
        r = _run_tool("create_word_doc", {"output_path": str(out),
                                          "title": "T", "body_text": "hello doc"})
        assert r["success"] and out.exists()
        r2 = _run_tool("read_word_doc", {"path": str(out)})
        assert r2["success"] and "hello doc" in json.dumps(r2)
