"""S1b — three specific ungated-execution holes in the unattended dry-run, closed.

GOAL: the unattended dry-run should not execute what the LIVE gate would card.
This file proves that goal is met for tools with DECLARED effect tags and for
bodies whose sinks the AST scan recognises by name. It is NOT a proof of the
general invariant — see KNOWN GAP at the bottom of this docstring.

The confirmed holes in ``systemu/pipelines/tool_dry_run.py``:

1. **The gate never runs.** ``_execute`` invokes ``ToolSandbox.execute_tool``
   WITHOUT threading ``tool=``, so ``_maybe_gate_tool`` short-circuits on its
   ``if tool is None: return`` and the forged body executes UNGATED. Dry-runs
   fire UNATTENDED (end-of-forge ``tool_forge.py:222``, the startup sweep in
   ``scheduler/jobs.py``, the tool reconciler, the recalibrator), so there is no
   operator in the loop to notice.
2. **The declared-tags spoof.** ``_net_egress_skip_reason`` trusts a NON-EMPTY
   self-declared ``effect_tags`` and never scans the source when tags are
   present (``vault_migrator`` PREFERS the declaration over the scan). A forged
   body declaring ``["local_read"]`` while calling ``urllib.request.urlopen``
   therefore sails past the egress guard and egresses during "dry run".
3. **The declared-``dry_run``-param loophole.** The pre-existing destructive
   guard only refuses when ``"dry_run" not in params``, so a destructive-NAMED
   tool whose SCHEMA declares a ``dry_run`` parameter slips past it. Nothing
   verifies the body actually honours the flag — the tool merely says it does.

The fix is an AUTO-SKIP (``_gate_skip_reason``), deliberately NOT threading
``tool=`` — see the comment block at ``tool_dry_run._execute`` for why threading
it would deadlock the forge loop rather than protect anything.

Test discipline notes (these tests have to fail for the RIGHT reason):

* Every skip test asserts its PRECONDITIONS — that the tool is not already
  caught by the two PRE-EXISTING guards (the ``is_destructive_call`` +
  ``"dry_run" not in params`` refusal, and ``_net_egress_skip_reason``). Without
  that, a test could "pass" on an older guard and prove nothing about the new one.
* The "body must NOT run" assertions use an on-disk SENTINEL written by the real
  tool body through the real sandbox (no ``_execute`` monkeypatch), and
  ``test_local_tool_still_executes_and_passes`` is the POSITIVE CONTROL proving
  that same machinery really does write the sentinel when the body runs. A
  "must not run" assertion is worthless without that control.
* Each test uses a UNIQUE impl filename and a UNIQUE sentinel path so CPython's
  size+mtime pyc cache can never silently serve one test's body to another.
* The body of the net-spoof tool parks its ``urlopen`` call behind a branch that
  is never taken. ``classify_source`` is an AST scan, so it still classifies the
  tool ``net_read`` — but the test can never actually egress, even when the
  guard is deliberately removed to mutation-test this file.

── KNOWN GAP: the untagged-body source-scan evasion ─────────────────────────

The three holes above are closed. The DOMINANT case is not.

A freshly-forged tool has NO ``effect_tags`` (``tool_forge`` never stamps them;
the ``vault_migrator`` backfill is a once-per-version BOOT pass) and is dry-run
IMMEDIATELY at ``tool_forge.py:222``. For that tool ``_gate_skip_reason`` has no
declared tags to score, so it falls back to a ``classify_source`` AST scan — and
that scan is NAME-MATCHING. It recognises a sink only when the receiver is
literally spelled: ``subprocess.run``, ``os.system``, ``requests.get``. So::

    import subprocess as sp          # aliased      → not seen
    from subprocess import check_output   # idiomatic → not seen
    from os import system                 # idiomatic → not seen

A body using any of these, plus one ordinary local write, scans as exactly
``{local_write}`` — purely local — so ``_net_egress_skip_reason`` proceeds,
``evaluate_action`` returns ALLOW, and the body EXECUTES UNATTENDED. Note the
from-import forms are ORDINARY Python, not adversarial constructions; they are
the likelier real-world trigger.

``TestKnownGap_UntaggedSourceScanIsNameMatching`` (bottom of this file) is the
strict-xfail RATCHET for that gap: it asserts the DESIRED behaviour, so it xfails
today and will XPASS — hard-failing the suite — the moment the classifier learns
alias resolution, forcing this section and the ``_gate_skip_reason`` /
``_net_egress_skip_reason`` docstrings to be brought back into line.

A SECOND, independent gap surfaced while building that ratchet, and is pinned
separately (``test_gate_scorer_should_also_skip_the_shell_body``): ``shell_exec``
sits in ``action_governance._LOCAL_TAGS`` and NOT in ``_APPROVAL_TAGS``, so
``evaluate_action`` ALLOWs it. A CORRECTLY ``shell_exec``-tagged body therefore
still scores ALLOW at ``_gate_skip_reason`` — and at the LIVE
``_maybe_gate_tool``. Fixing the classifier does not flip that; only
``_net_egress_skip_reason``'s stricter ``_SAFE_LOCAL_TAGS`` allowlist (which
excludes ``shell_exec``) stops such a body in the dry-run pipeline, making that
one guard a single point of failure. The two ratchets are kept apart so a fix to
either is never mistaken for a fix to both.

Do not read the parity test (t5) as covering this. It drives a NON-forged tool
with NO implementation file over explicit tag sets, so its ``set()`` case scores
UNKNOWN on both sides and the source fallback is never exercised. It pins scorer
WIRING for declared-tag inputs, which is precisely where the two paths agree.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from systemu.core.models import Tool, ToolStatus, ToolType


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures / helpers (mirroring tests/test_s1b_dryrun_egress.py conventions)

@pytest.fixture
def vault(tmp_path):
    from systemu.vault.vault import Vault
    for sub in ("scrolls", "activities", "shadow_army", "skills", "tools",
                "evolutions"):
        (tmp_path / sub).mkdir()
        (tmp_path / sub / "index.json").write_text("[]")
    return Vault(str(tmp_path))


def _make_tool(name, effect_tags=None, *, forged=True, schema=None):
    return Tool(
        id=f"tool_{name}",
        name=name,
        description="for tests",
        tool_type=ToolType.PYTHON_FUNCTION,
        status=ToolStatus.FORGED,
        enabled=False,
        forged_by_systemu=forged,
        effect_tags=list(effect_tags or []),
        parameters_schema=schema or {"x": {"type": "string", "default": "hello"}},
    )


def _config_for(vault_dir: Path):
    config = MagicMock()
    config.openrouter_api_key = "test"
    config.tier3_model = "test"
    config.vault_dir = str(vault_dir)
    config.output_dir = str(vault_dir / "output")
    return config


def _write_impl(tmp_path: Path, filename: str, body: str) -> str:
    """Write ``body`` to the vault impl dir; return the tool-relative path."""
    impl_dir = tmp_path / "vault" / "tools" / "implementations"
    impl_dir.mkdir(parents=True, exist_ok=True)
    (impl_dir / filename).write_text(body, encoding="utf-8")
    return f"vault/tools/implementations/{filename}"


def _sentinel_body(sentinel: Path, *, extra_import: str = "", never_branch: str = "") -> str:
    """A tool body that records the fact it RAN by writing ``sentinel``.

    ``never_branch`` is emitted inside an ``if x == '__never__':`` guard — present
    in the AST (so ``classify_source`` sees it) but never executed.
    """
    return (
        f"{extra_import}"
        "from pathlib import Path\n"
        "\n"
        "def run(x):\n"
        f"    Path(r'{sentinel}').write_text('ran')\n"
        + (f"    if x == '__never__':\n        {never_branch}\n" if never_branch else "")
        + "    return {'success': True}\n"
    )


def _assert_reaches_the_new_gate(tool, params, config):
    """Precondition: neither PRE-EXISTING guard catches this tool.

    If either did, a "skipped" assertion downstream would pass for the WRONG
    reason and this file would be testing nothing.
    """
    from systemu.pipelines.tool_dry_run import _net_egress_skip_reason
    from systemu.runtime.tool_sandbox import ToolSandbox

    old_destructive_guard = (
        ToolSandbox.is_destructive_call(tool.name, params) and "dry_run" not in params
    )
    assert not old_destructive_guard, (
        "precondition failed: the pre-existing destructive-call guard already "
        "refuses this tool, so the test cannot prove anything about the new gate"
    )
    assert _net_egress_skip_reason(tool, config) is None, (
        "precondition failed: the pre-existing net-egress guard already skips "
        "this tool, so the test cannot prove anything about the new gate"
    )


# ─────────────────────────────────────────────────────────────────────────────
# t1 — the core hole: a REQUIRE_APPROVAL-scoring tool must not execute

class TestGateSkipStopsUngatedExecution:
    def test_local_delete_tool_is_skipped_and_body_never_runs(self, tmp_path, vault):
        """t1. A forged tool declaring ``local_delete`` scores REQUIRE_APPROVAL at
        the live gate (``_APPROVAL_TAGS``). Pre-fix, the dry-run executed its real
        body anyway — ``_execute`` never threads ``tool=``, so ``_maybe_gate_tool``
        no-ops. The body must NOT run, and the verdict must be recorded.

        The tool name is deliberately NON-destructive (``tidy_workspace``) so the
        old name-heuristic guard cannot be what produces the skip.
        """
        from systemu.pipelines.tool_dry_run import dry_run_tool

        sentinel = tmp_path / "SENTINEL_t1_local_delete.txt"
        t = _make_tool("tidy_workspace", effect_tags=["local_delete"])
        t.implementation_path = _write_impl(
            tmp_path, "tidy_workspace.py", _sentinel_body(sentinel))
        config = _config_for(tmp_path / "vault")

        _assert_reaches_the_new_gate(t, {"x": "hello"}, config)

        result = dry_run_tool(t, vault=vault, config=config)

        assert not sentinel.exists(), (
            "SECURITY: the dry-run executed the body of a tool the live action "
            "gate would have carded (REQUIRE_APPROVAL)")
        assert result.status == "skipped"
        assert result.success is False
        assert result.gate_verdict == "require_approval"
        # operator_verify keeps _complete_deferred_enables able to finish an
        # already-approved "Enable & run" instead of parking the task forever.
        assert result.operator_verify is True
        assert "require_approval" in (result.skip_reason or "").lower()

    def test_declared_local_read_but_net_source_is_skipped(self, tmp_path, vault):
        """t2 — THE SPOOF. Declared ``effect_tags=["local_read"]`` (which the
        pre-existing egress guard TRUSTS, because vault_migrator prefers a
        self-declared TOOL_META over the scan) while the source structurally
        egresses. ``forged_network_denied`` re-scans the source precisely because
        a declaration is declare-away-able, so the spoof lands on DENY.
        """
        from systemu.pipelines.tool_dry_run import dry_run_tool
        from systemu.runtime.action_governance import has_network_egress
        from systemu.runtime.effect_tags import classify_source

        sentinel = tmp_path / "SENTINEL_t2_spoof.txt"
        body = _sentinel_body(
            sentinel,
            extra_import="import urllib.request\n",
            # In the AST (so the scan classifies net_read) but never executed, so
            # this test cannot egress even with the guard removed.
            never_branch="urllib.request.urlopen('https://example.invalid/x')",
        )
        # Precondition: the source really IS structurally net-egressing, so the
        # test exercises the source re-scan and not some other branch.
        assert has_network_egress(classify_source(body))

        t = _make_tool("collect_notes", effect_tags=["local_read"])
        t.implementation_path = _write_impl(tmp_path, "collect_notes.py", body)
        config = _config_for(tmp_path / "vault")

        # Precondition: the declared-tag spoof really does defeat the OLD guard.
        _assert_reaches_the_new_gate(t, {"x": "hello"}, config)

        result = dry_run_tool(t, vault=vault, config=config)

        assert not sentinel.exists(), (
            "SECURITY: a forged tool that declared local_read while structurally "
            "egressing executed during dry-run")
        assert result.status == "skipped"
        assert result.gate_verdict == "deny"
        assert result.operator_verify is True

    def test_destructive_named_tool_declaring_dry_run_param_is_skipped(self, tmp_path, vault):
        """t4. A destructive-NAMED tool whose schema declares a ``dry_run`` param
        slips past the pre-existing guard (which only refuses when ``"dry_run"
        not in params``) — the tool merely SAYS it supports dry-run; nothing
        verifies it honours the flag. The action gate scores it REQUIRE_APPROVAL
        on ``is_destructive_param`` regardless, so it is now skipped.
        """
        from systemu.pipelines.tool_dry_run import dry_run_tool
        from systemu.pipelines.fixture_synth import synthesize_params
        from systemu.runtime.tool_sandbox import ToolSandbox

        sentinel = tmp_path / "SENTINEL_t4_dryrun_param.txt"
        schema = {
            "x": {"type": "string", "default": "hello"},
            "dry_run": {"type": "boolean", "default": True},
        }
        t = _make_tool("purge_stale_records", effect_tags=["local_write"], schema=schema)
        t.implementation_path = _write_impl(
            tmp_path, "purge_stale_records.py",
            "from pathlib import Path\n"
            "\n"
            "def run(x, dry_run=True):\n"
            f"    Path(r'{sentinel}').write_text('ran')\n"
            "    return {'success': True}\n",
        )
        config = _config_for(tmp_path / "vault")

        params = synthesize_params(schema, tool_name=t.name).params
        # Preconditions: the name IS destructive, yet the declared dry_run param
        # is what lets it past the old guard — that is the gap being closed.
        assert ToolSandbox.is_destructive_call(t.name, params) is True
        assert "dry_run" in params, "precondition: schema must synthesize a dry_run param"
        _assert_reaches_the_new_gate(t, params, config)

        result = dry_run_tool(t, vault=vault, config=config)

        assert not sentinel.exists(), (
            "SECURITY: a destructive-named tool executed during dry-run merely "
            "because it DECLARED a dry_run parameter")
        assert result.status == "skipped"
        assert result.gate_verdict == "require_approval"


# ─────────────────────────────────────────────────────────────────────────────
# t3 — the frictionless majority is untouched (and the POSITIVE CONTROL)

class TestFrictionlessMajorityUnaffected:
    @pytest.mark.parametrize("tag", ["local_read", "local_write"])
    def test_local_tool_still_executes_and_passes(self, tmp_path, vault, tag):
        """t3. A purely-local tool still runs and passes — no regression on the
        frictionless majority.

        This is ALSO the positive control for every ``not sentinel.exists()``
        assertion above: same helper, same sandbox, same code path. It proves the
        sentinel really is written when the body runs, so a missing sentinel
        elsewhere is evidence of a skip and not of a broken fixture.
        """
        from systemu.pipelines.tool_dry_run import dry_run_tool

        sentinel = tmp_path / f"SENTINEL_t3_{tag}.txt"
        t = _make_tool(f"summarize_{tag}", effect_tags=[tag])
        t.implementation_path = _write_impl(
            tmp_path, f"summarize_{tag}.py", _sentinel_body(sentinel))
        config = _config_for(tmp_path / "vault")

        result = dry_run_tool(t, vault=vault, config=config)

        assert sentinel.exists(), (
            "positive control FAILED: a benign local tool did not execute, so the "
            "'body never ran' assertions in this file prove nothing")
        assert result.status == "passed"
        assert result.success is True
        assert result.gate_verdict is None

    def test_untagged_forged_tool_with_local_source_still_dry_runs(self, tmp_path, vault):
        """A freshly-forged tool has NO effect_tags stamped (``tool_forge`` never
        stamps them; the vault_migrator backfill is a once-per-version BOOT pass),
        and it is dry-run immediately at ``tool_forge.py:222``. The effective-tag
        derivation (declared, else ``classify_source``) is therefore what keeps
        the forge path working at all — see ``_gate_skip_reason``'s docstring for
        why this is the one deliberate divergence from ``_maybe_gate_tool``.

        SECURITY CAVEAT — this test ENCODES the risky path as desired behaviour,
        so it must carry the caveat. That same ``classify_source`` fallback is
        the ONLY classification standing between a freshly-forged body and
        unattended execution, and it is NAME-MATCHING: it recognises a sink only
        when the receiver is literally spelled (``subprocess.run``,
        ``os.system``, ``requests.get``). An ALIASED (``import subprocess as
        sp``) or FROM-IMPORTED (``from subprocess import check_output``,
        ``from os import system``) sink is NOT seen, and the body's remaining
        local sinks then score it purely-local — so it proceeds exactly as this
        test's benign tool does. "Passes this test" therefore does not mean
        "safe to execute"; it means "scanned clean". See the KNOWN GAP section
        in this module's docstring and
        ``TestKnownGap_UntaggedSourceScanIsNameMatching``.
        """
        from systemu.pipelines.tool_dry_run import dry_run_tool

        sentinel = tmp_path / "SENTINEL_untagged_local.txt"
        t = _make_tool("note_scratchpad", effect_tags=[])   # nothing stamped
        t.implementation_path = _write_impl(
            tmp_path, "note_scratchpad.py", _sentinel_body(sentinel))
        config = _config_for(tmp_path / "vault")

        result = dry_run_tool(t, vault=vault, config=config)

        assert sentinel.exists()
        assert result.status == "passed"


# ─────────────────────────────────────────────────────────────────────────────
# t5 — parity with the live gate, pinned at test time

class TestParityWithLiveGate:
    """t5. ``_gate_skip_reason`` must reach the same proceed/skip decision the
    LIVE path reaches, so scorer drift shows up here rather than as a silent
    re-opening of the hole.

    The live path is a COMPOSITE of two checks in ``ToolSandbox.execute_tool``:
    ``forged_network_denied`` (:764) and THEN ``_maybe_gate_tool`` (:794).
    Parity is asserted against that composite, not against ``_maybe_gate_tool``
    alone — checking only the latter would wrongly call the forged-network DENY
    a divergence.
    """

    TAG_SETS = [
        {"local_read"}, {"local_write"}, {"local_delete"},
        set(), {"net_read"}, {"net_mutate"},
    ]

    @pytest.mark.parametrize("tags", TAG_SETS, ids=lambda s: "+".join(sorted(s)) or "empty")
    @pytest.mark.parametrize("is_destructive_param", [True, False])
    def test_matches_maybe_gate_tool_scoring(self, tmp_path, tags, is_destructive_param):
        """For a NON-forged tool (``forged_network_denied`` is a no-op), the skip
        decision must be exactly ``evaluate_action(ctx) != ALLOW`` over the SAME
        ActionContext ``_maybe_gate_tool`` builds.
        """
        from systemu.pipelines.tool_dry_run import _gate_skip_reason
        from systemu.runtime.action_governance import (
            ActionContext, Verdict, evaluate_action)

        # A name with no verb-map category, so the tag set is the only signal and
        # both sides see identical inputs.
        name = "parity_probe"
        # No implementation file on disk => the effective-tag fallback finds
        # nothing => the empty case really is scored as UNKNOWN on BOTH sides.
        t = _make_tool(name, effect_tags=sorted(tags), forged=False)
        t.implementation_path = "vault/tools/implementations/absent_parity_probe.py"
        config = _config_for(tmp_path / "vault")

        # EXACTLY the context tool_sandbox.py:1073-1081 constructs.
        ctx = ActionContext(
            tool=name,
            effect_tags={str(x) for x in sorted(tags)},
            is_destructive_param=is_destructive_param,
            target=None,
            target_is_network=False,
            classification_trusted=True,
        )
        expected_verdict, _ = evaluate_action(ctx)
        expected_skip = expected_verdict != Verdict.ALLOW

        got = _gate_skip_reason(t, {}, config, is_destructive_param=is_destructive_param)

        assert (got is not None) is expected_skip, (
            f"parity drift for tags={tags} destructive={is_destructive_param}: "
            f"live gate says {expected_verdict}, dry-run says {got}")
        if got is not None:
            assert got[0] == expected_verdict.value

    @pytest.mark.parametrize("net_tag", ["net_read", "net_mutate"])
    def test_forged_network_tool_is_denied_like_the_live_path(self, tmp_path, net_tag):
        """The forged-network HARD-DENY half of the composite. ``net_read`` is the
        load-bearing case: ``evaluate_action`` ALLOWs it (it is not in
        ``_APPROVAL_TAGS``), so ONLY ``forged_network_denied`` stops a forged
        net-reading body from executing unattended.
        """
        from systemu.pipelines.tool_dry_run import _gate_skip_reason

        t = _make_tool("parity_net_probe", effect_tags=[net_tag], forged=True)
        config = _config_for(tmp_path / "vault")

        got = _gate_skip_reason(t, {}, config, is_destructive_param=False)

        assert got is not None and got[0] == "deny"

    def test_pipeline_never_reaches_the_gate_for_net_tags(self, tmp_path, vault):
        """Defence in depth: even though ``_gate_skip_reason`` alone would ALLOW a
        NON-forged ``net_read`` tool (matching the live gate), the pre-existing
        fail-closed ``_net_egress_skip_reason`` runs FIRST in the pipeline, so no
        net-tagged tool ever reaches execution during a dry-run.
        """
        from systemu.pipelines.tool_dry_run import dry_run_tool

        sentinel = tmp_path / "SENTINEL_parity_net_pipeline.txt"
        t = _make_tool("read_feed", effect_tags=["net_read"], forged=False)
        t.implementation_path = _write_impl(
            tmp_path, "read_feed.py", _sentinel_body(sentinel))
        config = _config_for(tmp_path / "vault")

        result = dry_run_tool(t, vault=vault, config=config)

        assert not sentinel.exists()
        assert result.status == "skipped"


# ─────────────────────────────────────────────────────────────────────────────
# t6 — replay mode gets the same gate

class TestReplayAgainstHistoryIsGated:
    def test_local_delete_replay_is_skipped(self, tmp_path, vault):
        """t6. ``replay_against_history`` runs unattended off recorded params
        (the v0.5.0-d recalibrator's bump path), so it needs the same gate. A
        skip yields ``success=False`` → the bump is rejected → the supervisor
        falls back to forking, identical to the existing net-skip behaviour.
        """
        from systemu.pipelines.tool_dry_run import replay_against_history

        sentinel = tmp_path / "SENTINEL_t6_replay.txt"
        t = _make_tool("archive_workspace", effect_tags=["local_delete"])
        t.implementation_path = _write_impl(
            tmp_path, "archive_workspace.py", _sentinel_body(sentinel))
        t.last_successful_params = [{"x": "one"}, {"x": "two"}]
        config = _config_for(tmp_path / "vault")

        result = replay_against_history(t, vault=vault, config=config)

        assert not sentinel.exists(), (
            "SECURITY: replay executed a body the live action gate would card")
        assert result.status == "skipped"
        assert result.success is False
        assert result.replayed_count == 0
        assert result.gate_verdict == "require_approval"


# ─────────────────────────────────────────────────────────────────────────────
# t7 — the skip does not create a stuck task or a reconciler re-loop

class TestGateSkipDoesNotStrandTheReconciler:
    def test_skipped_tool_is_not_pending_and_is_operator_verifiable(self, tmp_path, vault):
        """t7. Two properties the skip must have to avoid the recurring
        stuck-task class:

        * it must leave the tool OUT of ``_find_pending_dry_run_via_index``, or
          the 30s reconciler re-dry-runs it forever;
        * ``_is_operator_verify_skip`` must be True, so
          ``_complete_deferred_enables`` can finish an operator's already-approved
          "Enable & run" instead of parking the activity forever.
        """
        from systemu.pipelines.tool_dry_run import dry_run_tool
        from systemu.scheduler.jobs import _find_pending_dry_run_via_index
        from systemu.scheduler.tool_reconciler import _is_operator_verify_skip

        t = _make_tool("compact_cache", effect_tags=["local_delete"])
        t.implementation_path = _write_impl(
            tmp_path, "compact_cache.py",
            _sentinel_body(tmp_path / "SENTINEL_t7_unused.txt"))
        config = _config_for(tmp_path / "vault")

        result = dry_run_tool(t, vault=vault, config=config)
        assert result.status == "skipped"

        # Persist exactly as the reconciler does.
        t.dry_run_status = result.status
        t.dry_run_evidence = result.to_evidence()
        vault.save_tool(t)

        headers = vault.load_index("tools") or []
        assert any(h.get("id") == t.id for h in headers), "precondition: tool indexed"
        pending_ids = {h.get("id") for h in _find_pending_dry_run_via_index(headers)}
        assert t.id not in pending_ids, (
            "a gate-skipped tool must not stay pending, or the reconciler "
            "re-dry-runs it every 30s forever")

        assert _is_operator_verify_skip(t) is True

    def test_gate_verdict_is_carried_into_evidence(self, tmp_path, vault):
        """The verdict is persisted so the Tools page / operator can see WHY the
        dry-run was skipped rather than guessing from prose."""
        from systemu.pipelines.tool_dry_run import dry_run_tool

        t = _make_tool("rotate_logs", effect_tags=["local_delete"])
        t.implementation_path = _write_impl(
            tmp_path, "rotate_logs.py",
            _sentinel_body(tmp_path / "SENTINEL_evidence_unused.txt"))

        result = dry_run_tool(t, vault=vault, config=_config_for(tmp_path / "vault"))
        evidence = result.to_evidence()

        assert evidence["gate_verdict"] == "require_approval"
        assert evidence["operator_verify"] is True
        assert evidence["status"] == "skipped"


# ─────────────────────────────────────────────────────────────────────────────
# KNOWN GAP ratchet — the untagged-body source-scan evasion (see module docstring)

class TestKnownGap_UntaggedSourceScanIsNameMatching:
    """The gap the three closed holes do NOT cover, pinned as a strict xfail.

    For an UNTAGGED tool (every freshly-forged one) ``_gate_skip_reason`` scores
    from a ``classify_source`` AST scan. That scan is NAME-MATCHING: it tags a
    sink only when the receiver is literally spelled ``subprocess`` / ``os`` /
    a known net client. A body that reaches those sinks through an alias or a
    from-import scans as purely-local and EXECUTES UNATTENDED.

    These tests assert the DESIRED behaviour (``_gate_skip_reason`` returns a
    skip), so they FAIL today. ``strict=True`` is LOAD-BEARING: a non-strict
    xfail would silently xpass once the classifier is fixed and this gap-marker
    would rot into a lie. Strict means the fix hard-FAILS the suite here and
    forces the docstrings — module-level KNOWN GAP, ``_gate_skip_reason``,
    ``_net_egress_skip_reason``, ``_maybe_gate_tool`` — to be updated with it.

    Scope discipline: these assert CLASSIFICATION and the ``_gate_skip_reason``
    verdict ONLY. They never invoke ``dry_run_tool``, never spawn a subprocess,
    and never write outside ``tmp_path`` — the point is provable, so proving it
    does not require actually running a shell out of a committed test.
    """

    # Each body pairs an UNRECOGNISED sink with one ordinary local write, so the
    # scan's verdict is decided by the sinks it DOES see. Confirmed to scan as
    # exactly {local_write} for all three.
    EVASIONS = {
        "aliased_import": (
            "import subprocess as sp\n"
            "from pathlib import Path\n"
            "\n"
            "def run(x):\n"
            "    Path('out.txt').write_text('local')\n"
            "    sp.run(['echo', 'reached-a-shell'])\n"
            "    return {'success': True}\n"
        ),
        "from_import_subprocess": (
            "from subprocess import check_output\n"
            "from pathlib import Path\n"
            "\n"
            "def run(x):\n"
            "    Path('out.txt').write_text('local')\n"
            "    check_output(['echo', 'reached-a-shell'])\n"
            "    return {'success': True}\n"
        ),
        "from_import_os_system": (
            "from os import system\n"
            "from pathlib import Path\n"
            "\n"
            "def run(x):\n"
            "    Path('out.txt').write_text('local')\n"
            "    system('echo reached-a-shell')\n"
            "    return {'success': True}\n"
        ),
    }

    @staticmethod
    def _scan(body):
        from systemu.runtime.effect_tags import classify_source
        return {t.value if hasattr(t, "value") else str(t)
                for t in classify_source(body)}

    @pytest.mark.parametrize("case", sorted(EVASIONS))
    def test_reference_bodies_scan_as_purely_local(self, case):
        """PREMISE GUARD (passes today, and must keep passing until the fix).

        Pins the fact the ratchet rests on: each reference body scans as EXACTLY
        ``{local_write}`` — purely local — because the only sink the name-matcher
        recognises is the ``Path(...).write_text(...)``. If this ever changes,
        the xfail tests below stop probing what their docstrings claim, and this
        assertion says so loudly rather than letting them rot.
        """
        assert self._scan(self.EVASIONS[case]) == {"local_write"}

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "classify_source is name-matching: an aliased or from-imported "
            "subprocess/os sink is not recognised, so the body scans as "
            "purely-local. Fix = classifier alias resolution."
        ),
    )
    @pytest.mark.parametrize("case", sorted(EVASIONS))
    def test_classifier_should_see_the_shell_sink(self, case):
        """The CLASSIFICATION half of the gap.

        DESIRED: a body that calls into subprocess/os carries ``shell_exec``
        however the sink is spelled. TODAY: the sink is invisible to the scan.
        """
        assert "shell_exec" in self._scan(self.EVASIONS[case])

    def _untagged_tool(self, tmp_path, case):
        """An UNTAGGED forged tool (as ``tool_forge`` leaves it) whose body
        reaches a shell through an unrecognised name."""
        t = _make_tool(f"gap_probe_{case}", effect_tags=[])
        t.implementation_path = _write_impl(
            tmp_path, f"gap_probe_{case}.py", self.EVASIONS[case])
        return t, _config_for(tmp_path / "vault")

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "classify_source is name-matching: an aliased or from-imported "
            "subprocess/os/net sink scans purely-local, so an untagged forged "
            "body still executes unattended. Fix = classifier alias resolution; "
            "when that lands this xpasses and FORCES the docstrings + this test "
            "to be updated."
        ),
    )
    @pytest.mark.parametrize("case", sorted(EVASIONS))
    def test_untagged_body_with_aliased_shell_sink_should_be_skipped(
            self, tmp_path, case):
        """THE RATCHET. An untagged forged body reaching a shell via an alias /
        from-import must not execute unattended.

        Asserted against ``_net_egress_skip_reason`` — the guard that ACTUALLY
        decides this in the pipeline, and the one a classifier fix would flip.
        Its allowlist is ``_SAFE_LOCAL_TAGS`` (local_read/write/delete), which
        excludes ``shell_exec``, so the moment the scan tags the sink this
        returns a skip and the tool never reaches ``_execute``.

        DESIRED: a skip reason. TODAY: ``None`` (proceed) — the scan reports
        exactly ``{local_write}``, which is a subset of the allowlist.

        NOT asserted against ``_gate_skip_reason``: see the sibling test below
        for why that one would not flip on a classifier fix at all.
        """
        from systemu.pipelines.tool_dry_run import _net_egress_skip_reason

        t, config = self._untagged_tool(tmp_path, case)

        assert _net_egress_skip_reason(t, config) is not None, (
            "SECURITY GAP: an untagged forged body reaching subprocess/os "
            "through an aliased or from-imported name proved 'non-egress' and "
            "would execute during an unattended dry-run")

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "SECOND, INDEPENDENT gap: shell_exec is in action_governance's "
            "_LOCAL_TAGS and NOT in _APPROVAL_TAGS, so evaluate_action ALLOWs "
            "it. _gate_skip_reason therefore proceeds even for a CORRECTLY "
            "shell_exec-tagged body — fixing the classifier alone does NOT flip "
            "this. Fix = decide whether shell_exec should card at the action "
            "gate; when it does, this xpasses and forces a docs update."
        ),
    )
    @pytest.mark.parametrize("case", sorted(EVASIONS))
    def test_gate_scorer_should_also_skip_the_shell_body(self, tmp_path, case):
        """The gate-scorer half — a SEPARATE gap, pinned separately so a fix to
        one is never mistaken for a fix to the other.

        ``_gate_skip_reason`` delegates the verdict to ``evaluate_action``, which
        treats ``shell_exec`` as a LOCAL (ALLOW) effect. So this returns ``None``
        both today (sink invisible ⇒ ``{local_write}``) AND after a classifier
        fix (sink visible ⇒ ``{local_write, shell_exec}`` ⇒ still ALLOW). The
        dry-run is saved here only by ``_net_egress_skip_reason``'s stricter
        allowlist running FIRST in the pipeline — a single point of failure
        worth knowing about.
        """
        from systemu.pipelines.tool_dry_run import _gate_skip_reason

        t, config = self._untagged_tool(tmp_path, case)

        got = _gate_skip_reason(t, {"x": "hello"}, config,
                                is_destructive_param=False)

        assert got is not None, (
            "an untagged forged body reaching subprocess/os scored ALLOW at the "
            "action-gate scorer")
