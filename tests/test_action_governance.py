"""S1a — the universal action-governance evaluator (spec UNIFIED-v2 §5.7).

`evaluate_action(ActionContext) -> (Verdict, reason)` is the single deterministic
policy over every effectful call. This suite encodes the spec's §5.7 acceptance
criteria directly; it is the "first real test of the spec's core safety claim":

  * effect is derived PRIMARILY from the tool's EffectTags (G0) + target host;
    the name verb-map and is_destructive_param are POSITIVE-ONLY escalators;
    the self-declared HTTP method NEVER clears;
  * the OPEN vocabulary: UNKNOWN ⇒ gated (REQUIRE_APPROVAL), never refused —
    EXCEPT the two-band DENY floor (UNKNOWN ∩ a high-severity signal ⇒ DENY,
    an honest handoff, not a rubber-stampable card);
  * whole-token verb map (submit/post/upload/charge/file/issue/rsvp) with NO
    false-positive on a local tool named `send_summary_to_log`.

S1a is the evaluator only; wiring the live gates (`_maybe_gate_command`,
`_gate_mcp_call`, the forged path) to delegate through it is S1b.
"""
from __future__ import annotations

from systemu.runtime.action_governance import (
    ActionContext, Verdict, evaluate_action,
)
from systemu.runtime.effect_tags import EffectTag


def _v(**kw) -> Verdict:
    return evaluate_action(ActionContext(**kw))[0]


# --------------------------------------------------------------------------- #
# reversible-local majority → ALLOW (no friction regression)
# --------------------------------------------------------------------------- #

def test_reversible_local_read_allow():
    assert _v(tool="read_notes", effect_tags={EffectTag.LOCAL_READ.value}) == Verdict.ALLOW


def test_reversible_local_write_allow():
    assert _v(tool="save_draft", effect_tags={EffectTag.LOCAL_WRITE.value}) == Verdict.ALLOW


def test_net_read_allow():
    assert _v(tool="fetch_page", effect_tags={EffectTag.NET_READ.value}) == Verdict.ALLOW


# --------------------------------------------------------------------------- #
# known dangerous effects → REQUIRE_APPROVAL (approvable, not refused)
# --------------------------------------------------------------------------- #

def test_net_mutate_requires_approval():
    assert _v(tool="poster", effect_tags={EffectTag.NET_MUTATE.value}) == Verdict.REQUIRE_APPROVAL


def test_money_move_requires_approval():
    assert _v(tool="charge_card", effect_tags={EffectTag.MONEY_MOVE.value}) == Verdict.REQUIRE_APPROVAL


def test_local_delete_requires_approval():
    assert _v(tool="purge_cache", effect_tags={EffectTag.LOCAL_DELETE.value}) == Verdict.REQUIRE_APPROVAL


def test_send_message_requires_approval():
    assert _v(tool="emailer", effect_tags={EffectTag.SEND_MESSAGE.value}) == Verdict.REQUIRE_APPROVAL


# --------------------------------------------------------------------------- #
# shell_exec is an APPROVAL-band effect (coupled with the classifier's import-
# alias resolution). "local" describes a shell's EGRESS class, not its blast
# radius: a shell can run anything, so the gate must card it.
#
# Before this ratchet `shell_exec` was in `_LOCAL_TAGS` and ABSENT from
# `_APPROVAL_TAGS`, so `{shell_exec}` scored ALLOW. Once `classify_source`
# learned to resolve import aliases, forged shell bodies stopped scanning EMPTY
# (UNKNOWN ⇒ gated) and started scanning `{shell_exec}` (ALLOW ⇒ UNGATED) — a
# more accurate classifier made the gate LESS protective. These pin the fix.
# --------------------------------------------------------------------------- #

def test_shell_exec_requires_approval():
    assert _v(tool="exec_body", effect_tags={EffectTag.SHELL_EXEC.value}) == Verdict.REQUIRE_APPROVAL


def test_shell_exec_with_local_write_requires_approval():
    # the common forged shape: writes a file AND shells out. The benign local
    # companion tag must not dilute the shell escalation.
    assert _v(tool="exec_body", effect_tags={EffectTag.LOCAL_WRITE.value,
                                             EffectTag.SHELL_EXEC.value}) == Verdict.REQUIRE_APPROVAL


def test_shell_exec_with_net_read_requires_approval():
    # net_read alone is the frictionless majority (ALLOW); adding shell_exec
    # must still card.
    assert _v(tool="exec_body", effect_tags={EffectTag.NET_READ.value,
                                             EffectTag.SHELL_EXEC.value}) == Verdict.REQUIRE_APPROVAL


