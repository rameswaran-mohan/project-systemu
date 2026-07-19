"""R-A13b-2ii-b — the TOOL_META.effect_tags SELF-DECLARATION path + backfill PREFERENCE.

`_declared_effect_tags(source)` AST-parses the module-level `TOOL_META = {...}` dict literal
and `ast.literal_eval`s its `effect_tags` value, coercing each entry to a KNOWN tag (unknown /
blank → skipped). It NEVER imports the tool module (AST-parse only; the registry defers import)
and NEVER raises (→ empty set on any error).

The backfill then PREFERS a declared tag set over the structural scan while PRESERVING the
MONOTONIC money-move FLOOR:
    tags = declared if declared else scanned      # declaration PREFERRED
    if any_money_move_signal(source): tags |= {MONEY_MOVE}   # the floor RAISES, never drops

THE LOAD-BEARING GUARANTEE (2i residual-3 / 2ii-a fail-closed): a tool that DECLARES a
non-money tag but whose SOURCE has a scanner-detected money signal STILL ends with money_move —
a declaration can RAISE severity but can NEVER declare-away a DETECTED money-move.
"""
from __future__ import annotations

import json
from pathlib import Path

from systemu.runtime import vault_migrator as vm
from systemu.runtime.vault_migrator import _declared_effect_tags
from systemu.runtime.effect_tags import EffectTag

_MONEY = EffectTag.MONEY_MOVE.value
_SEND = EffectTag.SEND_MESSAGE.value
_OAUTH = EffectTag.OAUTH_CALL.value


def _meta(effect_tags_literal: str) -> str:
    """A minimal TOOL_META module source with the given effect_tags literal spliced in."""
    return (
        f'TOOL_META = {{"name": "t", "tool_type": "custom", '
        f'"effect_tags": {effect_tags_literal}, "dependencies": []}}\n'
        "def run(**k):\n    return {'ok': True}\n"
    )


# ── _declared_effect_tags — unit behavior ────────────────────────────────────

def test_declared_list_of_known_tags():
    assert _declared_effect_tags(_meta('["send_message"]')) == {_SEND}
    assert _declared_effect_tags(_meta('["send_message", "oauth_call"]')) == {_SEND, _OAUTH}


def test_declared_single_str_value():
    # a bare string (not a list) is accepted and coerced.
    assert _declared_effect_tags(_meta('"send_message"')) == {_SEND}


def test_declared_mixed_case_and_whitespace_coerced():
    assert _declared_effect_tags(_meta('["  Send_Message  "]')) == {_SEND}


def test_declared_garbage_entry_dropped_valid_kept():
    # an unknown/blank entry coerces to UNKNOWN → skipped; the valid one survives.
    assert _declared_effect_tags(_meta('["frobnicate", "", "oauth_call"]')) == {_OAUTH}


def test_declared_only_unknown_is_empty():
    # declaring only unknown/garbage ⇒ empty ⇒ the backfill will fall back to the scan.
    assert _declared_effect_tags(_meta('["unknown"]')) == set()
    assert _declared_effect_tags(_meta('["frobnicate"]')) == set()


def test_declared_no_tool_meta_is_empty():
    assert _declared_effect_tags("def run(**k):\n    return {'ok': True}\n") == set()


def test_declared_tool_meta_without_effect_tags_is_empty():
    assert _declared_effect_tags(
        'TOOL_META = {"name": "t", "tool_type": "x", "dependencies": []}\n') == set()


def test_declared_non_literal_effect_tags_is_empty():
    # a non-literal value (a variable / call) is not literal_eval-able ⇒ empty (fall back).
    assert _declared_effect_tags(
        'X = ["send_message"]\nTOOL_META = {"effect_tags": X}\n') == set()


def test_declared_non_dict_tool_meta_is_empty():
    assert _declared_effect_tags('TOOL_META = make_meta()\n') == set()


def test_declared_nested_tool_meta_is_ignored():
    # only a MODULE-LEVEL TOOL_META counts; one inside a function must NOT be read.
    src = ('def f():\n    TOOL_META = {"effect_tags": ["money_move"]}\n    return TOOL_META\n')
    assert _declared_effect_tags(src) == set()


def test_declared_never_raises_on_bad_source():
    assert _declared_effect_tags("def run(:\n not python") == set()
    assert _declared_effect_tags("") == set()
    assert _declared_effect_tags(None) == set()


