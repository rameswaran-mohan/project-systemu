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


# ===========================================================================
#  Capability Wishlist v1, Task 3 - the fulfilment nudge
#
#  APPEND-ONLY: everything above this line is the pre-wishlist contract and
#  stays verbatim.  A wish nudge is one more derivation in the SAME engine, so
#  it inherits one-at-a-time and decline-forever for free; what needs its own
#  pins is the PRECEDENCE it takes and the honesty wall in front of it.
#
#  PRECEDENCE AS IMPLEMENTED: the wish nudge is derived AFTER both starter
#  proposals.  A cold install is told to record something first; only once
#  `first_shadow` is satisfied or declined can a wish surface.  (`first_forge`
#  cannot compete: it requires an EMPTY toolbox, and a fulfilled wish requires
#  a deployed tool in it.)
# ===========================================================================


def _deployed(name, description):
    return {"id": name, "name": name, "description": description,
            "status": "deployed", "enabled": True}


_INVOICE_TOOL = _deployed("invoice_reminder",
                          "Email a reminder for an unpaid invoice.")
_INVOICE_WISH = "send my weekly invoice reminders by email"


def _lived_in(tmp_path, **kw):
    """A vault past the starter proposals: it has recorded something."""
    return _Vault(tmp_path, shadows=[{"id": "s1"}], **kw)


class TestWishFulfilment:
    def test_a_shipped_tool_that_matches_a_wish_becomes_the_proposal(self, tmp_path):
        from systemu.interface.wishes import add_wish

        v = _lived_in(tmp_path, tools=[_INVOICE_TOOL])
        fid = add_wish(v, _INVOICE_WISH)

        key, text, route = derive_proposal(v)
        assert key == f"wish:{fid}"
        assert route == "/tools"
        assert _INVOICE_WISH in text
        assert "invoice_reminder" in text
        assert text.isascii()

    def test_the_wish_nudge_ranks_below_the_two_starter_proposals(self, tmp_path):
        """The precedence pin.  On a cold install the starter proposal wins even
        though the wish is fulfillable; declining it lets the wish through."""
        from systemu.interface.wishes import add_wish

        v = _Vault(tmp_path, tools=[_INVOICE_TOOL])          # no shadows yet
        add_wish(v, _INVOICE_WISH)

        assert derive_proposal(v)[0] == "first_shadow"
        decline(v, "first_shadow")
        assert derive_proposal(v)[0].startswith("wish:")

    def test_a_wish_nothing_matches_leaves_the_prior_answer_untouched(self, tmp_path):
        from systemu.interface.wishes import add_wish

        v = _lived_in(tmp_path, tools=[_INVOICE_TOOL])
        add_wish(v, "walk my dog on tuesdays")
        assert derive_proposal(v) is None

        cold = _Vault(tmp_path / "cold")
        add_wish(cold, "walk my dog on tuesdays")
        assert derive_proposal(cold)[0] == "first_shadow"

        forging = _Vault(tmp_path / "forging", shadows=[{"id": "s1"}])
        add_wish(forging, "walk my dog on tuesdays")
        assert derive_proposal(forging)[0] == "first_forge"

    def test_the_newest_matching_wish_is_the_one_offered(self, tmp_path):
        from systemu.interface.wishes import add_wish

        v = _lived_in(tmp_path, tools=[
            _INVOICE_TOOL,
            _deployed("receipt_filer", "File a receipt into the right folder."),
        ])
        add_wish(v, _INVOICE_WISH)
        newest = add_wish(v, "file my receipt scans into the right folder")

        assert derive_proposal(v)[0] == f"wish:{newest}"

    def test_a_declined_wish_never_returns_but_a_new_wish_is_a_new_key(self, tmp_path):
        from systemu.interface.wishes import add_wish

        v = _lived_in(tmp_path, tools=[_INVOICE_TOOL])
        first = add_wish(v, _INVOICE_WISH)
        decline(v, f"wish:{first}")
        assert derive_proposal(v) is None

        second = add_wish(v, "email me an invoice reminder every friday")
        assert derive_proposal(v)[0] == f"wish:{second}"

    def test_a_dismissed_wish_is_never_offered(self, tmp_path):
        from systemu.interface.wishes import add_wish, dismiss_wish

        v = _lived_in(tmp_path, tools=[_INVOICE_TOOL])
        fid = add_wish(v, _INVOICE_WISH)
        assert dismiss_wish(v, fid) is True
        assert derive_proposal(v) is None

    def test_a_broken_vault_with_wishes_still_yields_none(self, tmp_path):
        from systemu.interface.wishes import add_wish

        add_wish(_Vault(tmp_path), _INVOICE_WISH)
        assert derive_proposal(_Exploding(tmp_path)) is None


