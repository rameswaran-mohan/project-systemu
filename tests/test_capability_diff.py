"""`systemu tools diff` -- the approved state of every standing tool approval
compared with what is on disk now.

WHAT THIS PINS
    A standing tool approval is stored as ONE opaque sha1 (``tool_signature`` over
    name + implementation body hash + declared effect tags + host class). That is
    the right shape for the gate -- any change re-gates -- but it is unreadable: an
    operator cannot ask "is the thing I approved still the thing that runs?" and
    get an answer that names WHAT changed. This report answers it per row:

      body         approved body hash vs the hash of the file on disk now
      effect tags  approved declared tags vs the tags declared now
      host class   approved vs current, or UNKNOWN when either side cannot be
                   determined -- an unknown is NEVER reported as a match
      dependency   each approved dependency spec vs the installed distribution

    and a one-line verdict that reads ``as approved`` only when every row matches.

WHERE THE APPROVED PARTS COME FROM
    Not from the approval store -- it holds only the sha1. They come from the
    decision ledger: the gate stamps ``tool_parts`` into the card's decision
    context at the moment it computes the signature, and the bulk first-gate card
    carries each entry's body hash. A ledger row is believed ONLY when its parts
    recompute to the approved signature -- the ledger is a vault file, and the
    verifier may not consult an input it does not check.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from systemu.core.models import Tool, ToolType
from systemu.runtime import capability_diff as cd
from systemu.runtime.command_approvals import tool_signature


# --------------------------------------------------------------------------- #
# Fixtures: a tmp vault with real implementation files, fake stores on tmp
# --------------------------------------------------------------------------- #

class _FakeVault:
    """The four vault calls the report makes, over in-memory tools + decisions."""

    def __init__(self, root: Path):
        self.root = root
        self.tools = {}
        self.decisions = []          # (id, dedup_key, context)

    def load_index(self, entity):
        if entity == "tools":
            return [{"id": t.id, "name": t.name} for t in self.tools.values()]
        if entity == "decisions":
            return [{"id": d[0], "dedup_key": d[1]} for d in self.decisions]
        return []

    def get_tool(self, tool_id):
        return self.tools[tool_id]

    def get_decision(self, decision_id):
        for did, dedup, ctx in self.decisions:
            if did == decision_id:
                return SimpleNamespace(id=did, dedup_key=dedup, context=ctx)
        raise KeyError(decision_id)


@pytest.fixture
def world(tmp_path, monkeypatch):
    """cwd = tmp (the stores live at ./data), vault root = tmp/vault, impl files
    under tmp/impls -- so every path in the report is inside this test's tmp."""
    monkeypatch.chdir(tmp_path)
    vroot = tmp_path / "vault"
    vroot.mkdir()
    (tmp_path / "impls").mkdir()
    (tmp_path / "data").mkdir()
    return SimpleNamespace(tmp=tmp_path, vault=_FakeVault(vroot))


BODY_V1 = "def run():\n    return {'success': True}\n"
BODY_V2 = "def run():\n    return {'success': True, 'changed': 1}\n"


def _add_tool(world, name, *, body=BODY_V1, effect_tags=("local_write",),
              dependencies=(), version=1):
    impl = world.tmp / "impls" / f"{name}.py"
    impl.write_bytes(body.encode("utf-8"))
    tool = Tool(id=f"tool_{name}", name=name, description=f"test {name}",
                tool_type=ToolType.PYTHON_FUNCTION,
                implementation_path=f"impls/{name}.py",
                effect_tags=list(effect_tags), dependencies=list(dependencies),
                version=version)
    world.vault.tools[tool.id] = tool
    return tool, impl


