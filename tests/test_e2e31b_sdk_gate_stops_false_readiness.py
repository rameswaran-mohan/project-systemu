"""e2e31 (c)+(d): a credentialed provider whose SDK this install cannot import.

THE DEFECT, MEASURED ON THIS TREE
---------------------------------
``systemu/llm/providers/__init__`` wrapped ``from .anthropic import
AnthropicProvider`` in ``try/except ImportError`` and set
``_ANTHROPIC_AVAILABLE`` from whether it fired.  It never fired: the SDK import
lives INSIDE ``AnthropicProvider.__init__`` (``anthropic.py:17``), so the module
imports cleanly on an interpreter with no ``anthropic`` package at all.  The
class was therefore always in the registry, and with only ``ANTHROPIC_API_KEY``
set plus the ``quality`` preset every surface reported success:

    routed_tier_providers      -> ['anthropic', 'google', 'openrouter']
    provider_status(anthropic) -> satisfied=True, state='set'
    setup_status key_present   -> ok=True          ("LLM provider configured")
    /welcome step 1            -> "Provider ready: Anthropic"
    the daemon health banner   -> silent

...and the first real call died with ``ModuleNotFoundError: No module named
'anthropic'``.  A dead guard is worse than no guard: it is the shape that makes
everyone downstream believe the question was asked.

THE PROPERTY pinned here
------------------------
    A provider whose credential is present is NOT usable, on any surface, until
    the SDK its class imports has been WITNESSED as importable on THIS
    interpreter -- and the verdict that withholds "ready" carries the remedy.

WITNESS, NOT DECLARATION (DEC-40/DEC-32).  The probe is one module-level seam,
``systemu.llm.providers._find_spec``, and it hands back a VALUE
(``sdk_importable`` -> bool); it never raises across the boundary and it fails
CLOSED, so "we could not tell" can only ever withhold a claim.  Every test below
drives that ONE seam in both directions, so the file is machine-independent --
and ``test_the_machine_this_suite_runs_on_really_has_no_anthropic_sdk`` records
the real witness this slice was found on.
"""
from __future__ import annotations

import importlib.util
from types import SimpleNamespace

import pytest

from systemu.llm import providers as pr
from systemu.runtime import optional_deps as od
from systemu.runtime import provider_status as ps


# --------------------------------------------------------------------------- #
#  the seam, and the machine
# --------------------------------------------------------------------------- #

def _sdk(monkeypatch, *, present: bool):
    """Drive THE seam: ``anthropic`` importable, or not, on this interpreter."""
    monkeypatch.setattr(
        pr, "_find_spec",
        lambda name: (object() if present else None) if name == "anthropic"
        else importlib.util.find_spec(name))


def _exploding_seam(monkeypatch):
    """The positive control: a probe that cannot answer at all."""
    def _boom(name):
        raise RuntimeError("a partially-installed namespace package")
    monkeypatch.setattr(pr, "_find_spec", _boom)


@pytest.fixture()
def bare(monkeypatch):
    """Build the machine under test; never inherit the developer's."""
    for spec in ps.PROVIDER_SPECS:
        monkeypatch.delenv(spec.env, raising=False)
    for i in (1, 2, 3):
        monkeypatch.delenv(f"SYSTEMU_TIER{i}_PROVIDER", raising=False)
        monkeypatch.delenv(f"SYSTEMU_TIER{i}_MODEL", raising=False)
    ps.clear_probe_cache()
    yield
    ps.clear_probe_cache()


def _machine(**kw):
    """A config carrying exactly what the mint and the router read."""
    from sharing_on.model_presets import PRESETS
    base = {s.attr: "" for s in ps.PROVIDER_SPECS}
    base.update({f"tier{i}_provider": "" for i in (1, 2, 3)})
    base.update({f"tier{i}_model": PRESETS["quality"][f"tier{i}"]
                 for i in (1, 2, 3)})
    base.update(output_dir="", non_interactive=False, vault_dir="/tmp/v")
    base.update(kw)
    return SimpleNamespace(**base)


def _statuses(cfg):
    """Zero-network mint: the keyless witness is never spent in this file."""
    return ps.all_provider_statuses(cfg, probe=ps.unprobed)


