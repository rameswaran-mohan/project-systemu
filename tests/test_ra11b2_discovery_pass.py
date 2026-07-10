# tests/test_ra11b2_discovery_pass.py
"""R-A11b-2 Task 2 — the deterministic discovery pass + DEPLOYED+enabled catalog."""
from systemu.runtime.discovery_pass import (
    DiscoveryResult, deployed_enabled_catalog, discovery_pass, REUSE_FLOOR,
)


class _FakeVault:
    """Minimal vault exposing list_tools(status=...) over header dicts."""
    def __init__(self, headers):
        self._headers = headers

    def list_tools(self, status=None):
        from systemu.core.models import ToolStatus
        if status is None:
            return list(self._headers)
        return [h for h in self._headers if h.get("status") == status.value]


def _hdr(name, desc, *, status="deployed", enabled=True, tid=None):
    return {"id": tid or f"tool_{name}", "name": name, "description": desc,
            "parameter_names": [], "status": status, "enabled": enabled}


def test_catalog_keeps_only_deployed_and_enabled():
    v = _FakeVault([
        _hdr("fetch_weather", "weather"),
        _hdr("half_forged", "x", status="forged"),          # not DEPLOYED
        _hdr("disabled_tool", "x", enabled=False),           # Gate-3 disabled
    ])
    cat = deployed_enabled_catalog(v)
    names = {c["name"] for c in cat}
    assert names == {"fetch_weather"}
    # each entry carries the three ranker fields
    assert set(cat[0]) >= {"id", "name", "description", "parameter_names"}


def test_exact_name_match_qualifies_for_reuse():
    cat = [{"id": "tool_fw", "name": "fetch_weather",
            "description": "get weather", "parameter_names": []}]
    res = discovery_pass("fetch_weather", "I need to fetch the weather", cat)
    assert isinstance(res, DiscoveryResult)
    assert res.reuse_tool_id == "tool_fw"
    assert res.reuse_tool_name == "fetch_weather"
    assert res.searched == 1


def test_weak_match_below_floor_does_not_qualify():
    # A single unrelated-name tool; the query shares no name token and only a
    # faint description token → score below the floor → no reuse.
    cat = [{"id": "tool_se", "name": "send_email",
            "description": "send an email to a recipient", "parameter_names": []}]
    res = discovery_pass("compress_pdf", "compress a pdf file", cat)
    assert res.reuse_tool_id is None
    assert res.best_score < REUSE_FLOOR


def test_empty_catalog_is_a_clean_miss():
    res = discovery_pass("anything", "rationale", [])
    assert res.reuse_tool_id is None
    assert res.searched == 0
    assert res.best_score == 0.0


def test_never_raises_on_broken_vault():
    class Boom:
        def list_tools(self, status=None):
            raise RuntimeError("boom")
    assert deployed_enabled_catalog(Boom()) == []
