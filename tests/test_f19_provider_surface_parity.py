"""F19 — every surface that discloses provider usability consumes THE ONE MINT.

THE DEFECT (both surfaces run on the same branch, same machine, same minute):

    Dashboard -> Settings          CLI: sharing_on settings show
    ---------------------------    ------------------------------------
    OpenRouter  OK SET             OpenRouter
    Google      NOT SET              API key set:  No - set
    Anthropic   NOT SET                            OPENROUTER_API_KEY in .env
    OpenAI      NOT SET
    Ollama      OK REACHABLE       (Google, Anthropic, OpenAI, Ollama
                (1 model)           are not mentioned at all)

F8 built the single mint (``systemu.runtime.provider_status``: a frozen
``PROVIDER_SPECS`` table plus one satisfaction rule per provider, deliberately
NiceGUI-free so non-dashboard callers could use it) and converted the DASHBOARD
ONLY. Every other surface kept its own recipe -- and each recipe was a different
one: ``setup_flow.key_present`` read the env + .env for OpenRouter,
``health_banner._openrouter_key_present`` read only the process env,
``first_run.setup_status`` read the config attribute, ``platform_profile
._provider_configured`` read the env again, and ``episodic_memory`` /
``open_world_planner`` each enumerated FOUR config ATTRIBUTE NAMES -- which is
why grepping for the env var could never find every site.

That is DEC-43 form (i) verbatim (a second copy of the recipe) and GATE-7a
(one gate of a multi-surface fact was widened, manufacturing a contradiction the
original did not have).

THE PROPERTY, quantified over ALL surfaces rather than over ``settings show``:

    Every surface that tells the operator whether a provider is usable derives
    that answer from ``systemu.runtime.provider_status``, and no surface
    re-derives it from raw config attributes or env vars.

THE FENCE is structural, not a list of examples. ``test_no_module_outside_the_
mint_reads_the_recipe`` scans the WHOLE tree for either spelling of every
provider credential -- attribute name AND env var -- and fails on any file that
is not in an explicitly-reasoned allowlist. A new surface added tomorrow either
consumes the mint or turns this test red; it cannot quietly grow a sixth recipe.

DEC-44 CAVEAT: these are unit tests over a stubbed world. The RUNTIME evidence
(both CLI commands against a live Ollama, with and without it reachable, plus a
booted daemon) is in the packet, not here.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parent.parent


# ── the two spellings of the recipe ──────────────────────────────────────────
#
# Both are needed and neither alone is enough: `episodic_memory` and
# `open_world_planner` enumerated ATTRIBUTE names, so an env-var grep missed
# them entirely, while `health_banner` and `platform_profile` used only the env
# var and an attribute grep missed those.
_RECIPE_TOKENS = (
    "openrouter_api_key", "google_api_key", "anthropic_api_key",
    "openai_api_key", "ollama_url",
    "OPENROUTER_API_KEY", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY", "OLLAMA_URL",
)

#: Files allowed to name a provider credential, each with the reason it is NOT a
#: verdict surface. Anything else naming one is re-deriving the recipe.
#:
#: Keep this SMALL and keep the reasons honest -- an entry here is a standing
#: exemption from DEC-43, so "it was already there" is not a reason.
_ALLOWED = {
    # THE MINT itself.
    "systemu/runtime/provider_status.py":
        "THE MINT: the one place the per-provider satisfaction rule is declared.",
    # THE LOADER: env/.env -> Config. Someone has to read the environment.
    "sharing_on/config.py":
        "THE LOADER: builds Config from the environment. Reads the names; makes "
        "no usability claim -- validate() consumes the mint.",
    # THE WRITERS: setup and the installer put credentials INTO a .env.
    "sharing_on/setup_flow.py":
        "THE SETUP WRITER: prompts for and writes credentials, and overlays an "
        "explicitly-named .env onto the config it hands the mint.",
    "install.py":
        "THE INSTALLER: writes the initial .env.",
    "systemu/interface/pages/welcome.py":
        "THE ONBOARDING WRITER: reloads a freshly-saved OpenRouter key from .env "
        "into the live config. Its STATUS disclosure consumes the mint.",
    # CONSUMPTION: hands the credential to a client. Not a verdict.
    "systemu/core/llm_router.py":
        "CONSUMPTION: passes the credential to the provider client and picks a "
        "class. Never tells the operator whether a provider is usable.",
    "sharing_on/cli.py":
        "CONSUMPTION + CLI option help: passes api_key into analyzer calls and "
        "names the env vars its --*-key flags populate.",
    "systemu/interface/cli_commands.py":
        "CONSUMPTION: passes api_key into generate_instructions on the analyze "
        "path. Its settings/doctor/daemon-start verdicts consume the mint.",
}


def _iter_sources():
    for base in ("systemu", "sharing_on"):
        for p in (REPO / base).rglob("*.py"):
            yield p
    yield REPO / "install.py"


def _rel(p: Path) -> str:
    return p.relative_to(REPO).as_posix()


def _docstring_nodes(tree):
    """Every node that is a docstring, so PROSE about the defect is not a hit."""
    out = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                and isinstance(first.value.value, str):
            out.add(id(first.value))
    return out


def recipe_hits(src: str):
    """Every place a module names a provider credential IN EXECUTABLE CODE.

    A STRUCTURAL scan, not a grep: comments never reach the AST and docstrings
    are skipped, so a module may explain the defect it no longer has. It reads
    attribute access (``config.openrouter_api_key``), bare names, keyword-arg
    names, and string constants -- which is what catches
    ``getattr(config, "google_api_key")`` and the ATTRIBUTE-NAME enumeration in
    ``episodic_memory`` that an env-var grep could never see.
    """
    tree = ast.parse(src)
    skip = _docstring_nodes(tree)
    hits = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in _RECIPE_TOKENS:
            hits.add(node.attr)
        elif isinstance(node, ast.Name) and node.id in _RECIPE_TOKENS:
            hits.add(node.id)
        elif isinstance(node, ast.keyword) and node.arg in _RECIPE_TOKENS:
            hits.add(node.arg)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in skip and node.value in _RECIPE_TOKENS:
            hits.add(node.value)
    return sorted(hits)


def test_no_module_outside_the_mint_reads_the_recipe():
    """THE F19 FENCE. Quantified over the tree, not over a list of known sites."""
    offenders = {}
    for p in _iter_sources():
        rel = _rel(p)
        if rel in _ALLOWED:
            continue
        try:
            hits = recipe_hits(p.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        if hits:
            offenders[rel] = hits
    assert offenders == {}, (
        "these modules name a provider credential themselves instead of "
        "consuming systemu.runtime.provider_status:\n"
        + "\n".join(f"  {k}: {v}" for k, v in sorted(offenders.items()))
    )


def test_the_scan_would_catch_each_shape_f19_actually_found():
    """The fence's own fence: an assertion of enforcement must be true (DEC-34).

    Each snippet is a re-derivation shape that was really in this tree.
    """
    for snippet in (
        'x = config.openrouter_api_key',                      # settings show
        'x = bool(os.environ.get("OPENROUTER_API_KEY"))',     # health_banner
        'for a in ("google_api_key", "openai_api_key"): pass',  # episodic_memory
        'x = getattr(cfg, "ollama_url", "")',                 # the keyless proxy
        'Config(openrouter_api_key="k")',                     # keyword form
    ):
        assert recipe_hits(snippet), snippet
    # ...and does not fire on prose or on a comment
    assert recipe_hits('"""OPENROUTER_API_KEY used to be read here."""') == []
    assert recipe_hits('x = 1  # OPENROUTER_API_KEY') == []


def test_the_allowlist_is_not_stale():
    """An exemption that no longer applies is an invitation to re-derive."""
    for rel in _ALLOWED:
        hits = recipe_hits((REPO / rel).read_text(encoding="utf-8",
                                                  errors="replace"))
        assert hits, (f"{rel} is exempted from the recipe scan but no longer "
                      f"names a credential in code -- drop the exemption")


def test_every_named_surface_imports_the_mint():
    """A reachability pin: the surfaces F19 converted must actually IMPORT it.

    Deleting the production call site from any of these makes this go red --
    the project's standing rule against a unit test that outlives its wiring.
    """
    surfaces = (
        "systemu/interface/cli_commands.py",
        "sharing_on/setup_flow.py",
        "systemu/interface/components/health_banner.py",
        "systemu/runtime/first_run.py",
        "systemu/runtime/platform_profile.py",
        "systemu/runtime/episodic_memory.py",
        "systemu/runtime/open_world_planner.py",
        "systemu/interface/pages/settings.py",
        "systemu/interface/pages/welcome.py",
        "systemu/scheduler/daemon.py",
        "sharing_on/config.py",
    )
    for rel in surfaces:
        src = (REPO / rel).read_text(encoding="utf-8", errors="replace")
        assert "provider_status" in src, f"{rel} does not consume the mint"


# ── helpers ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _bare_provider_env(monkeypatch):
    """Construct the bare machine these tests assert on; never assume one.

    F30. Several tests here assert that a machine with NOTHING configured is
    still warned, and that a bare config yields key_present=False. They passed a
    bare `_cfg()` but left the ENVIRONMENT alone -- and the surfaces under test
    consult `os.environ` as well as the config. So the verdict depended on
    whatever the rest of the suite had left lying around.

    It duly broke: tests/test_cli_dotenv_loading.py leaked
    OPENROUTER_API_KEY into the process (load_dotenv writes behind monkeypatch's
    back), and 60 files later "a machine with nothing configured" was no longer
    bare. Green in isolation, red in the full suite.

    That leak is now fixed at its source, but fixing only the polluter would
    leave these assertions just as fragile for the next one. DEC-34: a verifier
    must not depend on state the verified party -- here, the rest of the suite --
    controls. So the bare machine is CONSTRUCTED here.

    Scoped to this file, and it removes rather than injects, so it is not the
    kind of product-behaviour stub DEC-44 bans.
    """
    from systemu.runtime import provider_status as ps

    for spec in ps.PROVIDER_SPECS:
        monkeypatch.delenv(spec.env, raising=False)
    for i in (1, 2, 3):
        monkeypatch.delenv(f"SYSTEMU_TIER{i}_PROVIDER", raising=False)
    yield


def _cfg(**kw):
    """A config view carrying exactly the mint's attributes."""
    from systemu.runtime import provider_status as ps
    base = {s.attr: "" for s in ps.PROVIDER_SPECS}
    base.update({f"tier{i}_provider": "" for i in (1, 2, 3)})
    base.update({f"tier{i}_model": "x/y" for i in (1, 2, 3)})
    base.update(non_interactive=False, vault_dir="/tmp/v")
    base.update(kw)
    return SimpleNamespace(**base)