def test_the_machine_this_suite_runs_on_really_has_no_anthropic_sdk():
    """The REAL witness the finding was made on, recorded once and not relied
    on: every other test drives the seam explicitly, so the file holds on a
    machine where somebody has run ``pip install "systemu[anthropic]"``.

    SKIPS rather than fails there -- "this environment cannot reproduce the
    original witness" is not a defect, and turning it into a red test would
    punish the operator who took the remedy this slice prints.
    """
    if importlib.util.find_spec("anthropic") is not None:
        pytest.skip("the anthropic SDK is installed here; the seam-driven "
                    "tests below still hold, but the original witness "
                    "(find_spec -> None) does not reproduce on this box")
    assert pr.sdk_importable("anthropic") is False, (
        "the seam disagrees with the interpreter it is supposed to be asking")


# --------------------------------------------------------------------------- #
#  1. the registry: importability is a VALUE, per provider
# --------------------------------------------------------------------------- #

def test_the_registry_declares_which_providers_import_an_optional_sdk():
    """Only Anthropic. OpenRouter/Google/OpenAI all ride the CORE ``openai``
    client and Ollama is core ``httpx`` -- declaring them here would make the
    gate below refuse providers that work."""
    assert pr.sdk_package("anthropic") == "anthropic"
    for other in ("openrouter", "google", "openai", "ollama"):
        assert pr.sdk_package(other) == "", other


def test_sdk_importable_answers_the_seam_in_both_directions(monkeypatch):
    _sdk(monkeypatch, present=False)
    assert pr.sdk_importable("anthropic") is False
    _sdk(monkeypatch, present=True)
    assert pr.sdk_importable("anthropic") is True


def test_a_provider_with_no_optional_sdk_is_always_importable(monkeypatch):
    """It has nothing to miss; asking the seam about it would be a false
    negative on every machine."""
    _sdk(monkeypatch, present=False)
    for other in ("openrouter", "google", "openai", "ollama"):
        assert pr.sdk_importable(other) is True, other


def test_the_probe_fails_closed_and_never_raises(monkeypatch):
    """POSITIVE CONTROL. ``find_spec`` genuinely raises on a broken or partially
    installed distribution. A probe that could not answer must not read as
    'present' -- and must not become an exception crossing the boundary either,
    because a raise is no fence (DEC-32)."""
    _exploding_seam(monkeypatch)
    assert pr.sdk_importable("anthropic") is False


def test_the_probe_refuses_a_non_string_provider_id():
    """``type(x) is str`` before operating (DEC-36); a junk id withholds."""
    for junk in (None, 7, b"anthropic", object()):
        assert pr.sdk_importable(junk) is False, junk
        assert pr.sdk_package(junk) == "", junk


def test_the_registry_no_longer_carries_a_gate_that_cannot_fire():
    """THE DEAD GUARD. ``from .anthropic import AnthropicProvider`` cannot
    raise ImportError -- the SDK import is inside ``__init__`` -- so a
    try/except around it guarded nothing while looking like a live check."""
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(pr))
    guarded = [n for n in ast.walk(tree)
               if type(n) is ast.Try
               and any(type(h.body[0]) is not ast.Pass for h in n.handlers)
               and any(type(s) in (ast.Import, ast.ImportFrom) for s in n.body)]
    assert guarded == [], (
        "the registry is guarding an import again -- if the guard can fire, "
        "say what it guards; if it cannot, it is the dead gate this closes")


def test_the_provider_class_stays_in_the_registry_without_its_sdk(monkeypatch):
    """RULING (d). The disclosure must name the provider the OPERATOR
    configured and its SDK -- not a silently rerouted OpenRouter. Dropping the
    class here would reroute ``anthropic/*`` to the catch-all and the warning
    would be about the wrong provider."""
    _sdk(monkeypatch, present=False)
    from systemu.llm.providers.anthropic import AnthropicProvider
    assert pr.resolve_provider_class("anthropic/claude-sonnet-4.5") \
        is AnthropicProvider
    assert AnthropicProvider in pr._REGISTRY


# --------------------------------------------------------------------------- #
#  2. the optional-dependency registry now ships the remedy
# --------------------------------------------------------------------------- #

def test_the_anthropic_sdk_is_a_registered_optional_group():
    """It was an extra with no group since v0.7-e: a working `pip install
    "systemu[anthropic]"` that no surface in the product could ever print,
    which is why the mint below had no remedy to offer and nobody noticed."""
    group = od.group_for_package("anthropic")
    assert group is not None, "anthropic is still not a registered group"
    assert group.extra == "anthropic"
    assert group.label == "Anthropic models"
    assert group.post_install == "", "no second step: the wheel is enough"


