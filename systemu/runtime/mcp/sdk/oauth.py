"""OAuth for protected MCP servers.

Order of preference (spec §3.1 / §4.4 / §6):
  1. URL-MODE ELICITATION — the operator is shown the full authorize URL, consents
     out-of-band, and the secret NEVER transits the client, the LLM, or systemu
     logs. (acquire_oauth, Task 6.)
  2. FALLBACK — the SDK ``OAuthClientProvider`` driving the standard flow, with
     tokens persisted in a 0600 vault store readable ONLY by the manager process.

Tokens live at ``<vault>/connections/mcp_oauth/<server_id>.json`` and are never
passed to a subprocess, the LLM, or a log line.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)


class VaultTokenStore:
    """0600 JSON token store for one MCP server. Lives in the manager process;
    its contents are NEVER logged or handed to a subprocess."""

    def __init__(self, vault, server_id: str):
        root = getattr(vault, "root", None) or "data/systemu/vault"
        safe_id = "".join(c for c in str(server_id) if c.isalnum() or c in ("_", "-")) or "server"
        self.path: Path = Path(root) / "connections" / "mcp_oauth" / f"{safe_id}.json"

    def load(self) -> Dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            from systemu.runtime.credentials.at_rest import unprotect_json
            # S5: transparently decrypts a DPAPI envelope AND reads a legacy
            # plaintext token file (migrate-on-read — upgraded on next save).
            data = unprotect_json(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            logger.debug("[MCP-OAuth] token store unreadable at %s", self.path, exc_info=True)
            return {}

    def save(self, tokens: Dict[str, Any]) -> None:
        from systemu.runtime.credentials.at_rest import protect_json
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        # S5: encrypt at rest via DPAPI on Windows (0o600 is a Windows no-op).
        # The chmod stays as the POSIX-secondary control. Write then tighten
        # perms before the atomic replace, so the final file is never briefly
        # world-readable.
        tmp.write_text(protect_json(tokens), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            logger.debug("[MCP-OAuth] chmod 0600 unsupported on this platform")
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def clear(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            logger.debug("[MCP-OAuth] could not clear token store at %s", self.path, exc_info=True)


def build_sdk_oauth_provider(server_id: str, *, vault, authorization_url: str = ""):
    """FALLBACK path: construct the official SDK OAuthClientProvider backed by the
    0600 VaultTokenStore. Lazy SDK import so a missing `mcp` never breaks module
    load.

    VERIFY against the installed package: the class lives in `mcp.client.auth`
    (guess ``OAuthClientProvider``) and takes a token-storage object exposing
    ``get_tokens()`` / ``set_tokens()`` (guess — the real protocol may be
    ``TokenStorage`` with different method names). Adapt the adapter below to the
    actual contract. Isolated here so a name change is a one-file edit.
    """
    from mcp.client.auth import OAuthClientProvider  # VERIFY path + name

    store = VaultTokenStore(vault, server_id)

    class _Adapter:  # VERIFY method names against mcp's TokenStorage protocol
        async def get_tokens(self):
            return store.load() or None

        async def set_tokens(self, tokens):
            store.save(dict(tokens) if tokens else {})

    return OAuthClientProvider(  # VERIFY constructor kwargs
        server_url=authorization_url,
        storage=_Adapter(),
    )


def acquire_oauth(server_id, authorize_url, *, vault, elicitation_fn=None) -> Dict[str, Any]:
    """Acquire OAuth for a protected server, URL-mode FIRST.

    URL-mode: hand the full authorize URL to the P1 URL-mode elicitation surface
    so the operator consents out-of-band. The secret NEVER enters the form, the
    LLM context, or systemu logs. Returns a non-blocking pending marker — the run
    parks and the reconciler (jobs.py) retries when the operator completes.

    ``elicitation_fn`` is injectable for tests. In production it resolves to the
    P1 URL-mode request builder (VERIFY the function name in
    systemu/runtime/elicitation.py — the spec names it as the shared structured-
    input surface; default below resolves it lazily).
    """
    if elicitation_fn is None:
        # v0.9.38 (review LOW): OAuth URL-mode is NOT wired into the live connect
        # path yet (manager hard-codes oauth_required=False), so there is no real
        # default surface to resolve. The previous default imported a symbol that
        # does not exist (`request_url_mode`) and would raise a confusing
        # ImportError if ever invoked. Fail loudly + actionably instead. When
        # OAuth is activated, point this at the real card builder
        # (interface.harness_review.surface_oauth_url_card) and validate the URL
        # scheme is https before it becomes a clickable operator card.
        raise NotImplementedError(  # pragma: no cover - prod wiring not yet live
            "MCP OAuth URL-mode is not wired live; pass an explicit elicitation_fn"
        )

    # NOTE: the URL itself may contain a client_id / PKCE challenge but NOT a
    # bearer secret; we surface it to the operator only — never to the LLM/log.
    elicitation_fn(url=authorize_url, server_id=server_id)
    return {"status": "oauth_pending", "mode": "url", "server_id": server_id}
