"""D13 -- the cross-session-recall degradation names a REMEDY, not a module.

THE DEFECT
    A wizard `quality` preset selects an Anthropic tier-1 model. On an install
    without the Anthropic SDK the tier-1 summarize call raises
    `ModuleNotFoundError: No module named 'anthropic'`, `capture()` degrades to
    None, and the operator's console gets:

        Cross-session recall did not record this session: no episodic summary
        was stored, so future sessions cannot recall it.
        Reason: No module named 'anthropic'

    Everything in that line is true and none of it is actionable. The operator
    is told the name of a Python module they never asked for and left to guess
    that the fix is a pip install -- the exact shape `optional_deps` exists to
    prevent everywhere else in the product.

THE PROPERTY
    When the reason a summary was not stored is a MISSING PACKAGE, the line
    names the install command:
      * a package the optional-deps honesty registry knows -> that group's
        remedy, `pip install "systemu[<extra>]"`, plus its post-install step;
      * a package no registered extra ships -> the plain `pip install <pkg>`
        line, and NEVER an invented extra;
      * anything that is not an import failure -> the line is unchanged.

NO PRODUCT STUBS. `episodic_memory.capture` is driven through its real path;
only the tier-1 LLM call (the thing that raises) and `log_event` (the console
this is about) are replaced, and neither of them decides the outcome under
test -- the helper does.
"""
from __future__ import annotations

import types
from unittest.mock import patch

import pytest

from systemu.runtime import episodic_memory as EM
from systemu.runtime import optional_deps


# -- the environment the witnessed failure happened in ----------------------

def _config():
    """A config with recall on and a provider present, so `capture` reaches
    the summarize call instead of short-circuiting before it."""
    return types.SimpleNamespace(
        episodic_memory_enabled=True,
        openrouter_api_key="k",
        episodic_summary_max_chars=800,
        episodic_tags_max_count=8,
    )


class _EmptyVault:
    def query_session_summaries(self, limit=None):
        return []


def _degraded_line(exc) -> str:
    """The operator-facing line `capture()` emits when the summarize call
    raises `exc`. This goes through the REAL capture path -- the emission site
    is what the finding is about, so a helper-only test would not witness it."""
    with patch.object(EM, "llm_call_json", side_effect=exc), \
         patch("systemu.interface.notifications.log_event") as log_event:
        out = EM.capture(
            vault=_EmptyVault(), session_id="s-d13", intent="i",
            chat_result="r", files_produced=[], status="success",
            config=_config())

    assert out is None
    assert log_event.called, "the degradation was never told to the operator"
    return log_event.call_args.args[2]


# -- a registered optional group: the remedy is the extra -------------------

def test_a_registered_package_is_named_with_its_pip_extra():
    """`playwright` is in `optional_deps.GROUPS`, so the operator is given the
    one command that installs it -- quoted, because zsh refuses the unquoted
    form (F22), and with the post-install step the wheel alone does not do."""
    group = optional_deps.group_for_package("playwright")
    assert group is not None, "the registry stopped shipping the browser group"

    line = _degraded_line(ModuleNotFoundError(
        "No module named 'playwright'", name="playwright"))

    assert optional_deps.install_command(group.packages) in line, line
    assert 'systemu[browser]' in line, line
    assert group.post_install in line, line
    # ...and the operator is still told WHAT went dark.
    assert "recall" in line.lower(), line


def test_the_registered_remedy_comes_from_the_registry_not_a_second_copy():
    """DEC-43 form (i): the pip line is the ONE builder's, so renaming the
    extra in `optional_deps` moves this line too instead of leaving a second,
    drifting recipe behind."""
    group = optional_deps.group_for_package("nicegui")
    assert group is not None, "the registry stopped shipping the dashboard group"

    line = _degraded_line(ModuleNotFoundError(
        "No module named 'nicegui'", name="nicegui"))

    assert optional_deps.install_command(group.packages) in line, line
    assert group.label in line, line


def test_the_witnessed_anthropic_case_names_its_pip_extra():
    """THE WITNESSED CASE. It anticipated this move ("anthropic joined the
    registry -- this test now belongs above"): `anthropic` is a registered
    group now, so the operator is given the extra that really installs it --
    the registry's own F22 line -- and not the bare package name."""
    group = optional_deps.group_for_package("anthropic")
    assert group is not None, "the registry stopped shipping the anthropic group"

    line = _degraded_line(ModuleNotFoundError(
        "No module named 'anthropic'", name="anthropic"))

    assert optional_deps.install_command(group.packages) in line, line
    assert 'pip install "systemu[anthropic]"' in line, line
    assert "(pip install anthropic)" not in line, (
        "the plain-package line survived the group registration")
    assert "recall" in line.lower(), line


