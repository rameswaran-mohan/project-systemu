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


def test_net_egress_stdlib_imports_are_tagged_net():
    """R-A14a §15.1 hardening: a tool reaching the net via http.client / ftplib —
    whose call-site the structural scan misses (Attribute receiver) — is now caught
    at IMPORT, so classify_source tags it net (tightens the forged-net hard-DENY)."""
    from systemu.runtime.effect_tags import classify_source, EffectTag
    http_client = classify_source(
        "import http.client\n"
        "def run(**kw):\n"
        "    c = http.client.HTTPSConnection('x')\n"
        "    c.request('POST', '/')\n")
    assert EffectTag.NET_MUTATE in http_client
    ftp = classify_source("import ftplib\ndef run(**kw):\n    ftplib.FTP('h')\n")
    assert EffectTag.NET_MUTATE in ftp
    frm = classify_source("from http.client import HTTPSConnection\ndef run(**kw):\n    HTTPSConnection('x')\n")
    assert EffectTag.NET_MUTATE in frm


def test_http_server_import_is_not_net_egress():
    """Precision: http.server is INBOUND, not egress — it must NOT be net-tagged
    (keyed on the full dotted module, not the 'http' root)."""
    from systemu.runtime.effect_tags import classify_source, EffectTag
    tags = classify_source("import http.server\ndef run(**kw):\n    return 1\n")
    assert EffectTag.NET_MUTATE not in tags and EffectTag.NET_READ not in tags


# --------------------------------------------------------------------------- #
# IMPORT-ALIAS RESOLUTION — the classifier must not be defeated by ordinary
# Python spelling.
#
# Before this, the scan was NAME-MATCHING: it recognised a sink only when the
# receiver was spelled literally (``subprocess.run``, ``os.system``,
# ``requests.get``). Every body below is idiomatic, non-adversarial Python that
# scanned as exactly ``{local_write}`` — "purely local" — and so was executed
# unattended by the forge dry-run AND passed ``forged_network_denied`` at the
# LIVE gate. The dominant real-world trigger is a careless LLM forge, not an
# attacker.
#
# Each body pairs the aliased sink with one ordinary local write, so a scan that
# still misses the sink comes back ``{local_write}`` rather than empty — i.e.
# these tests fail LOUDLY on a regression instead of degrading to "no sinks".
# --------------------------------------------------------------------------- #

def _local_write_plus(imports: str, call: str) -> str:
    """A body whose ONLY other sink is a plain local write."""
    return (
        f"{imports}"
        "from pathlib import Path\n"
        "\n"
        "def run(x):\n"
        "    Path('out.txt').write_text('local')\n"
        f"    {call}\n"
        "    return {'success': True}\n"
    )


def _bare(imports: str, call: str) -> str:
    """The same body with NO local write, for assertions that must not be
    satisfied by a confounding sink (e.g. proving ``shutil.move`` itself is what
    produced ``local_write``)."""
    return f"{imports}\ndef run(x):\n    {call}\n    return {{'success': True}}\n"


class TestAliasedImportsAreResolved:
    """``import X as Y`` — the receiver is resolved through the import alias map."""

    def test_aliased_subprocess_is_shell_exec(self):
        tags = _tags(_local_write_plus(
            "import subprocess as sp\n", "sp.run(['echo', 'hi'])"))
        assert EffectTag.SHELL_EXEC.value in tags

    def test_aliased_shutil_rmtree_is_local_delete(self):
        tags = _tags(_local_write_plus(
            "import shutil as sh\n", "sh.rmtree('/tmp/x')"))
        assert EffectTag.LOCAL_DELETE.value in tags

    def test_aliased_requests_get_is_net_read(self):
        tags = _tags(_local_write_plus(
            "import requests as r\n", "r.get('https://example.com/x')"))
        assert EffectTag.NET_READ.value in tags

    def test_aliased_requests_delete_is_net_mutate(self):
        """``delete`` — deliberately NOT ``post``/``put``/``patch``, which the
        pre-existing ``_ATTR_ONLY_NET_MUTATE`` rule catches on ANY receiver and
        which would therefore pass without any alias resolution at all."""
        tags = _tags(_bare("import requests as r\n", "r.delete('https://example.com/x')"))
        assert EffectTag.NET_MUTATE.value in tags

    def test_aliased_os_system_is_shell_exec(self):
        tags = _tags(_local_write_plus("import os as o\n", "o.system('ls')"))
        assert EffectTag.SHELL_EXEC.value in tags

    def test_dotted_alias_resolves_to_the_full_module_not_its_root(self):
        """``import os.path as p`` binds ``p`` to ``os.path``, NOT to ``os``.

        The call here is deliberately ``p.remove(...)``: ``remove`` is in
        ``_DELETE_ATTRS`` as ``("os", "remove")`` and is NOT one of the
        attr-only rules, so resolving the alias to the ROOT would invent a
        ``local_delete`` that is not there. Resolving to the FULL dotted module
        yields ``("os.path", "remove")``, which matches nothing.

        (A ``p.exists(...)`` probe would NOT discriminate — it collides with no
        table under either resolution, so it passes even when the resolution is
        wrong. Confirmed by mutation-testing this rule.)
        """
        tags = _tags(_local_write_plus("import os.path as p\n", "p.remove('/tmp/x')"))
        assert tags == {EffectTag.LOCAL_WRITE.value}

    def test_plain_dotted_import_still_binds_its_root(self):
        """``import os.path`` (no asname) binds the name ``os`` — so ``os.remove``
        in the same body must still resolve."""
        tags = _tags(_local_write_plus("import os.path\n", "os.remove('/tmp/x')"))
        assert EffectTag.LOCAL_DELETE.value in tags


