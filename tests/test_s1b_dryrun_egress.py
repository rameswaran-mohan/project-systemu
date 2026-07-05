"""S1b / IMPL-3 — forge-time dry-run must never egress.

Prior to this fix, ``dry_run_tool``'s ONLY pre-execution guard was the
name/param destructive-call heuristic (``ToolSandbox.is_destructive_call``),
which is blind to network verbs. A freshly-forged, unreviewed tool tagged
(or classifiable) as net-effectful could therefore phone home LIVE during
what is supposed to be a safe dry-run, because S2 (the OS-level egress
jail) doesn't exist yet to catch it lower down.

These tests assert the net-egress guard added to ``dry_run_tool`` (and
mirrored in ``replay_against_history``): a net-tagged tool (via declared
``effect_tags`` OR the ``classify_source`` fallback for a freshly-forged
tool with no tags stamped yet) is skipped WITHOUT ever calling its body,
while a purely local tool still dry-runs normally.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from systemu.core.models import Tool, ToolStatus, ToolType


@pytest.fixture
def vault(tmp_path):
    from systemu.vault.vault import Vault
    for sub in ("scrolls", "activities", "shadow_army", "skills", "tools",
                "evolutions"):
        (tmp_path / sub).mkdir()
        (tmp_path / sub / "index.json").write_text("[]")
    return Vault(str(tmp_path))


def _make_tool(name="t", effect_tags=None):
    return Tool(
        id=f"tool_{name}",
        name=name,
        description="for tests",
        tool_type=ToolType.PYTHON_FUNCTION,
        status=ToolStatus.FORGED,
        enabled=False,
        effect_tags=list(effect_tags or []),
        parameters_schema={"x": {"type": "string", "default": "hello"}},
    )


def _config_for(vault_dir: Path):
    config = MagicMock()
    config.openrouter_api_key = "test"
    config.tier3_model = "test"
    config.vault_dir = str(vault_dir)
    return config


class TestNetTaggedDryRunNeverEgresses:
    def test_net_tagged_tool_via_source_scan_does_not_egress(self, tmp_path, vault, monkeypatch):
        """A freshly-forged tool with NO stamped effect_tags, whose name does
        NOT trip the destructive-call heuristic (``upload_report``), and whose
        body calls ``urllib.request.urlopen`` must be skipped BEFORE its body
        ever runs — never allowed to egress under the "dry run" label."""
        from systemu.pipelines.tool_dry_run import dry_run_tool

        impl_dir = tmp_path / "vault" / "tools" / "implementations"
        impl_dir.mkdir(parents=True, exist_ok=True)
        impl_file = impl_dir / "upload_report.py"
        impl_file.write_text(
            "import urllib.request\n"
            "\n"
            "def run(x):\n"
            "    urllib.request.urlopen('https://example.com/report', data=b'x')\n"
            "    return {'success': True}\n"
        )

        t = _make_tool(name="upload_report")  # no effect_tags stamped
        t.implementation_path = "vault/tools/implementations/upload_report.py"
        assert not __import__(
            "systemu.runtime.tool_sandbox", fromlist=["ToolSandbox"]
        ).ToolSandbox.is_destructive_call(t.name, {"x": "hello"})

        config = _config_for(tmp_path / "vault")

        egress_calls = []
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda *a, **kw: egress_calls.append((a, kw)),
        )

        result = dry_run_tool(t, vault=vault, config=config)

        assert egress_calls == [], "dry-run must NEVER execute a net-tagged tool's body"
        assert result.status == "skipped"
        assert result.success is False

    def test_net_tagged_tool_via_declared_effect_tags_does_not_egress(self, tmp_path, vault, monkeypatch):
        """Same as above, but the tool already has ``effect_tags`` stamped
        (e.g. by the vault_migrator backfill) — the guard must honour the
        declared tags directly, without needing the source-scan fallback."""
        from systemu.pipelines.tool_dry_run import dry_run_tool

        impl_dir = tmp_path / "vault" / "tools" / "implementations"
        impl_dir.mkdir(parents=True, exist_ok=True)
        impl_file = impl_dir / "upload_report2.py"
        impl_file.write_text(
            "import urllib.request\n"
            "\n"
            "def run(x):\n"
            "    urllib.request.urlopen('https://example.com/report')\n"
            "    return {'success': True}\n"
        )

        t = _make_tool(name="upload_report2", effect_tags=["net_read"])
        t.implementation_path = "vault/tools/implementations/upload_report2.py"

        config = _config_for(tmp_path / "vault")

        egress_calls = []
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda *a, **kw: egress_calls.append((a, kw)),
        )

        result = dry_run_tool(t, vault=vault, config=config)

        assert egress_calls == []
        assert result.status == "skipped"

    def test_replay_against_history_also_skips_net_tagged_tool(self, tmp_path, vault, monkeypatch):
        from systemu.pipelines.tool_dry_run import replay_against_history

        impl_dir = tmp_path / "vault" / "tools" / "implementations"
        impl_dir.mkdir(parents=True, exist_ok=True)
        impl_file = impl_dir / "upload_report3.py"
        impl_file.write_text(
            "import urllib.request\n"
            "\n"
            "def run(x):\n"
            "    urllib.request.urlopen('https://example.com/report')\n"
            "    return {'success': True}\n"
        )

        t = _make_tool(name="upload_report3")
        t.implementation_path = "vault/tools/implementations/upload_report3.py"
        t.last_successful_params = [{"x": "1"}]

        config = _config_for(tmp_path / "vault")

        egress_calls = []
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda *a, **kw: egress_calls.append((a, kw)),
        )

        result = replay_against_history(t, vault=vault, config=config)

        assert egress_calls == []
        assert result.status == "skipped"


class TestFailClosedOnUndeterminableTags:
    """The guard must FAIL CLOSED: it proceeds (executes the body) ONLY when it
    can affirmatively prove the tool is non-egress (non-empty local-only tags).
    Anything it CANNOT prove safe — shell egress, an aliased net import the AST
    scan misses, unparseable source, or empty tags — must be SKIPPED, not run.

    Each case below would have been ``passed`` (and would have egressed) under
    the old fail-OPEN code that only skipped on a positive net-tag match.
    """

    def test_shell_egress_with_empty_tags_is_skipped(self, tmp_path, vault, monkeypatch):
        """Empty declared tags + a body that shells out (`os.system('curl …')`)
        classifies as ``shell_exec`` — egress-capable, so it must SKIP. The old
        fail-open code returned None (shell_exec ∉ net set) and executed it."""
        from systemu.pipelines.tool_dry_run import dry_run_tool

        impl_dir = tmp_path / "vault" / "tools" / "implementations"
        impl_dir.mkdir(parents=True, exist_ok=True)
        impl_file = impl_dir / "shell_curl.py"
        impl_file.write_text(
            "import os\n"
            "\n"
            "def run(x):\n"
            "    os.system('curl https://example.com/exfil')\n"
            "    return {'success': True}\n"
        )

        t = _make_tool(name="run_report")  # no effect_tags, name not destructive
        t.implementation_path = "vault/tools/implementations/shell_curl.py"

        config = _config_for(tmp_path / "vault")

        executed = []
        monkeypatch.setattr(
            "systemu.pipelines.tool_dry_run._execute",
            lambda tool, params, vault, config: executed.append(True) or {"success": True},
        )

        result = dry_run_tool(t, vault=vault, config=config)

        assert executed == [], "shell-egress tool must not reach _execute"
        assert result.status == "skipped"

    def test_aliased_net_import_with_empty_tags_is_skipped(self, tmp_path, vault, monkeypatch):
        """An aliased net import (`import requests as r; r.get(...)`) is a known
        blind spot of the AST classifier → it yields NO tags → effective tag set
        is empty/undeterminable → fail-closed SKIP. The old code executed it."""
        from systemu.pipelines.tool_dry_run import dry_run_tool
        from systemu.runtime.effect_tags import classify_source

        body = (
            "import requests as r\n"
            "\n"
            "def run(x):\n"
            "    return r.get('https://example.com')\n"
        )
        # Precondition: this really is a classifier blind spot (no tags), so the
        # test proves the EMPTY/undeterminable branch, not a positive net match.
        assert classify_source(body) == set()

        impl_dir = tmp_path / "vault" / "tools" / "implementations"
        impl_dir.mkdir(parents=True, exist_ok=True)
        impl_file = impl_dir / "aliased_net.py"
        impl_file.write_text(body)

        t = _make_tool(name="fetch_data")  # no effect_tags
        t.implementation_path = "vault/tools/implementations/aliased_net.py"

        config = _config_for(tmp_path / "vault")

        executed = []
        monkeypatch.setattr(
            "systemu.pipelines.tool_dry_run._execute",
            lambda tool, params, vault, config: executed.append(True) or {"success": True},
        )

        result = dry_run_tool(t, vault=vault, config=config)

        assert executed == [], "aliased-net tool must not reach _execute"
        assert result.status == "skipped"

    def test_completely_empty_tags_no_source_is_skipped(self, tmp_path, vault, monkeypatch):
        """No declared tags AND no resolvable source (missing impl file) →
        undeterminable → fail-closed SKIP."""
        from systemu.pipelines.tool_dry_run import dry_run_tool

        # impl dir exists but the referenced file does not
        (tmp_path / "vault" / "tools" / "implementations").mkdir(parents=True, exist_ok=True)

        t = _make_tool(name="mystery_tool")
        t.implementation_path = "vault/tools/implementations/does_not_exist.py"

        config = _config_for(tmp_path / "vault")

        executed = []
        monkeypatch.setattr(
            "systemu.pipelines.tool_dry_run._execute",
            lambda tool, params, vault, config: executed.append(True) or {"success": True},
        )

        result = dry_run_tool(t, vault=vault, config=config)

        assert executed == []
        assert result.status == "skipped"


class TestLocalToolStillDryRuns:
    def test_local_write_tool_dry_runs_normally(self, tmp_path, vault, monkeypatch):
        """A purely local tool (writes a temp file, no network) must NOT be
        caught by the net-egress guard — classify_source tags it local_write,
        which is outside the net set, so dry_run_tool proceeds to _execute."""
        from systemu.pipelines.tool_dry_run import dry_run_tool

        impl_dir = tmp_path / "vault" / "tools" / "implementations"
        impl_dir.mkdir(parents=True, exist_ok=True)
        impl_file = impl_dir / "write_local_note.py"
        impl_file.write_text(
            "def run(x):\n"
            "    with open('note.txt', 'w') as f:\n"
            "        f.write(x)\n"
            "    return {'success': True}\n"
        )

        t = _make_tool(name="write_local_note")  # no effect_tags stamped
        t.implementation_path = "vault/tools/implementations/write_local_note.py"

        config = _config_for(tmp_path / "vault")

        egress_calls = []
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda *a, **kw: egress_calls.append((a, kw)),
        )
        # Bypass the real sandbox/subprocess machinery — we only care that the
        # net-egress guard does NOT short-circuit before _execute is reached.
        monkeypatch.setattr(
            "systemu.pipelines.tool_dry_run._execute",
            lambda tool, params, vault, config: {"success": True, "parsed": {}},
        )

        result = dry_run_tool(t, vault=vault, config=config)

        assert egress_calls == []          # never touched network (it has none)
        assert result.status == "passed"
        assert result.success is True

    def test_net_egress_skip_reason_helper_directly(self, tmp_path):
        """Unit-level check of the guard's classification: net verbs skip,
        local verbs don't — independent of the rest of the dry-run pipeline."""
        from systemu.pipelines.tool_dry_run import _net_egress_skip_reason

        impl_dir = tmp_path / "vault" / "tools" / "implementations"
        impl_dir.mkdir(parents=True, exist_ok=True)

        net_file = impl_dir / "net_tool.py"
        net_file.write_text(
            "import requests\n"
            "def run():\n"
            "    return requests.get('https://example.com')\n"
        )
        local_file = impl_dir / "local_tool.py"
        local_file.write_text(
            "def run():\n"
            "    with open('x.txt', 'w') as f:\n"
            "        f.write('hi')\n"
        )

        config = _config_for(tmp_path / "vault")

        net_tool = _make_tool(name="net_tool")
        net_tool.implementation_path = "vault/tools/implementations/net_tool.py"
        assert _net_egress_skip_reason(net_tool, config) is not None

        local_tool = _make_tool(name="local_tool")
        local_tool.implementation_path = "vault/tools/implementations/local_tool.py"
        assert _net_egress_skip_reason(local_tool, config) is None
