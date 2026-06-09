"""v0.9.8 KEYSTONE — multi-step web tasks complete because intermediate
"obtain X" objectives now have durable evidence the verifier can see.

Three fixes proven here:

FIX A — every SUCCESSFUL tool call writes one compact audit entry
        (``ShadowRuntime.execute`` → ``self.vault.append_action_audit(...)``).
        The entry has exactly the keys ``append_action_audit`` documents and
        round-trips through ``query_action_audit`` / ``state_delta.compute_delta``
        so the fresh-context verifier actually receives it.

FIX B — write-ish tools whose absolute path escapes ``output_dir`` are
        redirected into ``output_dir`` (the ``file_write(path="/tmp/x.txt")``
        case that landed outside the tree the verifier scans).

FIX C — the verifier prompt credits audit-entry / tool-action evidence for
        obtaining/determining/searching objectives.

All assertions are static (getsource / file content) or pure-function — no
LLM calls, no network, no live runtime loop.
"""
from pathlib import Path
import inspect

from systemu.runtime.tool_sandbox import (
    _normalize_output_paths,
    _is_write_ish,
    _PATH_PARAM_KEYS,
)
from systemu.runtime import shadow_runtime
from systemu.runtime.shadow_runtime import (
    ShadowRuntime,
    _build_tool_audit_entry,
    _truncate_audit_params,
    _current_objective_id_for_audit,
)


# ── FIX A: getsource guard — execute() audits successful tool calls ──────────

def test_execute_audits_successful_tool_calls():
    """The tool-success path in ShadowRuntime.execute must write an audit row
    via the vault so the verifier sees tool-action evidence."""
    src = inspect.getsource(ShadowRuntime.execute)
    assert "append_action_audit" in src, \
        "execute() must write an audit entry for successful tool calls"
    assert "_build_tool_audit_entry" in src, \
        "execute() should build the audit row via the tested helper"
    # The write must be guarded on tool success (not run for failed tools).
    assert 'getattr(result, "success"' in src


def test_audit_write_is_best_effort():
    """The audit write must be wrapped so it can NEVER break the run."""
    src = inspect.getsource(ShadowRuntime.execute)
    # locate the auto-audit block and confirm the call is followed by a
    # debug-swallowing except, and preceded by a try: within the same block.
    idx = src.index("append_action_audit")
    after = src[idx: idx + 400]
    assert "except Exception" in after, \
        "the auto-audit write must be followed by an except Exception (debug-swallow)"
    before = src[max(0, idx - 1200): idx]
    assert "try:" in before, \
        "the auto-audit write must sit inside a try block"


# ── FIX A: audit-entry shape + verifier round-trip ──────────────────────────

_REQUIRED_AUDIT_KEYS = {
    "ts", "execution_id", "objective_id", "action", "params", "success", "error",
}


def test_audit_entry_has_all_required_keys():
    entry = _build_tool_audit_entry(
        execution_id="exec_abc",
        objective_id=1,
        tool_name="file_write",
        params={"path": "/x", "content": "y"},
    )
    assert _REQUIRED_AUDIT_KEYS <= set(entry.keys())
    assert entry["execution_id"] == "exec_abc"
    assert entry["objective_id"] == 1
    assert entry["action"] == "file_write"
    assert entry["success"] is True
    assert entry["error"] is None
    assert isinstance(entry["params"], dict)
    # ts must be an ISO-ish string ending in Z (matches state_delta baseline fmt)
    assert isinstance(entry["ts"], str) and entry["ts"].endswith("Z")


def test_audit_entry_objective_id_falls_back_to_zero():
    entry = _build_tool_audit_entry(
        execution_id="e", objective_id=None, tool_name="t", params={})
    assert entry["objective_id"] == 0
    entry2 = _build_tool_audit_entry(
        execution_id="e", objective_id="not-an-int", tool_name="t", params={})
    assert entry2["objective_id"] == 0


def test_audit_params_are_truncated():
    big = "z" * 5000
    out = _truncate_audit_params({"content": big, "n": 7, "flag": True, "x": None})
    assert len(out["content"]) <= 256  # 200 cap + marker
    assert out["content"].endswith("[truncated]")
    assert out["n"] == 7 and out["flag"] is True and out["x"] is None