def _quiet(_url, _timeout):
    from systemu.runtime import provider_status as ps
    return (ps.STATE_UNREACHABLE, "nothing answered (test)")


def _answering(_url, _timeout):
    from systemu.runtime import provider_status as ps
    return (ps.STATE_REACHABLE, "answered at the test endpoint with 3 model(s)")


# ── the mint's new consumer-facing helpers ───────────────────────────────────

def test_unprobed_is_never_satisfied_and_says_so():
    """The honest third state for a path that may not pay for a probe."""
    from systemu.runtime import provider_status as ps
    st = ps.all_provider_statuses(_cfg(), probe=ps.unprobed)["ollama"]
    assert st.state == ps.STATE_UNKNOWN
    assert st.satisfied is False
    assert "not probed" in st.detail.lower()


def test_any_satisfied_is_true_for_a_keyless_provider_that_answers():
    """THE point of F19: a user with only a reachable Ollama IS provisioned."""
    from systemu.runtime import provider_status as ps
    sat = ps.all_provider_statuses(_cfg(ollama_url="http://x:1"), probe=_answering)
    assert ps.any_satisfied(sat) is True
    assert [s.provider for s in ps.satisfied_providers(sat)] == ["ollama"]

    unsat = ps.all_provider_statuses(_cfg(ollama_url="http://x:1"), probe=_quiet)
    assert ps.any_satisfied(unsat) is False


