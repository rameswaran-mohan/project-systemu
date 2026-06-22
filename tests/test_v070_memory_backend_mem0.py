"""v0.7-g: Mem0MemoryBackend — tests use monkeypatch instead of `patch().return_value`
chaining so they're CI-stable across pytest / mock library versions."""
from unittest.mock import MagicMock

import pytest


def _install_fake_mem0(monkeypatch, fake_mem):
    """Replace `systemu.runtime.memory_backends.mem0.Memory` with a factory
    that returns ``fake_mem`` when called.  Also ensures `sys.modules["mem0"]`
    has SOMETHING so any conditional `from mem0 import ...` paths don't hit
    a real ImportError."""
    monkeypatch.setitem(__import__("sys").modules, "mem0", MagicMock())
    monkeypatch.setattr(
        "systemu.runtime.memory_backends.mem0.Memory",
        lambda *a, **k: fake_mem,
    )


def test_mem0_load_buffer_serializes_via_mem0_get_all(monkeypatch):
    fake_mem = MagicMock()
    fake_mem.get_all.return_value = [
        {"memory": "lesson 1", "metadata": {"category": "tool_quirks"}},
    ]
    _install_fake_mem0(monkeypatch, fake_mem)

    from systemu.runtime.memory_backends.mem0 import Mem0MemoryBackend
    be = Mem0MemoryBackend()
    out = be.load_buffer("shadow_x")

    assert len(out) == 1
    assert out[0]["lesson"] == "lesson 1"
    assert out[0]["category"] == "tool_quirks"
    fake_mem.get_all.assert_called_once()
    kwargs = fake_mem.get_all.call_args.kwargs
    assert kwargs["user_id"] == "systemu_shadow_x"


def test_mem0_append_buffer_calls_mem0_add(monkeypatch):
    fake_mem = MagicMock()
    _install_fake_mem0(monkeypatch, fake_mem)

    from systemu.runtime.memory_backends.mem0 import Mem0MemoryBackend
    be = Mem0MemoryBackend()

    # Sanity check the wiring before asserting on the method call —
    # this surfaces CI-only patching glitches with a clearer message.
    assert be._mem0 is fake_mem, (
        f"Memory() should return the fake_mem we installed; got {be._mem0!r}"
    )

    be.append_buffer("shadow_y", {"category": "x", "lesson": "y"})

    fake_mem.add.assert_called_once()
    args, kwargs = fake_mem.add.call_args
    assert args[0] == "y"  # lesson text passed positionally
    assert kwargs["user_id"] == "systemu_shadow_y"
    assert kwargs["metadata"]["category"] == "x"


def test_mem0_load_consolidated_returns_concatenated_lessons(monkeypatch):
    fake_mem = MagicMock()
    fake_mem.get_all.return_value = [
        {"memory": "alpha", "metadata": {}},
        {"memory": "beta", "metadata": {}},
    ]
    _install_fake_mem0(monkeypatch, fake_mem)

    from systemu.runtime.memory_backends.mem0 import Mem0MemoryBackend
    be = Mem0MemoryBackend()

    assert be._mem0 is fake_mem, (
        f"Memory() should return the fake_mem we installed; got {be._mem0!r}"
    )

    text = be.load_consolidated("shadow_z")
    assert "alpha" in text and "beta" in text
    assert text.startswith("- ")  # bullet list format


def test_mem0_save_consolidated_is_noop(monkeypatch):
    """Mem0 manages its own consolidation; save_consolidated is intentionally
    a no-op so the consolidator's existing call doesn't error."""
    fake_mem = MagicMock()
    _install_fake_mem0(monkeypatch, fake_mem)

    from systemu.runtime.memory_backends.mem0 import Mem0MemoryBackend
    be = Mem0MemoryBackend()
    # No exception, no operation
    be.save_consolidated("shadow_w", "some markdown")


def test_get_backend_returns_mem0_when_env_set(monkeypatch):
    fake_mem = MagicMock()
    _install_fake_mem0(monkeypatch, fake_mem)
    monkeypatch.setenv("SYSTEMU_MEMORY_BACKEND", "mem0")

    from systemu.runtime.memory_backends import get_backend
    from systemu.runtime.memory_backends.mem0 import Mem0MemoryBackend
    be = get_backend(None)
    assert isinstance(be, Mem0MemoryBackend)
