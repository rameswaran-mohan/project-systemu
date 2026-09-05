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
    # THE ONE MINT (systemu.runtime.vault_root), not a second reading of
    # SYSTEMU_VAULT_DIR. The old default was the RELATIVE string
    # "systemu/vault", so a shadow's memory buffer was re-derived against
    # whatever cwd the reading process had; a resumed shadow that landed in a
    # different directory read an EMPTY buffer and silently lost its lessons.
    #
    # Deferred import, matching this module's existing convention of keeping
    # even `os`/`Path` function-local: a package `__init__` that imports a
    # sibling runtime module at module scope is the classic import-cycle
    # hazard, and this factory is on the shadow prompt-build path.
    #
    # The `refused` bit is not consulted here -- this is a consumer of the
    # root, and the caller (`ShadowRuntime._build_memory_context_for_prompt`)
    # wraps this call in a bare `except`, so a raise here would be SWALLOWED.
    # DEC-32: a raise with a catching frame between it and the decision is no
    # fence. The refusal is adjudicated at the boot boundary instead.
    from systemu.runtime.vault_root import resolve_vault_root
    return FilesystemMemoryBackend(
        memory_root=Path(resolve_vault_root().root) / "memory"
    )