def test_the_remedy_is_one_copyable_line_pip_accepts():
    assert od.install_command(("anthropic",)) == 'pip install "systemu[anthropic]"'
    assert "\n" not in od.install_command(("anthropic",))


def test_the_group_appears_in_the_doctor_view_with_its_remedy():
    """`group_status` is what `doctor` and the dashboard health page render;
    a group that is registered but not listed would be the never-subtract rule
    broken from the other side."""
    rows = {r["extra"]: r for r in od.group_status()}
    assert "anthropic" in rows, sorted(rows)
    row = rows["anthropic"]
    assert row["packages"] == ["anthropic"]
    if row["installed"]:                       # a box that took the remedy
        assert row["remedy"] == ""
    else:
        assert row["remedy"] == 'pip install "systemu[anthropic]"'


# --------------------------------------------------------------------------- #
#  3. THE MINT: a credential is not usability
# --------------------------------------------------------------------------- #

_REMEDY = 'pip install "systemu[anthropic]"'


def test_a_key_without_its_sdk_is_unsatisfied_and_carries_the_remedy(
        bare, monkeypatch):
    """THE FINDING. The key is genuinely set; the tier still cannot run."""
    _sdk(monkeypatch, present=False)
    st = _statuses(_machine(anthropic_api_key="sk-ant-test"))["anthropic"]
    assert st.satisfied is False, "the mint still calls this machine ready"
    assert st.state == ps.STATE_MISSING
    assert st.detail.startswith("SDK not installed"), st.detail
    assert _REMEDY in st.detail, st.detail
    assert "ANTHROPIC_API_KEY" in st.detail, (
        "the operator must not read 'not set' about a key they did set")


def test_the_same_key_with_the_sdk_present_is_satisfied(bare, monkeypatch):
    """The other direction, on the same seam: this must not become a provider
    that is refused on every machine."""
    _sdk(monkeypatch, present=True)
    st = _statuses(_machine(anthropic_api_key="sk-ant-test"))["anthropic"]
    assert st.satisfied is True
    assert st.state == ps.STATE_SET
    assert st.detail == "ANTHROPIC_API_KEY is set in .env"


def test_no_key_still_reads_as_no_key_not_as_a_missing_sdk(bare, monkeypatch):
    """Order matters. An operator with neither must be told to add the key --
    telling them to pip-install an SDK for a provider they never configured is
    the mirror of the defect."""
    _sdk(monkeypatch, present=False)
    st = _statuses(_machine())["anthropic"]
    assert st.state == ps.STATE_MISSING
    assert st.detail == "not set - add ANTHROPIC_API_KEY to .env"


def test_the_gate_touches_only_the_provider_that_declares_an_sdk(
        bare, monkeypatch):
    """A machine with no anthropic package must still be able to use Google."""
    _sdk(monkeypatch, present=False)
    sts = _statuses(_machine(google_api_key="g", openrouter_api_key="sk-or",
                             openai_api_key="oa"))
    for pid in ("google", "openrouter", "openai"):
        assert sts[pid].satisfied is True, pid
        assert sts[pid].state == ps.STATE_SET, pid


def test_a_probe_that_cannot_answer_withholds_ready(bare, monkeypatch):
    """POSITIVE CONTROL at the mint. The seam raises; the mint must report the
    provider unavailable, never ready, and never propagate the exception."""
    _exploding_seam(monkeypatch)
    st = _statuses(_machine(anthropic_api_key="sk-ant-test"))["anthropic"]
    assert st.satisfied is False
    assert st.state == ps.STATE_MISSING


def test_the_mint_never_raises_even_if_the_registry_is_unimportable(
        bare, monkeypatch):
    """Fails CLOSED, all the way down. If the provider registry itself cannot
    be imported, nothing can construct a provider at all -- so "not usable" is
    the true answer, and it must be a VALUE, not a traceback out of a render."""
    import builtins
    real = builtins.__import__

    def _refuse(name, *a, **k):
        if name == "systemu.llm.providers":
            raise ImportError("simulated: the LLM stack cannot be imported")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _refuse)
    st = _statuses(_machine(anthropic_api_key="sk-ant-test"))["anthropic"]
    assert st.satisfied is False
    assert st.state == ps.STATE_MISSING
    assert "SDK" in st.detail, st.detail


def test_the_verdict_is_ascii_and_prints_no_credential(bare, monkeypatch):
    """DEC-32c: this crosses a Windows console, a log file and the dashboard."""
    _sdk(monkeypatch, present=False)
    st = _statuses(_machine(anthropic_api_key="sk-ant-SECRET"))["anthropic"]
    st.detail.encode("ascii")
    ps.status_line(st).encode("ascii")
    assert "SECRET" not in st.detail and "SECRET" not in ps.status_line(st)