def test_truncate_handles_non_dict():
    assert _truncate_audit_params("nope") == {}
    assert _truncate_audit_params(None) == {}


def test_current_objective_id_helper():
    class _O:
        def __init__(self, oid, deps=None):
            self.id = oid
            self.depends_on = deps or []
    objs = [_O(1), _O(2, deps=[1])]
    # nothing completed → first ready objective is 1
    assert _current_objective_id_for_audit(objs, []) == 1
    # 1 done → 2's dep satisfied → 2
    assert _current_objective_id_for_audit(objs, [1]) == 2
    # all done → 0
    assert _current_objective_id_for_audit(objs, [1, 2]) == 0
    # no objectives → 0, never raises
    assert _current_objective_id_for_audit(None, None) == 0


def test_audit_entry_round_trips_to_verifier_delta(tmp_path):
    """The end-to-end contract: an entry written via append_action_audit is
    surfaced by state_delta.compute_delta (which is what the verifier sees)."""
    from systemu.vault.vault import Vault
    from systemu.runtime import state_delta
    from sharing_on.config import Config

    vault = Vault(root=tmp_path)
    out_dir = tmp_path / "output"
    out_dir.mkdir()

    # 1) baseline captured BEFORE the tool call (zero audit entries yet)
    baseline = state_delta.capture_baseline(
        vault=vault, execution_id="exec_1", objective_id=1,
        default_output_dir=str(out_dir),
    )
    assert baseline.audit_count == 0

    # 2) runtime writes the audit row exactly as execute() would
    entry = _build_tool_audit_entry(
        execution_id="exec_1", objective_id=1,
        tool_name="determine_location",
        params={"query": "current location"},
    )
    vault.append_action_audit(entry)

    # 3) compute_delta surfaces it to the verifier (the previously-missing link)
    delta = state_delta.compute_delta(
        baseline=baseline, vault=vault, default_output_dir=str(out_dir),
        chat_result=None, config=Config(), execution_id="exec_1",
    )
    assert len(delta.audit_entries_added) == 1
    surfaced = delta.audit_entries_added[0]
    assert surfaced["action"] == "determine_location"
    assert surfaced["success"] is True


# ── FIX B: write-ish path escaping output_dir is redirected ──────────────────

def test_file_write_tmp_path_redirected_into_output_dir(tmp_path):
    """The exact gyms-task failure: file_write(path="/tmp/x.txt") landed
    OUTSIDE output_dir. It must be redirected to <output_dir>/x.txt."""
    out = tmp_path / "out"
    out.mkdir()
    res = _normalize_output_paths(
        "file_write", {"path": "/tmp/x.txt", "content": "y"}, str(out))
    assert Path(res["path"]).name == "x.txt"
    # the redirected path must be inside output_dir
    assert Path(res["path"]).resolve().parent == out.resolve()
    assert res["content"] == "y"  # non-path params untouched


def test_file_write_is_classified_write_ish_and_path_is_a_path_key():
    """Confirm the two preconditions for the redirect to fire."""
    assert "path" in _PATH_PARAM_KEYS
    assert _is_write_ish("file_write", {}) is True
    assert _is_write_ish("write_text_file", {}) is True


# ── FIX C: verifier prompt credits audit / tool-action evidence ──────────────

def test_prompt_credits_audit_action_evidence_for_non_file_objectives():
    prompt_path = (
        Path(shadow_runtime.__file__).resolve().parent.parent
        / "prompts" / "verify_objective_completion.md"
    )
    text = prompt_path.read_text(encoding="utf-8").lower()
    # audit / tool-action evidence must be named as first-class evidence
    assert "audit" in text
    assert "tool action" in text or "tool-action" in text
    # it must tie that evidence to obtaining/determining/searching objectives
    assert any(w in text for w in ("obtain", "determin", "search")), \
        "prompt must credit audit evidence for obtain/determine/search objectives"
    # and it must still require a file only when the criteria name one
    assert "name" in text and "file" in text
