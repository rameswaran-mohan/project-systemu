"""DEC-31 — the redaction-order lint (``tools/lint_redaction_order.py``).

The rule this lint enforces was already written down, verbatim, in
``outbox._esc``: "*Order matters*". The leak shipped twice anyway. A rule that
lives only in a docstring is a convention; this is the control.

The largest block below is the FAIL-OPEN suite. A prior lint in this repo
shipped with two paths that returned the CLEAN value for input never examined —
an unparseable file returned ``[]``, and ``main()`` printed "clean" and exited 0
from any cwd that was not the repo root. A lint whose "all clear" is
indistinguishable from "I looked at nothing" is worse than no lint.
"""
from __future__ import annotations

import subprocess
import textwrap

import pytest

from tools.lint_redaction_order import (RedactionLintError, find_violations,
                                        iter_scanned_files, main, repo_root,
                                        scan_repo)


def _msgs(src: str):
    return [v.message for v in find_violations(textwrap.dedent(src), "f.py")]


def _init_git_repo(path) -> None:
    """A minimal, REAL git repo at ``path``. DEC-34c AC-5: the scanner now
    enumerates via ``git ls-files``, which reads the INDEX — a directory
    that merely holds ``.py`` files on disk is not enough to test it, unlike
    the old ``_SCAN_DIRS``/``rglob`` scanner."""
    subprocess.run(["git", "init", "-q"], cwd=str(path), check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"],
                   cwd=str(path), check=True)
    subprocess.run(["git", "config", "user.name", "redaction-lint-tests"],
                   cwd=str(path), check=True)


def _track(path, *rel_files: str) -> None:
    """``git add`` — stages ``rel_files`` (relative to ``path``) so
    ``git ls-files`` reports them."""
    subprocess.run(["git", "add", "--", *rel_files], cwd=str(path), check=True)


# ── what it must catch ───────────────────────────────────────────────────────

def test_flags_the_shipped_defect_verbatim():
    """``redact(str(value)[:64])`` — the exact expression that leaked a JWT."""
    assert _msgs("""
        def log(value):
            return redact(str(value)[:64])
    """)


def test_flags_the_shipped_defect_with_an_explicit_zero_lower_bound():
    """``redact(str(value)[0:64])`` — the SAME expression as
    :func:`test_flags_the_shipped_defect_verbatim`, one character different,
    and byte-identical at runtime. The first version of this matcher checked
    only ``lower is None`` and missed this spelling of the shipped defect
    entirely — see :func:`tools.lint_redaction_order._is_zero_lower`."""
    assert _msgs("""
        def log(value):
            return redact(str(value)[0:64])
    """)


def test_flags_a_bare_slice_argument():
    assert _msgs("""
        def log(v):
            return redact(v[:64])
    """)


def test_flags_a_bare_slice_argument_with_an_explicit_zero_lower_bound():
    assert _msgs("""
        def log(v):
            return redact(v[0:64])
    """)


def test_the_two_spellings_of_a_prefix_slice_are_flagged_identically():
    """``v[:64]`` and ``v[0:64]`` are ONE defect wearing two spellings, not
    two different findings — the matcher must treat them the same way."""
    no_lower = _msgs("""
        def log(v):
            return redact(v[:64])
    """)
    zero_lower = _msgs("""
        def log(v):
            return redact(v[0:64])
    """)
    assert no_lower and zero_lower
    assert no_lower == zero_lower


def test_flags_a_slice_with_a_NAMED_cap():
    """The cap is usually a constant, not a literal."""
    assert _msgs("""
        def log(v):
            return mask_outbound(v[:MAX_LEN])
    """)


def test_flags_a_module_qualified_redactor():
    assert _msgs("""
        from systemu.runtime import outbox
        def log(v):
            return outbox.redact(v[:64])
    """)


def test_flags_a_slice_nested_deeper_in_the_argument_expression():
    """The slice does not have to be the whole argument."""
    assert _msgs("""
        def log(a, b):
            return redact("prefix " + a[:64] + b)
    """)
    assert _msgs("""
        def log(v):
            return redact(f"value={v[:64]}")
    """)


def test_flags_a_slice_in_a_KEYWORD_argument():
    assert _msgs("""
        def log(v):
            return mask_outbound(text=v[:64])
    """)


def test_flags_every_redactor_in_the_set():
    from tools.lint_redaction_order import _REDACTORS

    for name in sorted(_REDACTORS):
        assert _msgs(f"""
            def log(v):
                return {name}(v[:64])
        """), name


def test_flags_every_redactor_in_the_set_with_an_explicit_zero_lower_bound():
    from tools.lint_redaction_order import _REDACTORS

    for name in sorted(_REDACTORS):
        assert _msgs(f"""
            def log(v):
                return {name}(v[0:64])
        """), name