# -- the package name is read off the exception, whatever its shape ---------

def test_a_bare_import_error_still_yields_the_package_name():
    """A plain `ImportError` carries the name only in its text. The remedy is
    the point of the line; losing it to a missing `.name` attribute would be
    the same silence in a rarer shape."""
    line = _degraded_line(ImportError("No module named 'anthropic'"))
    assert 'pip install "systemu[anthropic]"' in line, line


def test_a_submodule_failure_names_the_top_level_distribution():
    """`No module named 'anthropic.types'` is still the `anthropic` remedy.
    Handing the operator `pip install anthropic.types` would be a remedy that
    cannot run -- worse than the module name it replaced."""
    line = _degraded_line(ModuleNotFoundError(
        "No module named 'anthropic.types'", name="anthropic.types"))
    assert 'pip install "systemu[anthropic]"' in line, line
    assert "anthropic.types" not in line, line


# -- everything else is untouched -------------------------------------------

def test_a_non_import_reason_is_unchanged():
    """The line only changes for the one cause it has a remedy for. A summarize
    call that 500s is not a missing package and must not be told to pip."""
    line = _degraded_line(ValueError("upstream returned 500"))
    assert "Reason: upstream returned 500" in line, line
    assert "pip install" not in line, line


def test_a_non_json_result_is_unchanged():
    """The second, non-raising degradation path has no exception at all -- it
    must keep its own reason rather than inheriting a stale remedy."""
    with patch.object(EM, "llm_call_json", return_value=["not", "a", "dict"]), \
         patch("systemu.interface.notifications.log_event") as log_event:
        EM.capture(vault=_EmptyVault(), session_id="s-d13b", intent="i",
                   chat_result="r", files_produced=[], status="success",
                   config=_config())

    line = log_event.call_args.args[2]
    assert "not a JSON object" in line, line
    assert "pip install" not in line, line


def test_the_line_is_ascii_on_every_branch():
    """DEC-32c -- this crosses into event_log.jsonl, the dashboard panes and a
    Windows console."""
    for exc in (ModuleNotFoundError("No module named 'anthropic'", name="anthropic"),
                ModuleNotFoundError("No module named 'playwright'", name="playwright"),
                ValueError("café")):
        line = _degraded_line(exc)
        line.encode("ascii")


# -- the helper itself, and its fences ---------------------------------------

def test_the_helper_declines_anything_that_is_not_an_import_failure():
    """`None` is the "leave the line alone" answer, and it is the DEFAULT: a
    shape this helper does not recognise must not produce a guessed remedy."""
    for value in (ValueError("boom"), RuntimeError("boom"), None, "ImportError",
                  object(), 7):
        assert EM.missing_dependency_remedy(value) is None, value


def test_a_reason_that_merely_QUOTES_an_import_error_is_not_one():
    """The type pin is what decides, not the text. A tier-1 provider that
    returns somebody else's traceback in its error body -- or a wrapper that
    re-raises the words -- is not a missing SDK on THIS machine, and telling
    the operator to pip install something because a remote message said "No
    module named" is a remedy for a problem they do not have."""
    line = _degraded_line(RuntimeError(
        "the summarizer failed: No module named 'anthropic'"))
    assert "pip install" not in line, line
    assert "Reason: the summarizer failed" in line, line


def test_a_non_import_exception_carrying_a_name_is_not_an_import_failure():
    """`.name` is an attribute anything can have. It is read only AFTER the
    concrete class is pinned in the deciding frame (DEC-36), so a value that
    merely looks like an import failure cannot mint a remedy."""
    exc = ValueError("boom")
    exc.name = "anthropic"
    assert EM.missing_dependency_remedy(exc) is None
    line = _degraded_line(exc)
    assert "pip install" not in line, line


def test_an_import_error_SUBCLASS_falls_to_the_unchanged_line():
    """Documenting the direction the pin fails in: an unrecognised shape loses
    the remedy, never the notice. Losing polish is recoverable; a confidently
    wrong install command is not."""
    class _VendoredImportError(ImportError):
        pass

    assert EM.missing_dependency_remedy(
        _VendoredImportError("No module named 'anthropic'")) is None


