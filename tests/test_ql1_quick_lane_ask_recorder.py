"""R-QL1 -- the QUICK-LANE ask recorder, DEC-7's ONLY evidence source.

DEC-7's amended criterion is about the QUICK lane's operator-question cap
(``pipelines/quick_task.py:_ASK_USER_CAP``). Until this slice, NOTHING recorded a
quick-lane ask: the cap fired, terminated the run, and left no trace. The two
deep-lane corpora cannot stand in for it, and reusing either is a DEC-7 violation
BY RULING (``replay_metrics.py`` states the separation at the head of the
answer-linked section):

  * ``audit/ask_corpus.jsonl`` rows are scored by ``avoidable_ask_report``'s
    no-attempt proxy, and absent attempt fields default to 0 -- every quick-lane row
    folded in there would land in the NUMERATOR of a shipped metric.
  * ``audit/ask_avoidable.jsonl`` rows are ANSWER-LINKED observations keyed by a
    ``Requirement.schema_path``. A quick-lane ASK_USER is free text and has none.

So: a THIRD file, its own writer, its own CONC-MAP row.

The pins here, in order of severity:
  1. **SECRETS** -- the question TEXT never reaches the file (keyed HMAC ref only,
     and NO KEY => NO ROW), and a secret-class ask is excluded by a DOUBLE guard
     whose halves are pinned INDEPENDENTLY (each test below defeats one half and
     requires the other to refuse on its own).
  2. **OBSERVABILITY-ONLY** -- the run's result is byte-identical with the recorder
     raising. This is a measurement bolted onto a shipped lane; it may never change
     the run that made the ask.
  3. **REACHABILITY** -- AST pins on the hook and on the report call site. R-B5
     shipped once with fully unit-tested helpers and ZERO production callers; a
     green unit test is not evidence that anything renders.
  4. **HONESTY AT THE FLOOR** -- below DEC-7's measurement window (30 asks across
     >=10 distinct runs) the report says NOT MEASURED and prints NO rate. A
     fabricated ``0%`` is a quotable headline invented out of an empty population.
"""
from __future__ import annotations

import ast
import dataclasses
import json
import threading
from pathlib import Path

import pytest
from click.testing import CliRunner

import systemu.pipelines.quick_task as qt
from systemu.interface import cli_commands as cc
from systemu.runtime import replay_metrics as rm
from systemu.vault.vault import Vault

_CLI_SOURCE = Path(cc.__file__)
_QUICK_SOURCE = Path(qt.__file__)

#: Deliberately free of every ``elicitation._SECRET_NAME_TOKENS`` token, so these
#: fixtures exercise the recording path rather than the secret guard. (``pin`` is a
#: substring of ``shipping``; ``auth`` of ``author`` -- the guard is a substring
#: match and errs toward DROPPING a row, which is the safe direction.)
_BENIGN_Q = "Which colour do you want?"


# -- harness -----------------------------------------------------------------
class _Vault:
    """The minimal vault surface the recorder touches (root only)."""

    def __init__(self, root):
        self.root = str(root)

    def list_tools(self, status=None):
        return []


def _corpus(root) -> Path:
    return Path(root) / "audit" / "quick_lane_asks.jsonl"


def _rows(root) -> list:
    p = _corpus(root)
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines()
            if ln.strip()]


def _mk_vault(root: Path) -> Vault:
    """A REAL vault, shaped the way ``tests/test_wave8_quick_task.py`` shapes it."""
    for sub in ["tools/implementations", "elder", "notifications"]:
        (root / sub).mkdir(parents=True, exist_ok=True)
    (root / "tools" / "index.json").write_text("[]", encoding="utf-8")
    return Vault(str(root))


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    return _mk_vault(tmp_path)


def _fake_llm(script):
    """Replay an action script (the wave-8 harness, verbatim in behaviour)."""
    calls = {"payloads": [], "i": 0}

    def llm_json(*, system, user, config=None):
        calls["payloads"].append(user)
        action = script[min(calls["i"], len(script) - 1)]
        calls["i"] += 1
        return action

    llm_json.calls = calls
    return llm_json