def test_the_configure_hint_names_every_provider_and_is_ascii():
    """The refusal message may not name only OpenRouter (DEC-32c: ASCII)."""
    from systemu.runtime import provider_status as ps
    hint = ps.configure_hint(ps.all_provider_statuses(_cfg(), probe=_quiet))
    for spec in ps.PROVIDER_SPECS:
        assert spec.env in hint, f"{spec.display} is not offered to the operator"
    hint.encode("ascii")


def test_a_sixth_provider_flows_into_the_hint_without_editing_it(monkeypatch):
    """Generated from PROVIDER_SPECS, never written alongside it."""
    from systemu.runtime import provider_status as ps
    extra = ps.ProviderSpec("mistral", "Mistral", "mistral_api_key",
                            "MISTRAL_API_KEY", ps.RULE_CREDENTIAL)
    monkeypatch.setattr(ps, "PROVIDER_SPECS", ps.PROVIDER_SPECS + (extra,))
    hint = ps.configure_hint(ps.all_provider_statuses(_cfg(), probe=_quiet))
    assert "MISTRAL_API_KEY" in hint


def test_the_probe_cache_makes_a_second_call_free():
    """Render paths reuse an OBSERVED verdict; they never fake one."""
    from systemu.runtime import provider_status as ps
    calls = {"n": 0}

    def _count(url, timeout):
        calls["n"] += 1
        return _answering(url, timeout)

    ps.clear_probe_cache()
    cfg = _cfg(ollama_url="http://cache-test:1")
    for _ in range(4):
        out = ps.all_provider_statuses(cfg, probe=_count, cache_ttl_s=60.0)
    assert calls["n"] == 1, "the probe ran once, not once per caller"
    assert out["ollama"].satisfied is True
    ps.clear_probe_cache()


