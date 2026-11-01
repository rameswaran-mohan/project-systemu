"""The three masks that fell behind the outbound push now run its rules.

Round 1 fixed ``gateway.mask_outbound``. Three other masks carried their own,
older rule sets and kept leaking what the push no longer leaks:

  1. ``event_pusher.translate_event`` (task_outcome) CUT the summary to 300
     characters before anything masked it (DEC-31). A JWT cut before its third
     segment, or a hex key cut below 40 characters, is a shape no rule knows, so
     the gateway's mask then let the head of it through.
  2. ``external_verifier._mask_evidence`` -- the per-call action-audit row's
     masker, and ``ledger.mask_and_digest_params``'s -- had no ``Basic``,
     ``Token`` or URL-userinfo rule, so ``Authorization: Basic <b64>`` under a
     neutral parameter name reached ``audit/actions.jsonl`` in clear.
  3. ``decision_bridge._mask`` (``messaging/resolve_audit.jsonl``) dropped a value
     only when it carried a hint word; ``Authorization: Basic ...`` carries none.

Each now calls the shared primitive ``gateway.mask_secret_spans`` (no copy of
its rules). Every check here reads what actually left the process or reached
disk: the sent push, the bytes of every file under the vault, the resolve-audit
file. Fixture rule (DEC-31): each secret is longer than every cap on its path,
read LIVE from the module that owns the cap, and a leak is any 10-character
window of the secret surviving.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.redaction_fixtures import secret_longer_than

_WINDOW = 10


def _leaks(blob: str, secret: str) -> list:
    return [secret[i:i + _WINDOW] for i in range(len(secret) - _WINDOW + 1)
            if secret[i:i + _WINDOW] in blob]


def _assert_no_leak(blob: str, secret: str) -> None:
    found = _leaks(blob, secret)
    assert not found, f"credential bytes survived: {found[:3]}"


def _token68_longer_than(n: int) -> str:
    """A shapeless credential (letters, digits, token68 punctuation; no hex run,
    no sk-/JWT/ghp_/AKIA prefix) strictly longer than ``n``."""
    unit = "Zq7aB3cD4eF5gH6iJ7kL8mN9oP0qR1sT2uV3wX4yZ5-._~"
    out = unit
    while len(out) <= n:
        out += unit
    return out


def _basic_longer_than(n: int) -> str:
    pw = "Corr3ct-H0rse_Battery.Staple~2026"
    raw = "svc-deploy:" + pw
    while len(base64.b64encode(raw.encode())) <= n:
        raw += pw
    return base64.b64encode(raw.encode()).decode()


def test_fixture_builders_are_shapeless_and_long():
    from systemu.messaging import gateway as gw
    t, b = _token68_longer_than(300), _basic_longer_than(300)
    assert len(t) > 300 and len(b) > 300
    for s in (t, b):
        for rx in gw._TOKEN_SHAPE_RES:
            assert not rx.search(s), rx.pattern
        assert gw.mask_secret_spans(s) == s


# ═════════════════════════════════════════════════════════════════════════════
#  1. the task_outcome push: MASK, then cap
# ═════════════════════════════════════════════════════════════════════════════

_HEX64 = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"


def _summary_cap() -> int:
    from systemu.messaging.event_pusher import _SUMMARY_CAP
    return _SUMMARY_CAP


def _straddlers():
    cap = _summary_cap()
    return [
        # (id, secret, offset): every secret STARTS before the cap and ENDS after
        # it; 30 characters of it fall inside, too few for any shape rule.
        ("jwt-longer-than-cap", secret_longer_than(cap, kind="jwt"), cap - 30),
        ("hex-longer-than-cap", secret_longer_than(cap, kind="hex"), cap - 30),
        ("jwt-realistic", secret_longer_than(64, kind="jwt"), cap - 30),
        ("hex64-realistic", _HEX64, cap - 30),
    ]


def _outcome_event(summary: str) -> dict:
    return {"category": "task_outcome", "level": "SUCCESS",
            "message": "Quick task finished", "context": {},
            "details": {"summary": summary}}


def _straddling_summary(secret: str, offset: int) -> str:
    # ends in a space: the shape rules anchor on a word boundary, and a secret
    # glued to a word is a different (shape-rule) question than the cap order
    prose = ("Report ready for the quarterly review. " * 20)[:offset - 1] + " "
    assert len(prose) == offset
    return prose + secret + " and that is the end of the summary."


def _send_through_telegram(message):
    from systemu.messaging.telegram_gateway import TelegramGateway
    gw = TelegramGateway(bot_token="123:test-not-a-real-token", allowlist={"42"})
    gw._app = object()
    sent = []
    gw._send_to = lambda uid, msg: sent.append(msg)
    gw.push(message)
    assert len(sent) == 1
    return sent[0]


@pytest.mark.parametrize("fid,secret,offset", _straddlers(), ids=[s[0] for s in _straddlers()])
def test_task_outcome_summary_is_masked_before_it_is_cut(fid, secret, offset):
    from systemu.messaging.event_pusher import translate_event
    assert offset < _summary_cap() < offset + len(secret)       # it straddles the cap
    msg = translate_event(_outcome_event(_straddling_summary(secret, offset)))
    _assert_no_leak(msg.text, secret)                             # the composed push
    _assert_no_leak(_send_through_telegram(msg).text, secret)     # what is sent
    assert "Report ready for the quarterly review." in msg.text


def test_task_outcome_summary_is_still_capped():
    from systemu.messaging.event_pusher import translate_event
    cap = _summary_cap()
    msg = translate_event(_outcome_event("y" * (cap * 3)))
    assert "y" * cap in msg.text
    assert "y" * (cap + 1) not in msg.text


# ═════════════════════════════════════════════════════════════════════════════
#  2. the per-call audit write and the ledger
# ═════════════════════════════════════════════════════════════════════════════

def _audit_cap() -> int:
    from systemu.runtime.shadow_runtime import _AUDIT_PARAM_VALUE_CAP
    return int(_AUDIT_PARAM_VALUE_CAP)


def _audit_secrets():
    cap = _audit_cap()
    token, basic = _token68_longer_than(cap), _basic_longer_than(cap)
    url_pw = _token68_longer_than(cap)
    params = {   # every key NEUTRAL: only the value pass can catch these
        "note": f"Authorization: Basic {basic}",
        "hdr": f"Token {token}",
        "endpoint": f"https://svc:{url_pw}@api.example.com/v1/items",
        "proxy": f"Proxy-Authorization: Basic {basic}",
    }
    return params, (token, basic, url_pw)


def _real_vault(tmp_path):
    from systemu.storage.file_vault import FileVault
    from systemu.vault.vault import Vault
    return FileVault(Vault(str(tmp_path / "vault")))


def _all_bytes_under(*roots: Path) -> str:
    chunks = []
    for root in roots:
        for p in sorted(Path(root).rglob("*")):
            if p.is_file():
                chunks.append(p.read_bytes().decode("utf-8", errors="backslashreplace"))
    return "\n".join(chunks)


def test_mask_and_digest_params_masks_every_scheme_spelling():
    from systemu.runtime import ledger
    params, secrets = _audit_secrets()
    masked, digest = ledger.mask_and_digest_params(params)
    blob = json.dumps(masked)
    for s in secrets:
        _assert_no_leak(blob, s)
    assert masked["note"] == "Authorization: Basic ***"
    assert masked["hdr"] == "Token ***"
    assert masked["endpoint"] == "https://svc:***@api.example.com/v1/items"
    assert len(digest) == 64 and digest == ledger._digest(masked)


def test_the_per_call_audit_write_persists_no_credential(tmp_path):
    """The chat lane's real writer (``quick_task._audit_tool_success`` ->
    ``_truncate_audit_params`` -> ``audit_log.append_action``) on a real vault;
    every byte under the vault is read back from disk."""
    from systemu.pipelines.quick_task import _audit_tool_success
    params, secrets = _audit_secrets()
    v = _real_vault(tmp_path)
    _audit_tool_success(v, "quick_p22", "call_api", params, None)
    on_disk = _all_bytes_under(tmp_path / "vault")
    assert "call_api" in on_disk, "the audit row was not written"
    assert "Authorization: Basic ***" in on_disk
    for s in secrets:
        _assert_no_leak(on_disk, s)


def test_the_workflow_audit_entry_masks_before_it_caps():
    from systemu.runtime.shadow_runtime import _build_tool_audit_entry
    params, secrets = _audit_secrets()
    entry = _build_tool_audit_entry(execution_id="exec_p22", objective_id=0,
                                    tool_name="call_api", params=params)
    blob = json.dumps(entry)
    for s in secrets:
        _assert_no_leak(blob, s)


def test_the_ledger_projects_and_exports_no_credential_from_a_raw_row(tmp_path):
    """A row written RAW (a pre-fix row, or a writer that skipped the per-call
    mask) is masked again by ``mask_and_digest_params`` when the ledger projects
    it, and no export file carries it."""
    from systemu.runtime import audit_log, ledger, ledger_export
    params, secrets = _audit_secrets()
    v = _real_vault(tmp_path)
    audit_log.append_action(getattr(v, "_v", v), execution_id="quick_raw",
                            objective_id=0, action="call_api", params=params,
                            success=True)
    rows = ledger.project(v, data_dir=tmp_path)
    assert len(rows) == 1
    raw_beside = json.dumps(rows[0].raw_beside)
    res = ledger_export.write_export(v, fmt="jsonl", data_dir=tmp_path,
                                     dest_dir=tmp_path / "out")
    exported = Path(res["destination_path"]).read_text(encoding="utf-8")
    for s in secrets:
        _assert_no_leak(raw_beside, s)
        _assert_no_leak(exported, s)


# ═════════════════════════════════════════════════════════════════════════════
#  3. the resolve-audit row: an operator reply with no hint word
# ═════════════════════════════════════════════════════════════════════════════

SENDER = "111"


class _Bus:
    def __init__(self):
        self.events = []

    def publish(self, event):
        self.events.append(event)

    def subscribe(self, cb, replay=False):
        return lambda: None


@pytest.fixture
def chat(tmp_path, monkeypatch):
    from systemu.approval.decision_queue import OperatorDecisionQueue
    from systemu.interface import notifications
    from systemu.interface.event_bus import EventBus
    from systemu.messaging import decision_bridge as db
    from systemu.storage.file_vault import FileVault
    from systemu.vault.vault import Vault

    vault = FileVault(Vault(str(tmp_path / "v")))
    queue = OperatorDecisionQueue(vault)
    bus = _Bus()
    monkeypatch.setattr(EventBus, "get", classmethod(lambda cls: bus))
    monkeypatch.setattr(db, "_default_queue", lambda: queue)
    monkeypatch.setattr(notifications, "_decision_queue_instance", queue)
    monkeypatch.setattr(notifications, "_event_log_path", None)
    monkeypatch.setenv("SHARING_ON_TELEGRAM_ALLOWED_USER_IDS", SENDER)
    monkeypatch.setenv("SHARING_ON_MESSAGING_DECISION_RESOLUTION", "on")
    monkeypatch.setenv("SHARING_ON_MESSAGING_PUSH_DETAIL", "summary")
    monkeypatch.setattr(db, "_rate_hits", {})
    return SimpleNamespace(vault=vault, queue=queue, bus=bus, root=tmp_path / "v")


def _ask_free_text(chat):
    """Drive the real elicitation rail; return the pushed decision's tag."""
    from systemu.approval.exceptions import PendingChoiceRequest
    from systemu.runtime.elicitation import (elicitation_schema_from_fields,
                                             resolve_structured_input)
    schema = elicitation_schema_from_fields([{"name": "answer", "type": "string"}])
    with pytest.raises(PendingChoiceRequest) as exc:
        resolve_structured_input(message="Paste the header you used", requested_schema=schema)
    did = exc.value.decision_id
    posted = [e for e in chat.bus.events
              if e.get("category") == "operator_decision_posted"
              and (e.get("context") or {}).get("decision_id") == did]
    assert len(posted) == 1
    return did, posted[0]["context"]["tag"]