def _drive(vault, monkeypatch, script, *, answer="again"):
    """Drive a REAL quick-lane run. Same construction as
    ``test_wave8_quick_task.py::test_ask_user_cap_stops_reask_loop`` -- an
    ``_ask_operator_inline`` that always answers, so the model's re-asking is what
    walks the counter to the cap."""
    monkeypatch.setattr(qt, "_ask_operator_inline", lambda *a, **k: answer)
    llm = _fake_llm(script)
    return qt.run_quick_task("do it", None, vault, llm_json=llm,
                             chat_surface=True, max_iters=12,
                             synthesize=lambda *a, **k: "summary")


def _record(v, **over):
    kw = dict(run_id="run-1", ask_ordinal=1, cap_hit=False, re_ask=False,
              outcome="answered", question_text=_BENIGN_Q)
    kw.update(over)
    return rm.record_quick_lane_ask(v, **kw)


# == 1. a REAL capped quick-lane run accretes the corpus =====================
def test_a_real_capped_run_lands_one_row_per_ask_stamped_lane_quick(
        vault, monkeypatch, tmp_path):
    """The whole point of the slice: drive the shipped lane to its cap and find
    evidence on disk. One row per ask, ordinals dense from 1, every row stamped
    ``lane="quick"`` (the field a DEC-7 denominator filters on)."""
    res = _drive(vault, monkeypatch, [{"action": "ASK_USER", "question": _BENIGN_Q}])
    assert res.status in ("failed", "partial"), res

    rows = _rows(tmp_path)
    assert len(rows) == qt._ASK_USER_CAP + 1, rows
    assert [r["ask_ordinal"] for r in rows] == list(range(1, qt._ASK_USER_CAP + 2))
    assert all(r["lane"] == "quick" for r in rows), rows
    assert len({r["run_id"] for r in rows}) == 1, "one run must read as one run_id"
    assert all(r["run_id"] for r in rows), "an unattributed row cannot count runs"


def test_no_raw_question_text_appears_anywhere_in_the_file_bytes(
        vault, monkeypatch, tmp_path):
    """SEVERITY 1. This is a plaintext append-only audit artefact. The operator's
    question is operator content; only the keyed, NON-REVERSIBLE ref may be
    written. Asserted over the raw BYTES, not over parsed fields -- a leak into a
    field this test does not know about would still be a leak."""
    question = "Which zzzunique-marker folder do you want?"
    _drive(vault, monkeypatch, [{"action": "ASK_USER", "question": question}])

    blob = _corpus(tmp_path).read_bytes()
    assert b"zzzunique-marker" not in blob, "the raw question text reached the corpus"
    rows = _rows(tmp_path)
    assert rows
    for r in rows:
        assert rm._is_value_ref(r["question_ref"]), r
        assert r["question_ref"] == rm.value_ref(question, vault)
        assert question not in json.dumps(r)


def test_cap_hit_is_true_only_on_the_terminating_ask(vault, monkeypatch, tmp_path):
    """DEC-7's numerator. ``cap_hit`` marks the ask the cap REFUSED -- the one that
    terminated the run -- and nothing else. Flipping it on every row (or on none)
    would make the rate uninformative in opposite directions."""
    _drive(vault, monkeypatch, [{"action": "ASK_USER", "question": _BENIGN_Q}])
    rows = _rows(tmp_path)
    assert [r["cap_hit"] for r in rows] == [False] * qt._ASK_USER_CAP + [True], rows
    assert rows[-1]["outcome"] == "cap_terminated", rows[-1]


def test_re_ask_is_false_on_a_first_ask_and_true_on_the_repeat(
        vault, monkeypatch, tmp_path):
    """DEC-7's other half: how much of the traffic under the cap is the model
    RE-ASKING. Two distinct questions then a repeat of the first."""
    _drive(vault, monkeypatch, [
        {"action": "ASK_USER", "question": "Which colour?"},
        {"action": "ASK_USER", "question": "Which size?"},
        {"action": "ASK_USER", "question": "Which colour?"},
        {"action": "ANSWER", "answer_md": "done", "completed": True},
    ])
    rows = _rows(tmp_path)
    assert [r["re_ask"] for r in rows] == [False, False, True], rows


