"""R-P1 resolution path — ``resolve_from_channel`` (the server-side gate a phone,
or a forged callback, hits).

SEC-1 (the load-bearing rule): refuse anything whose PERSISTED
``context["resolution_class"] != "remotely_resolvable"``, DEFAULT-REFUSE on
absence. The refusal rests on the frozen persisted bit, never on the outbound
``surface_hint`` (which a forged callback could fake).

"No second rail": on the happy path ``resolve_from_channel`` calls the SAME
``OperatorDecisionQueue.resolve(decision_id, choice=...)`` the dashboard/CLI/inbox
call. It does NOT re-implement resume — the existing reconciler
(``scheduler/jobs.reconcile_resolved_stuck_decisions``) + the EventBus subscriber
dispatch the resume off the persisted resolved decision for free.

STEP-0 findings baked in:
  * Dashboard-resolve fn: ``OperatorDecisionQueue.resolve(decision_id, *, choice)``
    — keyword-only ``choice``; the choice value is one of ``decision.options``
    (inbox_rail resolves with ``options[-1]``/``options[0]``; cli_commands with
    ``--choice`` = an option label). So ``a1..a4`` map POSITIONALLY to
    ``options[0..3]`` re-derived from the record.
  * Open enumeration: ``queue.list_pending() -> List[OperatorDecision]``.
  * Allowlist: ``allowlist_from_env("SHARING_ON_TELEGRAM_ALLOWED_USER_IDS")``.
"""
import types

import pytest

from systemu.messaging import decision_bridge as db
from systemu.messaging.decision_bridge import resolve_from_channel


# ── test doubles ──────────────────────────────────────────────────────────────

class _Dec:
    """Minimal stand-in for OperatorDecision."""
    def __init__(self, *, id, options, context, status="pending"):
        self.id = id
        self.options = list(options)
        self.context = dict(context)
        self.status = status
        self.choice = None


class _FakeVault:
    """Minimal vault shim so the bridge can enumerate ALL rows (any status) —
    mirrors the production ``vault.load_index("decisions")`` + ``get_decision``
    seam, which is how an already-resolved decision stays findable for the
    idempotent EXPIRED double-tap."""
    def __init__(self, rows):
        self._rows = rows

    def load_index(self, name):
        assert name == "decisions"
        return [{"id": d.id, "status": d.status} for d in self._rows]

    def get_decision(self, did):
        for d in self._rows:
            if d.id == did:
                return d
        raise KeyError(did)


class _FakeQueue:
    """A stub OperatorDecisionQueue with a resolve() spy."""
    def __init__(self, rows):
        self._rows = list(rows)
        self._vault = _FakeVault(self._rows)   # enables full-index enumeration
        self.resolve_calls = []                # [(decision_id, choice), ...]

    def list_pending(self):
        # Mirror the real queue: only status=="pending" rows.
        return [d for d in self._rows if d.status == "pending"]

    def resolve(self, decision_id, *, choice):
        self.resolve_calls.append((decision_id, choice))
        for d in self._rows:
            if d.id == decision_id:
                d.status = "resolved"
                d.choice = choice
                return d
        raise KeyError(decision_id)


def _remote_gate(id="dec_remote1", options=("Deny", "Approve")):
    return _Dec(id=id, options=options,
                context={"kind": "gate", "gate_type": "tool",
                         "resolution_class": "remotely_resolvable"})


def _tag_for(dec):
    """The 6-char tag the wire would carry for this decision id."""
    return db.decision_tag(dec.id)


@pytest.fixture(autouse=True)
def _allowlist(monkeypatch):
    monkeypatch.setenv("SHARING_ON_TELEGRAM_ALLOWED_USER_IDS", "111,222")
    # The per-sender rate-limit window is module-level state; reset it per test
    # so hits from one test never leak into another (a real limiter keyed on a
    # long-lived process is correct; the test isolation is what we're after).
    db._rate_hits.clear()
    yield
    db._rate_hits.clear()


def _call(tag, choice, queue, *, sender_id="111", **kw):
    return resolve_from_channel(
        tag, choice, sender_id=sender_id, channel="telegram",
        queue=queue, **kw)


# ── happy path: SAME dashboard resolve fn, no second rail ──────────────────────