def _sha1(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _approve(world, sig, *, command=""):
    p = world.tmp / "data" / "command_approvals.json"
    data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {
        "version": 1, "approved": {}, "pending": {}}
    data["approved"][sig] = {"approved_at": "2026-10-01T00:00:00+00:00",
                             "approved_by": "operator", "command": command, "cwd": ""}
    p.write_text(json.dumps(data), encoding="utf-8")


def _ledger_card(world, *, sig, name, tool_id, body_hash, effect_tags, host_class=""):
    """A per-tool gate card exactly as the gate stamps it (tool_parts)."""
    ctx = {"kind": "gate", "gate_type": "tool", "tool_signature": sig,
           "tool_name": name, "tool_id": tool_id,
           "tool_parts": {"name": name, "tool_id": tool_id, "body_hash": body_hash,
                          "effect_tags": sorted(effect_tags),
                          "host_class": host_class}}
    world.vault.decisions.append((f"dec_{len(world.vault.decisions)}",
                                  f"tool:{sig}", ctx))


def _approve_v1(world, name, *, effect_tags=("local_write",)):
    """Approve ``name`` at BODY_V1 with a ledger row carrying its parts."""
    h = _sha1(BODY_V1)
    sig = tool_signature(name, h, set(effect_tags), host_class="")
    _approve(world, sig)
    _ledger_card(world, sig=sig, name=name, tool_id=f"tool_{name}", body_hash=h,
                 effect_tags=effect_tags)
    return sig, h


def _report(world, **kw):
    return cd.build_report_from_disk(vault=world.vault, data_dir=Path("data"),
                                     installed_version=kw.pop("installed_version",
                                                              lambda d: None),
                                     **kw)


def _row(td, kind):
    rows = [r for r in td.rows if r.kind == kind]
    assert len(rows) == 1, td.rows
    return rows[0]


def _only_tool(report, name):
    hits = [t for t in report.tools if t.name == name]
    assert len(hits) == 1, [t.name for t in report.tools]
    return hits[0]


# --------------------------------------------------------------------------- #
# Body row
# --------------------------------------------------------------------------- #

def test_a_drifted_body_is_named_with_both_hashes(world):
    """THE CORE CASE. Approved at v1, the file on disk is now v2: the report says
    the BODY drifted, shows the approved hash and the current one, and the verdict
    names the body. Mutation: build the approved side from the current hash too
    and this goes red (it would read `match`)."""
    _add_tool(world, "write_report")
    _sig, h1 = _approve_v1(world, "write_report")
    (world.tmp / "impls" / "write_report.py").write_bytes(BODY_V2.encode("utf-8"))

    td = _only_tool(_report(world), "write_report")
    body = _row(td, "body")
    assert body.status == cd.DRIFTED
    assert h1[:12] in body.approved
    assert _sha1(BODY_V2)[:12] in body.current
    assert body.approved != body.current
    assert td.verdict == "body drifted"
    assert td.as_approved is False


def test_an_unchanged_body_matches(world):
    _add_tool(world, "write_report")
    _approve_v1(world, "write_report")
    td = _only_tool(_report(world), "write_report")
    assert _row(td, "body").status == cd.MATCH
    assert _row(td, "effects").status == cd.MATCH


def test_a_deleted_implementation_file_reads_missing(world):
    _add_tool(world, "write_report")
    _approve_v1(world, "write_report")
    (world.tmp / "impls" / "write_report.py").unlink()
    td = _only_tool(_report(world), "write_report")
    assert _row(td, "body").status == cd.MISSING
    assert td.verdict == "body missing"


def test_a_tool_gone_from_the_vault_reads_missing(world):
    """The approval names a tool the vault no longer has."""
    _approve_v1(world, "vanished_tool")
    td = _only_tool(_report(world), "vanished_tool")
    assert _row(td, "body").status == cd.MISSING
    assert _row(td, "effects").status == cd.MISSING
    assert td.as_approved is False


# --------------------------------------------------------------------------- #
# Effect-tag row
# --------------------------------------------------------------------------- #

def test_changed_effect_tags_read_drifted(world):
    tool, _ = _add_tool(world, "write_report", effect_tags=("local_write",))
    _approve_v1(world, "write_report", effect_tags=("local_write",))
    tool.effect_tags = ["local_write", "local_delete"]
    td = _only_tool(_report(world), "write_report")
    assert _row(td, "body").status == cd.MATCH
    eff = _row(td, "effects")
    assert eff.status == cd.DRIFTED
    assert "local_delete" in eff.current and "local_delete" not in eff.approved
    assert td.verdict == "effect tags drifted"


# --------------------------------------------------------------------------- #
# Host-class row -- an unknown is never a match
# --------------------------------------------------------------------------- #

def test_an_undeterminable_host_class_reads_unknown_never_match(world):
    """Body and tags both match, but this NETWORKED tool was approved before host
    checks existed: the approval bound no host class. That is NOT a match -- the
    report says `unknown` and the verdict refuses `as approved`."""
    _add_tool(world, "write_report", effect_tags=("net_read",))
    _approve_v1(world, "write_report", effect_tags=("net_read",))
    td = _only_tool(_report(world), "write_report")
    host = _row(td, "host")
    assert host.status == cd.UNKNOWN
    assert td.as_approved is False
    assert td.verdict == "host class unknown"


@pytest.mark.parametrize("approved,current,expected", [
    (None, None, "unknown"),
    ("none", None, "unknown"),
    (None, "none", "unknown"),
    ("", "", "unknown"),            # an empty class is "not bound", not a value
    ("none", "none", "match"),
    ("api.example.com", "api.example.com", "match"),
    ("api.example.com", "other.example.com", "drifted"),
])
def test_the_host_row_table(approved, current, expected):
    row = cd.host_row(approved, current)
    assert row.status == expected
    assert row.label == "host class"


def test_as_approved_only_when_every_row_matches():
    """The pure builder: every row match -> `as approved`; one unknown -> named."""
    parts = cd.ApprovedParts(name="t", tool_id="tool_t", body_hash="a" * 40,
                             effect_tags=("local_read",), host_class="none")
    cur = cd.CurrentState(name="t", tool_id="tool_t", body_hash="a" * 40,
                          body_missing=False, effect_tags=("local_read",),
                          host_class="none", dependencies=())
    td = cd.diff_tool(signature="s" * 40, approved=parts, current=cur, dep_rows=())
    assert td.as_approved is True and td.verdict == "as approved"

    cur_unknown = cd.CurrentState(**{**cur.__dict__, "host_class": None})
    td2 = cd.diff_tool(signature="s" * 40, approved=parts, current=cur_unknown,
                       dep_rows=())
    assert td2.as_approved is False and td2.verdict == "host class unknown"


# --------------------------------------------------------------------------- #
# Dependency rows
# --------------------------------------------------------------------------- #

def _approve_dep(world, spec, *, tool_id):
    p = world.tmp / "data" / "dep_approvals.json"
    data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {
        "version": 1, "approved": {}, "pending": {}}
    data["approved"][spec] = {"approved_at": "2026-10-01T00:00:00+00:00",
                              "approved_by": "operator", "first_seen_tool": None,
                              "first_seen_tool_id": tool_id}
    p.write_text(json.dumps(data), encoding="utf-8")


def test_dependency_rows_match_different_and_not_installed(world):
    _add_tool(world, "make_doc", dependencies=("python-docx", "requests==1.0",
                                               "ghostpkg"))
    _approve_v1(world, "make_doc")
    for spec in ("python-docx", "requests==1.0", "ghostpkg"):
        _approve_dep(world, spec, tool_id="tool_make_doc")
    installed = {"python-docx": "1.1.0", "requests": "2.31.0"}

    td = _only_tool(_report(world, installed_version=installed.get), "make_doc")
    deps = {r.label: r for r in td.rows if r.kind == "dep"}
    assert deps["dependency python-docx"].status == cd.MATCH
    assert deps["dependency python-docx"].current == "1.1.0"
    assert deps["dependency requests==1.0"].status == cd.DIFFERENT
    assert deps["dependency requests==1.0"].current == "2.31.0"
    assert deps["dependency ghostpkg"].status == cd.NOT_INSTALLED


def test_a_dependency_approved_for_another_tool_is_not_this_tools_row(world):
    _add_tool(world, "make_doc")
    _approve_v1(world, "make_doc")
    _approve_dep(world, "numpy", tool_id="tool_someone_else")
    td = _only_tool(_report(world), "make_doc")
    assert not [r for r in td.rows if r.kind == "dep"]


# --------------------------------------------------------------------------- #
# Attribution -- what the ledger is allowed to say
# --------------------------------------------------------------------------- #

def test_a_legacy_approval_matching_the_current_signature_is_attributed(world):
    """No ledger row at all (an approval recorded before parts were stamped), but
    the approval IS the signature of the tool as it is now: body, tags AND host
    class are bound by that signature, so all three match. (For a local tool the
    pre-host-check signature is the `none`-class signature -- the upgrade ruling.)"""
    _add_tool(world, "write_report")
    sig = tool_signature("write_report", _sha1(BODY_V1), {"local_write"}, host_class="")
    _approve(world, sig)
    td = _only_tool(_report(world), "write_report")
    assert _row(td, "body").status == cd.MATCH
    assert _row(td, "effects").status == cd.MATCH
    assert _row(td, "host").status == cd.MATCH
    assert _row(td, "host").approved == "none"
    assert td.verdict == "as approved"


def test_an_unattributable_approval_is_reported_and_fails_the_exit(world):
    """A standing tool approval no ledger row explains and no current tool matches
    cannot be diffed -- it is listed, and it keeps the report from exiting 0."""
    _add_tool(world, "write_report")
    _approve(world, "f" * 40)
    rep = _report(world)
    assert rep.unattributed == ("f" * 40,)
    assert rep.exit_code == 1


def test_a_command_approval_is_not_a_tool_approval(world):
    """Shell-command and MCP allows share the store; they carry the command text
    and are not this report's business."""
    _approve(world, "c" * 40, command="rm -rf build")
    _approve(world, "d" * 40, command="mcp:server:tool")
    rep = _report(world)
    assert rep.tools == () and rep.unattributed == ()
    assert rep.exit_code == 0


def test_ledger_parts_that_do_not_reproduce_the_signature_are_not_believed(world):
    """A tampered ledger row (or one for a different signature) is ignored: the
    parts must recompute to the approved sha1 or they say nothing."""
    _add_tool(world, "write_report")
    real_sig = tool_signature("write_report", _sha1(BODY_V2), {"local_write"},
                              host_class="")
    _approve(world, real_sig)
    # The lie: claims the approval was for the CURRENT (v1) body.
    _ledger_card(world, sig=real_sig, name="write_report", tool_id="tool_write_report",
                 body_hash=_sha1(BODY_V1), effect_tags=("local_write",))
    td = _only_tool(_report(world), "write_report")
    assert _row(td, "body").status != cd.MATCH
    assert td.as_approved is False


def test_an_approval_attributed_by_name_only_reads_unknown_rows(world):
    """A legacy ledger row with a tool name but no parts: the tool is named, the
    approval no longer matches it, and the report does not pretend to know which
    part changed."""
    _add_tool(world, "write_report")
    old_sig = tool_signature("write_report", _sha1("old body"), {"local_write"},
                             host_class="")
    _approve(world, old_sig)
    world.vault.decisions.append((
        "dec_legacy", f"tool:{old_sig}",
        {"kind": "gate", "gate_type": "tool", "tool_signature": old_sig,
         "tool_name": "write_report", "tool_id": "tool_write_report"}))
    td = _only_tool(_report(world), "write_report")
    assert _row(td, "body").status == cd.UNKNOWN
    assert _row(td, "effects").status == cd.UNKNOWN
    assert td.as_approved is False


# --------------------------------------------------------------------------- #
# The gate stamps the parts; the bulk card carries them
# --------------------------------------------------------------------------- #

def test_the_live_gate_stamps_parts_that_reproduce_its_signature(tmp_path, monkeypatch):
    """Drive the REAL ToolSandbox gate: the card's context carries `tool_parts`,
    and those parts recompute to the `tool_signature` it stamped beside them."""
    from systemu.approval.exceptions import PendingOperatorDecision
    from systemu.runtime.command_approvals import (CommandApprovalStore,
                                                   parts_reproduce_signature)
    from systemu.runtime.tool_sandbox import ToolSandbox

    captured = {}

    class _FakeInbox:
        def __init__(self, vault):
            pass

        def enqueue(self, descriptor, *, gate_type, context_extras=None, **kw):
            captured["extras"] = context_extras or {}
            return "dec_capdiff"

    monkeypatch.setattr("systemu.interface.command.inbox.InboxQueue", _FakeInbox)
    vroot = tmp_path / "vault"
    vroot.mkdir()
    (tmp_path / "impls").mkdir()
    impl = tmp_path / "impls" / "send_status_email.py"
    impl.write_bytes(BODY_V1.encode("utf-8"))
    store = CommandApprovalStore(tmp_path / "command_approvals.json")
    sb = ToolSandbox(str(vroot), vault=object(), command_approvals=store)
    tool = Tool(id="tool_send_status_email", name="send_status_email",
                description="d", tool_type=ToolType.PYTHON_FUNCTION,
                implementation_path="impls/send_status_email.py",
                effect_tags=["send_message"], version=1)
    with pytest.raises(PendingOperatorDecision):
        asyncio.run(sb.execute_tool(tool.implementation_path, {}, tool=tool))

    extras = captured["extras"]
    parts = extras.get("tool_parts")
    assert type(parts) is dict, extras
    assert parts["body_hash"] == _sha1(BODY_V1)
    assert parts["name"] == "send_status_email"
    assert parts_reproduce_signature(extras["tool_signature"], parts) is True


def test_bulk_review_entries_carry_the_body_hash_the_ledger_needs(tmp_path):
    from systemu.runtime.command_approvals import parts_reproduce_signature
    from systemu.runtime.first_gate_review import collect_backfilled_entries

    vroot = tmp_path / "vault"
    (vroot / "tools").mkdir(parents=True)
    (tmp_path / "impls").mkdir()
    (tmp_path / "impls" / "tidy.py").write_bytes(BODY_V1.encode("utf-8"))
    body = {"id": "t1", "name": "tidy", "effect_tags": ["local_write"],
            "implementation_path": "impls/tidy.py", "version": 1}
    (vroot / "tools" / "index.json").write_text(json.dumps([{"id": "t1"}]),
                                                encoding="utf-8")
    (vroot / "tools" / "tool_t1.json").write_text(json.dumps(body), encoding="utf-8")

    (entry,) = collect_backfilled_entries(vroot)
    assert entry.body_hash == _sha1(BODY_V1)
    parts = {"name": entry.name, "tool_id": entry.tool_id, "body_hash": entry.body_hash,
             "effect_tags": list(entry.effect_tags), "host_class": entry.host_class}
    assert parts_reproduce_signature(entry.signature, parts) is True


def test_a_bulk_card_in_the_ledger_attributes_its_approvals(world):
    _add_tool(world, "tidy")
    h = _sha1(BODY_V1)
    sig = tool_signature("tidy", h, {"local_write"}, host_class="")
    _approve(world, sig)
    world.vault.decisions.append((
        "dec_bulk", "tool_bulk:0.10.33",
        {"kind": "gate", "gate_type": "tool_bulk",
         "bulk_entries": [{"tool_id": "tool_tidy", "name": "tidy", "signature": sig,
                           "effect_tags": ["local_write"], "verdict": "require_approval",
                           "reason": "", "body_hash": h, "host_class": ""}]}))
    (world.tmp / "impls" / "tidy.py").write_bytes(BODY_V2.encode("utf-8"))
    td = _only_tool(_report(world), "tidy")
    assert _row(td, "body").status == cd.DRIFTED


# --------------------------------------------------------------------------- #
# Strict reading of the approval store (exit 2)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("raw", ["{not json", "[]", '{"approved": []}'])
def test_an_unreadable_store_raises_naming_the_file(world, raw):
    (world.tmp / "data" / "command_approvals.json").write_text(raw, encoding="utf-8")
    with pytest.raises(cd.ApprovalStoreUnreadable) as exc:
        _report(world)
    assert "command_approvals.json" in str(exc.value)


def test_an_unreadable_dependency_store_raises_naming_the_file(world):
    (world.tmp / "data" / "dep_approvals.json").write_text("{nope", encoding="utf-8")
    with pytest.raises(cd.ApprovalStoreUnreadable) as exc:
        _report(world)
    assert "dep_approvals.json" in str(exc.value)


def test_a_malformed_entry_is_unattributable_not_a_crash(world):
    """A hand-edited entry that is not an object is an approval this report
    cannot read -- it is counted, and it fails the exit; it does not crash."""
    (world.tmp / "data" / "command_approvals.json").write_text(
        '{"approved": {"x": 1}}', encoding="utf-8")
    rep = _report(world)
    assert rep.unattributed == ("x",)
    assert rep.exit_code == 1


def test_a_missing_store_is_an_empty_one(world):
    rep = _report(world)
    assert rep.tools == () and rep.unattributed == () and rep.exit_code == 0


# --------------------------------------------------------------------------- #
# The CLI
# --------------------------------------------------------------------------- #

def _cli(world, *args):
    from click.testing import CliRunner

    from sharing_on import cli as sharing_on_cli
    runner = CliRunner()
    return runner.invoke(sharing_on_cli.cli, ["tools", "diff", *args],
                         obj={"config": object(), "vault": world.vault})


def _ascii(text):
    text.encode("ascii")
    return text


def test_cli_exit_0_and_a_plain_line_when_nothing_is_approved(world):
    res = _cli(world)
    assert res.exit_code == 0, res.output
    assert "No standing tool approvals" in _ascii(res.output)


def test_cli_exit_1_on_drift_and_names_it(world):
    _add_tool(world, "write_report")
    _approve_v1(world, "write_report")
    (world.tmp / "impls" / "write_report.py").write_bytes(BODY_V2.encode("utf-8"))
    res = _cli(world)
    assert res.exit_code == 1, res.output
    out = _ascii(res.output)
    assert "write_report" in out
    assert "verdict: body drifted" in out


def test_cli_exit_2_when_the_store_cannot_be_read_and_names_it(world):
    (world.tmp / "data" / "command_approvals.json").write_text("{nope",
                                                               encoding="utf-8")
    res = _cli(world)
    assert res.exit_code == 2, res.output
    out = _ascii(res.output)
    assert "command_approvals.json" in out
    assert "cannot read" in out.lower()


def test_cli_json_is_parseable_and_carries_the_exit_code(world):
    _add_tool(world, "write_report", effect_tags=("net_read",))
    _approve_v1(world, "write_report", effect_tags=("net_read",))
    res = _cli(world, "--json")
    assert res.exit_code == 1
    data = json.loads(res.output)
    assert data["exit_code"] == 1
    (tool,) = data["tools"]
    assert tool["name"] == "write_report"
    assert tool["verdict"] == "host class unknown"
    assert {r["kind"] for r in tool["rows"]} >= {"body", "effects", "host"}


def test_cli_named_tool_filters_and_an_unknown_name_is_not_success(world):
    _add_tool(world, "write_report")
    _add_tool(world, "other_tool")
    _approve_v1(world, "write_report")
    _approve_v1(world, "other_tool")
    res = _cli(world, "write_report", "--json")
    data = json.loads(res.output)
    assert [t["name"] for t in data["tools"]] == ["write_report"]

    res2 = _cli(world, "no_such_tool")
    assert res2.exit_code == 1
    assert "no_such_tool" in _ascii(res2.output)


def test_cli_exit_0_when_every_tool_is_as_approved(world):
    _add_tool(world, "write_report")
    _approve_v1(world, "write_report")
    res = _cli(world)
    assert res.exit_code == 0, res.output
    assert "verdict: as approved" in _ascii(res.output)


def test_tools_diff_help_is_ascii_and_states_the_exit_codes(world):
    res = _cli(world, "--help")
    assert res.exit_code == 0
    out = _ascii(res.output)
    for needle in ("0", "1", "2", "--json"):
        assert needle in out
