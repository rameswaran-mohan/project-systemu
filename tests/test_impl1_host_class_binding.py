"""The host class becomes real: derived from the tool spec, bound by the
standing-approval signature, shown on the card, and read by `tools diff`.

THE RULING
    A tool's host class is a canonical string derived deterministically from its
    spec, in ONE place (``command_approvals.tool_host_class``):

      * ``none``        -- it declares no network effect;
      * ``a.com,b.com`` -- it declares a network effect AND hosts (the sorted,
                           unique, lower-cased hostnames under ``hosts`` /
                           ``allowed_hosts`` -- no tool field for hosts existed);
      * ``undeclared``  -- it declares a network effect but no hosts.

    It is part of the standing-approval signature, so changing the hosts a tool
    may reach re-gates it. ``none`` is written into the signature as the empty
    string every signature carried before host classes existed -- which IS the
    upgrade ruling: an old approval stays valid exactly for a tool whose derived
    class is ``none``. A networked tool approved before host checks existed is
    re-asked ONCE, on a card that says so and names the hosts -- never silently
    re-approved, never silently denied.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from systemu.approval.exceptions import PendingOperatorDecision
from systemu.core.models import Tool, ToolType
from systemu.runtime import capability_diff as cd
from systemu.runtime.command_approvals import (CommandApprovalStore,
                                               parts_reproduce_signature,
                                               signature_from_parts,
                                               tool_host_class, tool_signature)

BODY = b"def run():\n    return {'success': True}\n"


# --------------------------------------------------------------------------- #
# Derivation table
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("spec,expected", [
    ({"effect_tags": []}, "none"),
    ({"effect_tags": ["local_write", "local_read"]}, "none"),
    ({"effect_tags": ["local_write"], "hosts": ["api.example.com"]}, "none"),
    ({"effect_tags": ["net_read"]}, "undeclared"),
    ({"effect_tags": ["net_read"], "hosts": []}, "undeclared"),
    ({"effect_tags": ["send_message"]}, "undeclared"),
    ({"effect_tags": ["browser_actuate"]}, "undeclared"),
    ({"effect_tags": ["net_read"], "hosts": ["B.Example.com", "a.example.com",
                                             "a.example.com."]},
     "a.example.com,b.example.com"),
    ({"effect_tags": ["net_mutate"], "allowed_hosts": ["api.example.com"]},
     "api.example.com"),
    ({"effect_tags": ["net_mutate"], "hosts": ["one.example.com"],
      "allowed_hosts": ["two.example.com"]}, "one.example.com,two.example.com"),
    ({"effect_tags": ["net_read"], "hosts": ["https://api.example.com:8443/v1"]},
     "api.example.com"),
    ({"effect_tags": ["net_read"], "hosts": [3, None, "", "bad,host"]}, "undeclared"),
    ({"effect_tags": ["net_read"], "hosts": "api.example.com"}, "api.example.com"),
])
def test_the_derivation_table(spec, expected):
    assert tool_host_class(spec) == expected
    assert tool_host_class(SimpleNamespace(**spec)) == expected   # attribute form


def test_the_derivation_reads_the_vault_tool_model():
    tool = Tool(id="t", name="fetch_page", description="d",
                tool_type=ToolType.PYTHON_FUNCTION, effect_tags=["net_read"])
    assert tool_host_class(tool) == "undeclared"
    tool2 = Tool(id="t", name="tidy", description="d",
                 tool_type=ToolType.PYTHON_FUNCTION, effect_tags=["local_write"])
    assert tool_host_class(tool2) == "none"


# --------------------------------------------------------------------------- #
# The signature binds it
# --------------------------------------------------------------------------- #

def _parts(**kw):
    base = {"name": "fetch_page", "tool_id": "t", "body_hash": "a" * 40,
            "effect_tags": ["net_read"], "host_class": "api.example.com"}
    base.update(kw)
    return base


def test_changing_the_hosts_changes_the_signature():
    """Mutation: drop the host class from the signature and this goes red."""
    one = signature_from_parts(_parts(host_class="api.example.com"))
    two = signature_from_parts(_parts(host_class="other.example.com"))
    undeclared = signature_from_parts(_parts(host_class="undeclared"))
    assert len({one, two, undeclared}) == 3


def test_none_is_written_as_the_pre_host_check_signature():
    """`none` signs exactly like an approval minted before host classes existed --
    the upgrade ruling, by construction."""
    p = _parts(effect_tags=["local_write"], host_class="none")
    assert signature_from_parts(p) == tool_signature("fetch_page", "a" * 40,
                                                     {"local_write"}, host_class="")
    assert parts_reproduce_signature(signature_from_parts(p), p)


# --------------------------------------------------------------------------- #
# The live gate
# --------------------------------------------------------------------------- #

class _Tool(SimpleNamespace):
    """A spec object carrying `hosts` -- the vault Tool model has no such field."""


def _gate(tmp_path, monkeypatch, tool, store):
    captured = {}

    class _FakeInbox:
        def __init__(self, vault):
            pass

        def enqueue(self, descriptor, *, gate_type, context_extras=None, **kw):
            captured["descriptor"] = descriptor
            captured["extras"] = context_extras or {}
            return "dec_hostclass"

    monkeypatch.setattr("systemu.interface.command.inbox.InboxQueue", _FakeInbox)
    from systemu.runtime.tool_sandbox import ToolSandbox
    sb = ToolSandbox(str(tmp_path / "vault"), vault=object(), command_approvals=store)
    try:
        asyncio.run(sb.execute_tool(tool.implementation_path, {}, tool=tool))
        captured["ran"] = True
    except PendingOperatorDecision:
        captured["ran"] = False
    return captured


def _net_tool(tmp_path, *, hosts=None, tags=("net_mutate",), name="post_update"):
    (tmp_path / "vault").mkdir(exist_ok=True)
    (tmp_path / "impls").mkdir(exist_ok=True)
    (tmp_path / "impls" / f"{name}.py").write_bytes(BODY)
    kw = dict(id=f"tool_{name}", name=name, description="d",
              tool_type="python_function", implementation_path=f"impls/{name}.py",
              effect_tags=list(tags), version=1, forged_by_systemu=False,
              dependencies=[])
    if hosts is not None:
        kw["hosts"] = list(hosts)
    return _Tool(**kw)


def _sig(name, tags, host_class):
    return signature_from_parts({"name": name, "tool_id": f"tool_{name}",
                                 "body_hash": hashlib.sha1(BODY).hexdigest(),
                                 "effect_tags": list(tags), "host_class": host_class})


def test_the_gate_binds_the_derived_host_class(tmp_path, monkeypatch):
    store = CommandApprovalStore(tmp_path / "ca.json")
    tool = _net_tool(tmp_path, hosts=["api.example.com"])
    cap = _gate(tmp_path, monkeypatch, tool, store)
    assert cap["ran"] is False
    parts = cap["extras"]["tool_parts"]
    assert parts["host_class"] == "api.example.com"
    assert cap["extras"]["tool_signature"] == _sig("post_update", ["net_mutate"],
                                                   "api.example.com")
    assert "hosts: api.example.com" in cap["descriptor"].inspect


def test_changing_a_tools_hosts_re_gates_it(tmp_path, monkeypatch):
    """Approved for one host; the spec now names another -> the gate asks again."""
    store = CommandApprovalStore(tmp_path / "ca.json")
    store.approve(_sig("post_update", ["net_mutate"], "api.example.com"))
    assert _gate(tmp_path, monkeypatch, _net_tool(tmp_path, hosts=["api.example.com"]),
                 store)["ran"] is True
    assert _gate(tmp_path, monkeypatch, _net_tool(tmp_path, hosts=["evil.example.com"]),
                 store)["ran"] is False


def test_a_legacy_approval_stays_valid_for_a_none_class_tool(tmp_path, monkeypatch):
    # R1 (ruled 2026-10-19): ``local_delete`` is irreversible, and NO standing
    # approval of any kind covers an irreversible call -- so it can no longer be
    # this test's example of a legacy allow. Re-pointed to ``clipboard_write``: an
    # approval-band, non-network, NON-irreversible class. Both properties are
    # asserted here, so the example can never silently become irreversible again.
    # (GATE-6, authorised for exactly this test.)
    from systemu.runtime.action_governance import (ActionContext, Verdict,
                                                   evaluate_action,
                                                   irreversible_effects)
    assert irreversible_effects(["clipboard_write"]) == ()
    assert evaluate_action(ActionContext(tool="tidy",
                                         effect_tags={"clipboard_write"}))[0] \
        is Verdict.REQUIRE_APPROVAL

    store = CommandApprovalStore(tmp_path / "ca.json")
    tool = _net_tool(tmp_path, tags=("clipboard_write",), name="tidy")
    # Non-vacuous: with no approval this tool DOES gate (clipboard_write is an
    # approval-band effect), so the run below is the legacy allow at work.
    assert _gate(tmp_path, monkeypatch, tool, store)["ran"] is False
    legacy = tool_signature("tidy", hashlib.sha1(BODY).hexdigest(), {"clipboard_write"},
                            host_class="")
    store.approve(legacy)
    assert _gate(tmp_path, monkeypatch, tool, store)["ran"] is True


def test_a_legacy_approval_for_a_networked_tool_re_asks_once_and_says_why(
        tmp_path, monkeypatch):
    """Never silently re-approved: the old approval bound no host, so the gate
    asks -- on a card that says why and names the hosts. Never silently denied:
    the card is an ordinary approval card and the old allow is left in place."""
    store = CommandApprovalStore(tmp_path / "ca.json")
    legacy = tool_signature("post_update", hashlib.sha1(BODY).hexdigest(),
                            {"net_mutate"}, host_class="")
    store.approve(legacy)
    cap = _gate(tmp_path, monkeypatch, _net_tool(tmp_path, hosts=["api.example.com"]),
                store)
    assert cap["ran"] is False
    card = cap["descriptor"]
    assert ("approved before host checks existed; confirm the hosts it may reach: "
            "api.example.com") in card.inspect
    assert "Always allow" in card.options
    assert store.is_approved(legacy) is True

    # The operator confirms: the NEW signature is approved, and it is asked no more.
    store.approve(cap["extras"]["tool_signature"])
    again = _gate(tmp_path, monkeypatch, _net_tool(tmp_path, hosts=["api.example.com"]),
                  store)
    assert again["ran"] is True


def test_a_networked_tool_with_no_legacy_approval_gets_no_legacy_line(tmp_path,
                                                                     monkeypatch):
    store = CommandApprovalStore(tmp_path / "ca.json")
    cap = _gate(tmp_path, monkeypatch, _net_tool(tmp_path), store)
    assert "approved before host checks existed" not in cap["descriptor"].inspect
    assert "hosts: undeclared" in cap["descriptor"].inspect


def test_the_card_claims_the_host_class_binding_now_that_it_is_true(tmp_path,
                                                                   monkeypatch):
    store = CommandApprovalStore(tmp_path / "ca.json")
    cap = _gate(tmp_path, monkeypatch, _net_tool(tmp_path, hosts=["api.example.com"]),
                store)
    what = cap["descriptor"].what_approve_does
    assert "tool body + effect set + host class" in what
    what.encode("ascii")


# --------------------------------------------------------------------------- #
# The bulk first-gate review binds it too
# --------------------------------------------------------------------------- #

def test_the_bulk_review_signature_includes_the_host_class(tmp_path):
    from systemu.runtime.first_gate_review import collect_backfilled_entries
    vroot = tmp_path / "vault"
    (vroot / "tools").mkdir(parents=True)
    (tmp_path / "impls").mkdir()
    (tmp_path / "impls" / "fetch.py").write_bytes(BODY)
    body = {"id": "t1", "name": "fetch_page", "description": "d",
            "tool_type": "python_function", "effect_tags": ["net_read"],
            "implementation_path": "impls/fetch.py", "version": 1}
    (vroot / "tools" / "index.json").write_text(json.dumps([{"id": "t1"}]),
                                                encoding="utf-8")
    (vroot / "tools" / "tool_t1.json").write_text(json.dumps(body), encoding="utf-8")
    (entry,) = collect_backfilled_entries(vroot)
    assert entry.host_class == "undeclared"
    assert entry.signature == signature_from_parts({
        "name": "fetch_page", "tool_id": "t1", "body_hash": entry.body_hash,
        "effect_tags": ["net_read"], "host_class": "undeclared"})


# --------------------------------------------------------------------------- #
# The diff's host row now reads real values
# --------------------------------------------------------------------------- #

class _Vault:
    def __init__(self, root, tools, decisions):
        self.root, self._tools, self._decisions = root, tools, decisions

    def load_index(self, entity):
        if entity == "tools":
            return [{"id": t.id} for t in self._tools]
        if entity == "decisions":
            return [{"id": d, "dedup_key": k} for d, k, _ in self._decisions]
        return []

    def get_tool(self, tid):
        return next(t for t in self._tools if t.id == tid)

    def get_decision(self, did):
        return next(SimpleNamespace(context=c) for d, _k, c in self._decisions if d == did)


def _diff(tmp_path, monkeypatch, tool, *, approved_host, legacy=False):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir(exist_ok=True)
    h = hashlib.sha1(BODY).hexdigest()
    tags = list(tool.effect_tags)
    parts = {"name": tool.name, "tool_id": tool.id, "body_hash": h,
             "effect_tags": tags, "host_class": approved_host}
    sig = (tool_signature(tool.name, h, set(tags), host_class="") if legacy
           else signature_from_parts(parts))
    (tmp_path / "data" / "command_approvals.json").write_text(json.dumps({
        "approved": {sig: {"command": "", "approved_at": "2026-10-01"}}}),
        encoding="utf-8")
    ctx = {"tool_signature": sig, "tool_parts": parts if not legacy else
           {**parts, "host_class": ""}}
    vault = _Vault(tmp_path / "vault", [tool], [("d1", f"tool:{sig}", ctx)])
    return cd.build_report_from_disk(vault=vault, data_dir=Path("data"),
                                     installed_version=lambda d: None)


def test_the_diff_host_row_matches_a_real_class(tmp_path, monkeypatch):
    rep = _diff(tmp_path, monkeypatch, _net_tool(tmp_path, hosts=["api.example.com"]),
                approved_host="api.example.com")
    (td,) = rep.tools
    host = next(r for r in td.rows if r.kind == "host")
    assert host.status == cd.MATCH and host.current == "api.example.com"
    assert td.verdict == "as approved" and rep.exit_code == 0


def test_the_diff_host_row_names_changed_hosts(tmp_path, monkeypatch):
    rep = _diff(tmp_path, monkeypatch, _net_tool(tmp_path, hosts=["evil.example.com"]),
                approved_host="api.example.com")
    (td,) = rep.tools
    host = next(r for r in td.rows if r.kind == "host")
    assert host.status == cd.DRIFTED
    assert host.approved == "api.example.com" and host.current == "evil.example.com"
    assert td.verdict == "host class drifted"


def test_the_diff_says_a_legacy_networked_approval_bound_no_hosts(tmp_path, monkeypatch):
    rep = _diff(tmp_path, monkeypatch, _net_tool(tmp_path, hosts=["api.example.com"]),
                approved_host="", legacy=True)
    (td,) = rep.tools
    host = next(r for r in td.rows if r.kind == "host")
    assert host.status == cd.UNKNOWN
    assert "before host checks existed" in host.detail


def test_the_diff_reads_a_legacy_local_approval_as_none_and_as_approved(tmp_path,
                                                                        monkeypatch):
    rep = _diff(tmp_path, monkeypatch,
                _net_tool(tmp_path, tags=("local_delete",), name="tidy"),
                approved_host="", legacy=True)
    (td,) = rep.tools
    host = next(r for r in td.rows if r.kind == "host")
    assert host.status == cd.MATCH and host.approved == "none"
    assert td.as_approved is True