class TestWishHonestyWall:
    """"The toolbox can now do this" may only be said about a tool that can
    actually run today.  A forged-but-undeployed or disabled tool is a plan."""

    def test_a_tool_that_is_not_deployed_cannot_fulfil_a_wish(self, tmp_path):
        from systemu.interface.wishes import add_wish

        for status in ("proposed", "forged", "tested"):
            v = _lived_in(tmp_path / status, tools=[
                dict(_INVOICE_TOOL, status=status)])
            add_wish(v, _INVOICE_WISH)
            assert derive_proposal(v) is None, status

    def test_a_disabled_tool_cannot_fulfil_a_wish(self, tmp_path):
        from systemu.interface.wishes import add_wish

        v = _lived_in(tmp_path, tools=[dict(_INVOICE_TOOL, enabled=False)])
        add_wish(v, _INVOICE_WISH)
        assert derive_proposal(v) is None

    def test_a_nameless_tool_is_never_named(self, tmp_path):
        from systemu.interface.wishes import add_wish

        v = _lived_in(tmp_path, tools=[dict(_INVOICE_TOOL, name="")])
        add_wish(v, _INVOICE_WISH)
        assert derive_proposal(v) is None

    def test_the_wish_route_is_a_page_the_dashboard_registers(self, tmp_path):
        from systemu.interface import dashboard
        from systemu.interface.wishes import add_wish

        v = _lived_in(tmp_path, tools=[_INVOICE_TOOL])
        add_wish(v, _INVOICE_WISH)
        route = derive_proposal(v)[2]
        assert f'@ui.page("{route}")' in inspect.getsource(dashboard)

    def test_a_long_wish_is_quoted_with_a_visible_ellipsis(self, tmp_path):
        from systemu.interface.wishes import add_wish

        long_wish = ("send my weekly invoice reminders by email " + "x" * 300)
        v = _lived_in(tmp_path, tools=[_INVOICE_TOOL])
        add_wish(v, long_wish)
        _key, text, _route = derive_proposal(v)
        assert "..." in text
        assert "x" * 300 not in text
        assert len(text) < 300


class TestWishPurity:
    def test_deriving_a_wish_proposal_writes_nothing_new(self, tmp_path):
        from systemu.interface.wishes import add_wish

        v = _lived_in(tmp_path, tools=[_INVOICE_TOOL])
        add_wish(v, _INVOICE_WISH)
        before = sorted(p.name for p in Path(tmp_path).iterdir())
        derive_proposal(v)
        derive_proposal(v)
        assert sorted(p.name for p in Path(tmp_path).iterdir()) == before

    def test_the_engine_still_never_reaches_for_the_onthetable_store(self):
        src = inspect.getsource(proposals)
        for forbidden in ("table_store", "table_reconciler", "TableItem"):
            assert forbidden not in src


# ===========================================================================
#  2b - the first-run replay nudge
#
#  APPEND-ONLY: everything above this line is the pre-2b contract and stays
#  verbatim.  The replay nudge is one more derivation in the SAME engine, so it
#  inherits one-at-a-time and decline-forever for free and adds NO rendering
#  machinery and NO writer; what needs its own pins is the condition (exactly
#  ONE finished workflow), the data source (work.py's own loader, never a second
#  private notion of "finished"), and the PRECEDENCE it takes - last.
#
#  The autouse fixture below is the one thing that reaches backwards: the new
#  candidate reads work.py's row loader, and `WorkflowTracker` is a process
#  singleton, so without it an unrelated earlier test could seed a workflow and
#  make the assertions above depend on suite ordering.  It stubs the ambient
#  loader to empty (the same guard, for the same reason, as
#  tests/test_home_growth_card.py::_no_ambient_workflows); no assertion above is
#  touched, and each test below installs its own rows.
# ===========================================================================


@pytest.fixture(autouse=True)
def _no_ambient_workflows(monkeypatch):
    from systemu.interface.pages import work
    monkeypatch.setattr(work, "_load_rows", lambda: [], raising=True)


_REPLAY_TEXT = ("Your first task finished. See exactly what it did - "
                "every stage, tool and gate.")


def _install_rows(monkeypatch, *rows):
    """Install `rows` as what work.py's list would render, for one test."""
    from systemu.interface.pages import work
    monkeypatch.setattr(work, "_load_rows", lambda: list(rows))


def _row(status, wid="wf_1"):
    return {"workflow_id": wid, "title": "Tidy my receipts", "status": status}