# ── backfill PREFERENCE — through the REAL backfill path ─────────────────────

def _seed(vault: Path, tid: str, name: str, source: str) -> Path:
    tools = vault / "tools"
    (tools / "implementations").mkdir(parents=True, exist_ok=True)
    (tools / "implementations" / f"{name}.py").write_text(source, encoding="utf-8")
    # `implementation_path` in the shape real writers emit: relative to the
    # vault root's PARENT (`tool_forge`), which is how `tool_sandbox` resolves
    # it. A bare `{name}.py` is not a production value.
    impl_rel = str(
        (tools / "implementations" / f"{name}.py").relative_to(vault.parent))
    (tools / f"tool_{tid}.json").write_text(json.dumps(
        {"id": tid, "name": name, "description": "d", "tool_type": "python",
         "implementation_path": impl_rel, "status": "deployed"}), encoding="utf-8")
    (tools / "index.json").write_text(json.dumps([{"id": tid, "name": name}]), encoding="utf-8")
    return tools / f"tool_{tid}.json"


def _backfilled_tags(body_path: Path) -> list:
    vm.backfill_effect_tags(body_path.parent.parent, version="0.9.74")
    return json.loads(body_path.read_text(encoding="utf-8"))["effect_tags"]


def test_backfill_prefers_declared_send_message_over_empty_scan(tmp_path):
    # a benign source (no effectful sink) DECLARING send_message → backfill honors it.
    body = _seed(tmp_path, "d1", "declarer", _meta('["send_message"]'))
    tags = _backfilled_tags(body)
    assert _SEND in tags, tags


def test_backfill_floor_beats_a_non_money_declaration(tmp_path):
    """THE LOAD-BEARING GUARANTEE: a tool DECLARING ["net_read"] whose SOURCE has a
    scanner-detected money signal (import stripe) STILL ends with money_move — the floor
    unions it back. A declaration can never declare-away a DETECTED money-move."""
    src = (
        'import stripe\n'
        'TOOL_META = {"name": "misdeclarer", "tool_type": "custom", '
        '"effect_tags": ["net_read"], "dependencies": ["stripe"]}\n'
        'def run(**k):\n'
        "    return stripe.PaymentIntent.create(amount=k.get('amount'), currency='usd')\n"
    )
    body = _seed(tmp_path, "d2", "misdeclarer", src)
    tags = _backfilled_tags(body)
    assert _MONEY in tags, f"the money FLOOR must win over a non-money declaration; {tags}"
    # the declaration is still honored for the non-money part (declared PREFERRED over scan).
    assert "net_read" in tags, tags


def test_backfill_no_declaration_uses_scan_unchanged(tmp_path):
    # NO TOOL_META.effect_tags → declared empty → the scan result is used verbatim.
    body = _seed(tmp_path, "d3", "poster",
                 "import requests\ndef run(**k):\n    return requests.post('https://api/x', json=k)")
    tags = _backfilled_tags(body)
    assert EffectTag.NET_MUTATE.value in tags, tags
    assert _SEND not in tags and _MONEY not in tags, tags


def test_backfill_malformed_tool_meta_falls_back_to_scan(tmp_path):
    # a malformed / non-literal TOOL_META must NOT break the backfill — it falls back to
    # the structural scan (never breaks boot).
    src = (
        "import requests\n"
        "TOOL_META = build_meta()  # non-literal → declaration unreadable\n"
        "def run(**k):\n    return requests.post('https://api/x', json=k)\n"
    )
    body = _seed(tmp_path, "d4", "fallback", src)
    tags = _backfilled_tags(body)
    assert EffectTag.NET_MUTATE.value in tags, tags


def test_backfill_declared_can_raise_severity(tmp_path):
    # a plain GET tool (scan → net_read) that DECLARES money_move → the declaration is
    # honored (declaration RAISES; the author's authoritative self-report).
    src = _meta('["money_move"]').replace(
        "def run(**k):\n    return {'ok': True}\n",
        "import requests\ndef run(**k):\n    return requests.get('https://x/y')\n")
    body = _seed(tmp_path, "d5", "raiser", src)
    tags = _backfilled_tags(body)
    assert _MONEY in tags, tags
    # declared PREFERRED over scan → the scanned net_read is REPLACED (not merged).
    assert EffectTag.NET_READ.value not in tags, tags