def test_the_memo_never_replays_a_non_observation():
    """A caller that declined to look may not answer for one that will.

    Found while building F19: `_has_llm_provider` warmed the memo with
    `unprobed`, and the next caller -- with a live probe -- read back "unknown".
    """
    from systemu.runtime import provider_status as ps
    ps.clear_probe_cache()
    cfg = _cfg(ollama_url="http://memo-test:1")
    assert ps.all_provider_statuses(cfg, probe=ps.unprobed,
                                    cache_ttl_s=60.0)["ollama"].satisfied is False
    st = ps.all_provider_statuses(cfg, probe=_answering, cache_ttl_s=60.0)["ollama"]
    assert st.state == ps.STATE_REACHABLE, (
        "a cached 'we did not ask' suppressed a real observation")
    ps.clear_probe_cache()


def test_unprobed_does_not_cash_someone_elses_observation():
    """The null witness must not read the memo either.

    Caught by tests/test_s4_episodic_guard: `_has_llm_provider(Config())`
    answered True on this machine ONLY when an earlier surface in the same
    process had probed the default Ollama URL. An answer that depends on who
    else ran recently is the hidden coupling F19 exists to remove.
    """
    from systemu.runtime import provider_status as ps
    ps.clear_probe_cache()
    cfg = _cfg(ollama_url="http://shared:1")
    assert ps.all_provider_statuses(cfg, probe=_answering,
                                    cache_ttl_s=60.0)["ollama"].satisfied is True
    st = ps.all_provider_statuses(cfg, probe=ps.unprobed, cache_ttl_s=60.0)["ollama"]
    assert st.state == ps.STATE_UNKNOWN and st.satisfied is False
    ps.clear_probe_cache()