# ── what it must NOT catch ───────────────────────────────────────────────────

def test_does_not_flag_slicing_the_RESULT_of_a_redaction():
    """``redact(v)[:64]`` is the CORRECT order — masking already happened. If
    this fired, the lint would be pushing people toward the defect."""
    assert _msgs("""
        def log(v):
            return redact(v)[:64]
    """) == []


def test_does_not_flag_the_redactors_own_max_len_cap():
    assert _msgs("""
        def log(v):
            return redact(v, max_len=64)
    """) == []


def test_does_not_flag_TOKENISER_segmentation():
    """THE false-positive class this tree actually contains.

    ``credentials.known_values.redact_known_secrets`` splits text and feeds each
    segment to ``_redact_token``. Those are ``[pos:]`` and ``[pos:m.start()]`` —
    segmentation, not truncation, and correct. A lint that flagged every ``[:``
    would be red on that file from day one.
    """
    assert _msgs("""
        def redact_known_secrets(text, digests, vault, mask):
            out = []
            pos = 0
            for m in SPLIT.finditer(text):
                out.append(_redact_token(text[pos:m.start()], digests, vault, mask))
                pos = m.end()
            out.append(_redact_token(text[pos:], digests, vault, mask))
            return "".join(out)
    """) == []


def test_the_real_known_values_module_is_not_flagged():
    """The claim above, against the actual shipped file rather than a paraphrase."""
    src = (repo_root() / "systemu" / "runtime" / "credentials"
           / "known_values.py").read_text(encoding="utf-8")
    assert find_violations(src, "known_values.py") == []


def test_does_not_flag_a_stride_or_a_suffix_slice():
    assert _msgs("""
        def log(v):
            return redact(v[::2]) + redact(v[3:]) + redact(v[2:9])
    """) == []


def test_a_NONZERO_explicit_lower_bound_is_still_segmentation_not_truncation():
    """Guards the fix against over-matching: teaching the matcher about the
    literal ``0`` must not make it start flagging every explicit lower bound.
    ``v[2:9]`` stays segmentation — only ``v[0:9]`` is the same slice as
    ``v[:9]``."""
    assert _msgs("""
        def log(v):
            return redact(v[2:9])
    """) == []


def test_a_bool_False_lower_bound_is_not_treated_as_the_literal_zero():
    """``_is_zero_lower`` deliberately excludes ``bool`` even though
    ``False == 0`` at runtime — nobody spells a slice bound this way, and the
    matcher should not have to guess at intent. Pins the documented exclusion
    rather than leaving it untested."""
    assert _msgs("""
        def log(v):
            return redact(v[False:64])
    """) == []


def test_does_not_flag_a_ZERO_lower_bound_reached_only_through_a_variable():
    """Documented RESIDUAL blind spot (see ``SCOPE_NOTE``): ``pos`` holds
    ``0`` at runtime, so ``v[pos:64]`` is byte-identical to ``v[0:64]`` — but
    a static AST matcher cannot see a ``Name``'s runtime value without data-
    flow analysis this lint does not do. Named and pinned so the gap stays
    honest rather than silently reappearing as a false claim of coverage."""
    assert _msgs("""
        def log(v):
            pos = 0
            return redact(v[pos:64])
    """) == []


def test_does_not_flag_a_slice_outside_any_redaction_call():
    assert _msgs("""
        def log(v):
            preview = v[:64]
            return preview
    """) == []


def test_does_not_flag_a_non_redactor_that_merely_sounds_like_one():
    """A substring heuristic on 'esc'/'mask'/'scrub' was tried first and matched
    ``_descriptor``, ``_escalate``, ``executescript`` and ``GateDescriptor``."""
    assert _msgs("""
        def build(v):
            return GateDescriptor(v[:64]) + _escalate(v[:64]) + html.escape(v[:64])
    """) == []


# ── the escape hatch ─────────────────────────────────────────────────────────

def test_a_justified_marker_on_the_call_line_suppresses():
    assert _msgs("""
        def log(v):
            return redact(v[:64])  # redaction-lint: ok - items are already safe
    """) == []


def test_a_justified_marker_in_the_comment_block_above_suppresses():
    assert _msgs("""
        def log(v):
            # redaction-lint: ok - v is a list of literal column names, and the
            # slice bounds the COUNT of them, not the length of any secret.
            return redact(v[:64])
    """) == []


def test_a_BARE_marker_does_NOT_suppress():
    """The hatch exists to make someone write down why. A bare "ok" is the hatch
    swallowing the finding it was meant to surface."""
    assert _msgs("""
        def log(v):
            return redact(v[:64])  # redaction-lint: ok
    """) != []


