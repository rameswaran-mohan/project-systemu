"""D8 -- `info` may not print a green verdict beside a model nobody will serve.

WITNESSED DEFECT (v0.10.27, a from-scratch home with no provider key)
    ``systemu info`` ended with, in green::

        v Configuration OK  (model: deepseek/deepseek-v4-flash)

    On that machine Ollama was the ONLY satisfied provider. The model named on
    that line is an OpenRouter catalog id, OpenRouter had no key, and nothing
    on the line said which of the two facts the green tick belonged to. An
    operator reads it as "this model is ready"; what it actually meant was
    "some provider somewhere is usable, and here is an unrelated config field".

    The daemon's startup banner has attributed its provider rows since F19/DEC-43
    -- it prints the MINTED state of every provider and warns when none is
    usable. ``info`` printed a verdict with no attribution at all, and
    ``_print_startup_banner``'s ``Model:`` row carried the same unattributed id.

THE RULING
    ``info`` attributes the verdict the way the daemon banner does: it names the
    satisfying provider(s) and the model that provider will actually serve, and
    when the configured model belongs to an UNSATISFIED provider it says so on
    that line. The capture banner's ``Model:`` row moves with it.

    Nothing here re-derives satisfaction or routing: both come from THE ONE MINT
    (``systemu.runtime.provider_status``), which is what keeps this surface from
    becoming the sixth private copy of the recipe F19 removed.

NO NETWORK
    The keyless witness is injected at the mint's own probe table, exactly as
    tests/test_welcome_tier_readiness.py and tests/test_p1c_cosmetics.py do, and
    the credential half is a function of environment variables this file clears
    and sets. The machine the suite runs on cannot change any verdict below.
"""
from __future__ import annotations

import pytest
from click.testing import CliRunner

#: The shipped default on a fresh install -- an OpenRouter catalog id, which is
#: the whole point of case (a): it is NOT what a keyless machine will run.
DEFAULT_MODEL = "deepseek/deepseek-v4-flash"


@pytest.fixture(autouse=True)
def _bare_machine(monkeypatch):
    """Build the machine each test asserts on; never inherit the developer's.

    ``sharing_on.config`` loads ``.env`` at IMPORT time, so without this the
    verdict would depend on whose checkout the suite is running in. Removes
    only, then each test sets what it wants. ``COLUMNS`` pins the rich console
    width so a wrap cannot decide whether a substring assertion passes.
    """
    from systemu.runtime import provider_status as ps
    for spec in ps.PROVIDER_SPECS:
        monkeypatch.delenv(spec.env, raising=False)
    for i in (1, 2, 3):
        monkeypatch.delenv(f"SYSTEMU_TIER{i}_MODEL", raising=False)
        monkeypatch.delenv(f"SYSTEMU_TIER{i}_PROVIDER", raising=False)
    monkeypatch.delenv("SHARING_ON_MODEL", raising=False)
    monkeypatch.delenv("SYSTEMU_MODEL_PRESET", raising=False)
    monkeypatch.setenv("COLUMNS", "200")
    ps.clear_probe_cache()
    yield
    ps.clear_probe_cache()


def _answering(_url, _timeout):
    from systemu.runtime import provider_status as ps
    return (ps.STATE_REACHABLE, "answered at the test endpoint with 1 model(s)")


def _silent(_url, _timeout):
    from systemu.runtime import provider_status as ps
    return (ps.STATE_UNREACHABLE, "nothing answered (test)")


def _flat(text: str) -> str:
    """Collapse every whitespace run, so a console wrap cannot break a match."""
    return " ".join(text.split())


def _run_info(monkeypatch, probe) -> str:
    from systemu.runtime import provider_status as ps
    from sharing_on.cli import cli
    monkeypatch.setitem(ps._PROBES, "ollama", probe)
    ps.clear_probe_cache()
    res = CliRunner().invoke(cli, ["info"])
    assert res.exit_code == 0, res.output
    return _flat(res.output)


def _hint() -> str:
    """The mint's own remedy line, so the assertion cannot drift from the copy."""
    from systemu.runtime import provider_status as ps
    from sharing_on.config import Config
    return _flat(ps.configure_hint(ps.all_provider_statuses(
        Config.from_env(), probe=ps.unprobed)))


# --------------------------------------------------------------------------- #
# (a) the witnessed machine: keyless provider up, cloud model configured
# --------------------------------------------------------------------------- #

def test_the_ok_line_names_the_provider_that_is_actually_usable(monkeypatch):
    """The green tick belongs to Ollama, so the line has to say Ollama."""
    out = _run_info(monkeypatch, _answering)
    assert "Configuration OK" in out, out
    assert "Ollama" in out, (
        "the verdict is green because OLLAMA is satisfied, and the operator is "
        f"never told so: {out}")


