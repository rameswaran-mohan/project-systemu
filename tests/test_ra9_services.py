"""R-A9 T3: S1 connected services / MCP servers (AC8: N servers -> N rows).

build_services(vault) surveys the MCP connections store into ConnectedService
rows. auth_kind + has_live_token are DERIVED (never persisted) from the transport
spec and the OAuth token-file presence; account is None in v1.
"""
from systemu.runtime.situational_inventory import ConnectedService, build_services


def _make_vault(tmp_path):
    """A real vault, mirroring the helper in test_ra9_creds_profile.py."""
    from systemu.vault.vault import Vault
    for sub in ["scrolls", "activities", "shadow_army", "skills", "tools/implementations",
                "evolutions", "notifications", "executions", "decisions"]:
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    for idx in ["scrolls", "activities", "shadow_army", "skills", "tools", "evolutions", "decisions"]:
        (tmp_path / idx / "index.json").write_text("[]", encoding="utf-8")
    return Vault(str(tmp_path))


def test_two_servers_two_rows(tmp_path):
    # AC8: two attached servers -> two ConnectedService rows.
    from systemu.runtime.mcp import connections
    vault = _make_vault(tmp_path)
    url_a = "https://mcp.example.com/a"
    url_b = "https://mcp.example.com/b"
    connections.add_server(vault, url_a)
    connections.add_server(vault, url_b)

    # Arrange server A to have a live OAuth token FILE (has_live_token = file exists,
    # NOT that the token is valid) via the real VaultTokenStore save path.
    from systemu.runtime.mcp.sdk.oauth import VaultTokenStore
    VaultTokenStore(vault, url_a).save({"access_token": "opaque"})

    rows = build_services(vault)
    assert len(rows) == 2                                   # AC8: 2 servers -> 2 rows
    assert all(isinstance(r, ConnectedService) for r in rows)

    names = {r.name for r in rows}
    assert names == {url_a, url_b}                          # name == server URL (stable identity)

    by_name = {r.name: r for r in rows}
    for r in rows:
        assert isinstance(r.auth_kind, str) and r.auth_kind  # non-empty derived string
        assert isinstance(r.has_live_token, bool)
        assert r.account is None                              # v1: account not persisted

    # Server A has a token file on disk -> live token derived True; B has none.
    assert by_name[url_a].has_live_token is True
    assert by_name[url_b].has_live_token is False


def test_empty_vault_no_servers(tmp_path):
    vault = _make_vault(tmp_path)
    assert build_services(vault) == []                       # nothing attached -> []


def test_defensive_never_raises():
    class _Boom:
        @property
        def root(self):
            raise RuntimeError("vault down")
    # A source that raises must degrade to [] and never propagate.
    assert build_services(_Boom()) == []