def test_a_run_that_never_asks_writes_nothing(vault, monkeypatch, tmp_path):
    """The denominator must be ASKS, not runs: a run that answers straight away is
    not an observation about the ask cap and may not dilute the rate."""
    _drive(vault, monkeypatch, [{"action": "ANSWER", "answer_md": "42",
                                 "completed": True}])
    assert not _corpus(tmp_path).exists(), _rows(tmp_path)


# == 2. the DOUBLE secret guard -- each half pinned INDEPENDENTLY =============
def test_the_callers_secret_class_alone_refuses_the_row(tmp_path):
    """Guard (b) half 1, ISOLATED: benign text (so the recorder's own text guard
    cannot fire) plus ``secret_class=True``. Deleting the caller-side guard makes
    this test the only thing that goes red -- which is what "independent" means."""
    v = _Vault(tmp_path)
    assert not rm.is_secret_ask_text(_BENIGN_Q), "fixture premise: benign text"
    _record(v, secret_class=True)
    assert not _corpus(tmp_path).exists()


def test_the_recorders_own_text_guard_alone_refuses_the_row(tmp_path):
    """Guard (b) half 2, ISOLATED: the caller says ``secret_class=False`` (a lying
    or simply mis-classifying caller) and the recorder must refuse anyway. This is
    the DEC-34 shape -- the verifier may not rely on an input the verified party
    controls."""
    v = _Vault(tmp_path)
    _record(v, secret_class=False, question_text="What is your API key for the site?")
    assert not _corpus(tmp_path).exists()


@pytest.mark.parametrize("marker", [
    "What is your API key?", "Paste the auth token", "Your password please",
    "Which credential should I use?", "Enter the card CVV",
])
def test_secret_shaped_questions_are_excluded_unconditionally(tmp_path, marker):
    """The predicate is the codebase's canonical one (``elicitation.is_secret_field``),
    fed the question in field-NAME shape so its substring tokens can see across the
    spaces free text has. Not a bespoke rule: a second, weaker regex here would drift
    from the one the product actually enforces."""
    v = _Vault(tmp_path)
    assert rm.is_secret_ask_text(marker)
    _record(v, question_text=marker, secret_class=False)
    assert not _corpus(tmp_path).exists()


def test_a_non_boolean_secret_class_fails_CLOSED(tmp_path):
    """DEC-36's terminating rule at the boundary: the marker is checked by IDENTITY
    (``is not False``) before any operation, so a mis-typed or attacker-shaped value
    -- ``0``, ``None``, ``"no"``, a dict -- refuses rather than being coerced into a
    permission by truthiness."""
    v = _Vault(tmp_path)
    for bad in (0, None, "no", "", [], {}):
        _record(v, secret_class=bad)
    assert not _corpus(tmp_path).exists()


def test_no_vault_key_means_no_row_and_no_file(tmp_path, monkeypatch):
    """Guard (a), fail-closed half. Without a per-vault key there is no
    non-reversible ref, and falling back to an unkeyed digest (or the raw text) is
    precisely what must never happen in a plaintext audit file. Silently: the run
    that made the ask must not learn that observability is unavailable."""
    from systemu.runtime import dashboard_auth
    monkeypatch.setattr(rm, "_REF_KEY_CACHE", {})
    monkeypatch.setattr(dashboard_auth, "session_secret", lambda root: "")
    v = _Vault(tmp_path)
    _record(v)
    assert not _corpus(tmp_path).exists()