def test_the_operator_line_does_not_say_not_set_about_a_key_that_is_set(
        bare, monkeypatch):
    """``status_line`` is the daemon boot banner. Its mark is keyed on the
    STATE, and 'missing' now covers two different machines, so a mark reading
    'not set' beside a detail reading 'ANTHROPIC_API_KEY is set' would be a
    self-contradicting line in the operator's log."""
    _sdk(monkeypatch, present=False)
    sts = _statuses(_machine(anthropic_api_key="sk-ant-test"))
    line = ps.status_line(sts["anthropic"])
    assert "not set" not in line, line
    assert "not usable" in line and _REMEDY in line, line
    # the genuinely-unconfigured provider still reads correctly
    assert "not set - add GOOGLE_API_KEY" in ps.status_line(sts["google"])


# --------------------------------------------------------------------------- #
#  4. SELECTION is untouched (ruling (d)) -- the disclosure names Anthropic
# --------------------------------------------------------------------------- #

def test_the_router_still_names_the_provider_the_operator_configured(
        bare, monkeypatch):
    """Ruling (d): `routed_tier_providers` keeps naming Anthropic, so the
    warning is about the Anthropic SDK and not about a rerouted OpenRouter's
    missing key. Satisfaction and selection stay different facts (DEC-43)."""
    _sdk(monkeypatch, present=False)
    cfg = _machine(anthropic_api_key="sk-ant-test")
    assert ps.routed_tier_providers(cfg) == ["anthropic", "google", "openrouter"]


def test_unusable_selected_names_it_with_the_remedy(bare, monkeypatch):
    """The mint's own consumer -- the Settings red flag and /welcome step 1
    both read this -- now carries the whole sentence."""
    _sdk(monkeypatch, present=False)
    cfg = _machine(anthropic_api_key="sk-ant-test", google_api_key="g",
                   openrouter_api_key="sk-or")
    statuses = _statuses(cfg)
    blocked = ps.unusable_selected(statuses, ps.routed_tier_providers(cfg))
    assert [u.provider for u in blocked] == ["anthropic"], blocked
    assert _REMEDY in blocked[0].detail


def test_the_hot_gate_and_the_table_agree(bare, monkeypatch):
    """``any_provider_usable`` is EXACTLY ``any_satisfied(all_provider_statuses)``
    (test_provider_verdict_unification pins that equality). A gate that skipped
    the SDK check would boot a daemon onto a machine the table calls broken."""
    _sdk(monkeypatch, present=False)
    cfg = _machine(anthropic_api_key="sk-ant-test")
    assert ps.any_provider_usable(cfg, probe=ps.unprobed, cache_ttl_s=0) is False
    assert ps.any_satisfied(_statuses(cfg)) is False
    _sdk(monkeypatch, present=True)
    assert ps.any_provider_usable(cfg, probe=ps.unprobed, cache_ttl_s=0) is True
    assert ps.any_satisfied(_statuses(cfg)) is True


# --------------------------------------------------------------------------- #
#  5. the surfaces the operator actually reads
# --------------------------------------------------------------------------- #

def test_the_readiness_checklist_no_longer_says_the_provider_is_configured(
        bare, monkeypatch):
    """``first_run.setup_status`` is the onboarding gate AND the /welcome
    redirect check. It said "LLM provider configured: ok" on this machine."""
    from systemu.runtime import first_run
    cfg = _machine(anthropic_api_key="sk-ant-test")
    _sdk(monkeypatch, present=False)
    checks = {c["id"]: c for c in first_run.setup_status(cfg, SimpleNamespace())}
    assert checks["key_present"]["ok"] is False, (
        "the checklist still admits a machine whose only provider cannot run")
    assert "provider" in checks["key_present"]["detail"].lower()
    _sdk(monkeypatch, present=True)
    ok = {c["id"]: c for c in first_run.setup_status(cfg, SimpleNamespace())}
    assert ok["key_present"]["ok"] is True


