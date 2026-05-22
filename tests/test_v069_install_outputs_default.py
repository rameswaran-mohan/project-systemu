"""docker-mode outputs default to ~/SystemuOutputs on Windows
(auto-shared by Docker Desktop), not project-local ./outputs."""
from pathlib import Path
import platform


def test_resolve_outputs_host_dir_default_on_windows(monkeypatch):
    from install import _resolve_outputs_host_dir
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    p = _resolve_outputs_host_dir()
    assert "SystemuOutputs" in str(p), \
        f"expected ~/SystemuOutputs default on Windows, got {p}"
    # path should be under user home on Windows
    home = str(Path.home()).replace("\\", "/")
    assert str(p).replace("\\", "/").startswith(home), \
        f"path should be under user home on Windows, got {p}"


def test_resolve_outputs_host_dir_default_on_linux(monkeypatch):
    """Linux/macOS use project-relative ./outputs."""
    from install import _resolve_outputs_host_dir
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    p = _resolve_outputs_host_dir()
    assert str(p).endswith("outputs"), f"expected ./outputs on Linux, got {p}"


def test_resolve_outputs_host_dir_creates_directory(tmp_path, monkeypatch):
    """The wizard auto-creates the directory if missing."""
    from install import _resolve_outputs_host_dir
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    p = _resolve_outputs_host_dir()
    assert p.exists(), f"directory should be created, but {p} does not exist"
    assert p.is_dir()