def test_the_probe_cache_expires_and_never_caches_across_urls():
    from systemu.runtime import provider_status as ps
    seen = []

    def _rec(url, timeout):
        seen.append(url)
        return _answering(url, timeout)

    ps.clear_probe_cache()
    t = [1000.0]
    ps.all_provider_statuses(_cfg(ollama_url="http://a:1"), probe=_rec,
                             cache_ttl_s=10.0, _now=lambda: t[0])
    ps.all_provider_statuses(_cfg(ollama_url="http://b:1"), probe=_rec,
                             cache_ttl_s=10.0, _now=lambda: t[0])
    assert seen == ["http://a:1", "http://b:1"], "cache must be per-URL"
    t[0] = 1100.0
    ps.all_provider_statuses(_cfg(ollama_url="http://a:1"), probe=_rec,
                             cache_ttl_s=10.0, _now=lambda: t[0])
    assert seen[-1] == "http://a:1" and len(seen) == 3, "a stale entry must re-probe"
    ps.clear_probe_cache()


def test_any_provider_usable_probes_only_when_a_tier_selects_the_keyless_one():
    """A hot path may not pay ~1-2 s of loopback I/O on every call.

    It also may not silently answer "no" for an operator who chose Ollama -- so
    the probe is spent exactly when a tier points at a keyless provider.
    """
    from systemu.runtime import provider_status as ps
    calls = {"n": 0}

    def _count(url, timeout):
        calls["n"] += 1
        return _answering(url, timeout)

    ps.clear_probe_cache()
    assert ps.any_provider_usable(_cfg(ollama_url="http://y:1"), probe=_count) is False
    assert calls["n"] == 0, "no tier selects a keyless provider - no probe"

    ps.clear_probe_cache()
    assert ps.any_provider_usable(_cfg(ollama_url="http://y:1",
                                       tier2_provider="ollama"),
                                  probe=_count) is True
    assert calls["n"] == 1
    ps.clear_probe_cache()


# ── surface 1: the CLI settings panel (the F19 headline) ─────────────────────

def _settings_text(cfg, probe) -> str:
    from systemu.interface import cli_commands as cc
    from systemu.runtime import provider_status as ps
    import io
    from rich.console import Console

    buf = io.StringIO()
    real = cc.console
    cc.console = Console(file=buf, width=200, no_color=True, legacy_windows=False)
    try:
        cc._render_settings_panel(cfg, statuses=ps.all_provider_statuses(
            cfg, probe=probe))
    finally:
        cc.console = real
    return buf.getvalue()


def test_settings_show_reports_every_provider():
    """It reported ONE. The dashboard reported five, on the same machine."""
    from systemu.runtime import provider_status as ps
    out = _settings_text(_cfg(google_api_key="g", ollama_url="http://x:1"),
                         _answering)
    for spec in ps.PROVIDER_SPECS:
        assert spec.display in out, f"{spec.display} is not on the CLI panel"


def test_settings_show_renders_the_minted_state_not_a_proxy(monkeypatch):
    """Change the mint's answer, the CLI text changes. That is the pin."""
    reachable = _settings_text(_cfg(ollama_url="http://x:1"), _answering)
    down = _settings_text(_cfg(ollama_url="http://x:1"), _quiet)
    assert "Reachable" in reachable and "3 model(s)" in reachable
    assert "Reachable" not in down.replace("Not reachable", "")
    assert "Not reachable" in down


def test_settings_show_never_calls_a_url_a_credential():
    """The F8 bug, one surface over: Ollama green on every machine ever."""
    out = _settings_text(_cfg(ollama_url="http://x:1"), _quiet)
    ollama_line = [ln for ln in out.splitlines() if "Ollama" in ln]
    assert ollama_line, out
    assert "Set" not in ollama_line[0] or "Not set" in ollama_line[0]


# ── surface 2: the daemon-start admission gate ───────────────────────────────

def test_daemon_start_still_refuses_a_machine_with_nothing_configured():
    """THE regression that must not happen. Refusal is correct and stays."""
    from sharing_on import setup_flow as sf
    from systemu.runtime import provider_status as ps
    assert sf.provider_available(config=_cfg(), probe=_quiet) is False