def test_resolve_ok_calls_dashboard_resolve():
    d = _remote_gate(options=("Deny", "Approve"))
    q = _FakeQueue([d])
    out, _msg = _call(_tag_for(d), "a2", q)
    assert out == "OK"
    # The SAME dashboard resolve fn was invoked with (decision_id, mapped_choice).
    assert q.resolve_calls == [(d.id, "Approve")]   # a2 -> options[1]
    assert d.status == "resolved" and d.choice == "Approve"


# ── SEC-1: the persisted-bit floor ────────────────────────────────────────────

def test_floor_decision_refused():
    d = _Dec(id="dec_floor1", options=("Deny", "Approve"),
             context={"kind": "gate", "gate_type": "evolution",
                      "resolution_class": "floor"})
    q = _FakeQueue([d])
    out, _ = _call(_tag_for(d), "a2", q)
    assert out == "REFUSED_TYPED_CONFIRM"
    assert q.resolve_calls == []


def test_missing_resolution_class_refused():
    # SEC-1 default-refuse-on-absence: no resolution_class key at all.
    d = _Dec(id="dec_norc", options=("Deny", "Approve"),
             context={"kind": "gate", "gate_type": "tool"})
    q = _FakeQueue([d])
    out, _ = _call(_tag_for(d), "a2", q)
    assert out == "REFUSED_TYPED_CONFIRM"
    assert q.resolve_calls == []


def test_forged_callback_for_never_pushed_floor_refused():
    # A floor decision that was never pushed with buttons. A forged callback
    # carries a plausible surface_hint claiming it's remote — but the refusal
    # must rest on the PERSISTED bit, not the wire hint.
    d = _Dec(id="dec_forge_floor", options=("Deny", "Approve"),
             context={"kind": "gate", "gate_type": "mcp_oauth",
                      "resolution_class": "floor",
                      "surface_hint": "remotely_resolvable"})  # attacker-controlled
    q = _FakeQueue([d])
    out, _ = _call(_tag_for(d), "a2", q)
    assert out == "REFUSED_TYPED_CONFIRM"
    assert q.resolve_calls == []


# ── tag / status / choice errors ──────────────────────────────────────────────

def test_unknown_tag():
    q = _FakeQueue([_remote_gate()])
    out, _ = _call("zzzzzz", "a1", q)
    assert out == "UNKNOWN_TAG"
    assert q.resolve_calls == []


def test_expired_or_resolved():
    d = _remote_gate()
    d.status = "resolved"   # already resolved → not in list_pending
    q = _FakeQueue([d])
    out, _ = _call(_tag_for(d), "a2", q)
    assert out == "EXPIRED"
    assert q.resolve_calls == []


def test_bad_choice_index():
    d = _remote_gate(options=("Deny", "Approve"))
    q = _FakeQueue([d])
    # a5 is not a recognized choice key at all → unmappable.
    out5, _ = _call(_tag_for(d), "a5", q)
    assert out5 == "BAD_CHOICE"
    # a3 is a valid key but out of range for a 2-option decision.
    out3, _ = _call(_tag_for(d), "a3", q)
    assert out3 == "BAD_CHOICE"
    assert q.resolve_calls == []


# ── allowlist defense-in-depth ────────────────────────────────────────────────

def test_non_allowlisted_sender_refused():
    d = _remote_gate()
    q = _FakeQueue([d])
    out, _ = _call(_tag_for(d), "a2", q, sender_id="999")   # not on allowlist
    assert out == "UNKNOWN_TAG"     # silent — do not leak decision existence
    assert q.resolve_calls == []


# ── rate limit ────────────────────────────────────────────────────────────────

def test_rate_limited_over_20_per_min():
    # Feed a fixed clock; 20 resolves are allowed inside a minute, the 21st is
    # rate-limited. Each resolve targets a distinct pending decision so nothing
    # else short-circuits before the limiter.
    t0 = 1_000.0
    now_box = {"t": t0}
    decs = [_remote_gate(id=f"dec_rl{i}") for i in range(25)]
    q = _FakeQueue(decs)
    outcomes = []
    for i in range(21):
        now_box["t"] = t0 + i * 0.1    # all within the same minute window
        out, _ = _call(_tag_for(decs[i]), "a1", q,
                       sender_id="111", now=lambda nb=now_box: nb["t"])
        outcomes.append(out)
    assert outcomes[:20] == ["OK"] * 20
    assert outcomes[20] == "RATE_LIMITED"


