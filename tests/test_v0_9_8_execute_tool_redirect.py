"""v0.9.8 — the output-path redirect must fire on the REAL dispatch path.

RCA: shadow_runtime dispatches tools via ToolSandbox.execute_tool(), but the
v0.9.7 redirect (_normalize_output_paths) lived only in ToolSandbox.execute().
So a model slip like file_write(path="/tmp/x.txt") escaped output_dir, the
objective verifier (which scans output_dir) never saw the deliverable, and the
run looped to MAX_ITERATIONS. These tests pin the redirect to execute_tool and
re-confirm the redirect semantics for an absolute /tmp write.
"""
import inspect

from systemu.runtime import tool_sandbox as ts


def test_execute_tool_applies_output_path_redirect():
    """The agentic-loop dispatch method must normalise output paths."""
    src = inspect.getsource(ts.ToolSandbox.execute_tool)
    assert "_normalize_output_paths" in src, (
        "execute_tool must call _normalize_output_paths — without it the redirect "
        "never fires in real runs and /tmp slips escape output_dir."
    )


def test_tmp_write_redirected_into_output_dir(tmp_path):
    """An absolute out-of-output_dir write for a write-ish tool is redirected."""
    out = tmp_path / "out"
    out.mkdir()
    params = {"path": "/tmp/nse_research.txt", "content": "x", "overwrite": True}
    new = ts._normalize_output_paths("file_write", params, str(out))
    assert new is not params  # a redirect happened (new dict returned)
    # the redirected path's basename lands inside output_dir
    assert new["path"].replace("\\", "/").endswith("/out/nse_research.txt")


def test_read_of_existing_file_not_redirected(tmp_path):
    """Reads to an existing location outside output_dir are left untouched."""
    out = tmp_path / "out"
    out.mkdir()
    existing = tmp_path / "real.txt"
    existing.write_text("hi")
    params = {"path": str(existing)}
    new = ts._normalize_output_paths("read_file", params, str(out))
    assert new["path"] == str(existing)  # untouched