class TestFromImportsAreResolved:
    """``from X import Y`` — a BARE call name is resolved to its (module, symbol)."""

    def test_from_import_subprocess_check_output_is_shell_exec(self):
        tags = _tags(_local_write_plus(
            "from subprocess import check_output\n", "check_output(['echo', 'hi'])"))
        assert EffectTag.SHELL_EXEC.value in tags

    def test_from_import_os_system_is_shell_exec(self):
        tags = _tags(_local_write_plus(
            "from os import system\n", "system('echo hi')"))
        assert EffectTag.SHELL_EXEC.value in tags

    def test_from_import_os_remove_is_local_delete(self):
        tags = _tags(_local_write_plus(
            "from os import remove\n", "remove('/tmp/x')"))
        assert EffectTag.LOCAL_DELETE.value in tags

    def test_from_import_shutil_rmtree_is_local_delete(self):
        tags = _tags(_local_write_plus(
            "from shutil import rmtree\n", "rmtree('/tmp/x')"))
        assert EffectTag.LOCAL_DELETE.value in tags

    def test_from_import_with_asname_is_resolved(self):
        """``from subprocess import check_output as co`` — the alias, not the
        symbol, is what appears at the call site."""
        tags = _tags(_local_write_plus(
            "from subprocess import check_output as co\n", "co(['echo', 'hi'])"))
        assert EffectTag.SHELL_EXEC.value in tags

    def test_from_import_shutil_move_is_local_write(self):
        """No confounding ``Path.write_text`` here — ``move`` itself must be what
        produces the tag, otherwise this would pass unchanged."""
        tags = _tags(_bare("from shutil import move\n", "move('/tmp/a', '/tmp/b')"))
        assert tags == {EffectTag.LOCAL_WRITE.value}

    def test_urllib_request_build_opener_is_net(self):
        """``from urllib.request import build_opener`` — the opener's ``.open()``
        has no recognisable receiver, so the IMPORT is what must carry the net
        signal (``urllib.request`` is unambiguously egress; ``urllib.parse`` is
        not — see the precision test below)."""
        from systemu.runtime.action_governance import has_network_egress
        tags = _tags(_local_write_plus(
            "from urllib.request import build_opener\n",
            "build_opener().open('https://example.com/x')"))
        assert has_network_egress(tags)

    def test_plain_urllib_request_import_is_net(self):
        from systemu.runtime.action_governance import has_network_egress
        assert has_network_egress(_tags("import urllib.request\ndef run(x):\n    return 1\n"))

    def test_relative_from_import_is_not_resolved_against_stdlib(self):
        """PRECISION: ``from .os import system`` imports a LOCAL module named
        ``os``, not the stdlib one. Resolving a relative import against the
        stdlib tables would be a false positive."""
        tags = _tags(_local_write_plus("from .os import system\n", "system('x')"))
        assert EffectTag.SHELL_EXEC.value not in tags


