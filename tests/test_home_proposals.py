"""Home's "Systemu noticed" card - one quiet, declinable growth proposal.

The engine (``systemu/interface/proposals.py``) is a PURE READER, and these
tests pin exactly that:

  * DERIVED AT RENDER - ``derive_proposal`` opens no store, starts no hook and
    writes nothing anywhere except through the decline path.  In particular it
    never touches the OnTheTable store, whose sole writer stays
    ``table_reconciler.project()``.
  * AT MOST ONE - the return type is a single optional proposal, never a list.
  * A DECLINE IS PERMANENT - it persists as a user fact tagged
    ``proposal_declined`` and that key can never be offered again.
  * NO UNWIRED PROMISES - every route a proposal points at is a page the
    dashboard actually registers.

The last block are WIRING pins rather than unit tests: remove the production
call site in ``console.build_home_page`` and they go red.  An engine nothing
renders is the half-built shape this card exists to avoid.
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from systemu.interface import proposals
from systemu.interface.pages import console
from systemu.interface.proposals import decline, derive_proposal


# ---------------------------------------------------------------------------
#  Vault stand-ins - only the two reads the engine makes, plus ``root``
# ---------------------------------------------------------------------------


class _Vault:
    """Minimal vault stand-in: two list reads over a real (temp) root."""

    def __init__(self, root, *, shadows=(), tools=()):
        self.root = str(root)
        self._shadows = list(shadows)
        self._tools = list(tools)

    def list_shadows(self, status=None):
        return list(self._shadows)

    def list_tools(self, status=None):
        return list(self._tools)


class _Exploding:
    """Every read raises - Home must still render."""

    def __init__(self, root):
        self.root = str(root)

    def list_shadows(self, status=None):
        raise RuntimeError("vault down")

    def list_tools(self, status=None):
        raise RuntimeError("vault down")


# ---------------------------------------------------------------------------
#  The plan's Step-1 tests, verbatim
# ---------------------------------------------------------------------------


def test_one_proposal_max_and_declines_are_permanent(tmp_path):
    from systemu.interface.proposals import derive_proposal, decline

    class V:
        root = str(tmp_path)

        def list_shadows(self, status=None):
            return []

        def list_tools(self, status=None):
            return []

    v = V()
    key, text, route = derive_proposal(v)
    assert key == "first_shadow" and route == "/chat"
    decline(v, key)
    nxt = derive_proposal(v)
    assert nxt is None or nxt[0] != "first_shadow"


def test_proposal_derivation_never_raises_on_broken_vault(tmp_path):
    from systemu.interface.proposals import derive_proposal

    class Broken:
        root = str(tmp_path)

        def list_shadows(self, status=None):
            raise RuntimeError("boom")

        def list_tools(self, status=None):
            raise RuntimeError("boom")

    assert derive_proposal(Broken()) is None


# ---------------------------------------------------------------------------
#  Derivation rules
# ---------------------------------------------------------------------------


class TestDerivation:
    def test_no_shadows_yet_proposes_recording_the_first_one(self, tmp_path):
        found = derive_proposal(_Vault(tmp_path))
        assert found is not None
        key, text, route = found
        assert key == "first_shadow"
        assert route == "/chat"
        assert text.strip()

    def test_shadows_but_no_tools_proposes_the_first_forge(self, tmp_path):
        found = derive_proposal(_Vault(tmp_path, shadows=[{"id": "s1"}]))
        assert found is not None
        key, _text, route = found
        assert key == "first_forge"
        assert route == "/chat"

    def test_an_install_with_both_gets_no_proposal(self, tmp_path):
        v = _Vault(tmp_path, shadows=[{"id": "s1"}], tools=[{"id": "t1"}])
        assert derive_proposal(v) is None

    def test_result_is_one_proposal_not_a_list_of_them(self, tmp_path):
        found = derive_proposal(_Vault(tmp_path))
        assert isinstance(found, tuple) and len(found) == 3
        assert all(isinstance(part, str) for part in found)

    def test_a_vault_missing_the_methods_entirely_yields_none(self, tmp_path):
        class _Bare:
            root = str(tmp_path)

        assert derive_proposal(_Bare()) is None

    def test_a_raising_vault_yields_none(self, tmp_path):
        assert derive_proposal(_Exploding(tmp_path)) is None

    def test_none_from_a_backend_reads_as_empty_not_a_crash(self, tmp_path):
        class _NoneVault(_Vault):
            def list_shadows(self, status=None):
                return None

            def list_tools(self, status=None):
                return None

        # No shadows -> the first-shadow proposal, not an exception.
        assert derive_proposal(_NoneVault(tmp_path))[0] == "first_shadow"


# ---------------------------------------------------------------------------
#  Declines are user facts, and they are permanent
# ---------------------------------------------------------------------------


class TestDecline:
    def test_decline_persists_a_tagged_user_fact(self, tmp_path):
        from systemu.runtime.user_profile import get_facts

        v = _Vault(tmp_path)
        decline(v, "first_shadow")
        facts = get_facts(v, tags=["proposal_declined"])
        assert len(facts) == 1
        assert "first_shadow" in facts[0].fact
        assert "proposal_declined" in facts[0].tags

    def test_a_declined_key_never_returns_even_when_still_applicable(self, tmp_path):
        v = _Vault(tmp_path)
        assert derive_proposal(v)[0] == "first_shadow"
        decline(v, "first_shadow")
        # Same still-empty vault: the condition holds, the offer does not.
        assert derive_proposal(v) is None

    def test_declining_one_key_does_not_suppress_the_other(self, tmp_path):
        v = _Vault(tmp_path, shadows=[{"id": "s1"}])
        decline(v, "first_shadow")
        assert derive_proposal(v)[0] == "first_forge"

    def test_declining_every_key_leaves_nothing_to_offer(self, tmp_path):
        v = _Vault(tmp_path)
        decline(v, "first_shadow")
        decline(v, "first_forge")
        assert derive_proposal(v) is None
        v2 = _Vault(tmp_path, shadows=[{"id": "s1"}])
        assert derive_proposal(v2) is None

    def test_an_unreadable_fact_store_degrades_to_offering_not_crashing(
        self, tmp_path, monkeypatch
    ):
        def _boom(*_a, **_kw):
            raise RuntimeError("facts unreadable")

        monkeypatch.setattr(
            "systemu.runtime.user_profile.get_facts", _boom, raising=True
        )
        assert derive_proposal(_Vault(tmp_path))[0] == "first_shadow"


# ---------------------------------------------------------------------------
#  Design locks: derived at render, never a writer
# ---------------------------------------------------------------------------


class TestPurity:
    def test_derivation_writes_nothing_to_the_vault(self, tmp_path):
        before = sorted(p.name for p in Path(tmp_path).iterdir())
        derive_proposal(_Vault(tmp_path))
        derive_proposal(_Vault(tmp_path, shadows=[{"id": "s1"}]))
        assert sorted(p.name for p in Path(tmp_path).iterdir()) == before

    def test_the_only_file_a_decline_creates_is_the_facts_log(self, tmp_path):
        decline(_Vault(tmp_path), "first_shadow")
        assert sorted(p.name for p in Path(tmp_path).iterdir()) == [
            "user_facts.jsonl"
        ]

    def test_the_engine_never_reaches_for_the_onthetable_store(self):
        """table_store's sole writer is ``table_reconciler.project()``; a
        proposal is derived, never projected onto the table."""
        src = inspect.getsource(proposals)
        for forbidden in ("table_store", "table_reconciler", "TableItem"):
            assert forbidden not in src, (
                f"proposals.py must not touch {forbidden} - proposals are "
                "derived at render, not written to any store"
            )

    def test_no_proposal_promises_an_unwired_surface(self, tmp_path):
        """Each route must be a page the dashboard actually registers."""
        from systemu.interface import dashboard

        routes = {
            derive_proposal(_Vault(tmp_path))[2],
            derive_proposal(_Vault(tmp_path, shadows=[{"id": "s1"}]))[2],
        }
        dash = inspect.getsource(dashboard)
        for route in routes:
            assert f'@ui.page("{route}")' in dash, \
                f"proposal points at {route}, which no page registers"


# ---------------------------------------------------------------------------
#  Wiring pins - the engine must reach the operator's screen
# ---------------------------------------------------------------------------


def test_home_page_renders_the_proposal_card():
    src = inspect.getsource(console.build_home_page)
    assert "_build_proposal_card(" in src, \
        "the proposal engine exists but Home never renders it"


def test_the_proposal_card_sits_directly_after_the_growth_card():
    src = inspect.getsource(console.build_home_page)
    assert src.index("_build_growth_card(") < src.index("_build_proposal_card(")


def test_the_card_composes_the_engine_and_offers_both_actions():
    src = inspect.getsource(console._build_proposal_card)
    assert "Systemu noticed" in src
    assert "derive_proposal(" in src
    # "Show me" navigates to the proposal's own route...
    assert "ui.navigate.to(" in src
    # ...and "No thanks" declines it permanently, then re-renders without it.
    assert "decline(" in src
    assert ".refresh()" in src
