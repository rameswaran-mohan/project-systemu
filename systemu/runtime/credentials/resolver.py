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
        Only promotes keyring/file values; env ones are already present. Never logs values."""
        promoted = {}
        for r in (reqs or []):
            v, src = self.resolve(r)
            if v and src not in (None, "env", "none"):
                os.environ[r.key] = v
                promoted[r.key] = src
        return promoted
