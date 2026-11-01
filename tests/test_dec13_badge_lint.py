"""DEC-43 -- the verification-badge lint (``tools/lint_verification_badge.py``).

The lint fails any registered surface that computes the badge itself instead of
calling the one mint. Enforcing from day one (no baseline): the tree is clean.
The planted-violation fixture proves the lint is alive; the clean fixture proves
it is not firing on a correct consumer; the fail-loud cases prove an empty or
broken scan is never mistaken for a clean one.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tools import lint_verification_badge as lint

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "dec13_badge_lint"


def _findings(name: str, function=None):
    p = FIXTURES / name
    return lint.find_violations(p.read_text(encoding="utf-8"), p.name, function=function)


def test_the_tree_is_clean():
    findings = lint.scan_repo()
    assert findings == [], "\n".join(map(str, findings))


def test_main_exits_zero_on_the_tree(capsys):
    assert lint.main([]) == 0
    assert "surfaces consume the one mint" in capsys.readouterr().out


# ── the planted-violation self-test (DEC-40 applied to the fence) ────────────

def test_the_planted_violation_is_reported_line_by_line():
    msgs = [(f.line, f.message) for f in _findings("planted_violation.py")]
    text = "\n".join(m for _, m in msgs)
    assert "reads the mint's private input .get('confirmed')" in text
    assert "reads the mint's private input ['shadow']" in text
    assert "reads the mint's private input .confirmed" in text
    assert "spells the badge state 'Verified' itself" in text
    assert "spells the badge state 'claimed' itself" in text
    assert "filled check 'check_circle'" in text
    assert "imports the receipt store reader read_receipts" in text
    assert "reads the receipt store (read_receipts)" in text
    assert "never references the verification-badge mint" in text


def test_the_planted_violation_fails_the_scan_when_registered(tmp_path, monkeypatch):
    """Register the planted file as a surface: the repo scan must go red."""
    from systemu.runtime import verification_badge as vb
    dst = tmp_path / "planted_violation.py"
    dst.write_text((FIXTURES / "planted_violation.py").read_text(encoding="utf-8"),
                   encoding="utf-8")
    monkeypatch.setattr(vb, "SURFACES", (("planted", "planted_violation.py"),))
    found = lint.scan_repo(tmp_path)
    assert len(found) >= 9
    assert {f.path for f in found} == {"planted_violation.py"}


def test_the_clean_consumer_is_not_reported():
    assert _findings("planted_clean.py") == []


def test_a_function_scope_only_sees_its_own_body():
    assert _findings("planted_violation.py", function="badge_icon")
    found = _findings("planted_clean.py", function="render")
    assert found == []


# ── fail loud: never a clean result from a scan that did not run ─────────────

def test_an_empty_registry_raises(monkeypatch):
    from systemu.runtime import verification_badge as vb
    monkeypatch.setattr(vb, "SURFACES", ())
    with pytest.raises(lint.BadgeLintError):
        lint.scan_repo()
    assert lint.main([]) == 2


def test_a_missing_surface_file_raises(monkeypatch):
    from systemu.runtime import verification_badge as vb
    monkeypatch.setattr(vb, "SURFACES", (("gone", "systemu/no_such_surface.py"),))
    with pytest.raises(lint.BadgeLintError):
        lint.scan_repo()


def test_a_missing_surface_function_raises():
    with pytest.raises(lint.BadgeLintError):
        _findings("planted_clean.py", function="no_such_function")


def test_an_unparseable_surface_raises():
    with pytest.raises(lint.BadgeLintError):
        lint.find_violations("def broken(:\n", "broken.py")


def test_the_pattern_set_is_generated_from_the_mint(monkeypatch):
    """The lint spells no verdict itself: change the mint's tokens and the lint
    follows (DEC-43(1): the registry is the single spelling)."""
    from systemu.runtime import verification_badge as vb
    src = ("import systemu.runtime.verification_badge\n"
           "def f():\n    return 'Shiny'\n")
    assert lint.find_violations(src, "f.py") == []
    monkeypatch.setattr(vb, "VERDICT_TOKENS", frozenset({"shiny"}))
    msgs = [f.message for f in lint.find_violations(src, "f.py")]
    assert any("'Shiny'" in m for m in msgs)
