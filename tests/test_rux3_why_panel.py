"""R-UX3 / UX-14 — the "why?" explain affordance (SPEC Part II 15-UX, AC-U14).

The load-bearing property is HONESTY, not coverage. An explain surface that
sounds confident about something it did not observe is worse than no explain
surface at all, so the pins below are weighted toward what the panel must
REFUSE to say:

* a card that never passed through the effect gate must NOT render as "allowed"
  or as having no effects — it must say the verdict was never recorded;
* empty effect tags must render as "unclassified" (which is what the gate
  itself treats them as), never as "none"/"safe";
* whether this tool signature was approved before is NOT on the card, so the
  panel must declare that unknown instead of inferring it from the absence of
  an approval.

And the hard rule from the spec: "why?" is READ-ONLY. It never re-runs
``evaluate_action`` or any resolution. Pinned by making ``evaluate_action``
explode and asserting the panel is unaffected.

NOT covered here (stated plainly): the inline expansion widget itself is a
NiceGUI element and is not exercised; only the pure explanation model and its
text render are.
"""
from __future__ import annotations

import pytest


# A tool gate context as ACTUALLY persisted: GateDescriptor.to_decision_context()
# merged with tool_sandbox's _resume_extras (see InboxQueue.enqueue).
def _tool_gate_ctx(**over):
    ctx = {
        "kind": "gate",
        "gate_type": "tool",
        "risk": "medium",
        "inspect": "tool: send_invoice\neffects: send_message\nverdict: require_approval",
        "safe_default": "Deny",
        "what_approve_does": "Runs the 'send_invoice' tool (send_message).",
        "tool_id": "",
        "tool_signature": "a1b2c3d4e5",
        "tool_name": "send_invoice",
        "verdict": "require_approval",
        "effect_tags": ["send_message"],
        "destructive": False,
        "gate_reason": "approval-band effect (network mutation / message / money)",
    }
    ctx.update(over)
    return ctx


def _decision(ctx, **over):
    d = {
        "id": "dec_1",
        "title": "Run tool: send_invoice",
        "body": "",
        "options": ["Deny", "Approve once", "Always allow"],
        "context": ctx,
        "dedup_key": "tool:a1b2c3d4e5",
        "status": "pending",
    }
    d.update(over)
    return d


# ── the deterministic gate path (AC-U14 headline) ───────────────────────────

class TestGateWhy:
    def test_renders_verdict_reason_and_tags(self):
        """AC-U14 headline: the deterministic fields render with NO provider."""
        from systemu.interface.components.why_panel import explain
        ex = explain(_decision(_tool_gate_ctx()))
        blob = ex.as_text().lower()
        assert "require_approval" in blob or "approval" in blob
        assert "send_message" in blob
        assert "approval-band effect" in blob

    def test_renders_the_tool_signature(self):
        from systemu.interface.components.why_panel import explain
        ex = explain(_decision(_tool_gate_ctx()))
        assert "a1b2c3d4e5" in ex.as_text()

    def test_destructive_parameter_is_surfaced_when_set(self):
        from systemu.interface.components.why_panel import explain
        ex = explain(_decision(_tool_gate_ctx(destructive=True)))
        assert "destructive" in ex.as_text().lower()

    def test_reclassification_is_surfaced_as_single_use(self):
        from systemu.interface.components.why_panel import explain
        ex = explain(_decision(_tool_gate_ctx(
            reclassified=True, assigned_class="net_mutate")))
        blob = ex.as_text().lower()
        assert "reclassif" in blob
        assert "net_mutate" in blob
        assert "single-use" in blob or "single use" in blob


# ── the honesty pins ────────────────────────────────────────────────────────

class TestHonesty:
    def test_missing_verdict_is_not_recorded_never_allowed(self):
        """A card with no persisted verdict must not read as permissive.

        Forge/dep/evolution/recovery gates never call ``evaluate_action``, so
        their context has no verdict. Rendering that as "allowed" — or simply
        omitting it — would tell the operator something we never observed.
        """
        from systemu.interface.components.why_panel import explain
        ctx = {"kind": "gate", "gate_type": "forge", "risk": "high"}
        ex = explain(_decision(ctx))
        blob = ex.as_text().lower()
        assert "not recorded" in blob
        assert "allow" not in blob, "an unscored card must never read as allowed"
        assert any("verdict" in u.lower() for u in ex.unknowns)

    def test_a_non_gate_card_says_it_skipped_the_effect_gate(self):
        from systemu.interface.components.why_panel import explain
        ex = explain(_decision({"kind": "question"}))
        assert "effect gate" in ex.as_text().lower()

    def test_empty_effect_tags_render_as_unclassified(self):
        """The gate treats an empty tag set as UNKNOWN — so must the panel.

        "no effects recorded" would read as harmless; it is the opposite.
        """
        from systemu.interface.components.why_panel import explain
        ex = explain(_decision(_tool_gate_ctx(effect_tags=[])))
        blob = ex.as_text().lower()
        assert "unclassified" in blob
        assert "no effects" not in blob

    def test_missing_reason_is_declared_not_invented(self):
        from systemu.interface.components.why_panel import explain
        ctx = _tool_gate_ctx()
        ctx.pop("gate_reason")
        ex = explain(_decision(ctx))
        assert "not recorded" in ex.as_text().lower()
        assert any("reason" in u.lower() for u in ex.unknowns)

    def test_prior_approval_history_is_declared_unknown(self):
        """The spec wants a signature STATUS ("new signature" / "re-forged body
        invalidated the old approval"). That history is NOT on the persisted
        card, and inferring "new signature" from the absence of an approval
        would be a proxy dressed up as a fact. The panel must say so.
        """
        from systemu.interface.components.why_panel import explain
        ex = explain(_decision(_tool_gate_ctx()))
        joined = " ".join(ex.unknowns).lower()
        assert "signature" in joined
        assert "not recorded" in joined or "unknown" in joined

    def test_panel_never_claims_a_reason_it_did_not_read(self):
        """Sanity: the reason rendered is the persisted one, verbatim."""
        from systemu.interface.components.why_panel import explain
        ex = explain(_decision(_tool_gate_ctx(gate_reason="explicit policy denial")))
        assert "explicit policy denial" in ex.as_text()


