"""G0 — EffectTag vocabulary + AST source classifier + legacy backfill.

The EffectTag vocabulary is the shared, EXTENSIBLE effect language every later
gate/verifier consumes (spec UNIFIED-v2 §5.7). Key properties under test:
  - an open vocabulary: an unrecognized effect coerces to UNKNOWN (gated, never
    rejected — Callout 2), and callers may register new tags at runtime;
  - a high-severity predicate backing the §5.7 two-band DENY floor;
  - a deterministic AST classifier over tool source (network / shell / local
    read/write/delete sinks), where ABSENCE of a tag is never "no effect"
    (unparseable source ⇒ UNKNOWN);
  - a one-pass, idempotent vault backfill that stamps effect_tags onto every
    pre-existing tool body from its implementation source.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from systemu.runtime import effect_tags as et
from systemu.runtime.effect_tags import EffectTag


# --------------------------------------------------------------------------- #
# vocabulary + open-world coercion + high-severity predicate
# --------------------------------------------------------------------------- #

def test_canonical_vocabulary_present():
    values = {t.value for t in EffectTag}
    assert {
        "local_read", "local_write", "local_delete", "shell_exec",
        "net_read", "net_mutate", "send_message", "money_move",
        "oauth_call", "unknown",
    } <= values


def test_coerce_known_and_unknown():
    assert et.coerce("net_mutate") == EffectTag.NET_MUTATE.value
    assert et.coerce(EffectTag.MONEY_MOVE) == "money_move"
    # open-world: an unrecognized effect is UNKNOWN, never an error
    assert et.coerce("frobnicate_the_widget") == EffectTag.UNKNOWN.value
    assert et.coerce("") == EffectTag.UNKNOWN.value
    assert et.coerce(None) == EffectTag.UNKNOWN.value


def test_register_extension_tag_is_recognized():
    # a tier/LLM may PROPOSE a new effect class at runtime; determinism then
    # classifies + gates it rather than refusing the plan.
    et.register_effect_tag("device_actuate", high_severity=True)
    assert et.coerce("device_actuate") == "device_actuate"
    assert et.is_high_severity("device_actuate") is True


def test_high_severity_predicate():
    assert et.is_high_severity(EffectTag.MONEY_MOVE) is True
    assert et.is_high_severity(EffectTag.NET_MUTATE) is True
    assert et.is_high_severity(EffectTag.LOCAL_DELETE) is True
    assert et.is_high_severity(EffectTag.SEND_MESSAGE) is True
    assert et.is_high_severity(EffectTag.LOCAL_READ) is False
    assert et.is_high_severity(EffectTag.NET_READ) is False
    # UNKNOWN alone is NOT high-severity — the §5.7 DENY floor is UNKNOWN *plus*
    # a separately-detected high-severity signal, not UNKNOWN by itself.
    assert et.is_high_severity(EffectTag.UNKNOWN) is False


# --------------------------------------------------------------------------- #
# AST source classifier
# --------------------------------------------------------------------------- #

def _tags(code: str) -> set[str]:
    return {t.value for t in et.classify_source(code)}


def test_classify_network_read_vs_mutate_by_method():
    assert EffectTag.NET_READ.value in _tags("import requests\nrequests.get('http://x')")
    assert EffectTag.NET_MUTATE.value in _tags("import requests\nrequests.post('http://x', json={})")
    assert EffectTag.NET_MUTATE.value in _tags("import httpx\nhttpx.put('http://x')")
    # a benignly-named tool that POSTs is still NET_MUTATE (no incriminating name needed)
    assert EffectTag.NET_MUTATE.value in _tags(
        "import requests\ndef run(**k):\n    return requests.post('https://api/x', json=k)"
    )


def test_classify_session_post_via_attribute_chain():
    # self.session.post(...) — receiver is not a bare module name
    code = "class T:\n    def run(self):\n        return self.session.post('http://x')"
    assert EffectTag.NET_MUTATE.value in _tags(code)


def test_classify_shell_exec():
    assert EffectTag.SHELL_EXEC.value in _tags("import os\nos.system('ls')")
    assert EffectTag.SHELL_EXEC.value in _tags("import subprocess\nsubprocess.run(['ls'])")
    assert EffectTag.SHELL_EXEC.value in _tags("import os\nos.popen('ls')")


def test_classify_local_delete():
    assert EffectTag.LOCAL_DELETE.value in _tags("import os\nos.remove('/tmp/x')")
    assert EffectTag.LOCAL_DELETE.value in _tags("import shutil\nshutil.rmtree('/tmp/x')")
    assert EffectTag.LOCAL_DELETE.value in _tags("from pathlib import Path\nPath('x').unlink()")


def test_classify_local_write_vs_read_by_open_mode():
    assert EffectTag.LOCAL_WRITE.value in _tags("open('/tmp/x', 'w')")
    assert EffectTag.LOCAL_WRITE.value in _tags("open('/tmp/x', mode='a')")
    assert EffectTag.LOCAL_WRITE.value in _tags("from pathlib import Path\nPath('x').write_text('y')")
    read = _tags("open('/tmp/x')")
    assert EffectTag.LOCAL_READ.value in read
    assert EffectTag.LOCAL_WRITE.value not in read


def test_classify_pure_local_read_has_no_high_severity_tag():
    tags = et.classify_source("open('/tmp/x').read()")
    assert not any(et.is_high_severity(t) for t in tags)


def test_unparseable_source_is_unknown_not_empty():
    # absence of a tag must never read as "no effect": broken source ⇒ UNKNOWN (gated)
    tags = _tags("def run(:\n  this is not python")
    assert tags == {EffectTag.UNKNOWN.value}


def test_no_sinks_yields_empty_set():
    # a genuinely inert helper legitimately has no effect tags
    assert et.classify_source("def run(a, b):\n    return a + b") == set()


# --------------------------------------------------------------------------- #
# R-A13b-2ii-a — curated SEMANTIC classes (money_move / send_message) through
# classify_source (the REAL entry the S1 gate + backfill + meter all consume)
# --------------------------------------------------------------------------- #

def test_classify_money_move_via_import_and_attr_chain():
    tags = _tags("import stripe\ndef run(**k):\n    return stripe.PaymentIntent.create(**k)")
    assert EffectTag.MONEY_MOVE.value in tags


def test_classify_send_message_via_smtplib_sendmail():
    tags = _tags("import smtplib\ndef run():\n    s = smtplib.SMTP('h')\n    s.sendmail('a','b','c')")
    assert EffectTag.SEND_MESSAGE.value in tags


def test_classify_money_move_via_url_host_literal():
    tags = _tags("import requests\nrequests.post('https://api.stripe.com/v1/charges', json={})")
    assert EffectTag.MONEY_MOVE.value in tags
    # both net_mutate (structural POST) and money_move (host) are present — a UNION
    # with money_move is the safe both-match case (monotonic).
    assert EffectTag.NET_MUTATE.value in tags


def test_classify_send_message_via_url_host_literal():
    tags = _tags("import requests\nrequests.post('https://api.twilio.com/2010/Messages.json', data={})")
    assert EffectTag.SEND_MESSAGE.value in tags


def test_classify_plain_post_is_net_mutate_only_not_money_or_send():
    tags = _tags("import requests\nrequests.post('https://example.com/x', json={})")
    assert EffectTag.NET_MUTATE.value in tags
    assert EffectTag.MONEY_MOVE.value not in tags
    assert EffectTag.SEND_MESSAGE.value not in tags


def test_classify_benign_get_still_net_read_only():
    tags = _tags("import requests\nrequests.get('https://api.stripe.com/v1/charges')")
    # a GET to a money host is a READ, not a money-move BY METHOD — but the host
    # literal still adds money_move (conservative/monotonic). net_read stays present.
    assert EffectTag.NET_READ.value in tags


def test_classify_money_move_is_monotonic_never_dropped():
    # a source that is money on TWO axes (import + attr) never yields a set WITHOUT it.
    src = "import stripe\nstripe.Charge.create(amount=5)\n"
    for _ in range(3):
        assert EffectTag.MONEY_MOVE.value in _tags(src)


def test_classify_send_message_via_twilio_attr_chain():
    tags = _tags("import twilio\ndef run(client):\n    return client.messages.create(to='x')")
    assert EffectTag.SEND_MESSAGE.value in tags


def test_classify_source_still_never_raises_with_signals():
    # never-raises contract holds with the new axes engaged: unparseable ⇒ {UNKNOWN}.
    assert _tags("import stripe\nstripe.") == {EffectTag.UNKNOWN.value}


# --------------------------------------------------------------------------- #
# vault backfill (legacy tools gain effect_tags; idempotent)
# --------------------------------------------------------------------------- #

def _seed_tool(vault: Path, tid: str, name: str, source: str) -> Path:
    tools = vault / "tools"
    impl = tools / "implementations"
    impl_dir = impl
    impl_dir.mkdir(parents=True, exist_ok=True)
    (impl_dir / f"{name}.py").write_text(source, encoding="utf-8")
    body = {
        "id": tid, "name": name, "description": "d", "tool_type": "python",
        "implementation_path": f"{name}.py", "status": "deployed",
    }
    (tools / f"tool_{tid}.json").write_text(json.dumps(body), encoding="utf-8")
    idx = tools / "index.json"
    entries = json.loads(idx.read_text(encoding="utf-8")) if idx.exists() else []
    entries.append({"id": tid, "name": name})
    idx.write_text(json.dumps(entries), encoding="utf-8")
    return tools / f"tool_{tid}.json"


def test_backfill_stamps_effect_tags_from_source(tmp_path):
    from systemu.runtime import vault_migrator as vm

    body_path = _seed_tool(
        tmp_path, "abc123", "poster",
        "import requests\ndef run(**k):\n    return requests.post('https://api/x', json=k)",
    )
    # pre-existing tool has NO effect_tags key
    assert "effect_tags" not in json.loads(body_path.read_text(encoding="utf-8"))

    vm.backfill_effect_tags(tmp_path, version="0.9.53")

    tags = json.loads(body_path.read_text(encoding="utf-8")).get("effect_tags")
    assert tags is not None
    assert EffectTag.NET_MUTATE.value in tags


def test_backfill_is_idempotent(tmp_path):
    from systemu.runtime import vault_migrator as vm

    body_path = _seed_tool(tmp_path, "d1", "deleter", "import os\nos.remove('/tmp/x')")
    vm.backfill_effect_tags(tmp_path, version="0.9.53")
    first = json.loads(body_path.read_text(encoding="utf-8"))["effect_tags"]

    # second run at the same version is a no-op (fast path via the stamp)
    vm.backfill_effect_tags(tmp_path, version="0.9.53")
    second = json.loads(body_path.read_text(encoding="utf-8"))["effect_tags"]

    assert first == second
    assert EffectTag.LOCAL_DELETE.value in second


def test_backfill_never_raises_on_missing_impl(tmp_path):
    from systemu.runtime import vault_migrator as vm

    tools = tmp_path / "tools"
    tools.mkdir(parents=True, exist_ok=True)
    (tools / "index.json").write_text(json.dumps([{"id": "x", "name": "ghost"}]), encoding="utf-8")
    (tools / "tool_x.json").write_text(
        json.dumps({"id": "x", "name": "ghost", "implementation_path": "ghost.py"}),
        encoding="utf-8",
    )
    # impl file does not exist — must not raise, must still stamp (empty)
    vm.backfill_effect_tags(tmp_path, version="0.9.53")
    assert "effect_tags" in json.loads((tools / "tool_x.json").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# R-A13b-2ii-a — the backfill money-move FLOOR (a money signal always wins)
# --------------------------------------------------------------------------- #

def test_backfill_stamps_money_move_from_stripe_source(tmp_path):
    from systemu.runtime import vault_migrator as vm

    body_path = _seed_tool(
        tmp_path, "pay1", "payer",
        "import stripe\ndef run(**k):\n    return stripe.PaymentIntent.create(**k)",
    )
    vm.backfill_effect_tags(tmp_path, version="0.9.73")
    tags = json.loads(body_path.read_text(encoding="utf-8"))["effect_tags"]
    assert EffectTag.MONEY_MOVE.value in tags


def test_backfill_stamps_send_message_from_smtplib_source(tmp_path):
    from systemu.runtime import vault_migrator as vm

    body_path = _seed_tool(
        tmp_path, "msg1", "mailer",
        "import smtplib\ndef run():\n    s = smtplib.SMTP('h')\n    s.sendmail('a','b','c')",
    )
    vm.backfill_effect_tags(tmp_path, version="0.9.73")
    tags = json.loads(body_path.read_text(encoding="utf-8"))["effect_tags"]
    assert EffectTag.SEND_MESSAGE.value in tags
    # a send-message tool is NOT a money-move (the floor is money-only).
    assert EffectTag.MONEY_MOVE.value not in tags


def test_backfill_benign_tool_gets_no_money_or_send(tmp_path):
    from systemu.runtime import vault_migrator as vm

    body_path = _seed_tool(
        tmp_path, "b1", "reader",
        "import requests\ndef run():\n    return requests.get('https://example.com/x')",
    )
    vm.backfill_effect_tags(tmp_path, version="0.9.73")
    tags = json.loads(body_path.read_text(encoding="utf-8"))["effect_tags"]
    assert EffectTag.MONEY_MOVE.value not in tags
    assert EffectTag.SEND_MESSAGE.value not in tags


def test_backfill_floor_catches_money_attr_ref_without_call(tmp_path):
    """The FLOOR is an INDEPENDENT re-derivation, not a by-product of the structural
    scan: even a money attr-chain the call-based visitor misses (an UNCALLED
    reference — a decorator/partial/assignment) still floors to money_move via
    any_money_move_signal. This is the defense-in-depth half of the merge."""
    from systemu.runtime import vault_migrator as vm

    body_path = _seed_tool(
        tmp_path, "pay3", "deferred",
        "import os\ndef run(client):\n    fn = client.PaymentIntent.create\n    return fn",
    )
    vm.backfill_effect_tags(tmp_path, version="0.9.73")
    tags = json.loads(body_path.read_text(encoding="utf-8"))["effect_tags"]
    assert EffectTag.MONEY_MOVE.value in tags


def test_backfill_money_move_floor_is_idempotent(tmp_path):
    from systemu.runtime import vault_migrator as vm

    body_path = _seed_tool(
        tmp_path, "pay2", "charger2",
        "import requests\nrequests.post('https://api.stripe.com/v1/charges', json={})",
    )
    vm.backfill_effect_tags(tmp_path, version="0.9.73")
    first = json.loads(body_path.read_text(encoding="utf-8"))["effect_tags"]
    vm.backfill_effect_tags(tmp_path, version="0.9.73")
    second = json.loads(body_path.read_text(encoding="utf-8"))["effect_tags"]
    assert first == second
    assert EffectTag.MONEY_MOVE.value in second
