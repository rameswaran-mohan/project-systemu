"""DEC-43 - ONE verdict for "can this install run?", on every surface.

THE DEFECT (observed live, one machine, one minute). No provider keys at all;
Ollama answering on http://localhost:11434 with 1 model installed; the shipped
default tier models, none of which is an Ollama model:

    systemu daemon start   -> BOOTS          (setup_flow.key_present() -> True)
    /welcome step 1        -> "No LLM provider is usable yet"
    /welcome Finish        -> REFUSED        ("Set up an LLM provider first")
    systemu doctor         -> Ollama "OK   Reachable"

Two answers to one operator-facing question. F19 had already removed the SIX
private copies of the satisfaction recipe, so this was not a second recipe --
it was a POLICY SPLIT inside the one mint. ``setup_flow.provider_available``
(the boot gate) spent the keyless witness unconditionally, while
``provider_status.any_provider_usable`` (the wizard gate, the first-run
checklist, the two hot short-circuits) spent it only when ``selects_keyless``
reported that a tier ALREADY pointed at the keyless provider.

That made SATISFACTION depend on SELECTION, which the mint's own docstring
calls a different fact ("The tier selection is a DIFFERENT fact from
satisfaction"), and it closed the loop the wizard itself opens: step 1's remedy
text offers "Ollama (no key - run it locally, or point OLLAMA_URL at it)", and
an operator who did exactly that was still refused.

THE PROPERTY pinned here:

    A keyless provider that ACTUALLY ANSWERS a live probe is usable on EVERY
    surface -- boot gate, wizard gate, first-run checklist -- whatever a tier
    currently selects. One witness policy, one verdict.

Tier viability remains a separate fact with its own separate surface
(``provider_status.unusable_selected``, the Settings red flag): a machine can
be usable while the tier the operator picked is not, and those are two honest
statements rather than two answers to one question.

No network: every probe is injected or monkeypatched onto the mint, and the
"machine" is a config view built here.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _bare_machine(monkeypatch):
    """Construct the machine these tests assert on; never inherit one.

    Same reasoning as tests/test_f19_provider_surface_parity.py::
    _bare_provider_env -- the surfaces under test consult os.environ as well as
    the config, so the verdict would otherwise depend on whatever the rest of
    the suite (or the developer's shell) left lying around. Removes only.
    """
    from systemu.runtime import provider_status as ps
    for spec in ps.PROVIDER_SPECS:
        monkeypatch.delenv(spec.env, raising=False)
    for i in (1, 2, 3):
        monkeypatch.delenv(f"SYSTEMU_TIER{i}_PROVIDER", raising=False)
    ps.clear_probe_cache()
    yield
    ps.clear_probe_cache()


def _machine(**kw):
    """A config view carrying exactly what the mint and its consumers read."""
    from systemu.runtime import provider_status as ps
    base = {s.attr: "" for s in ps.PROVIDER_SPECS}
    base.update({f"tier{i}_provider": "" for i in (1, 2, 3)})
    # The shipped defaults: NOT an Ollama model on any tier. This is the
    # machine shape the defect was found on.
    base.update({f"tier{i}_model": "deepseek/deepseek-v4-flash"
                 for i in (1, 2, 3)})
    base.update(output_dir="", non_interactive=False, vault_dir="/tmp/v")
    base.update(kw)
    return SimpleNamespace(**base)


def _keyless_only(**kw):
    """No keys anywhere; a keyless endpoint configured. Answering is decided
    by whichever witness the surface spends -- which is the whole point."""
    return _machine(ollama_url="http://localhost:11434", **kw)


def _answering(_url, _timeout):
    from systemu.runtime import provider_status as ps
    return (ps.STATE_REACHABLE, "answered at the test endpoint with 1 model(s)")


def _silent(_url, _timeout):
    from systemu.runtime import provider_status as ps
    return (ps.STATE_UNREACHABLE, "nothing answered (test)")


def _install_witness(monkeypatch, fn):
    """Make ``fn`` THE production witness for the keyless provider.

    The wizard gate takes no probe argument -- it is production code asking the
    mint -- so the injection has to be at the mint's own probe table. That is
    also what keeps this test off the network.
    """
    from systemu.runtime import provider_status as ps
    monkeypatch.setitem(ps._PROBES, "ollama", fn)


# -- the headline: two gates, one machine, one verdict -----------------------

def _boot_gate(cfg) -> bool:
    """`systemu daemon start`'s admission gate, called exactly as it is there."""
    from sharing_on.setup_flow import provider_available
    return provider_available(config=cfg)


def _wizard_gate(cfg) -> tuple:
    """/welcome's Finish gate. ``name=""`` so the NEXT gate after the provider
    one always fails: the message tells us which gate we stopped at, without
    needing a vault to persist into."""
    from systemu.interface.pages import welcome
    return welcome.finalize_onboarding(None, cfg, name="",
                                       refresh_key_fn=lambda c: False)


def test_boot_gate_and_wizard_gate_agree_when_the_keyless_provider_answers(
        monkeypatch):
    """THE defect. Both said the opposite of each other on this exact machine."""
    _install_witness(monkeypatch, _answering)
    cfg = _keyless_only()

    assert _boot_gate(cfg) is True, "the daemon boots on this machine (it did)"

    ok, msg = _wizard_gate(cfg)
    assert ok is False and "name" in msg.lower(), (
        "the wizard must get PAST the provider gate and stop at the next one; "
        f"it answered {msg!r}")
    assert "provider" not in msg.lower(), (
        "the wizard refused a machine the daemon boots on -- the F19 mint is "
        "minting two verdicts for one fact (DEC-43)")


def test_boot_gate_and_wizard_gate_agree_when_the_keyless_endpoint_is_silent(
        monkeypatch):
    """The refusal is unchanged for a machine with nothing usable -- and it is
    the SAME refusal on both surfaces."""
    _install_witness(monkeypatch, _silent)
    cfg = _keyless_only()

    assert _boot_gate(cfg) is False

    ok, msg = _wizard_gate(cfg)
    assert ok is False and "provider" in msg.lower()


def test_the_first_run_checklist_gate_agrees_too(monkeypatch):
    """`setup_status`'s `key_present` check drives the dashboard redirect and
    `systemu onboarding status`. It is the same question."""
    from systemu.runtime.first_run import setup_status
    _install_witness(monkeypatch, _answering)
    checks = {c["id"]: c for c in setup_status(_keyless_only(), SimpleNamespace())}
    assert checks["key_present"]["ok"] is True

    from systemu.runtime import provider_status as ps
    ps.clear_probe_cache()
    _install_witness(monkeypatch, _silent)
    checks = {c["id"]: c for c in setup_status(_keyless_only(), SimpleNamespace())}
    assert checks["key_present"]["ok"] is False


# -- the policy itself -------------------------------------------------------

def test_the_keyless_witness_is_spent_whatever_the_tiers_select():
    """The witness may not be gated on tier selection.

    This replaces the F19 pin that REQUIRED the gate. The cost it was buying is
    bought instead by the short-circuit below (a keyed provider means no probe)
    plus the mint's existing 20 s memo.
    """
    from systemu.runtime import provider_status as ps
    calls = {"n": 0}

    def _count(url, timeout):
        calls["n"] += 1
        return _answering(url, timeout)

    # No tier points at the keyless provider -- it is still asked.
    assert ps.any_provider_usable(_keyless_only(), probe=_count) is True
    assert calls["n"] == 1, "the keyless provider was never actually asked"


def test_a_satisfied_keyed_provider_costs_no_probe():
    """Lazy evaluation of a disjunction, not a second policy.

    ``any_satisfied`` is an OR over providers, so once a keyed provider is
    satisfied the keyless operand cannot change the answer -- and the hot
    callers (episodic capture, the open-world planner) must not pay ~1-2 s of
    loopback for an operand that cannot matter.
    """
    from systemu.runtime import provider_status as ps
    calls = {"n": 0}

    def _count(url, timeout):
        calls["n"] += 1
        return _answering(url, timeout)

    cfg = _keyless_only(google_api_key="g")
    assert ps.any_provider_usable(cfg, probe=_count) is True
    assert calls["n"] == 0, "a probe was spent on an operand that cannot matter"


@pytest.mark.parametrize("cfg_kwargs,probe_name", [
    ({}, "answering"),
    ({}, "silent"),
    ({"google_api_key": "g"}, "silent"),
    ({"tier2_provider": "ollama"}, "answering"),
    ({"tier2_provider": "ollama"}, "silent"),
])
def test_the_cheap_answer_equals_the_full_mint_on_every_shape(cfg_kwargs,
                                                              probe_name):
    """THE anti-drift fence: the short-circuit may never disagree with the
    exhaustive mint. If it ever does, the split is back."""
    from systemu.runtime import provider_status as ps
    probe = _answering if probe_name == "answering" else _silent
    cfg = _keyless_only(**cfg_kwargs)

    ps.clear_probe_cache()
    cheap = ps.any_provider_usable(cfg, probe=probe, cache_ttl_s=0.0)
    ps.clear_probe_cache()
    full = ps.any_satisfied(ps.all_provider_statuses(cfg, probe=probe))
    assert cheap is full


def _calls_the_selection_gate(src: str) -> bool:
    """Does this module NAME the deleted selection gate in executable code?

    AST, not grep: the mint's own docstring explains the defect by name, and a
    module must stay free to describe what it no longer does (the same reason
    the F19 fence in test_f19_provider_surface_parity.py skips docstrings).

    Deliberately narrow, so this claim is true: it catches a reference, a call
    and a redefinition, not a getattr-by-string -- which cannot work anyway,
    since the attribute is gone (asserted below).
    """
    import ast
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "selects_keyless":
            return True
        if isinstance(node, ast.Attribute) and node.attr == "selects_keyless":
            return True
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == "selects_keyless":
            return True
    return False


def test_the_selection_gate_helper_is_gone_and_unreferenced():
    """A structural fence, not an example: the helper that made satisfaction
    depend on selection is DELETED, so no surface can quietly re-adopt it."""
    from systemu.runtime import provider_status as ps
    assert not hasattr(ps, "selects_keyless"), (
        "selects_keyless is the instrument of the split -- its only ever use "
        "was gating the keyless witness on which provider a tier names")
    offenders = []
    for base in ("systemu", "sharing_on"):
        for p in (REPO / base).rglob("*.py"):
            try:
                if _calls_the_selection_gate(
                        p.read_text(encoding="utf-8", errors="replace")):
                    offenders.append(p.relative_to(REPO).as_posix())
            except SyntaxError:
                continue
    assert offenders == [], offenders


# -- the Re-check button -----------------------------------------------------

def test_recheck_refreshes_every_provider_not_only_openrouter(tmp_path,
                                                              monkeypatch):
    """Re-check's whole job is "notice the .env I just edited".

    It read OPENROUTER_API_KEY and nothing else, so an operator who followed
    step 1's own advice -- add a Google key, or point OLLAMA_URL at a running
    server -- clicked it and was told nothing had changed.
    """
    from systemu.interface.pages.welcome import _refresh_key_status
    env = tmp_path / ".env"
    env.write_text("GOOGLE_API_KEY=g-from-file\nOLLAMA_URL=http://box:11434\n",
                   encoding="utf-8")
    cfg = _machine()
    assert _refresh_key_status(cfg, env_file=str(env)) is True
    assert cfg.google_api_key == "g-from-file"
    assert cfg.ollama_url == "http://box:11434"
    import os
    assert os.environ.get("GOOGLE_API_KEY") is None, (
        "the process environment must not be stomped -- the daemon's env "
        "stays exactly as it booted")


def test_recheck_reports_nothing_found_on_a_bare_machine(tmp_path):
    from systemu.interface.pages.welcome import _refresh_key_status
    assert _refresh_key_status(_machine(),
                               env_file=str(tmp_path / "none.env")) is False


def test_recheck_reads_the_live_environment_too(monkeypatch, tmp_path):
    from systemu.interface.pages.welcome import _refresh_key_status
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a-from-env")
    cfg = _machine()
    assert _refresh_key_status(cfg, env_file=str(tmp_path / "none.env")) is True
    assert cfg.anthropic_api_key == "a-from-env"
