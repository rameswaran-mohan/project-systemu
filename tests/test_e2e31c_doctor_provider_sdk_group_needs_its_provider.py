"""e2e31: a provider-SDK group is "missing" only for an operator who chose that provider.

THE DEFECT
    Registering ``anthropic`` as an optional group (so the mint has a remedy to
    print) made ``platform_profile.build_doctor_report`` add
    ``optional_group_missing:anthropic`` on EVERY install without the extra --
    and /health turns any warning into the WARNINGS pill. So a fresh operator
    running entirely on OpenRouter or Google, who never typed an Anthropic key,
    was told their install was unhealthy over an SDK for a provider they never
    chose. Crying wolf on the page whose whole job is to be believed.

THE RULING
    A group that ships a PROVIDER SDK is reported missing ONLY when that
    provider is configured (its credential is set). Capability groups
    (browser, dashboard, ...) keep today's rule: missing is reported, always.

    Which group ships which provider's SDK is DERIVED -- the providers
    registry's ``sdk_package`` joined to the optional-deps group registry --
    never a second table. And "not configured" must be POSITIVELY observed:
    a provider whose status the mint could not produce is reported, because
    suppressing a warning is the "looks fine" direction.

Everything below drives the real ``build_doctor_report`` / ``health_view``
path. The seams are the two single probes the product already has --
``optional_deps.is_installed`` and ``systemu.llm.providers._find_spec`` -- plus
a config object, so nothing here depends on what this machine has installed.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from systemu.llm import providers as pr
from systemu.runtime import optional_deps as od
from systemu.runtime import platform_profile as pp
from systemu.runtime import provider_status as ps

_ID = "optional_group_missing:anthropic"
_REMEDY = 'pip install "systemu[anthropic]"'


class _PresentKeyring:
    """A keyring that exists and is unlocked, so the profile reports no
    plaintext-fallback warning and the chip follows only what is under test."""

    def get_password(self, service, key):
        return None


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    monkeypatch.setattr(pp, "_usable_keyring", lambda: _PresentKeyring())
    for spec in ps.PROVIDER_SPECS:
        monkeypatch.delenv(spec.env, raising=False)
    ps.clear_probe_cache()
    yield
    ps.clear_probe_cache()


def _installed(monkeypatch, *, missing=()):
    """THE ONE availability probe, flipped: exactly ``missing`` is absent."""
    gone = {od.canonical(p) for p in missing}
    monkeypatch.setattr(od, "is_installed",
                        lambda pkg: od.canonical(pkg) not in gone)


def _sdk(monkeypatch, *, present: bool):
    """THE SDK seam the provider mint asks."""
    monkeypatch.setattr(pr, "_find_spec",
                        lambda name: object() if present else None)


def _cfg(**kw):
    base = {s.attr: "" for s in ps.PROVIDER_SPECS}
    base.update(kw)
    return SimpleNamespace(**base)


def _report(cfg, **kw):
    args = dict(config=cfg, provider_probe=ps.unprobed,
                provider_reachable=True, keyring_locked=False,
                daemon_running=True)
    args.update(kw)
    return pp.build_doctor_report(**args)


def _ids(report):
    return {p["id"] for p in report["problems"]}


# --------------------------------------------------------------------------- #
#  1. the operator who never chose Anthropic is not warned about its SDK
# --------------------------------------------------------------------------- #

def test_no_key_and_no_sdk_reports_nothing_about_the_provider_group(monkeypatch):
    _installed(monkeypatch, missing=("anthropic",))
    _sdk(monkeypatch, present=False)
    report = _report(_cfg(google_api_key="g"))
    assert _ID not in _ids(report), [p["message"] for p in report["problems"]]


def test_the_group_is_still_listed_never_subtracted(monkeypatch):
    """The ruling gates the PROBLEM, not the inventory. `doctor`'s group table
    and the /health group rows still show the extra, its state and its remedy
    -- an operator who later picks an Anthropic tier can read how to get it."""
    _installed(monkeypatch, missing=("anthropic",))
    _sdk(monkeypatch, present=False)
    rows = {g["extra"]: g for g in _report(_cfg(google_api_key="g"))
            ["optional_groups"]}
    assert rows["anthropic"]["installed"] is False
    assert rows["anthropic"]["remedy"] == _REMEDY


def test_the_health_chip_stays_ok_on_such_an_install(monkeypatch):
    """THE SYMPTOM: the WARNINGS pill on every install without the extra."""
    from systemu.interface.pages import health
    _installed(monkeypatch, missing=("anthropic",))
    _sdk(monkeypatch, present=False)
    view = health.health_view(config=_cfg(google_api_key="g"),
                              provider_probe=ps.unprobed,
                              provider_reachable=True, keyring_locked=False,
                              daemon_running=True, load={})
    assert view["status_chip"] == "ok", view["problems"]
    assert view["ok"] is True


# --------------------------------------------------------------------------- #
#  2. the operator who DID choose it is told -- the warning is real for them
# --------------------------------------------------------------------------- #

def test_a_key_without_the_sdk_reports_the_group(monkeypatch):
    _installed(monkeypatch, missing=("anthropic",))
    _sdk(monkeypatch, present=False)
    report = _report(_cfg(anthropic_api_key="sk-ant-test", google_api_key="g"))
    hits = [p for p in report["problems"] if p["id"] == _ID]
    assert len(hits) == 1, report["problems"]
    assert hits[0]["severity"] == "warning" and hits[0]["blocking"] is False
    assert hits[0]["cta"] == _REMEDY
    # ...and the provider row beside it already carries the same remedy.
    row = {p["provider"]: p for p in report["providers"]}["anthropic"]
    assert row["satisfied"] is False and _REMEDY in row["detail"]


def test_a_key_without_the_sdk_turns_the_chip(monkeypatch):
    from systemu.interface.pages import health
    _installed(monkeypatch, missing=("anthropic",))
    _sdk(monkeypatch, present=False)
    view = health.health_view(config=_cfg(anthropic_api_key="sk-ant-test",
                                          google_api_key="g"),
                              provider_probe=ps.unprobed,
                              provider_reachable=True, keyring_locked=False,
                              daemon_running=True, load={})
    assert view["status_chip"] == "warn", view["problems"]


# --------------------------------------------------------------------------- #
#  3. capability groups keep today's rule
# --------------------------------------------------------------------------- #

def test_a_missing_capability_group_is_reported_as_today(monkeypatch):
    """No provider ships the browser, so no credential can make it optional
    to mention: missing is missing, with or without any key."""
    _installed(monkeypatch, missing=("playwright",))
    _sdk(monkeypatch, present=True)
    for cfg in (_cfg(google_api_key="g"), _cfg()):
        assert "optional_group_missing:browser" in _ids(_report(cfg))


# --------------------------------------------------------------------------- #
#  4. the mapping is DERIVED, and an unknown answer fails toward disclosure
# --------------------------------------------------------------------------- #

def test_the_provider_mapping_is_read_from_the_providers_registry(monkeypatch):
    """No second table. If the providers registry says no provider imports
    this package, the group is an ordinary capability group again and is
    reported without any key -- so a hard-coded "anthropic is a provider
    group" list goes red here."""
    _installed(monkeypatch, missing=("anthropic",))
    _sdk(monkeypatch, present=False)
    monkeypatch.setattr(pr, "sdk_package", lambda provider: "")
    assert _ID in _ids(_report(_cfg(google_api_key="g")))