def _real_completed_row():
    """A row built by work.py's OWN model from the status the workflow lane
    actually writes - not a literal guessed in a test."""
    from systemu.core.models import ActivityStatus
    from systemu.interface.pages.work import work_row_model
    from systemu.runtime.workflow_tracker import WorkflowSnapshot

    return work_row_model(WorkflowSnapshot(
        workflow_id="wf_real", title="Tidy my receipts", stage="done",
        status=ActivityStatus.COMPLETED.value,
    ))


class TestFirstRunReplay:
    def test_one_finished_task_offers_the_replay(self, tmp_path, monkeypatch):
        _install_rows(monkeypatch, _row("completed"))
        v = _lived_in(tmp_path, tools=[{"id": "t1"}])   # past both starters
        key, text, route = derive_proposal(v)
        assert key == "first_run_replay"
        assert route == "/work"
        assert text == _REPLAY_TEXT
        assert text.isascii()

    def test_the_status_it_looks_for_is_the_one_the_lanes_really_write(
        self, tmp_path, monkeypatch
    ):
        """Grounded in production, not in a literal: the row comes from
        work.work_row_model over ActivityStatus.COMPLETED (the workflow lane),
        and "success" is what pipelines/quick_task.py stamps on the quick lane."""
        from systemu.core.models import ActivityStatus
        assert ActivityStatus.COMPLETED.value == "completed"

        _install_rows(monkeypatch, _real_completed_row())
        v = _lived_in(tmp_path, tools=[{"id": "t1"}])
        assert derive_proposal(v)[0] == "first_run_replay"

        _install_rows(monkeypatch, _row("success"))
        quick = _lived_in(tmp_path / "quick", tools=[{"id": "t1"}])
        assert derive_proposal(quick)[0] == "first_run_replay"

    def test_no_task_yet_is_not_a_finished_one(self, tmp_path):
        assert derive_proposal(_lived_in(tmp_path, tools=[{"id": "t1"}])) is None

    def test_a_task_still_running_has_not_finished(self, tmp_path, monkeypatch):
        for status in ("running", "queued", "pending_approval", "waiting_on_tools"):
            _install_rows(monkeypatch, _row(status))
            assert derive_proposal(
                _lived_in(tmp_path / status, tools=[{"id": "t1"}])) is None, status

    def test_a_failed_first_task_is_never_called_finished(self, tmp_path, monkeypatch):
        for status in ("failed", "extraction_failed", "cancelled", "partial"):
            _install_rows(monkeypatch, _row(status))
            assert derive_proposal(
                _lived_in(tmp_path / status, tools=[{"id": "t1"}])) is None, status

    def test_the_second_finished_task_is_past_the_first_run(self, tmp_path, monkeypatch):
        _install_rows(monkeypatch, _row("completed", "wf_1"), _row("success", "wf_2"))
        assert derive_proposal(_lived_in(tmp_path, tools=[{"id": "t1"}])) is None

    def test_one_finished_among_several_in_flight_still_counts(self, tmp_path, monkeypatch):
        """"Your first task finished" is a claim about FINISHED runs; a second
        run still executing has not finished."""
        _install_rows(monkeypatch, _row("completed", "wf_1"), _row("running", "wf_2"))
        assert derive_proposal(
            _lived_in(tmp_path, tools=[{"id": "t1"}]))[0] == "first_run_replay"

    def test_a_declined_replay_never_returns(self, tmp_path, monkeypatch):
        _install_rows(monkeypatch, _row("completed"))
        v = _lived_in(tmp_path, tools=[{"id": "t1"}])
        assert derive_proposal(v)[0] == "first_run_replay"
        decline(v, "first_run_replay")
        assert derive_proposal(v) is None

    def test_a_broken_loader_costs_the_nudge_not_the_page(self, tmp_path, monkeypatch):
        from systemu.interface.pages import work

        def _boom():
            raise RuntimeError("tracker down")

        monkeypatch.setattr(work, "_load_rows", _boom)
        assert derive_proposal(_lived_in(tmp_path, tools=[{"id": "t1"}])) is None

        monkeypatch.setattr(work, "_load_rows", lambda: [None, {"status": None}])
        assert derive_proposal(_lived_in(tmp_path / "b", tools=[{"id": "t1"}])) is None

    def test_it_reads_the_same_loader_the_work_page_renders_from(self, tmp_path, monkeypatch):
        """Reachability pin: patching work._load_rows must change the answer - a
        second private notion of "has a task finished" would ignore this."""
        from systemu.interface.pages import work
        v = _lived_in(tmp_path, tools=[{"id": "t1"}])
        monkeypatch.setattr(work, "_load_rows", lambda: [])
        assert derive_proposal(v) is None
        monkeypatch.setattr(work, "_load_rows", lambda: [_row("completed")])
        assert derive_proposal(v)[0] == "first_run_replay"