# == 3. observability-only: the run is identical with the recorder raising ====
def test_the_run_result_is_identical_with_the_recorder_raising(tmp_path, monkeypatch):
    """SEVERITY 2. Two REAL runs of the same script against two clean vaults, one
    with the recorder exploding on every call. The returned ``QuickResult`` must be
    field-for-field identical -- the call site's swallow is what makes a measurement
    safe to bolt onto a shipped lane."""
    script = [{"action": "ASK_USER", "question": _BENIGN_Q}]

    good = _mk_vault(tmp_path / "good")
    with pytest.MonkeyPatch.context() as mp:
        res_ok = _drive(good, mp, script)

    boom = _mk_vault(tmp_path / "boom")
    with pytest.MonkeyPatch.context() as mp:
        def _explode(*a, **k):
            raise RuntimeError("recorder is broken")
        mp.setattr(rm, "record_quick_lane_ask", _explode)
        res_boom = _drive(boom, mp, script)

    assert dataclasses.asdict(res_ok) == dataclasses.asdict(res_boom)
    assert _rows(tmp_path / "good"), "premise: the healthy run DID record"
    assert not _corpus(tmp_path / "boom").exists()


# == 4. the report -- NOT MEASURED below the floor, never a fabricated 0% =====
def _seed(v, *, asks: int, runs: int, cap_hits: int = 0, re_asks: int = 0):
    """Write ``asks`` rows spread over ``runs`` distinct run_ids."""
    for i in range(asks):
        _record(v, run_id=f"run-{i % max(runs, 1)}", ask_ordinal=(i // max(runs, 1)) + 1,
                cap_hit=(i < cap_hits), re_ask=(i < re_asks),
                question_text=f"{_BENIGN_Q} {i}")


def _report_lines(v):
    return rm.format_quick_lane_ask(rm.quick_lane_ask_report(v))


def test_report_at_zero_rows_is_not_measured_and_names_the_floor(tmp_path):
    v = _Vault(tmp_path)
    text = "\n".join(_report_lines(v))
    assert "Quick-lane asks: NOT MEASURED (0 recorded asks;" in text, text
    assert "N=30 asks" in text and ">=10 distinct runs" in text, text
    assert "%" not in text, "a percentage was rendered over an empty population"


def test_report_below_the_ask_floor_says_not_measured_with_both_numbers(tmp_path):
    """RULING: below the floor the report must ALSO print NOT MEASURED even with
    rows present, stating BOTH the count and the floor -- so a reader can see how
    far off the window is instead of reading a rate off 12 asks."""
    v = _Vault(tmp_path)
    _seed(v, asks=12, runs=12)
    text = "\n".join(_report_lines(v))
    assert "NOT MEASURED" in text, text
    assert "12 recorded ask" in text, text
    assert "N=30 asks" in text, text
    assert "%" not in text, text


def test_report_above_the_ask_floor_but_below_the_run_floor_is_not_measured(tmp_path):
    """Both floors bind. 30 asks from 3 runs is one operator's afternoon, not a
    population -- DEC-7 needs >=10 distinct runs."""
    v = _Vault(tmp_path)
    _seed(v, asks=30, runs=3)
    text = "\n".join(_report_lines(v))
    assert "NOT MEASURED" in text, text
    assert "3 distinct run" in text, text
    assert "%" not in text, text


def test_report_above_both_floors_renders_the_two_real_rates(tmp_path):
    v = _Vault(tmp_path)
    _seed(v, asks=40, runs=10, cap_hits=10, re_asks=20)
    text = "\n".join(_report_lines(v))
    assert "NOT MEASURED" not in text, text
    assert "cap_hit_rate 25% (10/40)" in text, text
    assert "re_ask_fraction 50% (20/40)" in text, text


@pytest.mark.parametrize("asks,runs", [(0, 0), (12, 12), (30, 3), (40, 10)])
def test_every_rendered_line_is_ascii_only(tmp_path, asks, runs):
    """DEC-32c: verdict-carrying output is ASCII-only. The slice spec wrote the
    measured headline with a middle dot; through ``click.echo`` on a cp437 console
    that came back as a replacement character -- the one line an operator is meant to
    QUOTE was the one that corrupted. Pinned across ALL FOUR render states, because a
    non-ASCII character smuggled into the NOT MEASURED branch would be just as bad and
    is reached far more often."""
    v = _Vault(tmp_path)
    if asks:
        _seed(v, asks=asks, runs=runs)
    for line in _report_lines(v):
        line.encode("ascii")   # raises UnicodeEncodeError on any non-ASCII byte


def test_the_report_counts_only_lane_quick_rows(tmp_path):
    """The lane stamp is load-bearing, not decoration: a hand-appended or mis-routed
    row must not enter a DEC-7 denominator."""
    d = Path(tmp_path) / "audit"
    d.mkdir(parents=True)
    (d / "quick_lane_asks.jsonl").write_text(
        json.dumps({"lane": "quick", "run_id": "a", "cap_hit": True}) + "\n"
        + json.dumps({"lane": "deep", "run_id": "b", "cap_hit": True}) + "\n"
        + "not json {\n\n",
        encoding="utf-8")
    rep = rm.quick_lane_ask_report(_Vault(tmp_path))
    assert rep["total_asks"] == 1 and rep["cap_hit_count"] == 1


def test_below_the_floor_the_report_exposes_no_rate_at_all(tmp_path):
    """Belt for the formatter: the REPORT itself must carry ``None`` rather than a
    0.0 a future renderer could print as 0%."""
    v = _Vault(tmp_path)
    _seed(v, asks=5, runs=5)
    rep = rm.quick_lane_ask_report(v)
    assert rep["measured"] is False
    assert rep["cap_hit_rate"] is None and rep["re_ask_fraction"] is None


# == 5. the CLI surface actually renders it ==================================
@pytest.fixture()
def cli_vault(tmp_path):
    return Vault(str(tmp_path / "vault"))


def _run_cli(monkeypatch, tmp_path, v):
    monkeypatch.setattr(cc, "_get_vault_and_config", lambda ctx: (None, v))
    monkeypatch.chdir(tmp_path)
    res = CliRunner().invoke(cc.debug_avoidable_ask, obj={})
    assert res.exit_code == 0, res.output
    return res.output


def test_cli_renders_not_measured_when_nothing_was_recorded(
        monkeypatch, tmp_path, cli_vault):
    out = _run_cli(monkeypatch, tmp_path, cli_vault)
    lines = [ln for ln in out.splitlines() if "Quick-lane asks" in ln]
    assert lines, "the quick-lane block did not render at all"
    assert any("NOT MEASURED" in ln for ln in lines), lines
    assert not any("cap_hit_rate" in ln for ln in lines), lines


def test_cli_renders_the_rates_once_both_floors_are_met(
        monkeypatch, tmp_path, cli_vault):
    _seed(cli_vault, asks=40, runs=10, cap_hits=10, re_asks=20)
    out = _run_cli(monkeypatch, tmp_path, cli_vault)
    assert "cap_hit_rate 25% (10/40)" in out, out
    assert "re_ask_fraction 50% (20/40)" in out, out


# == 6. REACHABILITY pins ====================================================
def _function_node(path: Path, name: str):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{path.name}: function {name!r} not found")


def _called_names(fn):
    out = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name):
                out.add(f.id)
            elif isinstance(f, ast.Attribute):
                out.add(f.attr)
    return out


