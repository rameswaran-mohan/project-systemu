from pathlib import Path
import json
from systemu.runtime.memory_backends.filesystem import FilesystemMemoryBackend


def test_load_buffer_returns_jsonl_entries(tmp_path):
    sd = tmp_path / "shadow_x"
    sd.mkdir()
    buf = sd / "memory_buffer.jsonl"
    buf.write_text(
        json.dumps({"category": "tool_quirks", "lesson": "first"}) + "\n" +
        json.dumps({"category": "heuristics", "lesson": "second"}) + "\n",
        encoding="utf-8",
    )
    be = FilesystemMemoryBackend(memory_root=tmp_path)
    entries = be.load_buffer("shadow_x")
    assert len(entries) == 2
    assert entries[0]["category"] == "tool_quirks"
    assert entries[1]["lesson"] == "second"


def test_load_buffer_missing_file_returns_empty(tmp_path):
    be = FilesystemMemoryBackend(memory_root=tmp_path)
    assert be.load_buffer("shadow_never_existed") == []


def test_load_buffer_skips_malformed_jsonl_lines(tmp_path):
    sd = tmp_path / "shadow_y"
    sd.mkdir()
    (sd / "memory_buffer.jsonl").write_text(
        json.dumps({"lesson": "ok"}) + "\n" +
        "this is not json\n" +
        json.dumps({"lesson": "also ok"}) + "\n",
        encoding="utf-8",
    )
    be = FilesystemMemoryBackend(memory_root=tmp_path)
    entries = be.load_buffer("shadow_y")
    assert len(entries) == 2  # malformed line silently dropped
    assert entries[0]["lesson"] == "ok"
    assert entries[1]["lesson"] == "also ok"


def test_append_buffer_writes_jsonl(tmp_path):
    be = FilesystemMemoryBackend(memory_root=tmp_path)
    be.append_buffer("shadow_z", {"category": "x", "lesson": "y"})
    buf = tmp_path / "shadow_z" / "memory_buffer.jsonl"
    assert buf.exists()
    line = json.loads(buf.read_text("utf-8").strip())
    assert line == {"category": "x", "lesson": "y"}


def test_append_buffer_appends_not_overwrites(tmp_path):
    be = FilesystemMemoryBackend(memory_root=tmp_path)
    be.append_buffer("shadow_a", {"lesson": "first"})
    be.append_buffer("shadow_a", {"lesson": "second"})
    entries = be.load_buffer("shadow_a")
    assert len(entries) == 2


def test_load_consolidated_returns_md_text(tmp_path):
    sd = tmp_path / "shadow_b"
    sd.mkdir()
    (sd / "SHADOW_MEMORY.md").write_text(
        "# Heading\n\nBody.", encoding="utf-8",
    )
    be = FilesystemMemoryBackend(memory_root=tmp_path)
    assert "Heading" in be.load_consolidated("shadow_b")
    assert "Body." in be.load_consolidated("shadow_b")


def test_load_consolidated_returns_empty_for_missing(tmp_path):
    be = FilesystemMemoryBackend(memory_root=tmp_path)
    assert be.load_consolidated("shadow_missing") == ""


def test_save_consolidated_overwrites(tmp_path):
    be = FilesystemMemoryBackend(memory_root=tmp_path)
    be.save_consolidated("shadow_c", "v1")
    be.save_consolidated("shadow_c", "v2")
    text = (tmp_path / "shadow_c" / "SHADOW_MEMORY.md").read_text("utf-8")
    assert text == "v2"


def test_get_backend_returns_filesystem_by_default(monkeypatch, tmp_path):
    """The env-var dispatcher returns FilesystemMemoryBackend when
    SYSTEMU_MEMORY_BACKEND is unset or 'filesystem'."""
    from systemu.runtime.memory_backends import get_backend

    monkeypatch.delenv("SYSTEMU_MEMORY_BACKEND", raising=False)
    monkeypatch.setenv("SYSTEMU_VAULT_DIR", str(tmp_path))
    config = None  # backend dispatch may accept None until config is plumbed
    be = get_backend(config)
    from systemu.runtime.memory_backends.filesystem import FilesystemMemoryBackend
    assert isinstance(be, FilesystemMemoryBackend)


def test_get_backend_respects_env_var(monkeypatch, tmp_path):
    from systemu.runtime.memory_backends import get_backend
    monkeypatch.setenv("SYSTEMU_MEMORY_BACKEND", "filesystem")
    monkeypatch.setenv("SYSTEMU_VAULT_DIR", str(tmp_path))
    be = get_backend(None)
    from systemu.runtime.memory_backends.filesystem import FilesystemMemoryBackend
    assert isinstance(be, FilesystemMemoryBackend)