def test_daemon_start_now_admits_a_google_only_machine():
    from sharing_on import setup_flow as sf
    assert sf.provider_available(config=_cfg(google_api_key="g"),
                                 probe=_quiet) is True


def test_daemon_start_now_admits_an_ollama_only_machine():
    """A user with Ollama running was told to go and get an OpenRouter key."""
    from sharing_on import setup_flow as sf
    assert sf.provider_available(config=_cfg(ollama_url="http://x:1"),
                                 probe=_answering) is True


def test_the_refusal_names_every_provider_not_only_openrouter():
    """'No OPENROUTER_API_KEY configured' hid four other ways to proceed."""
    from systemu.interface import cli_commands as cc
    from systemu.runtime import provider_status as ps
    msg = cc._no_provider_message(_cfg(), probe=_quiet)
    for spec in ps.PROVIDER_SPECS:
        assert spec.env in msg, f"{spec.display} is not offered"
    msg.encode("ascii")


def test_key_present_is_retained_as_the_pinned_admission_predicate():
    """`daemon_start` calls `key_present()` and tests monkeypatch it there."""
    from sharing_on import setup_flow as sf
    assert callable(sf.key_present)
    import inspect
    from systemu.interface import cli_commands
    fn = getattr(cli_commands.daemon_start, "callback", cli_commands.daemon_start)
    assert "key_present()" in inspect.getsource(fn)


# ── surface 3: doctor ────────────────────────────────────────────────────────

def test_doctor_reports_every_provider_from_the_mint():
    from systemu.runtime import platform_profile as pp
    from systemu.runtime import provider_status as ps
    rep = pp.build_doctor_report(
        config=_cfg(google_api_key="g", ollama_url="http://x:1"),
        provider_probe=_answering, keyring_locked=False,
        daemon_running=False, last_error=None)
    rows = {r["provider"]: r for r in rep["providers"]}
    assert set(rows) == {s.provider for s in ps.PROVIDER_SPECS}
    assert rows["ollama"]["state"] == ps.STATE_REACHABLE
    assert rows["google"]["state"] == ps.STATE_SET
    assert rows["openai"]["state"] == ps.STATE_MISSING
    assert rep["provider"]["configured"] is True


def test_doctors_blocking_problem_names_every_provider_when_nothing_is_set():
    from systemu.runtime import platform_profile as pp
    from systemu.runtime import provider_status as ps
    rep = pp.build_doctor_report(config=_cfg(), provider_probe=_quiet,
                                 keyring_locked=False,
                                 daemon_running=False, last_error=None)
    absent = [p for p in rep["problems"] if p["id"] == "provider_absent"]
    assert absent, "a machine with nothing configured must still be blocked"
    blob = absent[0]["message"] + " " + absent[0].get("cta", "")
    for spec in ps.PROVIDER_SPECS:
        assert spec.env in blob, f"{spec.display} is not offered"


def test_doctor_is_not_blocked_by_an_ollama_only_machine():
    from systemu.runtime import platform_profile as pp
    rep = pp.build_doctor_report(config=_cfg(ollama_url="http://x:1"),
                                 provider_probe=_answering,
                                 keyring_locked=False,
                                 daemon_running=False, last_error=None)
    assert [p for p in rep["problems"] if p["id"] == "provider_absent"] == []


# ── surface 4: the dashboard health banner ───────────────────────────────────

def test_the_health_banner_stops_warning_a_google_only_machine():
    from systemu.interface.components import health_banner as hb
    state = hb.build_health_state(vault_dir=None, config=_cfg(google_api_key="g"),
                                  provider_probe=_quiet)
    assert not [i for i in state.issues if "provider" in i.message.lower()
                or "API_KEY" in i.message]


