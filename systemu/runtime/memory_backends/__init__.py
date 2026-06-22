"""v0.7-g: pluggable memory backends."""
from .base import BaseMemoryBackend  # noqa: F401


def get_backend(config) -> BaseMemoryBackend:
    """Resolve the backend instance from env (or config).  Default filesystem.

    Env var: ``SYSTEMU_MEMORY_BACKEND`` in {"filesystem", "mem0"}.
    """
    import os
    from pathlib import Path

    name = (os.environ.get("SYSTEMU_MEMORY_BACKEND") or "filesystem").lower()
    if name == "mem0":
        from .mem0 import Mem0MemoryBackend
        return Mem0MemoryBackend()
    from .filesystem import FilesystemMemoryBackend
    return FilesystemMemoryBackend(
        memory_root=Path(os.environ.get("SYSTEMU_VAULT_DIR", "systemu/vault")) / "memory"
    )
