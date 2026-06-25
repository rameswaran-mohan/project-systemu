import os
from systemu.vault.tools.implementations import file_list_dir


def test_recursive_refuses_drive_root(tmp_path):
    # A recursive scan rooted at a drive root must be refused, not attempted.
    root = os.path.splitdrive(os.getcwd())[0] + os.sep  # e.g. "C:\\" or "/"
    res = file_list_dir.run(path=root, recursive=True)
    assert res["success"] is False
    assert "recursiv" in (res.get("error") or "").lower()


def test_recursive_depth_and_count_bounded(tmp_path):
    # Build a deep, wide tree; recursive listing must succeed but be bounded.
    d = tmp_path
    for i in range(8):
        d = d / f"lvl{i}"
        d.mkdir()
        (d / f"f{i}.txt").write_text("x")
    res = file_list_dir.run(path=str(tmp_path), recursive=True, pattern="*.txt")
    assert res["success"] is True
    # Bounded by max depth (default 5) — deeper files are not returned.
    assert all("lvl6" not in f and "lvl7" not in f for f in res["files"])


def test_non_recursive_unaffected(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    res = file_list_dir.run(path=str(tmp_path), recursive=False, pattern="*.txt")
    assert res["success"] is True and res["count"] == 1