def _answer(text):
    from systemu.messaging.gateway import dispatch, parse_command
    from systemu.messaging.handlers import default_handlers
    return dispatch(parse_command(text, user_id=SENDER, platform="telegram"),
                    default_handlers())


def _resolve_audit_text(chat) -> str:
    p = chat.root / "messaging" / "resolve_audit.jsonl"
    assert p.is_file(), "no resolve-audit row was written"
    return p.read_bytes().decode("utf-8", errors="backslashreplace")


def _replies():
    cap = 200  # decision_bridge._ECHO_CAP, the longest cap on the reply path
    basic, pw = _basic_longer_than(cap), _token68_longer_than(cap)
    return [
        ("authorization-basic", f"Authorization: Basic {basic}", basic, "Authorization: Basic ***"),
        ("url-userinfo", f"https://svc:{pw}@api.example.com/v1", pw, "https://svc:***@api.example.com/v1"),
    ]


@pytest.mark.parametrize("fid,reply,secret,expect", _replies(), ids=[r[0] for r in _replies()])
def test_an_operator_reply_with_no_hint_word_is_masked_on_disk(chat, fid, reply, secret, expect):
    from systemu.messaging import decision_bridge as db
    assert not db._SECRET_HINT.search(reply), "the fixture must carry NO hint word"
    did, tag = _ask_free_text(chat)
    _answer(f"/answer {tag} {reply}")
    on_disk = _resolve_audit_text(chat)
    rows = [json.loads(l) for l in on_disk.splitlines() if l]
    assert [r["outcome"] for r in rows] == ["OK"]
    assert rows[0]["decision_id"] == did
    assert expect in rows[0]["mapped_choice"]
    _assert_no_leak(on_disk, secret)


def test_an_ordinary_reply_stays_readable_in_the_audit_row(chat):
    did, tag = _ask_free_text(chat)
    _answer(f"/answer {tag} 9 copies, stapled")
    rows = [json.loads(l) for l in _resolve_audit_text(chat).splitlines() if l]
    assert json.loads(rows[0]["mapped_choice"]) == {"answer": "9 copies, stapled"}


def test_a_value_the_primitive_cannot_mask_is_dropped(monkeypatch):
    """Fail closed: this row goes to disk."""
    from systemu.messaging import decision_bridge as db
    from systemu.messaging import gateway as gw

    def _boom(text):
        raise RuntimeError("primitive down")
    monkeypatch.setattr(gw, "mask_secret_spans", _boom)
    assert db._mask("Authorization: Basic abcdefghijklmnop") == "***"
    assert db._mask(7) == 7
