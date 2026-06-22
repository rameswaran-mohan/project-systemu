"""Build deeplink URLs into the dashboard recovery panel."""
from __future__ import annotations
import os
from typing import Literal

ScopeKind = Literal["tool", "shadow", "scroll", "activity"]
_VALID_SCOPES = {"tool", "shadow", "scroll", "activity"}


def dashboard_base_url() -> str:
    return os.environ.get("SYSTEMU_DASHBOARD_URL", "http://localhost:8765")


def recover_url(scope: str, scope_id: str) -> str:
    if scope not in _VALID_SCOPES:
        raise ValueError(f"unknown scope {scope!r}; expected one of {_VALID_SCOPES}")
    return f"{dashboard_base_url()}/recover/{scope}/{scope_id}"
