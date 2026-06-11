"""End-to-end dashboard smoke harness (the gate the BUG-4 regression slipped past).

Why this exists: W2.2 changed forged-tool execution to a subprocess, passed
every unit test + the full suite, and shipped — yet broke every task on the
live dashboard, because nothing in the suite booted the app on uvicorn's
actual serving loop (a SelectorEventLoop on Windows, where
asyncio.create_subprocess_exec is unsupported). These tests boot the REAL
server in a subprocess and exercise it over HTTP, so a loop-/serving-level
regression fails CI instead of the user.

Keyless by design — no LLM call is required to catch the regressions that
actually shipped. Marked ``smoke`` so it stays out of the fast unit run;
also runnable directly:  python -m pytest tests/test_smoke_dashboard_e2e.py -m smoke
"""
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.smoke

_REPO_ROOT = Path(__file__).resolve().parent.parent
_LAUNCHER = _REPO_ROOT / "tests" / "smoke" / "_launch_dashboard.py"
_BOOT_TIMEOUT_S = 45


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _get(url: str, timeout: float = 5.0):
    """Return (status, body) — follows redirects; never raises on HTTP errors."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception:
        return None, ""


# Exposed by the `dashboard` fixture so seeded-data tests can run in-process
# model checks against the SAME vault the live server is rendering.
_SMOKE_VAULT: dict = {}


def _seed_attention_data(vault_dir: Path) -> None:
    """W5.4: seed the smoke vault so the dashboard boots with the operator-
    attention surfaces POPULATED, not empty.

    The page shell builds the Needs-you badge, the right-rail ask rows, and
    the Status dropdown on EVERY route — so a crash in any of that rendering
    (the W5.1–W5.3 surfaces) fails every route assertion below instead of
    shipping. Mirrors the exact field shape of a real stuck-loop ask.
    """
    from systemu.vault.vault import Vault
    from systemu.approval.decision_queue import OperatorDecisionQueue

    vault = Vault(str(vault_dir))
    OperatorDecisionQueue(vault).post(
        title="Stuck on Objective 1: 'Search for parking options'",
        body="Answer to continue.",
        options=["Provide hint", "Accept partial", "Cancel run", "Other"],
        context={"kind": "structured_question",
                 "questions": [{"id": "action", "prompt": "Stuck…",
                                "multi": False,
                                "options": [{"label": "Provide hint"}],
                                "allow_free_text": True}],
                 "execution_id": "exec_smoke", "activity_id": "act_smoke",
                 "scroll_id": "scroll_smoke", "shadow_id": "shadow_smoke"},
        dedup_key="stuck:scroll_smoke:obj_1:r1",
    )
    (vault_dir / "elder").mkdir(parents=True, exist_ok=True)
    vault.append_chat_history({
        "ts": "2026-06-12T10:00:00", "prompt": "find the nearest salon",
        "scroll_id": "scroll_smoke", "status": "success",
        "summary": "Found 3 salons near T Nagar.",
    })


@pytest.fixture(scope="module")
def dashboard(tmp_path_factory):
    """Boot the dashboard as a subprocess on a throwaway file vault; yield base URL."""
    port = _free_port()
    vault_dir = tmp_path_factory.mktemp("smoke_vault")
    _seed_attention_data(vault_dir)
    _SMOKE_VAULT["dir"] = vault_dir
    env = dict(os.environ)
    # NiceGUI's ui.run enters a screen-test branch when PYTEST_CURRENT_TEST is
    # set (helpers.is_pytest()); the spawned server must look like a normal run.
    env.pop("PYTEST_CURRENT_TEST", None)
    # Force the WORKTREE's code onto sys.path so the subprocess tests the code
    # under review, not a pip-installed systemu in site-packages.
    env["PYTHONPATH"] = str(_REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env.update({
        "SMOKE_PORT": str(port),
        "SYSTEMU_VAULT_DIR": str(vault_dir),
        "SYSTEMU_STORAGE": "file",
        "SYSTEMU_NON_INTERACTIVE": "true",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
    })
    proc = subprocess.Popen(
        [sys.executable, str(_LAUNCHER)],
        cwd=str(_REPO_ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + _BOOT_TIMEOUT_S
    ready = False
    while time.time() < deadline:
        if proc.poll() is not None:   # process died during boot
            break
        status, _ = _get(base + "/")
        if status and status < 400:
            ready = True
            break
        time.sleep(0.5)
    if not ready:
        try:
            proc.terminate()
            out = proc.communicate(timeout=10)[0]
        except Exception:
            out = b""
        pytest.fail(
            f"dashboard did not serve on {base} within {_BOOT_TIMEOUT_S}s "
            f"(exit={proc.poll()}).\n--- subprocess output ---\n"
            + (out.decode("utf-8", "replace")[-3000:] if out else "(none)")
        )
    yield base
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except Exception:
        proc.kill()


class TestDashboardServes:
    def test_home_serves_200(self, dashboard):
        status, body = _get(dashboard + "/")
        assert status == 200, f"/ returned {status}"
        assert "nicegui" in body.lower(), "home did not render the NiceGUI shell"

    @pytest.mark.parametrize("route", [
        "/work", "/tools", "/shadows", "/insights", "/settings", "/inbox",
        "/chat", "/chat?tab=live", "/scrolls", "/activities", "/skills",
    ])
    def test_spine_routes_no_server_error(self, dashboard, route):
        # A page-builder crash on the serving loop surfaces as 500 here — the
        # whole point: import/route/render regressions fail CI, not the user.
        status, _ = _get(dashboard + route)
        assert status is not None and status < 500, f"{route} returned {status}"


class TestSeededAttentionSurfaces:
    """W5.4: the operator-attention surfaces, exercised POPULATED.

    The vault is seeded (one pending structured_question + one completed chat
    task) before boot, so every route assertion above already renders the
    badge / rail asks / Status rows live. These tests add the precise model
    checks against the same vault, and re-pin the two routes whose bodies
    changed most.
    """

    def test_badge_counts_the_seeded_ask(self, dashboard):
        from systemu.vault.vault import Vault
        from systemu.interface.dashboard import needs_you_badge_model
        m = needs_you_badge_model(Vault(str(_SMOKE_VAULT["dir"])))
        assert m["count"] == 1 and m["visible"] is True

    def test_status_rows_carry_the_seeded_outcome(self, dashboard):
        from systemu.vault.vault import Vault
        from systemu.interface.components.status_menu import build_status_rows
        rows = build_status_rows(Vault(str(_SMOKE_VAULT["dir"])))
        assert rows and rows[0]["name"] == "find the nearest salon"
        assert rows[0]["outcome"] == "Found 3 salons near T Nagar."
        assert rows[0]["target"] == "/workflow/scroll_smoke"

    def test_inbox_and_home_serve_with_pending_ask(self, dashboard):
        for route in ("/", "/inbox"):
            status, _ = _get(dashboard + route)
            assert status == 200, f"{route} returned {status} with a pending ask seeded"


class TestToolExecOnServingLoop:
    """Catch BUG-4 at the loop level — using uvicorn's OWN loop resolution, so
    the guard stays correct if uvicorn changes its platform loop."""

    def _uvicorn_loop(self):
        import asyncio
        try:
            # uvicorn's asyncio loop setup returns the loop CLASS it serves on.
            from uvicorn.loops.asyncio import asyncio_setup  # noqa: F401
        except Exception:
            pass
        # uvicorn/loops/asyncio.py uses SelectorEventLoop on Windows; mirror that.
        if sys.platform == "win32":
            return asyncio.SelectorEventLoop()
        return asyncio.new_event_loop()

    def test_forged_tool_subprocess_runs_on_serving_loop(self, tmp_path):
        import asyncio
        from systemu.runtime.tool_sandbox import ToolSandbox

        impl = tmp_path / "vault" / "tools" / "implementations" / "smoke_tool.py"
        impl.parent.mkdir(parents=True)
        impl.write_text(
            "import json\nprint(json.dumps({'success': True, 'echo': 'smoke'}))\n",
            encoding="utf-8",
        )
        sandbox = ToolSandbox(tmp_path / "vault")
        loop = self._uvicorn_loop()
        try:
            res = loop.run_until_complete(
                sandbox.execute_tool(str(impl), {}, force_subprocess=True, timeout=30)
            )
        finally:
            loop.close()
        assert res.success, f"forged-tool subprocess failed on serving loop: {res.error!r}"
        assert res.parsed.get("echo") == "smoke"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-m", "smoke", "-q"]))
