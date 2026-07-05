"""W10.3 — the keyless golden-task E2E gate.

WHY this file exists: in v0.9.13 every curated vault tool became a silent
no-op — W2.2 ran the module-style implementations (TOOL_META + run(), no
__main__ block) directly as scripts, so they defined their functions and
exited with code 0 and empty stdout, which the sandbox reported as SUCCESS.
The regression shipped through THREE releases because no test executed the
REAL tool implementations end-to-end and asserted on OUTCOMES.

These six golden tasks are that gate. Each one:

  * drives `run_quick_task` with a scripted `llm_json` injectable — no API
    key, no LLM network traffic, fully deterministic (W8.2 seam);
  * registers the REAL implementation files from
    systemu/vault/tools/implementations/ (copied into a temp vault) with
    `forged_by_systemu=True`, so execution goes through the REAL W6
    subprocess runner — the exact path that broke;
  * asserts on OUTCOMES, never on reported status alone: a file on disk
    with the expected bytes, the tool's parsed payload visible in the LLM
    transcript (the anti-no-op assertion — an empty `parsed` can never
    satisfy it), or an honest `failed` status that names the tool.

Network policy: nothing beyond 127.0.0.1 (golden 4 serves a stub page from
a stdlib http.server thread). Total runtime is bounded by a handful of
short-lived Python subprocesses.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from systemu.core.models import Tool, ToolStatus
from systemu.core.utils import generate_id
from systemu.vault.vault import Vault

_REPO_ROOT = Path(__file__).resolve().parents[1]
_REAL_IMPLEMENTATIONS = _REPO_ROOT / "systemu" / "vault" / "tools" / "implementations"

# LLM-facing parameter names for the registered tools (mirrors each tool's
# run() signature / documented kwargs). The scripted LLM doesn't read these,
# but the Tool records should look like the real registry entries.
_PARAMETER_NAMES = {
    "write_csv_file":      ["output_path", "data"],
    "write_text_file":     ["file_path", "content"],
    "file_read":           ["path", "encoding"],
    "parse_json":          ["input", "mode"],
    "fetch_html":          ["url", "headers"],
    "file_list_dir":       ["path", "pattern", "recursive"],
    "write_markdown_file": ["file_path", "content"],
}


# ─── fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    for sub in ["tools/implementations", "elder", "notifications"]:
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    (tmp_path / "tools" / "index.json").write_text("[]", encoding="utf-8")
    return Vault(str(tmp_path))


@pytest.fixture
def cfg(tmp_path: Path) -> SimpleNamespace:
    """Quick-lane config: only output_dir matters (deliverables contract).
    The quick lane pre-creates it, mirroring the sandbox derivation."""
    return SimpleNamespace(output_dir=str(tmp_path / "deliverables"))


@pytest.fixture(autouse=True)
def _clean_dep_cache():
    """The dependency installer caches per-process; reset around each test so
    golden 4's `requests` satisfaction can never mask another test's state."""
    from systemu.runtime import dependency_installer as di
    di.reset_cache_for_tests()
    yield
    di.reset_cache_for_tests()


@pytest.fixture
def stub_http_server():
    """A loopback-only stdlib HTTP server for golden 4 — the web tools get a
    REAL socket round-trip with zero external network."""
    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 — http.server API
            body = _GOLDEN_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # keep pytest output clean
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# ─── helpers ─────────────────────────────────────────────────────────────────

def _load_tool_meta(impl: Path) -> dict:
    """Read TOOL_META from the implementation file itself, so the registered
    record's dependencies/tool_type can never drift from the source of truth.
    (All curated implementations are stdlib-only at module level.)"""
    spec = importlib.util.spec_from_file_location(f"_golden_meta_{impl.stem}", impl)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return dict(getattr(module, "TOOL_META", {}) or {})


def _register_impl(vault: Vault, name: str, impl: Path) -> Tool:
    """Save a full Tool record for an implementation file already inside the
    temp vault. enabled + DEPLOYED so the quick lane's readiness gate admits
    it; forged_by_systemu so the W6 subprocess-isolation path is exercised.

    S1b: the live per-tool action gate treats an untagged tool as UNKNOWN
    effect (dangerous-until-proven) and gates it. In production the forge
    pipeline backfills ``effect_tags`` from ``classify_source`` at forge
    time — mirror that here so these golden-task registrations are tagged
    the same way a real forged tool would be, instead of unrealistically
    shipping with no tags.
    """
    from systemu.runtime.effect_tags import classify_source

    meta = _load_tool_meta(impl)
    body = impl.read_text(encoding="utf-8")
    effect_tags = sorted(t.value for t in classify_source(body))
    tool = Tool(
        id=generate_id("tool"),
        name=name,
        description=f"golden-task registration of {name}",
        tool_type=meta.get("tool_type") or "python_function",  # validator coerces
        status=ToolStatus.DEPLOYED,
        enabled=True,
        implementation_path=str(impl),
        forged_by_systemu=True,
        dependencies=list(meta.get("dependencies") or []),
        parameter_names=_PARAMETER_NAMES.get(name, []),
        effect_tags=effect_tags,
    )
    vault.save_tool(tool)
    return tool


def _register_real_tool(vault: Vault, name: str) -> Tool:
    """Copy the REAL implementation from the repo tool pack into the temp
    vault and register it. This is the load-bearing move: the golden tasks
    must execute the shipped code, not a test stand-in."""
    src = _REAL_IMPLEMENTATIONS / f"{name}.py"
    assert src.is_file(), f"real implementation missing from repo: {src}"
    dst = Path(vault.root) / "tools" / "implementations" / f"{name}.py"
    shutil.copyfile(src, dst)
    return _register_impl(vault, name, dst)


def _register_inline_tool(vault: Vault, name: str, body: str) -> Tool:
    impl = Path(vault.root) / "tools" / "implementations" / f"{name}.py"
    impl.write_text(body, encoding="utf-8")
    return _register_impl(vault, name, impl)


def _pre_approve_tool(tool: Tool, tmp_path: Path):
    """Bless a tool signature the way an operator's "Always allow" would,
    for a tool whose body ``classify_source`` genuinely cannot resolve to a
    known low-risk effect (e.g. no recognizable I/O sink, or a body that
    only returns a literal) — a legitimately-gated UNKNOWN, not a tagging
    gap. Computes the signature EXACTLY the way
    ``ToolSandbox._maybe_gate_tool`` does (name + body sha1 + sorted
    effect_tags + host_class=""), then returns a ``ToolSandbox`` wired to
    the pre-populated store so ``run_quick_task(..., sandbox=...)`` picks
    it up instead of the default ``data/`` store."""
    import hashlib

    from systemu.runtime.command_approvals import CommandApprovalStore, tool_signature
    from systemu.runtime.tool_sandbox import ToolSandbox

    body_hash = hashlib.sha1(Path(tool.implementation_path).read_bytes()).hexdigest()
    sig = tool_signature(tool.name, body_hash, set(tool.effect_tags or []),
                         host_class="")
    store = CommandApprovalStore(tmp_path / "command_approvals.json")
    store.approve(sig, command=tool.name)
    return store


def _fake_llm(script):
    """Return an llm_json callable that replays `script` (list of action
    dicts) and records every user payload it was shown (wave-8 pattern)."""
    calls = {"payloads": [], "i": 0}

    def llm_json(*, system, user, config=None):
        calls["payloads"].append(user)
        action = script[min(calls["i"], len(script) - 1)]
        calls["i"] += 1
        return action

    llm_json.calls = calls
    return llm_json


def _tool_results(payload: str, tool: str):
    """Tool-result transcript entries for `tool` inside one LLM user payload.
    Each entry's "parsed" field is the json.dumps of the REAL parsed stdout —
    with the v0.9.13 no-op regression it would have been "{}" for every call,
    so substring assertions against it are the anti-no-op oracle."""
    history = json.loads(payload).get("history", [])
    return [h for h in history
            if h.get("role") == "tool_result" and h.get("tool") == tool]


# ─── the golden tasks ────────────────────────────────────────────────────────

_GOLDEN_HTML = (
    "<html><head><title>Vendor portal</title></head>"
    "<body><h1>SYSTEMU-GOLDEN-MARKER-9f3b</h1>"
    "<p>Q2 vendor onboarding portal — staging.</p></body></html>"
)

_FAIL_BODY = (
    "TOOL_META = {'name': 'broken_export', 'tool_type': 'file_operation', "
    "'dependencies': []}\n"
    "\n"
    "def run(**kwargs):\n"
    "    return {'success': False, 'data': None, 'error': 'always broken'}\n"
)


class TestGoldenTasks:
    def test_golden_write_csv_file_lands_real_rows_on_disk(self, vault, cfg):
        """Golden 1: an office CSV deliverable. The assertion is the FILE —
        a no-op tool would still let the run report success (the scripted
        ANSWER arrives regardless), but the bytes can only exist if the real
        write_csv_file ran."""
        from systemu.pipelines.quick_task import run_quick_task
        _register_real_tool(vault, "write_csv_file")

        rows = [
            {"vendor": "Acme Supplies", "invoice": "INV-1001", "amount": "1200.50"},
            {"vendor": "Borealis Print", "invoice": "INV-1002", "amount": "318.00"},
            {"vendor": "Cypress Catering", "invoice": "INV-1003", "amount": "942.75"},
        ]
        csv_path = Path(cfg.output_dir) / "vendors.csv"
        llm = _fake_llm([
            {"action": "TOOL_CALL", "tool": "write_csv_file",
             "params": {"output_path": str(csv_path), "data": rows},
             "reasoning": "save the vendor list"},
            {"action": "ANSWER", "answer_md": "Saved the vendor list to vendors.csv."},
        ])

        res = run_quick_task("Save our three vendors as a CSV", cfg, vault,
                             llm_json=llm)

        assert res.status == "success" and res.tool_calls == 1
        assert csv_path.is_file(), "deliverable never reached disk"
        lines = [l for l in csv_path.read_text(encoding="utf-8").splitlines() if l]
        assert lines[0] == "vendor,invoice,amount"
        assert "Acme Supplies,INV-1001,1200.50" in lines
        assert "Borealis Print,INV-1002,318.00" in lines
        assert "Cypress Catering,INV-1003,942.75" in lines
        assert len(lines) == 4
        assert str(csv_path.resolve()) in res.files_produced

    def test_golden_memo_write_then_read_round_trip(self, vault, cfg):
        """Golden 2: write a memo, read it back, answer from it. The
        round-trip is verified at the transcript level: the file_read RESULT
        entry must carry the memo text, which only the real read of the real
        file can produce."""
        from systemu.pipelines.quick_task import run_quick_task
        _register_real_tool(vault, "write_text_file")
        _register_real_tool(vault, "file_read")

        memo = ("Facilities memo: the conference-room projector arrives "
                "Thursday under PO-7741; AV setup is booked for 09:00.")
        memo_path = Path(cfg.output_dir) / "memo.txt"
        llm = _fake_llm([
            {"action": "TOOL_CALL", "tool": "write_text_file",
             "params": {"file_path": str(memo_path), "content": memo},
             "reasoning": "write the memo"},
            {"action": "TOOL_CALL", "tool": "file_read",
             "params": {"path": str(memo_path)},
             "reasoning": "read it back to confirm"},
            {"action": "ANSWER",
             "answer_md": f"The memo on file reads: {memo}"},
        ])

        res = run_quick_task("File the projector memo and read it back",
                             cfg, vault, llm_json=llm)

        assert res.status == "success" and res.tool_calls == 2
        assert memo_path.read_text(encoding="utf-8") == memo
        # The third LLM turn saw the file_read RESULT — the memo content in
        # that entry came off the disk, not from the scripted write params.
        reads = _tool_results(llm.calls["payloads"][2], "file_read")
        assert len(reads) == 1 and reads[0]["success"] is True
        assert "PO-7741" in reads[0]["parsed"]
        # And the answer is grounded in what was actually read back.
        assert "PO-7741" in res.answer_md

    def test_golden_parse_json_extracts_invoice_total(self, vault, cfg):
        """Golden 3: parse an invoice JSON string and surface the total. The
        anti-no-op assertion is on the parse_json RESULT entry the second LLM
        turn received — a silent no-op yields parsed == {} and the total can
        never appear there."""
        from systemu.pipelines.quick_task import run_quick_task
        _register_real_tool(vault, "parse_json")

        invoice = {"invoice_id": "INV-2026-014", "total": 4317.5,
                   "currency": "EUR",
                   "lines": [{"sku": "DESK-04", "qty": 5, "unit": 863.5}]}
        llm = _fake_llm([
            {"action": "TOOL_CALL", "tool": "parse_json",
             "params": {"input": json.dumps(invoice), "mode": "string"},
             "reasoning": "extract the structured invoice"},
            {"action": "ANSWER",
             "answer_md": "Invoice INV-2026-014 totals **4317.5 EUR**."},
        ])

        res = run_quick_task("What is the total on this invoice JSON?",
                             cfg, vault, llm_json=llm)

        assert res.status == "success" and res.tool_calls == 1
        parses = _tool_results(llm.calls["payloads"][1], "parse_json")
        assert len(parses) == 1 and parses[0]["success"] is True
        assert "4317.5" in parses[0]["parsed"], \
            "parsed invoice payload never reached the LLM transcript"
        assert "INV-2026-014" in parses[0]["parsed"]
        assert "4317.5" in res.answer_md

    def test_golden_fetch_html_reads_loopback_stub_page(self, vault, cfg,
                                                        stub_http_server):
        """Golden 4: fetch_html against a 127.0.0.1 stub — a REAL socket
        round-trip through the subprocess runner. fetch_html declares
        dependencies=["requests"] (installed in this repo's environment), so
        the sandbox is built with InstallMode.ALWAYS: the installer's PROMPT
        default fail-closes without an approval store, which would test the
        approval gate instead of the tool."""
        from systemu.pipelines.quick_task import run_quick_task
        from systemu.runtime import dependency_installer as di
        from systemu.runtime.tool_sandbox import ToolSandbox

        tool = _register_real_tool(vault, "fetch_html")
        assert tool.dependencies == ["requests"]   # matches TOOL_META
        assert importlib.util.find_spec("requests") is not None, \
            "precondition: requests must be installed (it is a repo dep)"

        sandbox = ToolSandbox(vault.root, vault=vault, config=cfg,
                              install_mode=di.InstallMode.ALWAYS)
        llm = _fake_llm([
            {"action": "TOOL_CALL", "tool": "fetch_html",
             "params": {"url": stub_http_server},
             "reasoning": "fetch the portal page"},
            {"action": "ANSWER", "answer_md": "Fetched the vendor portal page."},
        ])

        res = run_quick_task("Check the vendor portal staging page", cfg,
                             vault, llm_json=llm, sandbox=sandbox)

        assert res.status == "success" and res.tool_calls == 1
        fetches = _tool_results(llm.calls["payloads"][1], "fetch_html")
        assert len(fetches) == 1 and fetches[0]["success"] is True
        assert "SYSTEMU-GOLDEN-MARKER-9f3b" in fetches[0]["parsed"], \
            "the page served by the local stub never reached the transcript"

    def test_golden_list_dir_then_write_markdown_index(self, vault, cfg,
                                                       tmp_path):
        """Golden 5: list a seeded folder, then write a markdown index of it.
        The listing assertion targets the SECOND turn's payload — at that
        point the only place the seeded filenames can appear is the real
        file_list_dir result (the later write params aren't in history yet)."""
        from systemu.pipelines.quick_task import run_quick_task
        from systemu.runtime.tool_sandbox import ToolSandbox
        list_dir_tool = _register_real_tool(vault, "file_list_dir")
        _register_real_tool(vault, "write_markdown_file")
        # file_list_dir's body walks the filesystem with Path.exists/is_dir/
        # glob — none of which classify_source recognizes as a read sink
        # (it only knows read_text/read_bytes/open()), so it legitimately
        # classifies to {} -> UNKNOWN -> REQUIRE_APPROVAL. That is a
        # correctly-gated tool (dangerous-until-proven), not a tagging gap,
        # so bless it the way an operator's "Always allow" would.
        store = _pre_approve_tool(list_dir_tool, tmp_path)
        sandbox = ToolSandbox(vault.root, vault=vault, config=cfg,
                              command_approvals=store)

        inbox = tmp_path / "inbox"
        inbox.mkdir()
        seeded = ["minutes_2026-06-01.txt", "q1_budget.xlsx", "vendor_contract.pdf"]
        for name in seeded:
            (inbox / name).write_text("seed", encoding="utf-8")

        index_md = Path(cfg.output_dir) / "inbox_index.md"
        index_body = "# Inbox index\n\n" + "\n".join(f"- {n}" for n in seeded) + "\n"
        llm = _fake_llm([
            {"action": "TOOL_CALL", "tool": "file_list_dir",
             "params": {"path": str(inbox), "pattern": "*"},
             "reasoning": "see what is in the inbox"},
            {"action": "TOOL_CALL", "tool": "write_markdown_file",
             "params": {"file_path": str(index_md), "content": index_body},
             "reasoning": "write the index"},
            {"action": "ANSWER", "answer_md": "Indexed 3 inbox files."},
        ])

        res = run_quick_task("Index the inbox folder as markdown", cfg, vault,
                             llm_json=llm, sandbox=sandbox)

        assert res.status == "success" and res.tool_calls == 2
        listings = _tool_results(llm.calls["payloads"][1], "file_list_dir")
        assert len(listings) == 1 and listings[0]["success"] is True
        for name in seeded:
            assert name in listings[0]["parsed"], \
                f"real listing missing seeded file {name!r}"
        assert index_md.is_file()
        written = index_md.read_text(encoding="utf-8")
        for name in seeded:
            assert f"- {name}" in written
        assert str(index_md.resolve()) in res.files_produced

    def test_golden_failing_tool_fails_the_run_honestly(self, vault, cfg, tmp_path):
        """Golden 6: the honest-failure contract. A tool that reports
        success=False on every call must end the run as `failed` with an
        error naming the tool after the 3-strike streak — the exact governor
        the v0.9.13 no-ops disarmed (empty stdout read as success, so the
        streak never counted and runs looped silently)."""
        from systemu.pipelines.quick_task import run_quick_task
        from systemu.runtime.tool_sandbox import ToolSandbox
        tool = _register_inline_tool(vault, "broken_export", _FAIL_BODY)
        # _FAIL_BODY has no recognizable I/O sink (it only returns a literal
        # failure dict) -> classify_source legitimately yields {} -> UNKNOWN
        # -> REQUIRE_APPROVAL. Bless it so the run fails for the RIGHT reason
        # (the tool's own reported failure), not because the gate denied it.
        store = _pre_approve_tool(tool, tmp_path)
        sandbox = ToolSandbox(vault.root, vault=vault, config=cfg,
                              command_approvals=store)

        llm = _fake_llm([
            {"action": "TOOL_CALL", "tool": "broken_export",
             "params": {"report": "q2"}, "reasoning": "export the report"},
        ])

        res = run_quick_task("Export the Q2 report", cfg, vault, llm_json=llm,
                             sandbox=sandbox)

        assert res.status == "failed"
        assert "broken_export" in (res.error or ""), \
            "failure must name the offending tool"
        assert "3 times in a row" in (res.error or "")
        assert "always broken" in (res.error or ""), \
            "the tool's own error text must surface, not a generic message"
        # quick-lane block-repeat: an IDENTICAL failing call runs ONCE, then the
        # two identical retries are blocked (not re-executed). They still count
        # toward the 3-strike cap, so the honest-failure contract holds (tool
        # named, its error surfaced, no files) but the broken tool runs once.
        assert res.tool_calls == 1
        assert res.files_produced == []