def test_the_daemon_health_banner_raises_the_provider_issue(bare, monkeypatch):
    """Read-only, through the builder's own no-network path. The banner was
    SILENT on this machine, because ``any_satisfied`` was True."""
    from systemu.interface.components import health_banner as hb
    cfg = _machine(anthropic_api_key="sk-ant-test")
    _sdk(monkeypatch, present=False)
    state = hb.build_health_state(config=cfg, provider_probe=ps.unprobed)
    hits = [i for i in state.issues if "provider" in i.message.lower()]
    assert hits, "the banner is still silent about a machine that cannot run"
    assert hits[0].severity == "warning"
    _sdk(monkeypatch, present=True)
    healthy = hb.build_health_state(config=cfg, provider_probe=ps.unprobed)
    assert not [i for i in healthy.issues if "provider" in i.message.lower()]


# --------------------------------------------------------------------------- #
#  6. the wizard: the remedy appears EXACTLY ONCE, on whichever screen the
#     operator is standing on
# --------------------------------------------------------------------------- #

def _welcome():
    from systemu.interface.pages import welcome
    return welcome


def test_the_blocked_provider_is_read_off_the_minted_value(bare, monkeypatch):
    """DEC-34. The page tells the two `missing` machines apart by the FIELD the
    mint sets, not by matching the wording of `detail`, which the mint owns and
    may reword."""
    w = _welcome()
    _sdk(monkeypatch, present=False)
    configured = _statuses(_machine(anthropic_api_key="sk-ant-test"))
    assert [s.provider for s in w.blocked_providers(configured)] == ["anthropic"]
    assert configured["anthropic"].blocker == configured["anthropic"].detail

    never_configured = _statuses(_machine())
    assert w.blocked_providers(never_configured) == [], (
        "a provider nobody configured is not 'blocked' -- it is unset")
    assert never_configured["anthropic"].blocker == ""

    _sdk(monkeypatch, present=True)
    assert w.blocked_providers(_statuses(_machine(anthropic_api_key="k"))) == []


def test_the_witnessed_machine_reads_the_remedy_exactly_once(bare, monkeypatch):
    """THE WITNESSED MACHINE, end to end. Only an Anthropic key, no SDK: no
    provider is satisfied, so step 1 takes the no-provider branch -- which used
    to tell this operator to go and set the very key they had already set, and
    said nothing at all about the SDK."""
    w = _welcome()
    _sdk(monkeypatch, present=False)
    cfg = _machine(anthropic_api_key="sk-ant-test")
    statuses = _statuses(cfg)
    assert ps.satisfied_providers(statuses) == []
    banner = w.no_provider_banner(statuses)
    assert banner.count(_REMEDY) == 1, banner
    assert "Anthropic SDK is missing" in banner
    assert w.tier_readiness_warning(statuses, cfg) == "", (
        "the two branches must not both speak")


def test_the_three_key_machine_reads_the_remedy_exactly_once(bare, monkeypatch):
    """The other branch. Google and OpenRouter work, so step 1 says "Provider
    ready" and the caveat under it names the blocked tier -- carrying the
    mint's sentence. The retired welcome-side clause appended a SECOND wording
    of the same remedy to this very paragraph (measured: 2 occurrences)."""
    w = _welcome()
    _sdk(monkeypatch, present=False)
    cfg = _machine(anthropic_api_key="sk-ant-test", google_api_key="g",
                   openrouter_api_key="sk-or")
    statuses = _statuses(cfg)
    line = w.tier_readiness_warning(statuses, cfg)
    assert line.count(_REMEDY) == 1, line
    assert "Anthropic" in line and "SDK not installed" in line


def test_a_machine_with_no_keys_is_not_lectured_about_an_sdk(bare, monkeypatch):
    """CRYING WOLF, the mirror defect. The retired clause scored
    `routed_tier_providers`, and on a machine with no keys at all the `quality`
    preset still routes tier 1 to Anthropic -- so a fresh operator whose real
    problem is "no credentials" was told to pip-install an SDK for a provider
    they had never chosen."""
    w = _welcome()
    _sdk(monkeypatch, present=False)
    cfg = _machine()
    banner = w.no_provider_banner(_statuses(cfg))
    assert _REMEDY not in banner, banner
    assert "SDK" not in banner, banner
    assert "OPENROUTER_API_KEY" in banner, "the real remedy is still offered"


def test_a_healthy_machine_gets_neither_banner_nor_caveat(bare, monkeypatch):
    w = _welcome()
    _sdk(monkeypatch, present=True)
    cfg = _machine(anthropic_api_key="sk-ant-test", google_api_key="g",
                   openrouter_api_key="sk-or")
    statuses = _statuses(cfg)
    assert ps.any_satisfied(statuses) is True
    assert w.tier_readiness_warning(statuses, cfg) == ""