def test_an_unrelated_comment_does_not_suppress():
    assert _msgs("""
        def log(v):
            # this is fine, honest
            return redact(v[:64])
    """) != []


def test_a_marker_cannot_leak_down_past_intervening_code():
    assert _msgs("""
        def log(a, b):
            # redaction-lint: ok - excuses the next line only
            x = redact(a[:64])
            y = redact(b[:64])
            return x + y
    """) != []


# ── FAIL LOUD, never fail open ───────────────────────────────────────────────

def test_unparseable_input_raises_instead_of_returning_the_clean_value():
    """REGRESSION shape — ``except SyntaxError: return []``.

    ``[]`` is precisely the value that means "this file is clean".
    """
    with pytest.raises(RedactionLintError) as exc:
        find_violations("def f(:\n    redact(v[:64])\n", "broken.py")
    assert "broken.py" in str(exc.value)
    assert "parse" in str(exc.value).lower()


def test_a_scan_root_that_is_not_a_checkout_raises(tmp_path):
    with pytest.raises(RedactionLintError):
        scan_repo(tmp_path)
    with pytest.raises(RedactionLintError):
        scan_repo(tmp_path / "definitely-not-here")


def test_a_missing_base_says_WHY_not_merely_that_something_was_empty(tmp_path):
    with pytest.raises(RedactionLintError, match="not a git checkout"):
        scan_repo(tmp_path)


