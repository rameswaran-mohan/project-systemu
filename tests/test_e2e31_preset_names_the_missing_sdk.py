"""e2e31 D-13: a preset that needs a provider SDK this install does not have.

THE MACHINE. Pick ``quality`` in step 2 and the wizard writes
``SYSTEMU_TIER1_MODEL=anthropic/claude-sonnet-4.5``. On an install with an
Anthropic key and no ``anthropic`` package, every surface reports success:
``routed_tier_providers`` answers ``['anthropic', 'google', 'openrouter']``,
step 1 prints "Provider ready: Anthropic - you're set to run tasks", Finish
accepts the machine, and the first real task dies with a bare
``ModuleNotFoundError: No module named 'anthropic'`` -- because
``AnthropicProvider`` imports the SDK inside ``__init__``, so the registry's
``try: from .anthropic import ...`` gate in ``systemu/llm/providers/__init__``
never fires and the class is present right up to the moment it is constructed.
(Measured: that is exactly what happens on this tree today.)

The tier/provider disclosure the wizard already carries does not cover it. On
that machine it names tiers 2 and 3 -- "your model tiers are set to run on
Google, OpenRouter ... add GOOGLE_API_KEY / OPENROUTER_API_KEY" -- and says
nothing about tier 1, the reasoning brain the operator chose ``quality`` FOR,
which the mint scores as satisfied.

THE PROPERTY pinned here:

    When a tier is routed to a provider whose SDK this install cannot import,
    the wizard's disclosure says so and names the remedy the optional-deps
    registry gives -- or, when no extra ships that SDK, names the package that
    does, and invents no extra.

The SDK seam (``systemu.llm.providers._find_spec``) is faked in every test
below, so these assertions are about what the wizard DOES with an availability
answer, not about which packages this machine happens to have.

RETIRED (e2e31). The welcome-side clause this file was first written against
(``missing_sdk_remedy`` / ``tier_sdk_warning`` and their private
``_optional_deps`` / ``PROVIDER_SDK_PACKAGES`` table) is gone: the provider
mint owns the fact now, and the tests that exercised the clause directly were
deleted with it. Each property they carried is pinned against the mint in
``tests/test_e2e31b_sdk_gate_stops_false_readiness.py``.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from systemu.interface.pages import welcome


def _machine(**kw):
    """A config carrying exactly what the mint and the router read."""
    from systemu.runtime import provider_status as ps
    from sharing_on.model_presets import PRESETS
    base = {s.attr: "" for s in ps.PROVIDER_SPECS}
    base.update({f"tier{i}_provider": "" for i in (1, 2, 3)})
    base.update({f"tier{i}_model": PRESETS["quality"][f"tier{i}"]
                 for i in (1, 2, 3)})
    base.update(output_dir="")
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture
def bare(monkeypatch):
    """Build the machine under test; never inherit the developer's."""
    from systemu.runtime import provider_status as ps
    for spec in ps.PROVIDER_SPECS:
        monkeypatch.delenv(spec.env, raising=False)
    for i in (1, 2, 3):
        monkeypatch.delenv(f"SYSTEMU_TIER{i}_PROVIDER", raising=False)
        monkeypatch.delenv(f"SYSTEMU_TIER{i}_MODEL", raising=False)
    ps.clear_probe_cache()
    yield
    ps.clear_probe_cache()


# ── it reaches the disclosure the operator actually reads ───────────────────

def _statuses(cfg):
    from systemu.runtime import provider_status as ps
    return ps.all_provider_statuses(cfg, probe=ps.unprobed)


def test_the_witnessed_machine_is_told_about_the_sdk(bare, monkeypatch):
    """The end-to-end shape of the finding: an Anthropic key, the `quality`
    preset, no SDK.

    REWRITTEN for e2e31 ruling (c) (authorised). The fake used to sit at
    ``welcome._optional_deps``, because the page owned both the probe and the
    wording. It does not any more: the MINT refuses this machine, so the fake
    now sits at the mint's own seam -- ``systemu.llm.providers._find_spec`` --
    and the assertion is that the machine IS told, through the mint, with the
    remedy. It is told on the no-provider banner rather than in the caveat,
    because with no SDK the Anthropic key satisfies nothing and step 1 takes
    the other branch; that is the point of the fix, not a gap in it.
    """
    from systemu.llm import providers as pr
    from systemu.runtime import provider_status as ps
    monkeypatch.setattr(pr, "_find_spec", lambda name: None)

    cfg = _machine(anthropic_api_key="sk-ant-test")
    statuses = _statuses(cfg)
    st = statuses["anthropic"]
    assert st.satisfied is False, "the mint still calls this machine ready"
    assert "SDK not installed" in st.detail and "Anthropic" in st.detail
    assert 'pip install "systemu[anthropic]"' in st.detail

    assert ps.satisfied_providers(statuses) == []
    banner = welcome.no_provider_banner(statuses)
    assert "Anthropic" in banner and "SDK" in banner
    assert banner.count('pip install "systemu[anthropic]"') == 1, banner


def test_the_sdk_clause_stands_alone_when_nothing_else_is_wrong(bare,
                                                               monkeypatch):
    """The sharpest case: every routed provider is keyed, so the existing
    disclosure had nothing to say -- and tier 1 still could not run.

    REWRITTEN for e2e31 ruling (c) (authorised). The line this test used to
    pin, ``unusable_selected(...) == []``, WAS the false-ready mint state: the
    Anthropic key satisfied the mint, so the one surface built to red-flag a
    tier that cannot run saw nothing to flag, and the page had to grow a
    private clause to say it. The mint now answers ``[anthropic]`` and its
    verdict carries the reason and the remedy, so the disclosure says it
    itself -- ONCE. Two wordings of one remedy in one paragraph is what the
    retired clause actually produced here (measured: 2 occurrences).
    """
    from systemu.llm import providers as pr
    from systemu.runtime import provider_status as ps
    monkeypatch.setattr(pr, "_find_spec", lambda name: None)

    cfg = _machine(anthropic_api_key="sk-ant-test",
                   google_api_key="g", openrouter_api_key="sk-or")
    statuses = _statuses(cfg)
    blocked = ps.unusable_selected(statuses, ps.routed_tier_providers(cfg))
    assert [u.provider for u in blocked] == ["anthropic"], blocked

    line = welcome.tier_readiness_warning(statuses, cfg)
    assert "Anthropic" in line and "SDK not installed" in line
    assert line.count('pip install "systemu[anthropic]"') == 1, line


def test_a_healthy_machine_still_gets_no_warning(bare, monkeypatch):
    """REWRITTEN for e2e31 ruling (c) (authorised): the same machine with the
    SDK seam answering YES. The gate must be able to say "fine" -- a check
    that refuses every machine is not a check, and this is the direction that
    proves the seam is really consulted rather than the provider blanket-
    refused."""
    from systemu.llm import providers as pr
    from systemu.runtime import provider_status as ps
    monkeypatch.setattr(pr, "_find_spec", lambda name: object())

    cfg = _machine(anthropic_api_key="sk-ant-test",
                   google_api_key="g", openrouter_api_key="sk-or")
    statuses = _statuses(cfg)
    assert statuses["anthropic"].satisfied is True
    assert ps.unusable_selected(statuses, ps.routed_tier_providers(cfg)) == []
    assert welcome.tier_readiness_warning(statuses, cfg) == ""