def test_the_ok_line_says_the_configured_model_is_not_what_will_serve(monkeypatch):
    """The witnessed defect itself: a cloud model id sitting inside a green
    verdict on a machine that cannot call it."""
    out = _run_info(monkeypatch, _answering)
    assert DEFAULT_MODEL in out, out
    assert "OpenRouter" in out, (
        "the model is an OpenRouter id and OpenRouter is unusable here; the "
        f"line must name the provider it belongs to: {out}")
    assert "not usable" in out.lower(), (
        f"the line never says the configured model cannot be served: {out}")


def test_the_bare_witnessed_line_would_still_fail_this_file(monkeypatch):
    """Anti-vacuity: the exact string that shipped does not satisfy the pins.

    Without this, a change that merely reworded the line could pass the two
    tests above by accident.
    """
    witnessed = _flat("v Configuration OK  (model: deepseek/deepseek-v4-flash)")
    assert "Ollama" not in witnessed
    assert "not usable" not in witnessed.lower()


# --------------------------------------------------------------------------- #
# (b) the keyed machine: the model IS served by a satisfied provider
# --------------------------------------------------------------------------- #

def test_a_keyed_machine_names_the_provider_that_will_serve_the_model(monkeypatch):
    """The counterpart, so the attribution is not a warning that always fires."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-present")
    out = _run_info(monkeypatch, _silent)
    assert "Configuration OK" in out, out
    assert "OpenRouter" in out, out
    assert DEFAULT_MODEL in out, out
    assert "not usable" not in out.lower(), (
        "OpenRouter holds a key and serves this model; warning here would be "
        f"crying wolf about a working install: {out}")


# --------------------------------------------------------------------------- #
# (c) nothing satisfied: no green tick, and the remedy is named
# --------------------------------------------------------------------------- #

def test_a_machine_with_nothing_usable_gets_no_ok_and_gets_the_remedy(monkeypatch):
    out = _run_info(monkeypatch, _silent)
    assert "Configuration OK" not in out, (
        f"nothing on this machine is usable and info still said OK: {out}")
    assert _hint() in out, (
        f"the configure remedy the mint generates is missing: {out}")


# --------------------------------------------------------------------------- #
# the capture banner's Model: row carries the same attribution
# --------------------------------------------------------------------------- #

def _banner(monkeypatch, capsys, probe) -> str:
    from systemu.runtime import provider_status as ps
    from sharing_on.cli import _print_startup_banner
    from sharing_on.config import Config
    from sharing_on.platform_info import detect_platform
    monkeypatch.setitem(ps._PROBES, "ollama", probe)
    ps.clear_probe_cache()
    _print_startup_banner("a task", detect_platform(), Config.from_env())
    return _flat(capsys.readouterr().out)


def test_the_capture_banner_model_row_is_attributed_too(monkeypatch, capsys):
    """`_print_startup_banner` printed the same unattributed id, at the moment a
    run starts -- the other half of the same defect, moved with it."""
    out = _banner(monkeypatch, capsys, _answering)
    assert DEFAULT_MODEL in out, out
    assert "OpenRouter" in out, (
        f"the banner still names a model with no provider attached: {out}")
    assert "not usable" in out.lower(), out


def test_the_banner_says_nothing_alarming_on_a_machine_that_works(
        monkeypatch, capsys):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-present")
    out = _banner(monkeypatch, capsys, _silent)
    assert DEFAULT_MODEL in out, out
    assert "not usable" not in out.lower(), out


# --------------------------------------------------------------------------- #
# it consumes the mint rather than re-deriving the fact
# --------------------------------------------------------------------------- #

def test_the_attribution_is_minted_not_re_derived(monkeypatch):
    """DEC-43: silence the mint's routing derivation and this surface withholds
    the claim instead of computing a second answer from the model prefix."""
    from systemu.runtime import provider_status as ps
    monkeypatch.setattr(ps, "routed_provider", lambda _m, _o, _c: "")
    out = _run_info(monkeypatch, _answering)
    assert "OpenRouter" not in out, (
        "the mint could not attribute the model, so this surface must not "
        f"assert a provider of its own: {out}")


def test_nothing_raises_into_info_when_the_mint_blows_up(monkeypatch):
    """CARE: an inspection command must still render if attribution fails."""
    from systemu.runtime import provider_status as ps
    monkeypatch.setattr(ps, "all_provider_statuses",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    from sharing_on.cli import cli
    res = CliRunner().invoke(cli, ["info"])
    assert res.exit_code == 0, res.output
    assert "System Info" in _flat(res.output)


def test_every_line_info_prints_is_ascii(monkeypatch):
    """DEC-32c: this crosses a Windows console. The attribution is generated
    text, so it is held to the same bar as the rest of the operator output."""
    from systemu.runtime import provider_status as ps
    from sharing_on.cli import _provider_attribution_lines
    from sharing_on.config import Config
    monkeypatch.setitem(ps._PROBES, "ollama", _answering)
    ps.clear_probe_cache()
    for line in _provider_attribution_lines(Config.from_env()):
        assert line.isascii(), repr(line)