def test_the_health_banner_still_warns_a_bare_machine_and_names_all_five():
    from systemu.interface.components import health_banner as hb
    from systemu.runtime import provider_status as ps
    state = hb.build_health_state(vault_dir=None, config=_cfg(),
                                  provider_probe=_quiet)
    hits = [i for i in state.issues if "provider" in i.message.lower()]
    assert hits, "a machine with nothing configured must still be warned"
    blob = hits[0].message + " " + (hits[0].cta or "")
    for spec in ps.PROVIDER_SPECS:
        assert spec.env in blob


# ── surface 5: the first-run checklist / onboarding gate ─────────────────────

def test_setup_status_key_check_accepts_any_provider():
    from systemu.runtime.first_run import setup_status
    vault = SimpleNamespace()
    checks = {c["id"]: c for c in setup_status(_cfg(anthropic_api_key="a"), vault)}
    assert checks["key_present"]["ok"] is True
    bare = {c["id"]: c for c in setup_status(_cfg(), vault)}
    assert bare["key_present"]["ok"] is False


def test_the_headless_remedy_names_every_provider_env_var():
    """A headless operator was told about one env var out of five."""
    from systemu.runtime.first_run import HEADLESS_REMEDIES
    from systemu.runtime import provider_status as ps
    env = HEADLESS_REMEDIES["key_present"]["env"]
    for spec in ps.PROVIDER_SPECS:
        assert spec.env in env, f"{spec.display} is not a headless remedy"


# ── surface 6: the two attribute-enumerating short-circuits ──────────────────

@pytest.mark.parametrize("mod", ["episodic_memory", "open_world_planner"])
def test_the_llm_short_circuits_consume_the_mint(mod):
    import importlib
    m = importlib.import_module(f"systemu.runtime.{mod}")
    assert m._has_llm_provider(_cfg()) is False
    assert m._has_llm_provider(_cfg(openai_api_key="k")) is True
    assert m._has_llm_provider(_cfg(openrouter_api_key="   ")) is False


@pytest.mark.parametrize("mod", ["episodic_memory", "open_world_planner"])
def test_the_short_circuits_do_no_network_io_unless_ollama_is_selected(mod):
    """DEC-44 aside, this one is about latency: these run per-objective."""
    import importlib
    from systemu.runtime import provider_status as ps
    m = importlib.import_module(f"systemu.runtime.{mod}")
    ps.clear_probe_cache()
    seen = {"n": 0}

    def _boom(url, timeout):
        seen["n"] += 1
        return _answering(url, timeout)

    real = ps._PROBES.get("ollama")
    ps._PROBES["ollama"] = _boom
    try:
        m._has_llm_provider(_cfg(ollama_url="http://z:1"))
        assert seen["n"] == 0
        assert m._has_llm_provider(_cfg(ollama_url="http://z:1",
                                        tier1_provider="ollama")) is True
        assert seen["n"] == 1
    finally:
        ps._PROBES["ollama"] = real
        ps.clear_probe_cache()


# ── surface 7: Config.validate() (rendered by `sharing_on info`) ─────────────

def test_config_validate_accepts_any_provider_and_names_all_when_none():
    from sharing_on.config import Config
    from systemu.runtime import provider_status as ps

    bare = Config()
    bare.ollama_url = "http://127.0.0.1:1"
    errs = bare.validate()
    assert errs, "a machine with nothing configured is still misconfigured"
    for spec in ps.PROVIDER_SPECS:
        assert spec.env in errs[0], f"{spec.display} is not offered"

    ok = Config()
    ok.anthropic_api_key = "a"
    assert ok.validate() == []


# ── surface 8: the daemon startup banner ─────────────────────────────────────

def test_the_daemon_banner_reports_every_provider():
    from systemu.scheduler.daemon import provider_banner_lines
    from systemu.runtime import provider_status as ps
    lines = provider_banner_lines(_cfg(google_api_key="g",
                                       ollama_url="http://x:1"),
                                  probe=_answering)
    blob = "\n".join(lines)
    for spec in ps.PROVIDER_SPECS:
        assert spec.display in blob, f"{spec.display} is missing from the banner"
    blob.encode("ascii")
    assert not re.search(r"\bg\b", blob), "a credential VALUE must never be printed"