class TestNoFalsePositivesFromAliasResolution:
    """Precision guards — the resolution must not invent effects."""

    def test_urllib_parse_is_not_net(self):
        from systemu.runtime.action_governance import has_network_egress
        assert not has_network_egress(
            _tags("from urllib.parse import urlencode\ndef run(x):\n    return urlencode(x)\n"))

    def test_unrelated_alias_is_inert(self):
        tags = _tags(_local_write_plus(
            "import json as j\n", "j.dumps({'a': 1})"))
        assert tags == {EffectTag.LOCAL_WRITE.value}

    def test_from_import_of_a_benign_symbol_is_inert(self):
        tags = _tags(_local_write_plus(
            "from os import getcwd\n", "getcwd()"))
        assert tags == {EffectTag.LOCAL_WRITE.value}

    def test_local_variable_shadowing_a_module_name_still_classifies(self):
        """Pre-existing behaviour must be preserved: a receiver that is NOT in
        the alias map falls back to its literal name, so ``session.post(...)``
        and an unimported ``os.system(...)`` keep classifying as they did."""
        assert EffectTag.NET_MUTATE.value in _tags("session.post('https://x/y')")
        assert EffectTag.SHELL_EXEC.value in _tags("os.system('ls')")


class TestDynamicAccessForcesUnknown:
    """Genuinely DYNAMIC module/attribute access cannot be resolved statically,
    so it forces UNKNOWN (gated, never refused).

    Deliberately EXCLUDES ``getattr``: it was measured at ~15% of this repo's own
    tool bodies used benignly, so forcing UNKNOWN on it would make every such body
    skip and collapse the dry-run into a no-op.
    """

    @pytest.mark.parametrize("call", [
        "__import__('subprocess').run(['ls'])",
        "exec('import os; os.system(\"ls\")')",
        "eval('__import__(\"os\").system(\"ls\")')",
    ])
    def test_dynamic_builtin_forces_unknown(self, call):
        assert EffectTag.UNKNOWN.value in _tags(_local_write_plus("", call))

    def test_importlib_import_module_forces_unknown(self):
        tags = _tags(_local_write_plus(
            "import importlib\n", "importlib.import_module('subprocess').run(['ls'])"))
        assert EffectTag.UNKNOWN.value in tags

    def test_from_importlib_import_module_forces_unknown(self):
        tags = _tags(_local_write_plus(
            "from importlib import import_module\n", "import_module('subprocess')"))
        assert EffectTag.UNKNOWN.value in tags

    def test_getattr_is_deliberately_not_unknown_forcing(self):
        """The measured-cost exclusion, pinned so it is not "tightened" by
        accident: ~15% of real tool bodies use getattr benignly."""
        tags = _tags(_local_write_plus("", "getattr(obj, 'name', None)"))
        assert EffectTag.UNKNOWN.value not in tags


class TestAliasResolutionReachesBothSafetyControls:
    """The classifier is load-bearing for TWO controls. Both must see the sink.

    1. ``tool_dry_run._net_egress_skip_reason`` — whose ``_SAFE_LOCAL_TAGS``
       allowlist excludes ``shell_exec``, so a resolved shell sink now skips.
    2. ``action_governance.forged_network_denied`` — the LIVE gate, which
       re-scans the source precisely because declared tags are untrustworthy.
    """

    def test_live_gate_sees_an_aliased_network_sink(self, tmp_path):
        from systemu.runtime.action_governance import forged_network_denied

        # ``get`` — NOT post/put/patch, which the pre-existing attr-only rule
        # already catches on any receiver; this must fail without alias resolution.
        impl = tmp_path / "aliased_net.py"
        impl.write_text(_local_write_plus(
            "import requests as r\n", "r.get('https://example.invalid/x')"),
            encoding="utf-8")

        class _T:
            forged_by_systemu = True
            effect_tags = []          # exactly as tool_forge leaves it
            implementation_path = str(impl)

        assert forged_network_denied(_T()) is not None, (
            "SECURITY: the LIVE forged-network gate did not see an aliased "
            "requests sink in a forged tool's source")

    def test_live_gate_still_clears_a_purely_local_forged_tool(self, tmp_path):
        """The no-false-positive half: a genuinely local forged tool must still
        pass the live gate, or every forge is denied."""
        from systemu.runtime.action_governance import forged_network_denied

        impl = tmp_path / "purely_local.py"
        impl.write_text(_local_write_plus("import json as j\n", "j.dumps({'a': 1})"),
                        encoding="utf-8")

        class _T:
            forged_by_systemu = True
            effect_tags = []
            implementation_path = str(impl)

        assert forged_network_denied(_T()) is None