def test_a_provider_registry_that_cannot_answer_reports_the_group(monkeypatch):
    """`sdk_package` raising is "we could not tell which provider this group
    serves" -- the group is reported, never quietly dropped."""
    _installed(monkeypatch, missing=("anthropic",))
    _sdk(monkeypatch, present=False)

    def _boom(provider):
        raise RuntimeError("registry unreadable")

    monkeypatch.setattr(pr, "sdk_package", _boom)
    assert _ID in _ids(_report(_cfg(google_api_key="g")))


def test_a_status_the_mint_could_not_produce_reports_the_group(monkeypatch):
    """"Not configured" must be OBSERVED. A mint that yielded nothing (its
    documented fail-closed answer) cannot vouch that nobody chose Anthropic,
    so the warning stays."""
    _installed(monkeypatch, missing=("anthropic",))
    _sdk(monkeypatch, present=False)
    monkeypatch.setattr(pp, "provider_statuses", lambda *a, **k: {})
    assert _ID in _ids(_report(_cfg(google_api_key="g")))


def test_a_malformed_status_reports_the_group(monkeypatch):
    """A status whose fields are not the mint's concrete types is not an
    observation of "unconfigured" either (DEC-36)."""
    _installed(monkeypatch, missing=("anthropic",))
    _sdk(monkeypatch, present=False)
    real = pp.provider_statuses

    def _junk(*a, **k):
        out = dict(real(*a, **k))
        st = out["anthropic"]
        out["anthropic"] = ps.ProviderStatus(st.provider, st.display, st.env,
                                             st.rule, st.state, st.detail,
                                             None)
        return out

    monkeypatch.setattr(pp, "provider_statuses", _junk)
    assert _ID in _ids(_report(_cfg(google_api_key="g")))


def test_an_installed_provider_group_is_silent_with_or_without_a_key(monkeypatch):
    _installed(monkeypatch, missing=())
    _sdk(monkeypatch, present=True)
    for cfg in (_cfg(google_api_key="g"),
                _cfg(anthropic_api_key="sk-ant-test")):
        assert not [i for i in _ids(_report(cfg))
                    if i.startswith("optional_group_missing")]
