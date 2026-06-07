import pytest
from pathlib import Path

from systemu.core.models import Tool, ToolStatus, ToolType
from systemu.vault.vault import Vault
from systemu.runtime.tool_registry import ToolRegistry, ToolNotEnabledError
from systemu.runtime import dependency_installer as di
from systemu.runtime.dep_approvals import DepApprovalStore

@pytest.fixture
def tmp_vault(tmp_path):
    for sub in ["scrolls", "activities", "shadow_army", "skills", "tools/implementations",
                "evolutions", "notifications", "executions"]:
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    for idx_dir in ["scrolls", "activities", "shadow_army", "skills", "tools", "evolutions"]:
        (tmp_path / idx_dir / "index.json").write_text("[]", encoding="utf-8")
    return Vault(str(tmp_path))

@pytest.fixture
def implementations_dir(tmp_vault):
    impl_dir = Path(tmp_vault.root) / "tools" / "implementations"
    return impl_dir

@pytest.fixture
def registry(tmp_vault, implementations_dir):
    return ToolRegistry(implementations_dir, tmp_vault)

@pytest.mark.asyncio
async def test_load_tool_success(tmp_vault, implementations_dir, registry):
    tool = Tool(
        id="tool_1", name="dynamic_tool", description="Test",
        tool_type=ToolType.PYTHON_FUNCTION, status=ToolStatus.DEPLOYED,
        enabled=True
    )
    tmp_vault.save_tool(tool)

    code = """
def run(x):
    return {"success": True, "result": x * 2}
"""
    (implementations_dir / "dynamic_tool.py").write_text(code, encoding="utf-8")

    result = await registry.execute("dynamic_tool", {"x": 5})
    assert result["success"] is True
    assert result["result"] == 10

@pytest.mark.asyncio
async def test_load_tool_missing_file(tmp_vault, implementations_dir, registry):
    tool = Tool(
        id="tool_1", name="missing_tool", description="Test",
        tool_type=ToolType.PYTHON_FUNCTION, status=ToolStatus.DEPLOYED,
        enabled=True
    )
    tmp_vault.save_tool(tool)

    result = await registry.execute("missing_tool", {})
    assert result["success"] is False
    assert "No implementation file" in result["error"]

@pytest.mark.asyncio
async def test_load_invalid_python(tmp_vault, implementations_dir, registry):
    tool = Tool(
        id="tool_1", name="bad_tool", description="Test",
        tool_type=ToolType.PYTHON_FUNCTION, status=ToolStatus.DEPLOYED,
        enabled=True
    )
    tmp_vault.save_tool(tool)

    # Invalid python syntax
    (implementations_dir / "bad_tool.py").write_text("def run(:\n  pass", encoding="utf-8")

    with pytest.raises(SyntaxError):
        await registry.execute("bad_tool", {})

@pytest.mark.asyncio
async def test_tool_not_enabled_error(tmp_vault, implementations_dir, registry):
    tool = Tool(
        id="tool_1", name="disabled_tool", description="Test",
        tool_type=ToolType.PYTHON_FUNCTION, status=ToolStatus.FORGED,
        enabled=False
    )
    tmp_vault.save_tool(tool)

    with pytest.raises(ToolNotEnabledError):
        await registry.execute("disabled_tool", {})


# ─────────────────────────────────────────────────────────────────────────────
# v0.3.3 — Self-heal on missing dependency
# ─────────────────────────────────────────────────────────────────────────────
#
# The registry must recover from ImportError when the tool's manifest declares
# the missing pip package, without falling back to free-form / unsafe inference
# from the ImportError name.  These tests pin that contract.

@pytest.fixture(autouse=True)
def _reset_dep_caches():
    di.reset_cache_for_tests()
    yield
    di.reset_cache_for_tests()


def _make_tool(name, *, dependencies=None):
    return Tool(
        id=f"tool_{name}",
        name=name,
        description="Test tool",
        tool_type=ToolType.PYTHON_FUNCTION,
        status=ToolStatus.DEPLOYED,
        enabled=True,
        dependencies=dependencies or [],
    )