def test_shell_exec_stays_in_local_tags_so_the_name_map_cannot_escalate():
    """REGRESSION PIN: `shell_exec` must REMAIN in `_LOCAL_TAGS`.

    `_LOCAL_TAGS` is not "the safe tags" — it is the set `_effective_tags` uses
    to compute `local_only`, which SUPPRESSES the NAME verb-map. That
    suppression is the `send_summary_to_log` no-false-positive rule, and
    `local_delete` is already in BOTH sets for exactly this reason. Drop
    `shell_exec` from `_LOCAL_TAGS` and a shell tool named `run_deploy_script`
    or `send_report_via_shell` silently acquires NET_MUTATE / SEND_MESSAGE it
    never had.

    This asserts on the TAG SET, not on the verdict. The verdict is
    REQUIRE_APPROVAL either way now that `shell_exec` is an approval tag, so a
    verdict assertion here would pass for the wrong reason and prove nothing.
    """
    from systemu.runtime.action_governance import _effective_tags

    tags = _effective_tags(ActionContext(
        tool="send_report_and_update_index",   # "send" + "update" both in the verb map
        effect_tags={EffectTag.SHELL_EXEC.value, EffectTag.LOCAL_WRITE.value},
    ))

    assert EffectTag.SEND_MESSAGE.value not in tags, (
        "the name verb-map escalated a tool-side-local shell tool to SEND_MESSAGE "
        "— shell_exec was dropped from _LOCAL_TAGS")
    assert EffectTag.NET_MUTATE.value not in tags, (
        "the name verb-map escalated a tool-side-local shell tool to NET_MUTATE "
        "— shell_exec was dropped from _LOCAL_TAGS")
    # and it still cards, on the shell tag itself rather than on a phantom one
    assert _v(tool="send_report_and_update_index",
              effect_tags={EffectTag.SHELL_EXEC.value,
                           EffectTag.LOCAL_WRITE.value}) == Verdict.REQUIRE_APPROVAL


# --------------------------------------------------------------------------- #
# the two-band UNKNOWN rule (BLOCKER-1)
# --------------------------------------------------------------------------- #

def test_unknown_reversible_local_requires_approval():
    # unknown on a reversible/local target ⇒ gated, NOT refused
    assert _v(tool="mystery", effect_tags=set()) == Verdict.REQUIRE_APPROVAL
    assert _v(tool="mystery2", effect_tags={EffectTag.UNKNOWN.value}) == Verdict.REQUIRE_APPROVAL


def test_unknown_high_severity_denies_irreversible():
    # unclassifiable AND flagged irreversible ⇒ DENY (honest handoff)
    assert _v(tool="mystery", effect_tags={EffectTag.UNKNOWN.value}, irreversible=True) == Verdict.DENY


def test_unknown_high_severity_denies_destructive_param():
    # unclassifiable AND a destructive param (e.g. rm -rf) ⇒ DENY
    assert _v(tool="mystery", effect_tags=set(), is_destructive_param=True) == Verdict.DENY


def test_explicit_policy_deny():
    assert _v(tool="whatever", effect_tags={EffectTag.LOCAL_READ.value}, denied_by_policy=True) == Verdict.DENY


# --------------------------------------------------------------------------- #
# whole-token verb map + NO false positive (BLOCKER-1 / spec AC7)
# --------------------------------------------------------------------------- #

def test_actuation_verb_classifies_net_mutate_without_tags():
    # a forged submit_expense/open_issue with no tags + no incriminating param
    assert _v(tool="submit_expense", effect_tags=set()) == Verdict.REQUIRE_APPROVAL
    assert _v(tool="open_issue", effect_tags=set()) == Verdict.REQUIRE_APPROVAL
    assert _v(tool="upload_report", effect_tags=set()) == Verdict.REQUIRE_APPROVAL


def test_money_verb_classifies_money_move():
    assert _v(tool="charge_customer", effect_tags=set()) == Verdict.REQUIRE_APPROVAL


def test_no_false_positive_on_local_send_tool():
    # `send_summary_to_log` is a LOCAL write; the name token "send" must NOT
    # escalate it to SEND_MESSAGE when the tool-side tags say local-only.
    assert _v(tool="send_summary_to_log", effect_tags={EffectTag.LOCAL_WRITE.value}) == Verdict.ALLOW


# --------------------------------------------------------------------------- #
# network-reachable target + method-never-clears
# --------------------------------------------------------------------------- #

