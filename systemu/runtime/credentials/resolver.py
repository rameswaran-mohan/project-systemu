"""v0.8.18 — resolve declared credentials: keyring -> env -> missing."""
from __future__ import annotations

import logging
import os
from typing import List, Optional, Tuple

from systemu.runtime.credentials.store import CredentialStore

logger = logging.getLogger(__name__)


class CredentialResolver:
    def __init__(self, store: Optional[CredentialStore] = None):
        self._store = store or CredentialStore()

    def resolve(self, req) -> Tuple[Optional[str], Optional[str]]:
        if getattr(req, "auth_type", "api_key") == "none":
            return ("", "none")
        v = self._store.get(req.key)
        if v:
            return (v, "keyring")
        v = os.environ.get(req.key)
        if v:
            return (v, "env")
        return (None, None)

    def missing(self, reqs) -> List:
        return [r for r in (reqs or []) if self.resolve(r)[0] is None]

    def promote_to_env(self, reqs) -> dict:
        """Make resolved secrets visible to tools via os.environ (the .env model).
        Only promotes keyring/file values; env ones are already present. Never logs values.

        R-4: this is a WRITE to process configuration under a key the TOOL
        declared (for a forged tool, the model). A reserved key -- any name the
        runtime or the interpreter reads as configuration -- is REFUSED here, at
        the writer, even when the requirement never went through validation (a
        row loaded from disk, a model_construct): it is logged by name (never by
        value) and not set."""
        from systemu.core.reserved_env import reserved_reason
        promoted = {}
        for r in (reqs or []):
            key = getattr(r, "key", None)
            why = reserved_reason(key)
            if why:
                logger.warning(
                    "[Credentials] refused to promote %r into the process environment: "
                    "it is reserved (%s)", key, why)
                continue
            v, src = self.resolve(r)
            if v and src not in (None, "env", "none"):
                os.environ[r.key] = v
                promoted[r.key] = src
        return promoted