def test_the_quick_lane_hook_is_reachable_from_run_quick_task():
    """THE hook pin. Remove the call site in ``run_quick_task`` and this goes red --
    the dominant failure mode on this roadmap is code that lands with no production
    caller, and a recorder nobody calls records nothing forever."""
    called = _called_names(_function_node(_QUICK_SOURCE, "run_quick_task"))
    assert "record_quick_lane_ask" in called, \
        "the quick lane no longer records its operator asks (DEC-7 loses its only input)"


def test_the_hook_does_not_reuse_either_deep_lane_recorder():
    """DEC-7 violation guard, by ruling. ``record_ask`` / ``record_ask_avoidable``
    write corpora whose consumers would silently mis-score a quick-lane row."""
    called = _called_names(_function_node(_QUICK_SOURCE, "run_quick_task"))
    assert "record_ask" not in called
    assert "record_ask_avoidable" not in called


def test_the_metrics_command_consults_the_quick_lane_report():
    called = _called_names(_function_node(_CLI_SOURCE, "debug_avoidable_ask"))
    assert "quick_lane_ask_report" in called, \
        "the metrics command no longer consults the quick-lane ask report"
    assert "format_quick_lane_ask" in called, \
        "the metrics command no longer renders the quick-lane lines"


def test_the_metrics_command_never_records(tmp_path):
    """A printout may not accrete the corpus it reports on."""
    called = _called_names(_function_node(_CLI_SOURCE, "debug_avoidable_ask"))
    assert "record_quick_lane_ask" not in called


