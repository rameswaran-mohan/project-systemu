"""D-3 (e2e 0.10.31): the availability probe walked the metadata tree per ROW.

THE WITNESS
-----------
Profiled on this machine, ``_build_layout("Home", "/")`` against a 40-tool
vault, psutil TTL pinned warm so only the steady-state cost is in frame::

    ncalls  tottime  cumtime  function
       246    0.001    0.663  runtime/optional_deps.py:211(unavailable_reason)
       126    0.002    0.659  runtime/optional_deps.py:124(is_installed)
       126    0.000    0.654  importlib/metadata/__init__.py:855(distribution)
      1095    0.579    0.605  {built-in method nt.stat}

126 calls over three renders: FORTY-TWO ``importlib.metadata.distribution``
lookups on every single page paint, each one walking every ``sys.path`` entry
with ``listdir`` + ``stat``. 218 ms per render, on the event loop, on EVERY
route -- and the same probe is what ``entity_rows`` pays per row on /tools and
/skills, which measured 4-5 s server-side.

THE PROPERTY
------------
    THE PROBE IS ANSWERED ONCE PER DISTRIBUTION, NOT ONCE PER CALLER.

    ``is_installed`` stays THE SINGLE PROBE and its verdict is unchanged: a
    distribution we cannot positively find is reported absent. What changes is
    how many times the same question walks the filesystem.

WHY A PROCESS-LIFETIME MEMO IS HONEST HERE
------------------------------------------
The remedy this module mints already says so: every ``unavailable_reason``
ends in a ``pip install`` the operator runs OUTSIDE this process, and the
dashboard's own copy adds "Then restart the daemon". An interpreter that has
already resolved its import machinery does not gain a working extra mid-run,
so a memo cannot make the answer stale in a direction that matters. A TTL
would be the wrong shape anyway: it bounds how OFTEN the walk is paid, not
WHERE -- and WHERE is the whole finding (D4's lesson, restated).

``clear_probe_cache()`` is the seam for the one caller that genuinely needs a
re-walk (a test, an installer that just finished).
"""
from __future__ import annotations

import importlib.metadata

import pytest

from systemu.runtime import optional_deps as od


@pytest.fixture(autouse=False)
def counted(monkeypatch):
    """Count the REAL metadata walks, and keep answering truthfully."""
    real = importlib.metadata.distribution
    seen = []

    def _counting(name):
        seen.append(name)
        return real(name)

    monkeypatch.setattr(importlib.metadata, "distribution", _counting)
    od.clear_probe_cache()
    yield seen
    od.clear_probe_cache()


# -- THE REPRO ---------------------------------------------------------------

def test_the_metadata_tree_is_walked_once_per_distribution(counted):
    """THE REPRO. Before the memo this was 50 walks, one per call."""
    for _ in range(50):
        od.is_installed("click")
    assert counted == ["click"], (
        "the availability probe re-walked sys.path per caller: "
        + repr(len(counted)) + " walks")


def test_an_absent_distribution_is_also_asked_only_once(counted):
    """The miss is the expensive one: a distribution that is NOT there costs a
    full walk of every sys.path entry before it can be ruled out."""
    for _ in range(30):
        assert od.is_installed("no-such-distribution-at-all") is False
    assert len(counted) == 1, counted


def test_the_render_shaped_call_pays_one_walk_per_distinct_package(counted):
    """``missing_packages`` over a tool corpus: 40 tools, 3 distinct packages."""
    corpus = (["click", "nicegui", "no-such-distribution-at-all"] * 40)
    od.missing_packages(corpus)
    assert len(counted) == 3, counted


# -- the verdict is UNCHANGED ------------------------------------------------

def test_the_memo_answers_exactly_what_the_walk_answers(counted):
    """A cache that changes the answer is not a cache."""
    assert od.is_installed("click") is True
    assert od.is_installed("click") is True          # from the memo
    assert od.is_installed("no-such-distribution-at-all") is False
    assert od.is_installed("no-such-distribution-at-all") is False


def test_a_probe_that_blows_up_is_still_reported_absent(monkeypatch):
    """Fail CLOSED, memo or no memo: 'I could not tell' is never 'ready'."""
    od.clear_probe_cache()

    def _explode(_name):
        raise RuntimeError("metadata is unreadable")

    monkeypatch.setattr(importlib.metadata, "distribution", _explode)
    try:
        assert od.is_installed("click") is False
        assert od.is_installed("click") is False
    finally:
        od.clear_probe_cache()


def test_the_name_is_canonicalised_before_it_is_memoised(counted):
    """``Foo_Bar`` and ``foo-bar`` are the same distribution, so they are the
    same memo entry -- otherwise a corpus that spells a package two ways pays
    the walk twice and the memo silently under-delivers."""
    a = od.canonical("Ruamel_Yaml")
    b = od.canonical("ruamel-yaml")
    assert a == b, (a, b)
    od.is_installed("Ruamel_Yaml")
    od.is_installed("ruamel-yaml")
    assert len(counted) == 1, counted


# -- the seams other tests rely on -------------------------------------------

def test_clear_probe_cache_makes_the_next_call_walk_again(counted):
    od.is_installed("click")
    od.is_installed("click")
    assert len(counted) == 1, counted
    od.clear_probe_cache()
    od.is_installed("click")
    assert len(counted) == 2, counted


def test_patching_is_installed_still_overrides_everything(monkeypatch):
    """Six existing test files fake availability by replacing this module
    attribute. The memo must live INSIDE the probe, so replacing the probe
    replaces the memo with it."""
    od.clear_probe_cache()
    od.is_installed("nicegui")                       # prime a real answer
    monkeypatch.setattr(od, "is_installed", lambda pkg: od.canonical(pkg) != "nicegui")
    assert od.missing_packages(["nicegui"]) == ("nicegui",)
    od.clear_probe_cache()


def test_the_memo_is_bounded(counted):
    """An unbounded corpus must not grow an unbounded dict: the memo is a
    render optimisation, not a registry."""
    assert type(od._MEMO_CAP) is int and od._MEMO_CAP > 0
    for i in range(od._MEMO_CAP + 25):
        od.is_installed("systemu-not-a-real-package-%d" % i)
    assert len(od._INSTALLED_MEMO) <= od._MEMO_CAP, len(od._INSTALLED_MEMO)