# ── choice index re-derived from the RECORD, not the wire ─────────────────────

def test_choice_index_re_derived_from_record_not_wire():
    # a1 maps to options[0] taken FROM THE RECORD. The wire only carries the
    # index "a1"; the label is never trusted from the wire.
    d = _remote_gate(options=("Skip", "Run it"))
    q = _FakeQueue([d])
    out, _ = _call(_tag_for(d), "a1", q)
    assert out == "OK"
    assert q.resolve_calls == [(d.id, "Skip")]   # options[0] from the record


# ── Finding 6: tag collision — the resolver MUST disambiguate the SAME way the
#    push side does, and NEVER resolve the wrong decision ─────────────────────
#
# ``dec_4d54`` and ``dec_6762`` share the 6-char tag ``tbhp6n`` (brute-forced).
# When BOTH are pending, the push side extends each to its distinct 8-char form
# (``disambiguate_tag`` over the OTHER pending id) — ``tbhp6noo`` / ``tbhp6nwj``.
# A tap therefore always carries the 8-char form; the resolver must match that
# form to the RIGHT decision, and refuse an ambiguous bare-6 (which could never
# come from a legit push in a collision) rather than resolve whichever the index
# happens to yield first.

_COLLIDE_A = "dec_4d54"   # decision_tag -> tbhp6n, disambiguated -> tbhp6noo
_COLLIDE_B = "dec_6762"   # decision_tag -> tbhp6n, disambiguated -> tbhp6nwj


def test_colliding_tags_resolve_correct_decision_not_the_wrong_one():
    da = _Dec(id=_COLLIDE_A, options=("Deny", "Approve"),
              context={"kind": "gate", "gate_type": "tool",
                       "resolution_class": "remotely_resolvable"})
    dbc = _Dec(id=_COLLIDE_B, options=("Deny", "Approve"),
               context={"kind": "gate", "gate_type": "tool",
                        "resolution_class": "remotely_resolvable"})
    q = _FakeQueue([da, dbc])
    # sanity: they really do share a bare-6 tag.
    assert db.decision_tag(_COLLIDE_A) == db.decision_tag(_COLLIDE_B)

    # The 8-char disambiguated tag for A resolves A (never B).
    tag_a = db.disambiguate_tag(_COLLIDE_A, {db.decision_tag(_COLLIDE_B)})
    out, _ = _call(tag_a, "a2", q)
    assert out == "OK"
    assert q.resolve_calls == [(_COLLIDE_A, "Approve")]   # NOT dec_6762

    # And the 8-char tag for B resolves B.
    q2 = _FakeQueue([_Dec(id=_COLLIDE_A, options=("Deny", "Approve"),
                          context={"kind": "gate", "gate_type": "tool",
                                   "resolution_class": "remotely_resolvable"}),
                     _Dec(id=_COLLIDE_B, options=("Deny", "Approve"),
                          context={"kind": "gate", "gate_type": "tool",
                                   "resolution_class": "remotely_resolvable"})])
    tag_b = db.disambiguate_tag(_COLLIDE_B, {db.decision_tag(_COLLIDE_A)})
    out2, _ = _call(tag_b, "a1", q2)
    assert out2 == "OK"
    assert q2.resolve_calls == [(_COLLIDE_B, "Deny")]      # NOT dec_4d54


def test_ambiguous_bare6_tag_refuses_never_resolves_wrong():
    # An incoming BARE-6 tag that maps to >1 pending decision is ambiguous (a
    # legit push would have sent the 8-char form). Refuse — never pick one.
    da = _Dec(id=_COLLIDE_A, options=("Deny", "Approve"),
              context={"kind": "gate", "gate_type": "tool",
                       "resolution_class": "remotely_resolvable"})
    dbc = _Dec(id=_COLLIDE_B, options=("Deny", "Approve"),
               context={"kind": "gate", "gate_type": "tool",
                        "resolution_class": "remotely_resolvable"})
    q = _FakeQueue([da, dbc])
    bare6 = db.decision_tag(_COLLIDE_A)   # == decision_tag(_COLLIDE_B)
    out, _ = _call(bare6, "a2", q)
    assert out == "UNKNOWN_TAG"
    assert q.resolve_calls == []          # never resolved the wrong (or any) one
