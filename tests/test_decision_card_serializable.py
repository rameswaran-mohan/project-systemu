"""Defect E — the tools_blocked / harness decision card must never carry a
Python callable into a NiceGUI element prop (orjson crashes Outbox._emit with
'Type ... is not JSON serializable: function'). These tests pin both the pure
card-boundary guard and the render-time json_editor payloads."""
import orjson
import systemu.interface.pages.insights as insights_mod


def _leak():
    return "I am a function"


def test_sanitize_card_payload_neutralizes_nested_callable():
    card = {
        "id": "dec_x", "title": "Task blocked — 1 tool(s) not ready", "body": "b",
        "options": ["Dismiss", "Enable & run"],
        "dedup_key": "tools_blocked:act_1",
        "context": {"kind": "gate", "gate_type": "tools_blocked",
                    "tool_ids": ["t1"], "activity_id": "act_1",
                    "spec": {"name": "encrypt_docx", "impl": _leak}},
    }
    safe = insights_mod.sanitize_card_payload(card)
    # The whole sanitized payload must orjson-serialize — this is what _emit does.
    orjson.dumps(safe)  # must not raise
    # The callable degraded to a string, not silently dropped into a crash.
    assert isinstance(safe["context"]["spec"]["impl"], str)
    # Non-callable fields preserved verbatim.
    assert safe["options"] == ["Dismiss", "Enable & run"]
    assert safe["context"]["tool_ids"] == ["t1"]


def test_sanitize_card_payload_passes_through_non_dict():
    assert insights_mod.sanitize_card_payload("nope") == "nope"
    assert insights_mod.sanitize_card_payload(None) is None


# ── render-time tests: drive render_decision_card with a NiceGUI stub and assert
#    every json_editor `properties` payload orjson-serializes (== Outbox._emit) ──

class _FakeElement:
    """Chainable no-op element that is also a context manager."""
    def __getattr__(self, name):
        return lambda *a, **k: self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeUi:
    """Records every ``ui.json_editor`` payload; every other ``ui.*`` is inert."""
    def __init__(self):
        self.json_editor_props = []

    def json_editor(self, properties, *a, **k):
        self.json_editor_props.append(properties)
        return _FakeElement()

    def __getattr__(self, name):
        return lambda *a, **k: _FakeElement()


class _FakeQueue:
    def resolve(self, *a, **k):
        pass

    def resolve_with_context_patch(self, *a, **k):
        pass


def _harness_card_with_callable():
    def _impl():
        return "callable in spec"
    return {
        "id": "dec_h", "title": "Harness request: tool [hreq_1]", "body": "?",
        "options": ["Deny", "Approve"],
        "dedup_key": "harness:e1:hreq_1",
        "context": {"kind": "gate", "gate_type": "harness", "harness_kind": "tool",
                    "execution_id": "e1", "activity_id": "act_e1", "shadow_id": "sh_e1",
                    "request_id": "hreq_1", "risk_band": "high",
                    "spec": {"name": "encrypt_docx", "impl": _impl}},
    }


def _tools_blocked_card_with_callable():
    def _cb():
        return "x"
    return {
        "id": "dec_tb", "title": "Task blocked — 2 tool(s) not ready",
        "body": "Task: report.docx", "options": ["Dismiss", "Enable & run"],
        "dedup_key": "tools_blocked:act_docx",
        "context": {"kind": "gate", "gate_type": "tools_blocked",
                    "tool_ids": ["hash_tool", "encrypt_docx"], "activity_id": "act_docx",
                    "stray": _cb},
    }


def test_harness_card_json_editor_payloads_serializable(monkeypatch):
    fake = _FakeUi()
    monkeypatch.setattr(insights_mod, "ui", fake)
    # Must not raise; capture every json_editor payload built during render.
    insights_mod.render_decision_card(_harness_card_with_callable(), _FakeQueue(), lambda: None)
    assert fake.json_editor_props, "render built no json_editor for the harness card"
    for props in fake.json_editor_props:
        orjson.dumps(props)  # the exact thing Outbox._emit does — must not raise


def test_tools_blocked_card_renders_without_orjson_crash(monkeypatch):
    fake = _FakeUi()
    monkeypatch.setattr(insights_mod, "ui", fake)
    insights_mod.render_decision_card(_tools_blocked_card_with_callable(), _FakeQueue(), lambda: None)
    for props in fake.json_editor_props:
        orjson.dumps(props)  # must not raise — this is the 5/5 crash in the log