def _write_tool_with_missing_import(impl_dir: Path, name: str, missing_module: str):
    """Write a tool whose module-level import fails."""
    (impl_dir / f"{name}.py").write_text(
        f"import {missing_module}\n"
        f"def run(**kw):\n"
        f"    return {{'success': True}}\n",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_self_heal_when_manifest_declares_dep(tmp_vault, implementations_dir, monkeypatch):
    """When the manifest declares the package, registry calls the installer
    and retries the import once.  After 'install' the second import succeeds
    because we swap the offending source for a clean one in the install stub.
    """
    tool = _make_tool("needs_dep", dependencies=["fakedep_xyz"])
    tmp_vault.save_tool(tool)
    _write_tool_with_missing_import(implementations_dir, "needs_dep", "fakedep_xyz")

    # Stub the pip subprocess: pretend 'install' succeeded AND fix the source
    # so the next import works.  This is the most realistic stub because real
    # pip side-effects make `import fakedep_xyz` succeed.
    def fake_pip(packages, *, timeout):
        # Rewrite the impl to drop the bad import, simulating the package
        # now being importable.
        (implementations_dir / "needs_dep.py").write_text(
            "def run(**kw):\n    return {'success': True, 'healed': True}\n",
            encoding="utf-8",
        )
        return di.InstallResult(
            ok=True, status=di.InstallStatus.INSTALLED,
            installed_now=list(packages),
        )
    monkeypatch.setattr(di, "_run_pip_install", fake_pip)

    registry = ToolRegistry(
        implementations_dir, tmp_vault,
        install_mode=di.InstallMode.ALWAYS,
    )
    result = await registry.execute("needs_dep", {})
    assert result["success"] is True
    assert result.get("healed") is True


@pytest.mark.asyncio
async def test_no_self_heal_when_manifest_empty(tmp_vault, implementations_dir, monkeypatch):
    """SECURITY: When the manifest declares zero deps, the registry must NOT
    derive a package name from the ImportError and install it.  The contract
    is "manifest is the only trusted source" — installing anything else would
    let a malicious tool author choose what gets pip-installed.
    """
    tool = _make_tool("undeclared_dep", dependencies=[])
    tmp_vault.save_tool(tool)
    _write_tool_with_missing_import(implementations_dir, "undeclared_dep", "evil_package")

    install_called = {"count": 0}
    def fake_pip(packages, *, timeout):
        install_called["count"] += 1
        return di.InstallResult(ok=True, status=di.InstallStatus.INSTALLED, installed_now=list(packages))
    monkeypatch.setattr(di, "_run_pip_install", fake_pip)

    registry = ToolRegistry(
        implementations_dir, tmp_vault,
        install_mode=di.InstallMode.ALWAYS,
    )
    result = await registry.execute("undeclared_dep", {})
    assert result["success"] is False
    assert result.get("error_type") == "missing_dependency"
    assert install_called["count"] == 0  # never invoked


@pytest.mark.asyncio
async def test_prompt_mode_blocks_without_approval(tmp_vault, implementations_dir, tmp_path):
    tool = _make_tool("needs_approval", dependencies=["pending_pkg"])
    tmp_vault.save_tool(tool)
    _write_tool_with_missing_import(implementations_dir, "needs_approval", "pending_pkg")

    approvals = DepApprovalStore(tmp_path / "approvals.json")
    registry = ToolRegistry(
        implementations_dir, tmp_vault,
        install_mode=di.InstallMode.PROMPT,
        approvals=approvals,
    )
    result = await registry.execute("needs_approval", {})
    assert result["success"] is False
    assert result.get("error_type") == "dependency_install_pending_approval"
    assert "pending_pkg" in (result.get("missing_packages") or [])
    # The approval store should have recorded the pending request.
    pending = approvals.list_pending()
    assert len(pending) == 1
    assert pending[0]["package"] == "pending_pkg"


@pytest.mark.asyncio
async def test_off_mode_returns_blocked(tmp_vault, implementations_dir):
    tool = _make_tool("blocked_dep", dependencies=["someair_pkg"])
    tmp_vault.save_tool(tool)
    _write_tool_with_missing_import(implementations_dir, "blocked_dep", "someair_pkg")

    registry = ToolRegistry(
        implementations_dir, tmp_vault,
        install_mode=di.InstallMode.OFF,
    )
    result = await registry.execute("blocked_dep", {})
    assert result["success"] is False
    assert result.get("error_type") == "dependency_install_blocked"


@pytest.mark.asyncio
async def test_install_failed_surfaces_distinct_error_type(tmp_vault, implementations_dir, monkeypatch):
    tool = _make_tool("fails_to_install", dependencies=["impossible_pkg"])
    tmp_vault.save_tool(tool)
    _write_tool_with_missing_import(implementations_dir, "fails_to_install", "impossible_pkg")

    def fake_pip(packages, *, timeout):
        return di.InstallResult(
            ok=False, status=di.InstallStatus.FAILED,
            error="pip exit 1",
            pip_stderr_tail="ERROR: no matching distribution",
        )
    monkeypatch.setattr(di, "_run_pip_install", fake_pip)

    registry = ToolRegistry(
        implementations_dir, tmp_vault,
        install_mode=di.InstallMode.ALWAYS,
    )
    result = await registry.execute("fails_to_install", {})
    assert result["success"] is False
    assert result.get("error_type") == "dependency_install_failed"


@pytest.mark.asyncio
async def test_self_heal_does_not_retry_more_than_once(tmp_vault, implementations_dir, monkeypatch):
    """Pin: a tool whose import still fails after install should NOT loop —
    it should surface a single dep error.  Manifest-wrong scenarios end here.
    """
    tool = _make_tool("still_broken", dependencies=["claimed_pkg"])
    tmp_vault.save_tool(tool)
    _write_tool_with_missing_import(implementations_dir, "still_broken", "claimed_pkg")

    install_calls = {"count": 0}
    def fake_pip(packages, *, timeout):
        install_calls["count"] += 1
        return di.InstallResult(ok=True, status=di.InstallStatus.INSTALLED, installed_now=list(packages))
    monkeypatch.setattr(di, "_run_pip_install", fake_pip)

    registry = ToolRegistry(
        implementations_dir, tmp_vault,
        install_mode=di.InstallMode.ALWAYS,
    )
    result = await registry.execute("still_broken", {})
    assert result["success"] is False
    # First install ran, but the import still fails — final outcome is
    # missing_dependency (manifest is wrong / incomplete) and pip ran exactly once.
    assert install_calls["count"] == 1
    assert result.get("error_type") == "missing_dependency"


@pytest.mark.asyncio
async def test_param_name_mismatch_is_reconciled(tmp_vault, implementations_dir, registry):
    """v0.9.6 regression: the exact bug behind 3 consecutive live-run parks.

    The tool's run() declares (output_path, content); the LLM (or a forge
    schema/code drift) calls it with (path, text). Before reconciliation this
    returned {'success': False, 'error': "Parameter mismatch ..."}. Now the
    params are remapped onto the real signature and the tool executes.
    """
    tool = Tool(
        id="tool_pm", name="write_text_file", description="write text",
        tool_type=ToolType.PYTHON_FUNCTION, status=ToolStatus.DEPLOYED,
        enabled=True,
    )
    tmp_vault.save_tool(tool)
    code = """
def run(output_path, content):
    return {"success": True, "wrote_to": output_path, "len": len(content)}
"""
    (implementations_dir / "write_text_file.py").write_text(code, encoding="utf-8")

    # LLM used training-prior names: path / text
    result = await registry.execute(
        "write_text_file", {"path": "/tmp/poem.txt", "text": "four lines"},
    )
    assert result["success"] is True, result
    assert result["wrote_to"] == "/tmp/poem.txt"
    assert result["len"] == len("four lines")


@pytest.mark.asyncio
async def test_extra_hallucinated_param_is_dropped(tmp_vault, implementations_dir, registry):
    """An LLM-hallucinated extra kwarg must not crash the tool."""
    tool = Tool(
        id="tool_h", name="reader", description="read",
        tool_type=ToolType.PYTHON_FUNCTION, status=ToolStatus.DEPLOYED,
        enabled=True,
    )
    tmp_vault.save_tool(tool)
    code = """
def run(path):
    return {"success": True, "read": path}
"""
    (implementations_dir / "reader.py").write_text(code, encoding="utf-8")
    result = await registry.execute(
        "reader", {"path": "/tmp/x", "encoding": "utf-8", "mode": "r"},
    )
    assert result["success"] is True, result
    assert result["read"] == "/tmp/x"
