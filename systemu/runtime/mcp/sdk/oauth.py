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


class VaultOAuthStorage:
    """The SDK ``mcp.client.auth.TokenStorage`` protocol over a VaultTokenStore.

    Verified identical on mcp 2.3.0 and mcp 1.30.0: async ``get_tokens`` /
    ``set_tokens`` (``mcp.shared.auth.OAuthToken``) and ``get_client_info`` /
    ``set_client_info`` (``OAuthClientInformationFull``). Both live in ONE
    encrypted-at-rest file as ``{"tokens": {...}, "client_info": {...}}``;
    nothing here is ever logged."""

    def __init__(self, store: VaultTokenStore):
        self._store = store

    def _section(self, key: str):
        raw = self._store.load().get(key)
        return raw if isinstance(raw, dict) and raw else None

    def _write(self, key: str, model) -> None:
        data = self._store.load()
        data[key] = model.model_dump(mode="json", exclude_none=True)
        self._store.save(data)

    async def get_tokens(self):
        from mcp.shared.auth import OAuthToken
        raw = self._section("tokens")
        return OAuthToken.model_validate(raw) if raw else None

    async def set_tokens(self, tokens) -> None:
        self._write("tokens", tokens)

    async def get_client_info(self):
        from mcp.shared.auth import OAuthClientInformationFull
        raw = self._section("client_info")
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    async def set_client_info(self, client_info) -> None:
        self._write("client_info", client_info)


def build_sdk_oauth_provider(server_id: str, *, vault, authorization_url: str = "",
                             redirect_uri: str = "http://127.0.0.1/callback",
                             client_name: str = "systemu"):
    """FALLBACK path: construct the official SDK OAuthClientProvider backed by the
    vault token store. Lazy SDK import so a missing `mcp` never breaks module
    load. ``authorization_url`` is the protected MCP server's URL (the SDK's
    ``server_url``; it discovers the authorization server from there).

    D-MCP2: verified on mcp 2.3.0 and mcp 1.30.0 -- ``OAuthClientProvider(
    server_url, client_metadata, storage, ...)`` lives in ``mcp.client.auth`` on
    both lines; ``client_metadata`` is REQUIRED (the pre-port call omitted it
    and raised TypeError on either line). The provider is an ``Auth`` of the
    HTTP library the installed line uses (httpx on 1.x, httpx2 on 2.x), which is
    what ``compat.open_streamable_http(auth=...)`` hands to that line's client.
    Not wired into the live connect path (OAuth URL-mode stays the preferred
    flow; see acquire_oauth).
    """
    from mcp.client.auth import OAuthClientProvider
    from mcp.shared.auth import OAuthClientMetadata

    metadata = OAuthClientMetadata(
        redirect_uris=[redirect_uri],
        client_name=client_name,
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
    )
    return OAuthClientProvider(
        server_url=authorization_url,
        client_metadata=metadata,
        storage=VaultOAuthStorage(VaultTokenStore(vault, server_id)),
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