# == 7. the append-writer shape (CONC-MAP:65's measured Windows loss) =========
def test_the_recorder_writes_through_the_serialized_append_writer():
    """Source pin: the row must go through ``_append_line`` -- the lock + O_APPEND +
    single ``os.write`` writer -- and not through a buffered ``open(p,"a")``."""
    src = Path(rm.__file__).read_text(encoding="utf-8")
    fn = _function_node(Path(rm.__file__), "record_quick_lane_ask")
    assert "_append_line" in _called_names(fn), \
        "record_quick_lane_ask no longer uses the serialized append writer"
    assert 'open(' not in ast.unparse(fn), \
        "a buffered append reintroduces the measured silent row loss"
    assert "O_APPEND" in src and "_lock_whole_file" in src


def test_rows_are_utf8_lf_with_no_crlf_translation(tmp_path):
    """``os.write`` on a raw fd bypasses Python's text layer, so the fd MUST carry
    ``O_BINARY`` on Windows or every ``\\n`` becomes ``\\r\\n`` and the corpus stops
    matching the UTF-8/LF shape every other JSONL artefact here uses."""
    v = _Vault(tmp_path)
    for i in range(3):
        _record(v, question_text=f"{_BENIGN_Q} {i}")
    blob = _corpus(tmp_path).read_bytes()
    assert b"\r\n" not in blob
    assert blob.count(b"\n") == 3


def test_concurrent_appends_never_lose_a_row(tmp_path):
    """Concurrent quick-lane RUNS are threads of ONE daemon, so this corpus has
    genuinely concurrent appenders. A buffered text-mode append is not atomic across
    handles -- measured on this very corpus at ~1155/1200 rows on POSIX and ~886/1200
    on Windows (the CRT emulates O_APPEND as seek-then-write): no torn line, no
    exception, just silent loss the blanket ``except`` never sees."""
    v = _Vault(tmp_path)
    threads, per_thread = 8, 100
    barrier = threading.Barrier(threads)

    def _writer(t):
        barrier.wait()
        for i in range(per_thread):
            _record(v, run_id=f"t{t}", ask_ordinal=i,
                    question_text=f"{_BENIGN_Q} {t}-{i}")

    ts = [threading.Thread(target=_writer, args=(t,)) for t in range(threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(_rows(tmp_path)) == threads * per_thread


def test_concurrent_appends_survive_without_os_file_locking(tmp_path, monkeypatch):
    """The OS file lock is BEST-EFFORT (fcntl/msvcrt may be unavailable, a network
    filesystem may not honour it). The in-process lock is the guaranteed floor and
    must hold alone -- otherwise removing it looks free until OS locking no-ops."""
    monkeypatch.setattr(rm, "_lock_whole_file", lambda fd: False)
    v = _Vault(tmp_path)
    threads, per_thread = 8, 60
    barrier = threading.Barrier(threads)

    def _writer(t):
        barrier.wait()
        for i in range(per_thread):
            _record(v, run_id=f"t{t}", ask_ordinal=i,
                    question_text=f"{_BENIGN_Q} {t}-{i}")

    ts = [threading.Thread(target=_writer, args=(t,)) for t in range(threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(_rows(tmp_path)) == threads * per_thread


def test_the_corpus_is_its_own_file_never_the_deep_lane_ones(tmp_path):
    """DEC-7, literally: quick-lane rows may not touch either deep-lane corpus."""
    v = _Vault(tmp_path)
    _record(v)
    assert _corpus(tmp_path).exists()
    assert not (Path(tmp_path) / "audit" / "ask_corpus.jsonl").exists()
    assert not (Path(tmp_path) / "audit" / "ask_avoidable.jsonl").exists()