# ── the read-only hard rule ─────────────────────────────────────────────────

class TestReadOnly:
    def test_explain_never_reruns_evaluate_action(self, monkeypatch):
        from systemu.runtime import action_governance
        from systemu.interface.components.why_panel import explain

        def _boom(*a, **k):
            raise AssertionError("why? re-ran the gate — it must be read-only")

        monkeypatch.setattr(action_governance, "evaluate_action", _boom)
        ex = explain(_decision(_tool_gate_ctx()))
        assert ex.as_text()

    def test_explain_does_not_mutate_the_decision(self):
        import copy
        from systemu.interface.components.why_panel import explain
        d = _decision(_tool_gate_ctx())
        before = copy.deepcopy(d)
        explain(d)
        assert d == before


# ── ask cards ───────────────────────────────────────────────────────────────

class TestAskWhy:
    def test_renders_requirement_kind_schema_path_and_source(self):
        from systemu.interface.components.why_panel import explain
        ctx = {
            "kind": "ask",
            "requirement": {
                "kind": "input", "schema_path": "/message/subject",
                "state": "missing", "source": "schema",
                "rationale": "required by the capability schema",
            },
        }
        blob = explain(_decision(ctx)).as_text()
        assert "input" in blob
        assert "/message/subject" in blob
        assert "schema" in blob

    def test_renders_attempted_resolutions_when_recorded(self):
        from systemu.interface.components.why_panel import explain
        ctx = {
            "kind": "ask",
            "requirement": {"kind": "input", "schema_path": "/x",
                            "state": "missing", "source": "schema"},
            "attempted": ["inventory: no match", "operator profile: no match"],
        }
        blob = explain(_decision(ctx)).as_text()
        assert "inventory: no match" in blob

    def test_absent_attempted_resolutions_are_declared(self):
        """Silence about what was tried must not read as "nothing was tried"."""
        from systemu.interface.components.why_panel import explain
        ctx = {"kind": "ask",
               "requirement": {"kind": "input", "schema_path": "/x",
                               "state": "missing", "source": "schema"}}
        ex = explain(_decision(ctx))
        assert any("attempt" in u.lower() for u in ex.unknowns)


# ── console safety ──────────────────────────────────────────────────────────

class TestAsciiOnly:
    @pytest.mark.parametrize("ctx", [
        _tool_gate_ctx(),
        _tool_gate_ctx(effect_tags=[], destructive=True),
        {"kind": "gate", "gate_type": "forge"},
        {"kind": "ask", "requirement": {"kind": "input", "schema_path": "/x",
                                        "state": "missing", "source": "schema"}},
    ])
    def test_render_is_ascii(self, ctx):
        """cp1252 is the stock Windows console encoding — /why goes to a chat
        transport and a CLI, so a stray glyph is a UnicodeEncodeError."""
        from systemu.interface.components.why_panel import explain
        text = explain(_decision(ctx)).as_text()
        text.encode("cp1252")
        assert all(ord(c) < 128 for c in text), \
            [c for c in text if ord(c) >= 128]


# ── Telegram /why <tag> (PAR-2 parity) ──────────────────────────────────────

class TestWhyCommand:
    def test_registered_in_the_default_handlers(self):
        from systemu.messaging.handlers import default_handlers
        assert "why" in default_handlers()

    def test_listed_in_help(self):
        from systemu.messaging.handlers import handle_help
        from systemu.messaging.gateway import InboundCommand
        assert "/why" in handle_help(InboundCommand(user_id="u", command="help", args=""))

    def test_help_is_cp1252_safe(self):
        """NOT pure-ASCII: the existing help text already uses em dashes, which
        cp1252 encodes fine. The real bar is "the stock Windows console can
        print it" — an arrow (U+2192) could not."""
        from systemu.messaging.handlers import handle_help
        from systemu.messaging.gateway import InboundCommand
        handle_help(InboundCommand(user_id="u", command="help", args="")).encode("cp1252")

    def test_missing_tag_returns_usage(self):
        from systemu.messaging.handlers import handle_why
        from systemu.messaging.gateway import InboundCommand
        out = handle_why(InboundCommand(user_id="u", command="why", args=""))
        assert "usage" in out.lower()

    def test_unknown_tag_is_honest(self, monkeypatch):
        from systemu.messaging import handlers
        from systemu.messaging.gateway import InboundCommand
        monkeypatch.setattr(handlers, "_why_lookup", lambda tag: None)
        out = handlers.handle_why(InboundCommand(user_id="u", command="why", args="zzzzzz"))
        assert "zzzzzz" in out
        assert "no " in out.lower() or "not " in out.lower()

    def test_known_tag_returns_the_same_text_as_the_panel(self, monkeypatch):
        from systemu.messaging import handlers
        from systemu.messaging.gateway import InboundCommand
        from systemu.interface.components.why_panel import explain

        dec = _decision(_tool_gate_ctx())
        monkeypatch.setattr(handlers, "_why_lookup", lambda tag: dec)
        out = handlers.handle_why(InboundCommand(user_id="u", command="why", args="abc123"))
        assert out == explain(dec).as_text()