def test_a_directory_that_holds_py_files_but_is_not_a_git_checkout_still_raises(
        tmp_path):
    """DEC-34c AC-5: the scanner enumerates via ``git ls-files``, not a
    directory walk — so files merely SITTING on disk (as the old
    ``_SCAN_DIRS`` layout would have provided) must not be enough to read as
    a clean scan. This replaces the old ``_SCAN_DIRS``-shaped "partial
    checkout" scenario, which no longer applies: there is no longer a fixed
    set of base directories to be partially present."""
    (tmp_path / "systemu").mkdir()
    (tmp_path / "systemu" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(RedactionLintError, match="not a git checkout"):
        scan_repo(tmp_path)


def test_a_git_repo_with_no_tracked_python_files_raises_rather_than_reporting_clean(
        tmp_path):
    """A REAL git checkout that simply has not committed any ``.py`` file
    yet (or tracks none) must still refuse to report "clean" — ``[]`` is the
    value this module treats as "nothing wrong", and a scan that never
    looked at anything must not produce it."""
    _init_git_repo(tmp_path)
    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")
    _track(tmp_path, "README.md")
    with pytest.raises(RedactionLintError, match="never scanned as clean"):
        scan_repo(tmp_path)


def test_an_untracked_py_file_does_not_count_as_scanned(tmp_path):
    """The other half of AC-5's "tracked" requirement: a ``.py`` file that
    exists on disk but was never ``git add``-ed must not be treated as part
    of the tree either — ``git ls-files`` is the source of truth, not the
    filesystem."""
    _init_git_repo(tmp_path)
    (tmp_path / "untracked.py").write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(RedactionLintError, match="never scanned as clean"):
        scan_repo(tmp_path)


def test_an_unparseable_file_inside_a_real_tree_is_reported_not_swallowed(tmp_path):
    """It must not abort the whole scan — the other findings are still worth
    having — but it must appear, with its path, and force a non-zero exit."""
    _init_git_repo(tmp_path)
    (tmp_path / "broken.py").write_text("def f(:\n", encoding="utf-8")
    (tmp_path / "alsobad.py").write_text(
        "def h(v):\n    return redact(v[:64])\n", encoding="utf-8")
    _track(tmp_path, "broken.py", "alsobad.py")

    found = scan_repo(tmp_path)
    joined = " ".join(v.message for v in found)
    assert "UNPARSEABLE" in joined, joined
    assert "broken.py" in " ".join(v.path for v in found)
    assert any("DEC-31" in v.message for v in found), joined


def test_an_unparseable_file_makes_main_exit_NON_zero(tmp_path, monkeypatch, capsys):
    """The end-to-end claim: feeding the gate a file it cannot parse fails the
    gate. A raise that nothing converts into an exit code is not a gate."""
    import tools.lint_redaction_order as mod

    _init_git_repo(tmp_path)
    (tmp_path / "broken.py").write_text("def f(:\n", encoding="utf-8")
    _track(tmp_path, "broken.py")
    monkeypatch.setattr(mod, "repo_root", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)

    assert mod.main() == 1
    assert "UNPARSEABLE" in capsys.readouterr().out


def test_main_does_not_report_clean_from_a_foreign_working_directory(
        monkeypatch, tmp_path, capsys):
    """REGRESSION shape — ``main()`` exited 0 with "clean" from ANY cwd that was
    not the repo root, because the base was ``Path.cwd()/...``.

    The base comes from ``__file__``, so the cwd is irrelevant: running from
    elsewhere scans the real tree rather than an imaginary empty one.
    """
    monkeypatch.chdir(tmp_path)
    rc = main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "clean" in out
    # ...and it says how much it looked at, so "clean" is falsifiable
    assert "file(s) scanned" in out
    n = int(out.split("clean")[1].split("file(s)")[0].strip().split()[-1])
    assert n > 500, out


def test_main_returns_a_DISTINCT_code_when_it_could_not_run(monkeypatch, capsys):
    """Exit 1 means "violations found"; exit 2 means "the lint did not run".
    Collapsing them lets a broken checkout read as a passing gate."""
    import tools.lint_redaction_order as mod

    def _boom(*a, **k):
        raise RedactionLintError("no checkout here")

    monkeypatch.setattr(mod, "scan_repo", _boom)
    assert mod.main() == 2
    assert "CANNOT RUN" in capsys.readouterr().err


def test_tracked_python_files_rejects_a_falsy_root_rather_than_defaulting(tmp_path):
    """``root or repo_root()`` would turn an explicit-but-empty root into the
    default. ``Path('')`` normalises to the CWD."""
    from tools.lint_redaction_order import _tracked_python_files

    with pytest.raises(RedactionLintError):
        _tracked_python_files(tmp_path / "nope")


def test_the_scope_limitation_is_printed_with_the_RESULT(monkeypatch, tmp_path,
                                                         capsys):
    """The matcher cannot see the two-line form or a truncation helper. That
    limit belongs where the "clean" is read, not in a docstring the reader of a
    green CI line never opens."""
    monkeypatch.chdir(tmp_path)
    main()
    out = capsys.readouterr().out
    assert "CANNOT see" in out
    assert "NOT 'nothing is truncated before it is redacted'" in out


# ── the actual gate ──────────────────────────────────────────────────────────

def test_the_real_tree_has_no_truncate_then_redact_site():
    violations = scan_repo()
    assert violations == [], "\n".join(
        f"{v.path}:{v.line} {v.message}" for v in violations)


def test_the_scan_actually_reaches_the_real_trees():
    """Guards the vacuous-pass shape: a scan that silently walked an empty or
    wrong directory would make the gate above pass unconditionally."""
    files = list(iter_scanned_files())
    assert len(files) > 500, len(files)
    names = {f.name for f in files}
    assert "outbox.py" in names
    assert "shadow_runtime.py" in names
    assert "known_values.py" in names


def test_the_scan_reaches_files_the_old_fixed_dir_list_used_to_miss():
    """DEC-34c AC-5, against the REAL tree: ``_SCAN_DIRS`` used to be
    ``("systemu", "sharing_on", "tools", "tests")`` — four names that left a
    tracked repo-root script, and whole tracked directories such as
    ``alembic/``, ``plugins/``, and ``scripts/``, unscanned forever. All of
    them must be reachable now."""
    rels = {str(f.relative_to(repo_root())).replace("\\", "/")
            for f in iter_scanned_files()}
    assert "alembic/env.py" in rels, sorted(r for r in rels if "alembic" in r)
    assert any(r.startswith("scripts/") for r in rels), (
        "no scripts/*.py reached — the old _SCAN_DIRS list omitted this "
        "directory entirely")
    # at least one TRACKED file directly at the repo root (not inside any
    # of the four old _SCAN_DIRS names) must be present too.
    root_level = {r for r in rels if "/" not in r}
    assert root_level, "no repo-root .py file reached the scan"


def test_the_escape_hatch_is_used_ONLY_by_the_defect_characterisations():
    """Every use of the hatch in this tree lives in the DEC-31 test that
    demonstrates the leak, where writing the safe order would assert nothing.

    If a use appears anywhere else, someone silenced a real finding — this fails
    and makes them say so in a diff rather than in a comment nobody reads. The
    count is pinned too: seven is what the demonstrations need (five from the
    original ``[:64]`` characterisation, plus two added when the matcher
    learned to also catch the explicit-zero ``[0:64]`` spelling), and an
    eighth in the same file is just as much a decision as one in a new file.
    """
    from tools.lint_redaction_order import _ALLOW_MARKER

    # Counted by FILE, not by line number: a line-number pin breaks on any
    # unrelated edit above it, and a pin people routinely re-baseline stops
    # being read.
    hits = {}
    for py in iter_scanned_files():
        if py.name in ("lint_redaction_order.py",           # defines the marker
                       "test_dec31_redaction_order_lint.py"):  # tests it
            continue
        n = sum(1 for line in py.read_text(encoding="utf-8").splitlines()
                if _ALLOW_MARKER in line)
        if n:
            hits[py.name] = n
    assert hits == {"test_dec31_redaction_before_truncation.py": 7}, hits