def test_the_welcome_side_sdk_clause_is_gone(bare):
    """RETIRED (e2e31). A second table of which provider needs which SDK, a
    second probe (`importlib.metadata` against the mint's `find_spec`) and a
    second wording of one remedy. Re-adding any of them re-opens the split."""
    w = _welcome()
    for gone in ("tier_sdk_warning", "missing_sdk_remedy",
                 "PROVIDER_SDK_PACKAGES", "_optional_deps"):
        assert not hasattr(w, gone), (
            f"welcome.{gone} is back -- the mint owns this fact now")


def test_the_render_consumes_the_banner_builder(bare):
    """REACHABILITY. Delete the call site and this goes red, however green the
    builder's own tests stay -- the standing rule for this repo."""
    import inspect
    w = _welcome()
    src = inspect.getsource(w.build_welcome_page)
    assert "no_provider_banner(" in src, (
        "step 1 renders its no-provider banner from somewhere else again")
    assert "tier_readiness_warning(" in src


def test_the_banner_never_raises_on_a_config_it_cannot_read(bare, monkeypatch):
    """The operator must still be told nothing works, whatever else failed."""
    w = _welcome()
    _sdk(monkeypatch, present=False)
    banner = w.no_provider_banner(_statuses(_machine()))
    assert "No LLM provider is usable yet" in banner
    assert w.no_provider_banner({})


# --------------------------------------------------------------------------- #
#  7. the two fail-OPEN holes an adversarial pass on this slice found
# --------------------------------------------------------------------------- #

def test_a_status_carrying_a_blocker_can_never_read_as_satisfied():
    """The invariant the dataclass already promised, extended to the new field.

    `satisfied` is derived from `state` precisely so "a caller constructing this
    cannot assert a capability the state does not support". `blocker` is a
    second thing that decides usability, so a status built with a blocker AND a
    satisfied state would be exactly the disagreement that design exists to
    prevent -- reachable by an honest-code accident in any future surface that
    sets one field and forgets the other.
    """
    blocked = ps.ProviderStatus("anthropic", "Anthropic", "ANTHROPIC_API_KEY",
                                ps.RULE_CREDENTIAL, ps.STATE_SET,
                                "ANTHROPIC_API_KEY is set in .env",
                                "SDK not installed - ...")
    assert blocked.satisfied is False, (
        "a provider that cannot be built was reported usable")

    clean = ps.ProviderStatus("google", "Google", "GOOGLE_API_KEY",
                              ps.RULE_CREDENTIAL, ps.STATE_SET,
                              "GOOGLE_API_KEY is set in .env")
    assert clean.satisfied is True


def test_a_blocker_of_the_wrong_type_fails_closed():
    """`type(x) is T` in the same frame, then decide (DEC-36). A blocker that
    is not a string is not something this module can vouch for, and every
    unvouched-for answer withholds the claim."""
    for junk in (object(), 1, [], {}, None):
        st = ps.ProviderStatus("anthropic", "Anthropic", "ANTHROPIC_API_KEY",
                               ps.RULE_CREDENTIAL, ps.STATE_SET, "detail",
                               junk)
        assert st.satisfied is False, junk


def test_a_provider_id_the_gate_cannot_read_blocks_rather_than_passes(bare):
    """FAIL-OPEN, found by an adversarial pass on this slice. `sdk_blocker`
    normalised the id with `type(x) is str`, which REJECTS a str subclass -- and
    the rejected id then looked up as "needs no SDK", i.e. satisfied. A gate
    whose own input it could not read must refuse, not wave through."""
    class _NotQuiteStr(str):
        pass

    rogue = ps.ProviderSpec(_NotQuiteStr("anthropic"), "Anthropic",
                            "anthropic_api_key", "ANTHROPIC_API_KEY",
                            ps.RULE_CREDENTIAL)
    blocked = ps.sdk_blocker(rogue)
    assert blocked, "an unreadable provider id was treated as needing no SDK"

    cfg = _machine(anthropic_api_key="sk-ant-test")
    st = ps.provider_status(rogue, cfg)
    assert st.satisfied is False
    assert st.state == ps.STATE_MISSING


def test_the_recognised_specs_are_all_still_readable():
    """The other direction: the fail-closed above must not refuse the real
    table. Every shipped spec's id is exactly ``str``."""
    for spec in ps.PROVIDER_SPECS:
        assert type(spec.provider) is str and spec.provider.strip(), spec