def test_network_target_forces_net_mutate():
    # a network-reachable target ⇒ NET_MUTATE unless operator-confirmed read-only
    assert _v(tool="call_api", effect_tags=set(), target="api.x.com",
              target_is_network=True) == Verdict.REQUIRE_APPROVAL


def test_operator_confirmed_read_only_network_allows():
    assert _v(tool="call_api", effect_tags={EffectTag.NET_READ.value}, target="api.x.com",
              target_is_network=True, operator_confirmed_read_only=True) == Verdict.ALLOW


def test_self_declared_get_does_not_clear():
    # a declared http_method=GET must NOT downgrade a network-reachable action
    assert _v(tool="sneaky", effect_tags=set(), target="api.x.com",
              target_is_network=True, http_method="GET") == Verdict.REQUIRE_APPROVAL


def test_untrusted_discovered_mcp_mutation_gated():
    # a discovered/registry/first-use MCP tool (classification_trusted=False) is
    # gated on an effectful call regardless of a self-declared read-only hint
    assert _v(tool="third_party_tool", effect_tags={EffectTag.NET_READ.value},
              classification_trusted=False, target_is_network=True) == Verdict.REQUIRE_APPROVAL


# --------------------------------------------------------------------------- #
# is_destructive_call stays POSITIVE-ONLY (escalate, never clear)
# --------------------------------------------------------------------------- #

def test_destructive_param_escalates_known_local():
    # a known local write with a destructive param escalates to approval
    assert _v(tool="run_command", effect_tags={EffectTag.SHELL_EXEC.value},
              is_destructive_param=True) == Verdict.REQUIRE_APPROVAL


def test_reason_string_present():
    verdict, reason = evaluate_action(ActionContext(tool="poster",
                                                    effect_tags={EffectTag.NET_MUTATE.value}))
    assert verdict == Verdict.REQUIRE_APPROVAL and isinstance(reason, str) and reason


# --------------------------------------------------------------------------- #
# S1b — close the trusted_inprocess bypass (§13.3)
# --------------------------------------------------------------------------- #

def test_requires_isolation_policy():
    from systemu.runtime.action_governance import requires_isolation
    assert requires_isolation({EffectTag.NET_MUTATE.value}) is True
    assert requires_isolation({EffectTag.MONEY_MOVE.value}) is True
    assert requires_isolation({EffectTag.SEND_MESSAGE.value}) is True
    assert requires_isolation({EffectTag.OAUTH_CALL.value}) is True
    # reversible/local + net_read are fine in-process (no ambient-secret egress risk)
    assert requires_isolation({EffectTag.LOCAL_WRITE.value}) is False
    assert requires_isolation({EffectTag.NET_READ.value}) is False
    assert requires_isolation(set()) is False


class _FakeTool:
    def __init__(self, *, forged, trusted, effect_tags):
        self.forged_by_systemu = forged
        self.trusted_inprocess = trusted
        self.effect_tags = effect_tags


def test_trusted_inprocess_bypass_closed_for_dangerous_forged_tool():
    from systemu.runtime.tool_sandbox import requires_subprocess_isolation
    # a forged tool the operator marked trusted_inprocess BUT that mutates
    # externally must STILL be isolated — trusted_inprocess is a speed grant,
    # not a governance grant.
    dangerous = _FakeTool(forged=True, trusted=True, effect_tags=[EffectTag.NET_MUTATE.value])
    assert requires_subprocess_isolation(dangerous) is True


def test_benign_trusted_inprocess_forged_tool_still_in_process():
    from systemu.runtime.tool_sandbox import requires_subprocess_isolation
    # unchanged behavior: a benign forged tool the operator trusted runs in-process
    benign = _FakeTool(forged=True, trusted=True, effect_tags=[EffectTag.LOCAL_WRITE.value])
    assert requires_subprocess_isolation(benign) is False


def test_forged_untrusted_still_isolated_unchanged():
    from systemu.runtime.tool_sandbox import requires_subprocess_isolation
    t = _FakeTool(forged=True, trusted=False, effect_tags=[])
    assert requires_subprocess_isolation(t) is True


def test_builtin_tool_unaffected_by_isolation_policy():
    from systemu.runtime.tool_sandbox import requires_subprocess_isolation
    # a built-in (repo code, not forged) with a network tag is trusted → in-process
    builtin = _FakeTool(forged=False, trusted=False, effect_tags=[EffectTag.NET_MUTATE.value])
    assert requires_subprocess_isolation(builtin) is False
