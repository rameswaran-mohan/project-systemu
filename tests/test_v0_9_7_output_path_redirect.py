"""v0.9.7 — sandbox output-path redirect: deliverables must land in output_dir
even when the LLM mangles a long absolute path (the GATE-1 root cause).
"""
from systemu.runtime.tool_sandbox import _normalize_output_paths


def test_write_outside_output_dir_existing_parent_is_redirected(tmp_path):
    out = tmp_path / "out"; out.mkdir()
    sibling = tmp_path / "wrong"; sibling.mkdir()  # existing but wrong tree
    bad = str(sibling / "my_location.txt")
    res = _normalize_output_paths("file_write", {"path": bad, "content": "Chennai"}, str(out))
    assert res["path"] == str(out / "my_location.txt")
    assert res["content"] == "Chennai"


def test_write_missing_parent_is_redirected(tmp_path):
    out = tmp_path / "out"; out.mkdir()
    bad = str(tmp_path / "does_not_exist" / "deep" / "f.txt")
    res = _normalize_output_paths("file_write", {"path": bad}, str(out))
    assert res["path"] == str(out / "f.txt")


def test_write_inside_output_dir_unchanged(tmp_path):
    out = tmp_path / "out"; out.mkdir()
    good = str(out / "report.md")
    res = _normalize_output_paths("write_markdown_file", {"output_path": good}, str(out))
    assert res["output_path"] == good  # already in output_dir → untouched


def test_read_outside_output_dir_existing_parent_preserved(tmp_path):
    out = tmp_path / "out"; out.mkdir()
    src = tmp_path / "inputs"; src.mkdir()
    infile = str(src / "data.csv")
    # read tool, path outside output_dir but parent EXISTS → must NOT redirect
    res = _normalize_output_paths("file_read", {"path": infile}, str(out))
    assert res["path"] == infile


def test_read_missing_parent_is_redirected_safe(tmp_path):
    out = tmp_path / "out"; out.mkdir()
    infile = str(tmp_path / "nope" / "data.csv")  # parent missing → safe to redirect
    res = _normalize_output_paths("file_read", {"path": infile}, str(out))
    assert res["path"] == str(out / "data.csv")


def test_non_path_params_untouched(tmp_path):
    out = tmp_path / "out"; out.mkdir()
    res = _normalize_output_paths("file_write", {"content": "x", "encoding": "utf-8"}, str(out))
    assert res == {"content": "x", "encoding": "utf-8"}


def test_no_output_dir_is_noop(tmp_path):
    p = {"path": str(tmp_path / "nope" / "f.txt")}
    assert _normalize_output_paths("file_write", dict(p), None) == p


def test_exact_gate1_mangled_path_scenario(tmp_path):
    """The real failure: agent dropped a path segment; deliverable must still
    land in the correct output_dir."""
    out = tmp_path / "Project_pro" / "output"; out.mkdir(parents=True)
    mangled = str(tmp_path / "Project" / "output" / "my_location.txt")  # wrong tree
    res = _normalize_output_paths("file_write", {"path": mangled, "content": "Chennai"}, str(out))
    assert res["path"] == str(out / "my_location.txt")