class TestReplayPrecedence:
    def test_the_full_precedence_order_end_to_end(self, tmp_path, monkeypatch):
        """ONE test, ONE vault root, the whole ladder: first_shadow ->
        first_forge -> wish -> first_run_replay -> nothing.  Every rung is
        satisfiable at the moment it is asserted, so this pins ORDER rather than
        mere availability."""
        from systemu.interface.wishes import add_wish

        _install_rows(monkeypatch, _row("completed"))   # replay live throughout
        root = tmp_path / "ladder"
        fid = add_wish(_Vault(root), _INVOICE_WISH)

        cold = _Vault(root)                             # no shadows, no tools
        assert derive_proposal(cold)[0] == "first_shadow"
        decline(cold, "first_shadow")

        forging = _Vault(root, shadows=[{"id": "s1"}])  # shadows, still no tools
        assert derive_proposal(forging)[0] == "first_forge"
        decline(forging, "first_forge")

        lived = _Vault(root, shadows=[{"id": "s1"}], tools=[_INVOICE_TOOL])
        assert derive_proposal(lived)[0] == "wish:%s" % fid
        decline(lived, "wish:%s" % fid)

        assert derive_proposal(lived)[0] == "first_run_replay"
        decline(lived, "first_run_replay")
        assert derive_proposal(lived) is None

    def test_a_cold_install_is_told_to_record_before_it_is_told_to_replay(
        self, tmp_path, monkeypatch
    ):
        _install_rows(monkeypatch, _row("completed"))
        assert derive_proposal(_Vault(tmp_path))[0] == "first_shadow"


class TestReplayPurity:
    def test_the_replay_route_is_a_page_the_dashboard_registers(self, tmp_path, monkeypatch):
        from systemu.interface import dashboard
        _install_rows(monkeypatch, _row("completed"))
        route = derive_proposal(_lived_in(tmp_path, tools=[{"id": "t1"}]))[2]
        assert '@ui.page("%s")' % route in inspect.getsource(dashboard)

    def test_deriving_the_replay_writes_nothing(self, tmp_path, monkeypatch):
        _install_rows(monkeypatch, _row("completed"))
        v = _lived_in(tmp_path, tools=[{"id": "t1"}])
        before = sorted(p.name for p in Path(tmp_path).iterdir())
        derive_proposal(v)
        derive_proposal(v)
        assert sorted(p.name for p in Path(tmp_path).iterdir()) == before

    def test_the_replay_adds_no_rendering_machinery(self):
        """2b is a proposal, not a page: the card that already exists renders
        it, and knows nothing about this key."""
        src = inspect.getsource(console._build_proposal_card)
        assert "first_run_replay" not in src
        assert "derive_proposal(" in src

    def test_the_engine_is_still_not_a_writer(self):
        """The property is 'derivation never writes a store', so the pin is on
        IMPORTS and CALLS, not on raw source bytes.  The original substring scan
        over ``getsource`` reddened on the world-scan rules' pure DATA (a tool
        NAMED ``write_csv_file`` in a ``requires=`` honesty-wall field) while a
        writer smuggled through ``getattr`` would have passed it -- checking
        cheaper than the meaning, in both directions.  The behavioural half
        (``test_derivation_writes_nothing_to_the_vault`` above) stays the
        primary witness; this is the structural companion."""
        import ast as _ast
        tree = _ast.parse(inspect.getsource(proposals))
        imported = []
        called = []
        for node in _ast.walk(tree):
            if type(node) is _ast.Import:
                imported += [a.name for a in node.names]
            elif type(node) is _ast.ImportFrom:
                imported.append(node.module or "")
                imported += [a.name for a in node.names]
            elif type(node) is _ast.Call:
                f = node.func
                if type(f) is _ast.Name:
                    called.append(f.id)
                elif type(f) is _ast.Attribute:
                    called.append(f.attr)
        for banned_import in ("table_store", "table_reconciler"):
            assert not any(banned_import in name for name in imported), (
                f"proposals.py imports {banned_import!r} - the engine must "
                f"never reach a table writer")
        assert "TableItem" not in imported and "TableItem" not in called
        for name in called:
            assert not name.startswith(("save_", "write_")), (
                f"proposals.py CALLS {name!r} - derivation must never write")
