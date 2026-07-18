"""R-W1 (W-A slice-1) — the World Model v2 fact substrate (§5.11.a/.b).

Pins the store/schema HALF of AC1/AC2/AC4 (the binder/discovery/planner halves are
slice-2), the E1 origin_class update-path immutability soundness fix, WM-2 negative
TTL, WM-4 never-subtract + the query views, durability, and the risk-5 "absent/empty
⇒ identical" invariant (no run-loop module imports the store yet).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from systemu.runtime import world_model as wm
from systemu.runtime.world_model import (
    Fact, FactStore, ImmutableProvenanceError, NegativeFact, ProvStep,
)


def _store(tmp_path):
    return FactStore(SimpleNamespace(root=tmp_path))


def _fact(fact_id="service:abc", kind="service", value="github",
          origin_class="operator", confidence=0.5, steps=None):
    return Fact(fact_id=fact_id, kind=kind, value=value, origin_class=origin_class,
                confidence=confidence,
                source_chain=steps or [ProvStep(source_kind="inventory", ref="mcp:gh")])


# ── WM-1 schema + the taint advisory (AC1, store/schema half) ─────────────────

def test_fact_exposes_the_four_provenance_fields():
    f = _fact()
    assert f.origin_class == "operator"
    assert hasattr(f, "confidence") and hasattr(f, "last_confirmed")
    assert isinstance(f.source_chain, list) and f.source_chain[0].source_kind == "inventory"


def test_taint_permits_silent_bind_is_a_whitelist_fail_closed():
    # F1: taint advisory is a WHITELIST — only the two taint-permitted classes are True;
    # content_derived AND anything unrecognized read as NOT silent-bind-permitted.
    assert _fact(origin_class="operator").taint_permits_silent_bind is True
    assert _fact(origin_class="systemu_authored").taint_permits_silent_bind is True
    assert _fact(origin_class="content_derived").taint_permits_silent_bind is False


def test_fact_rejects_an_out_of_vocab_origin_class():
    # F1: origin_class is a CLOSED taint axis (unlike the open `kind`). A typo'd/unknown
    # origin must be refused at construction — fail-closed, not silently taint-permitted.
    from pydantic import ValidationError
    for bad in ("content-derived", "Content_Derived", "content_derived ", "", "trusted"):
        with pytest.raises(ValidationError):
            Fact(fact_id="x", kind="service", value="v", origin_class=bad)


# ── E1: origin_class is IMMUTABLE across the UPDATE path (the soundness fix) ───

def test_put_fact_rejects_an_origin_class_change_on_update(tmp_path):
    s = _store(tmp_path)
    s.put_fact(_fact(origin_class="content_derived"))
    with pytest.raises(ImmutableProvenanceError):
        s.put_fact(_fact(origin_class="operator"))          # taint-launder attempt → refused
    assert s.get("service:abc").origin_class == "content_derived"   # unchanged


def test_put_fact_updates_confidence_and_appends_source_chain(tmp_path):
    s = _store(tmp_path)
    s.put_fact(_fact(confidence=0.2,
                     steps=[ProvStep(source_kind="inventory", ref="mcp:gh", at="2026-01-01T00:00:00+00:00")]))
    s.put_fact(_fact(confidence=0.9,
                     steps=[ProvStep(source_kind="probe", ref="whoami", at="2026-02-02T00:00:00+00:00")]))
    got = s.get("service:abc")
    assert got.confidence == 0.9                            # confidence updates
    kinds = [st.source_kind for st in got.source_chain]
    assert kinds == ["inventory", "probe"]                  # append-only: old step preserved


def test_reobserving_the_same_source_does_not_grow_the_chain(tmp_path):
    # F4 fix (now load-bearing with a per-run populator): re-confirming a fact from the
    # SAME (source_kind, ref) — even at a different time — must NOT append a duplicate
    # step. Recency is carried by last_confirmed, not by chain growth.
    s = _store(tmp_path)
    for at in ("2026-01-01T00:00:00+00:00", "2026-02-01T00:00:00+00:00", "2026-03-01T00:00:00+00:00"):
        s.put_fact(Fact(fact_id="service:gh", kind="service", value="github", origin_class="operator",
                        source_chain=[ProvStep(source_kind="inventory", ref="mcp:gh", at=at)]))
    assert len(s.get("service:gh").source_chain) == 1          # deduped, not grown
    # a DISTINCT source is still appended (append-only for genuinely new provenance)
    s.put_fact(Fact(fact_id="service:gh", kind="service", value="github", origin_class="operator",
                    source_chain=[ProvStep(source_kind="probe", ref="whoami")]))
    assert len(s.get("service:gh").source_chain) == 2


def test_put_facts_bulk_writes_once_and_applies_the_same_rules(tmp_path):
    # F1: the per-run populator must not rewrite the whole file once per fact (O(N²)).
    # put_facts does ONE save for the batch, with identical immutability/dedup rules.
    s = _store(tmp_path)
    saves = {"n": 0}
    orig = s._save_facts
    def _counting(f):
        saves["n"] += 1
        return orig(f)
    s._save_facts = _counting
    n = s.put_facts([_fact(fact_id=f"service:{i}", value=f"svc{i}") for i in range(5)])
    assert n == 5 and saves["n"] == 1                      # ONE save for the whole batch
    assert len(s.query_facts()) == 5
    # a batch member violating immutability is SKIPPED, not aborting the rest
    s._save_facts = orig
    stored = s.put_facts([
        _fact(fact_id="service:0", value="svc0", origin_class="content_derived"),  # taint change
        _fact(fact_id="service:new", value="fresh"),
    ])
    assert stored == 1                                      # only the valid one landed
    assert s.get("service:0").origin_class == "operator"     # unchanged (refused)
    assert s.get("service:new") is not None


def test_put_facts_empty_batch_writes_nothing(tmp_path):
    s = _store(tmp_path)
    assert s.put_facts([]) == 0
    assert not s._facts_path.exists()                       # no file created for a no-op


def test_put_fact_rejects_a_kind_change_on_update(tmp_path):
    # F3: kind is identity-defining (fact_id_for folds it in). A hand-set fact_id
    # re-typed to a different kind is a caller error — refused, not silently re-typed.
    s = _store(tmp_path)
    s.put_fact(Fact(fact_id="x:1", kind="service", value="v", origin_class="operator"))
    with pytest.raises(ImmutableProvenanceError):
        s.put_fact(Fact(fact_id="x:1", kind="capability", value="v", origin_class="operator"))
    assert s.get("x:1").kind == "service"                   # unchanged


# ── WM-2 negative knowledge + TTL (AC2, store/schema half) ────────────────────

def test_negative_fact_within_ttl_is_returned_and_cites_what_and_when(tmp_path):
    s = _store(tmp_path)
    s.put_negative(NegativeFact(scope="find:burrito@chennai",
                                probes=["justdial", "duckduckgo", "zomato"],
                                recorded_at="2026-07-18T00:00:00+00:00", ttl_seconds=3600))
    hit = s.query_negative("find:burrito@chennai", now="2026-07-18T00:30:00+00:00")
    assert hit is not None and hit.probes == ["justdial", "duckduckgo", "zomato"]   # cites WHAT
    assert hit.recorded_at == "2026-07-18T00:00:00+00:00"                            # cites WHEN


def test_negative_fact_past_ttl_returns_none_so_the_caller_researches(tmp_path):
    s = _store(tmp_path)
    s.put_negative(NegativeFact(scope="find:x", recorded_at="2026-07-18T00:00:00+00:00",
                                ttl_seconds=3600))
    assert s.query_negative("find:x", now="2026-07-18T02:00:00+00:00") is None      # expired


def test_absence_expires_faster_than_presence_by_default():
    # WM-2: absence expires faster than presence. Slice-1 has no positive decay, so the
    # invariant is a SHORT absolute default TTL for negatives.
    assert wm.DEFAULT_NEGATIVE_TTL_SECONDS <= 24 * 60 * 60
    assert NegativeFact(scope="s").ttl_seconds == wm.DEFAULT_NEGATIVE_TTL_SECONDS


def test_corrupt_negative_timestamp_fails_open_to_expired():
    assert NegativeFact(scope="s", recorded_at="not-a-timestamp").is_expired() is True


# ── WM-4 view family (AC4, store half) ────────────────────────────────────────

def test_what_can_matches_the_canonical_slot(tmp_path):
    s = _store(tmp_path)
    s.put_fact(Fact(fact_id="capability:mk", kind="capability", value="create_issue",
                    origin_class="systemu_authored", confidence=0.7))
    s.put_fact(Fact(fact_id="capability:snd", kind="capability", value="send_email",
                    origin_class="systemu_authored", confidence=0.7))
    hits = wm.what_can(s, "make", "issues")                 # 'make'→create synonym, 'issues'→issue plural
    assert [f.value for f in hits] == ["create_issue"]


def test_find_services_and_find_data_and_about(tmp_path):
    s = _store(tmp_path)
    s.put_fact(Fact(fact_id="service:gh", kind="service", value="github",
                    origin_class="operator", confidence=0.9))
    s.put_fact(Fact(fact_id="data:inv", kind="data_location", value="C:/Users/me/Invoices",
                    origin_class="content_derived", confidence=0.4))
    assert [f.value for f in wm.find_services(s, "github")] == ["github"]
    assert [f.value for f in wm.find_data(s, "invoices", under="C:/Users/me")] == ["C:/Users/me/Invoices"]
    assert wm.find_data(s, "invoices", under="D:/other") == []
    assert any(f.fact_id == "service:gh" for f in wm.about(s, "github"))


def test_provenance_returns_the_source_chain(tmp_path):
    s = _store(tmp_path)
    s.put_fact(_fact())
    assert [st.source_kind for st in wm.provenance(s, "service:abc")] == ["inventory"]
    assert wm.provenance(s, "nope") is None


# ── E3 never-subtract: a fact outside a limited view stays reachable ──────────

def test_never_subtract_a_trimmed_fact_is_still_reachable(tmp_path):
    s = _store(tmp_path)
    for i in range(5):
        s.put_fact(Fact(fact_id=f"service:{i}", kind="service", value=f"svc{i}",
                        origin_class="operator", confidence=0.1 * i))
    limited = wm.find_services(s, "svc0", limit=1)          # a ranked, trimmed VIEW
    assert len(limited) == 1
    # the store itself never hides a row — the broadened query returns every service
    assert len(s.query_facts(kind="service")) == 5
    assert any(f.fact_id == "service:4" for f in s.query_facts())   # reachable via the raw store
    assert any(f.fact_id == "service:4" for f in wm.about(s, "svc4"))  # …and via the escape hatch


# ── durability + defensive reads ──────────────────────────────────────────────

def test_facts_round_trip_across_store_instances(tmp_path):
    _store(tmp_path).put_fact(_fact())
    assert _store(tmp_path).get("service:abc").value == "github"     # a fresh instance reads it


def test_broken_store_file_reads_as_empty_not_crash(tmp_path):
    s = _store(tmp_path)
    s.put_fact(_fact())
    s._facts_path.write_text("{ this is not json", encoding="utf-8")
    assert s.all_facts() == []                              # defensive: never crash


def test_fact_id_for_is_deterministic_and_dedups():
    a = wm.fact_id_for("service", {"name": "gh", "host": "api.github.com"})
    b = wm.fact_id_for("service", {"host": "api.github.com", "name": "gh"})
    assert a == b and a.startswith("service:")              # key-order independent → dedups


# ── risk-5: the substrate is inert — no run-loop module imports it (slice-1) ──

#: The ONLY modules — anywhere in the package — allowed to reference the world model,
#: and the role each is allowed to play. Scanning the WHOLE tree (not just runtime/)
#: matters: a runtime-only scan silently rests on "every decision path lives under
#: systemu/runtime/", which is true today but unpinned.
_WM_ALLOWED = {
    # runtime — the decision zone. Write-side only.
    "world_model.py": "defines the store + the read API",
    "world_model_populator.py": "the WRITE-ONLY projector",
    "shadow_runtime.py": "hosts the populator call (write seam)",
    # outside runtime — operator-facing, never on a decision path.
    "cli_commands.py": "read-only operator CLI surface",
}

#: The read surface. Checked only against the small write-only projector — a read API
#: includes generic names (``get``) that cannot be grepped across a large module without
#: false positives, which is exactly why the invariant below gates on MODULE REFERENCE
#: rather than on a symbol blocklist.
_WM_READ_SURFACE = ("find_services", "what_can", "find_data", "about(", "provenance(",
                    "query_facts", "query_negative", "all_facts", "all_negatives")


def test_only_the_allowed_modules_reference_the_world_model_anywhere():
    # The load-bearing invariant: the store is WRITE-ONLY on every decision path today —
    # reading it to influence a bind is the trust-critical, separately-gated slice-2c.
    # Gate on REFERENCE, not on a symbol blocklist: a reader added anywhere trips this no
    # matter which call it uses (including `about()`/`get()`, which a substring list would
    # miss or false-positive on). Scan the WHOLE package, so the invariant does not quietly
    # depend on where decision code happens to live.
    import pathlib
    systemu_root = pathlib.Path(wm.__file__).parent.parent          # systemu/
    touching = {p.name for p in systemu_root.rglob("*.py")
                if "world_model" in p.read_text(encoding="utf-8", errors="replace")}
    unexpected = touching - set(_WM_ALLOWED)
    assert unexpected == set(), f"these modules must not touch the world model: {unexpected}"


def test_the_write_only_projector_never_reads_the_store():
    import pathlib
    text = (pathlib.Path(wm.__file__).parent / "world_model_populator.py").read_text(
        encoding="utf-8", errors="replace")
    hits = [s for s in _WM_READ_SURFACE if s in text]
    assert hits == [], f"the populator is write-only; it must not read: {hits}"


def test_the_write_host_reaches_the_store_only_through_the_populator():
    # shadow_runtime drives the planner, so it is the likeliest place a future
    # read-for-decision would land. Pin that it never opens the store itself.
    import pathlib
    text = (pathlib.Path(wm.__file__).parent / "shadow_runtime.py").read_text(
        encoding="utf-8", errors="replace")
    assert "world_model_populator" in text          # the write seam is present…
    assert "FactStore(" not in text                 # …and it never opens the store itself
    assert "from systemu.runtime.world_model import" not in text
    assert "world_model.FactStore" not in text


# ── E5: the `sharing-on world` CLI is READ-ONLY ───────────────────────────────

def test_world_cli_on_empty_store_is_read_only(tmp_path, capsys):
    from systemu.interface.cli_commands import run_world
    rc = run_world(SimpleNamespace(root=tmp_path))
    assert rc == 0 and "empty" in capsys.readouterr().out.lower()
    # a read must never persist — the store files are not created by viewing
    assert not (tmp_path / "world_model" / "facts.json").exists()


def test_world_cli_summarises_and_queries(tmp_path, capsys):
    from systemu.interface.cli_commands import run_world
    s = _store(tmp_path)
    s.put_fact(Fact(fact_id="service:gh", kind="service", value="github",
                    origin_class="operator", confidence=0.9))
    assert run_world(SimpleNamespace(root=tmp_path)) == 0
    assert "service" in capsys.readouterr().out
    assert run_world(SimpleNamespace(root=tmp_path), "github") == 0
    assert "github" in capsys.readouterr().out