def test_the_helper_declines_an_unusable_package_name():
    """The name comes from an exception somebody else raised. A value that is
    not a plain, printable distribution name yields no remedy at all rather
    than a `pip install` line with somebody else's text in it."""
    for name in ("", "  ", "not a package", "a" * 200, "pkg;rm -rf /", "-x",
                 b"anthropic", None):
        exc = ModuleNotFoundError("No module named '?'")
        exc.name = name
        assert EM.missing_dependency_remedy(exc) is None, repr(name)


def test_the_helper_is_pure_of_the_installed_environment():
    """The import ALREADY failed -- that is the witness. Re-asking the
    metadata whether the package is installed would be a second, cheaper
    source that can disagree with it (DEC-34), and a broken-but-present dist
    would answer "installed" and take the remedy away."""
    here = EM.missing_dependency_remedy(
        ModuleNotFoundError("No module named 'nicegui'", name="nicegui"))
    assert here and "pip install" in here, here
    # nicegui IS installed in this interpreter for the dashboard tests; the
    # remedy must survive that.
    assert optional_deps.is_installed("nicegui") in (True, False)


# -- THE PIN: the emission site really consults the helper -------------------

def test_the_emission_site_consults_the_helper_and_not_str_exc():
    """MUTATION PIN. Bypass the helper -- go back to `Reason: {str(exc)}` --
    and this goes red: the raw module text is exactly what the operator was
    given before, and it is not allowed to be the whole line any more."""
    line = _degraded_line(ModuleNotFoundError(
        "No module named 'anthropic'", name="anthropic"))

    assert "Reason: No module named 'anthropic'" not in line, line
    assert 'pip install "systemu[anthropic]"' in line, line


def test_the_helper_raising_does_not_cost_the_operator_the_notice():
    """A cosmetic helper may not become a new way to lose the whole warning.
    The degradation notice is the load-bearing half; the remedy is the polish
    on it."""
    with patch.object(EM, "missing_dependency_remedy",
                      side_effect=RuntimeError("helper exploded")):
        line = _degraded_line(ModuleNotFoundError(
            "No module named 'anthropic'", name="anthropic"))
    assert "recall" in line.lower(), line


def test_the_second_gate_carries_the_remedy_too():
    """GATE-7a corollary: `capture()` returning None and `capture()` RAISING
    are two gates on the same fact. A remedy on one and a module name on the
    other is the half-fix this project keeps re-finding."""
    from systemu.runtime import shadow_runtime as SR

    cfg = types.SimpleNamespace(summarize_after_run=True,
                                episodic_memory_enabled=True)

    with patch("systemu.runtime.episodic_memory.capture",
               side_effect=ModuleNotFoundError("No module named 'anthropic'",
                                               name="anthropic")), \
         patch("systemu.interface.notifications.log_event") as log_event:
        SR._trigger_episodic_capture(
            vault=object(), config=cfg, session_id="s-d13c", intent="i",
            chat_result="r", files_produced=[], status="success")

    lines = [c.args[2] for c in log_event.call_args_list
             if len(c.args) > 3 and (c.args[3] or {}).get("kind") == "episodic_degraded"]
    assert lines, "the raising gate told the operator nothing"
    assert 'pip install "systemu[anthropic]"' in lines[0], lines[0]


def test_the_event_record_keeps_the_raw_reason_for_diagnosis():
    """The operator gets the remedy; the log keeps the cause. Replacing the
    stored reason would trade one blindness for another."""
    with patch.object(EM, "llm_call_json",
                      side_effect=ModuleNotFoundError(
                          "No module named 'anthropic'", name="anthropic")), \
         patch("systemu.interface.notifications.log_event") as log_event:
        EM.capture(vault=_EmptyVault(), session_id="s-d13d", intent="i",
                   chat_result="r", files_produced=[], status="success",
                   config=_config())

    ctx = log_event.call_args.args[3]
    assert ctx.get("kind") == "episodic_degraded"
    assert "No module named 'anthropic'" in ctx.get("reason", ""), ctx


# -- the unregistered branch keeps a witness after `anthropic` moved above ----

def test_an_unregistered_package_names_the_plain_pip_line_and_invents_no_extra():
    """`anthropic` used to be this branch's only witness. With it registered,
    the branch needs a package no extra ships, or "never an invented
    `systemu[...]`" stops being pinned at all -- pip accepts an unknown extra
    and silently installs nothing."""
    assert optional_deps.group_for_package("openpyxl") is None, (
        "openpyxl joined the registry -- pick another unregistered package")

    line = _degraded_line(ModuleNotFoundError(
        "No module named 'openpyxl'", name="openpyxl"))

    assert "the openpyxl SDK is not installed (pip install openpyxl)" in line, line
    assert "systemu[" not in line, line
