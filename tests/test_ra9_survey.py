"""R-A9 Task 7: survey_situation orchestration (§5.1) — the tie-together.

survey_situation(scroll, *, vault, cache=None) runs the 5 source builders in
PARALLEL off the event loop (each via asyncio.to_thread, so a blocking builder
never stalls the loop), each under a per-source TIMEOUT, composes the OnTheTable
view on a FRESH report, computes per-slice freshness STAMPS, and — given a prior
cached (report, stamps) — reuses unchanged slices and re-surveys only changed
ones (AC3). A slow/failing source degrades to its cached-or-empty slice; the
survey never blocks or crashes.

cache shape (contract for Tasks 8/9):
    cache = {"report": SituationReport | dict, "stamps": dict}
where ``stamps`` is the ``situation_stamps`` dict a prior survey returned.
"""
from __future__ import annotations

import asyncio
import os
import time

import pytest

from systemu.runtime import situational_inventory as si
from systemu.runtime.situational_inventory import (
    SituationReport,
    survey_situation,
)


# --------------------------------------------------------------------------- #
# Fixtures — a REAL vault seeded with a connected server + a granted root.
# --------------------------------------------------------------------------- #
def _make_vault(tmp_path):
    from systemu.vault.vault import Vault
    for sub in ["scrolls", "activities", "shadow_army", "skills",
                "tools/implementations", "evolutions", "notifications",
                "executions", "decisions"]:
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    for idx in ["scrolls", "activities", "shadow_army", "skills", "tools",
                "evolutions", "decisions"]:
        (tmp_path / idx / "index.json").write_text("[]", encoding="utf-8")
    return Vault(str(tmp_path))


def _grant_root_with_pdf(vault, tmp_path):
    """Grant a root under the vault base_dir that GrantedRootsStore uses, and drop
    a salient PDF in it. Returns (store, root_path)."""
    from systemu.runtime.granted_roots import GrantedRootsStore
    root = tmp_path / "Docs"
    root.mkdir(parents=True, exist_ok=True)
    (root / "report.pdf").write_text("%PDF-1.4 ...")
    store = GrantedRootsStore(base_dir=vault.root)
    store.grant(str(root))
    return store, root


# --------------------------------------------------------------------------- #
# AC1 — cold survey: both the service AND the root-with-salient are present.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_cold_survey_has_service_and_root(tmp_path):
    from systemu.runtime.mcp import connections
    vault = _make_vault(tmp_path)
    url = "https://mcp.example.com/a"
    connections.add_server(vault, url)
    _store, root = _grant_root_with_pdf(vault, tmp_path)

    report, stamps = await survey_situation(None, vault=vault)

    assert isinstance(report, SituationReport)
    assert isinstance(stamps, dict)  # new contract: (report, stamps) tuple
    assert report.schema_version == 1
    assert report.surveyed_at  # a timestamp was stamped

    # the connected service is present
    names = {s.name for s in report.services}
    assert url in names

    # the granted root is present WITH its salient PDF surfaced
    root_paths = {r.path for r in report.roots}
    assert any(os.path.normcase(str(root)) == os.path.normcase(p)
               for p in root_paths)
    surv = next(r for r in report.roots
                if os.path.normcase(str(root)) == os.path.normcase(r.path))
    assert any(fh.name == "report.pdf" for fh in surv.salient)


# --------------------------------------------------------------------------- #
# AC3 — per-slice cache invalidation.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_ac3_unchanged_slices_not_rebuilt(tmp_path, monkeypatch):
    from systemu.runtime.mcp import connections
    vault = _make_vault(tmp_path)
    connections.add_server(vault, "https://mcp.example.com/a")
    _store, root = _grant_root_with_pdf(vault, tmp_path)

    # 1st survey populates the cache.
    report1, stamps1 = await survey_situation(None, vault=vault)
    assert stamps1 is not None, "survey must expose per-slice stamps for the cache"

    # Count each builder's invocations on the 2nd survey.
    calls = {"services": 0, "capabilities": 0, "profile": 0,
             "credentials": 0, "roots": 0}
    orig = {name: getattr(si, f"build_{name}") for name in calls}

    def _spy(name):
        def wrapper(*a, **k):
            calls[name] += 1
            return orig[name](*a, **k)
        return wrapper

    for name in calls:
        monkeypatch.setattr(si, f"build_{name}", _spy(name))

    # 2nd survey with UNCHANGED sources → no builder re-invoked.
    cache = {"report": report1, "stamps": stamps1}
    report2, _ = await survey_situation(None, vault=vault, cache=cache)
    assert calls == {"services": 0, "capabilities": 0, "profile": 0,
                     "credentials": 0, "roots": 0}, (
        f"unchanged slices must NOT rebuild; got {calls}")

    # the reused report still carries the service + root.
    assert {s.name for s in report2.services} == {s.name for s in report1.services}
    assert {r.path for r in report2.roots} == {r.path for r in report1.roots}


@pytest.mark.asyncio
async def test_ac3_changing_a_root_reruns_only_roots(tmp_path, monkeypatch):
    from systemu.runtime.mcp import connections
    vault = _make_vault(tmp_path)
    connections.add_server(vault, "https://mcp.example.com/a")
    _store, root = _grant_root_with_pdf(vault, tmp_path)

    report1, stamps1 = await survey_situation(None, vault=vault)

    # CHANGE the root's contents → its root_freshness_stamp changes.
    (root / "newly_added.txt").write_text("x")

    calls = {"services": 0, "capabilities": 0, "profile": 0,
             "credentials": 0, "roots": 0}
    orig = {name: getattr(si, f"build_{name}") for name in calls}

    def _spy(name):
        def wrapper(*a, **k):
            calls[name] += 1
            return orig[name](*a, **k)
        return wrapper

    for name in calls:
        monkeypatch.setattr(si, f"build_{name}", _spy(name))

    cache = {"report": report1, "stamps": stamps1}
    report2, _ = await survey_situation(None, vault=vault, cache=cache)

    # ONLY roots re-ran; every other slice was reused.
    assert calls["roots"] == 1, f"changed root must rebuild roots; got {calls}"
    assert calls["services"] == 0
    assert calls["capabilities"] == 0
    assert calls["profile"] == 0
    assert calls["credentials"] == 0

    # the re-surveyed root now surfaces the new file.
    surv = next(r for r in report2.roots
                if os.path.normcase(str(root)) == os.path.normcase(r.path))
    assert any(fh.name == "newly_added.txt" for fh in surv.salient)


# --------------------------------------------------------------------------- #
# Timeout / degrade (IMPL-14) — a slow builder must not hang the survey.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_slow_builder_times_out_and_degrades(tmp_path, monkeypatch):
    from systemu.runtime.mcp import connections
    vault = _make_vault(tmp_path)
    connections.add_server(vault, "https://mcp.example.com/a")
    _store, root = _grant_root_with_pdf(vault, tmp_path)

    # Make services block far past its per-source timeout (0.2s below). 3s is a
    # 15x margin over the timeout — plenty to prove the survey returns without
    # waiting on the builder — while keeping the leaked worker thread (which
    # survives wait_for and only joins at loop/pool shutdown) from dragging out
    # test teardown the way a 30s sleep would.
    def _hang(_vault):
        time.sleep(3)
        return []

    monkeypatch.setattr(si, "build_services", _hang)
    # Tighten the per-source timeout so the test is fast and bounded.
    monkeypatch.setattr(si, "_SLICE_TIMEOUTS",
                        {"services": 0.2, "capabilities": 2.0, "profile": 2.0,
                         "credentials": 2.0, "roots": 5.0}, raising=False)

    start = time.monotonic()
    report, _ = await survey_situation(None, vault=vault)
    elapsed = time.monotonic() - start

    # bounded: the survey returns well before the 3s hang would (timeout=0.2s).
    assert elapsed < 2, f"survey blocked on the slow builder ({elapsed:.1f}s)"
    assert isinstance(report, SituationReport)
    # the slow slice degraded to empty; the OTHER slices still surveyed.
    assert report.services == []
    assert any(os.path.normcase(str(root)) == os.path.normcase(r.path)
               for r in report.roots)


@pytest.mark.asyncio
async def test_slow_builder_degrades_to_cached_slice(tmp_path, monkeypatch):
    """A slow slice with a prior cached value degrades to the CACHED slice, not
    empty (it only degrades to empty when there is nothing cached)."""
    from systemu.runtime.mcp import connections
    vault = _make_vault(tmp_path)
    connections.add_server(vault, "https://mcp.example.com/a")
    _store, root = _grant_root_with_pdf(vault, tmp_path)

    report1, stamps1 = await survey_situation(None, vault=vault)
    assert report1.services  # cached services present

    # Force services to be re-surveyed (stamp change) but hang the builder past
    # its 0.2s timeout. 3s (not 30) keeps the leaked worker thread from dragging
    # out test teardown while still comfortably exceeding the per-source timeout.
    def _hang(_vault):
        time.sleep(3)
        return []

    monkeypatch.setattr(si, "build_services", _hang)
    monkeypatch.setattr(si, "_slice_stamp_services",
                        lambda vault: "CHANGED-force-resurvey", raising=False)
    monkeypatch.setattr(si, "_SLICE_TIMEOUTS",
                        {"services": 0.2, "capabilities": 2.0, "profile": 2.0,
                         "credentials": 2.0, "roots": 5.0}, raising=False)

    cache = {"report": report1, "stamps": stamps1}
    report2, _ = await survey_situation(None, vault=vault, cache=cache)
    # timed out re-survey → falls back to the CACHED services slice, not empty.
    assert {s.name for s in report2.services} == {s.name for s in report1.services}


# --------------------------------------------------------------------------- #
# Failure / degrade — a raising builder must not crash the survey.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_raising_builder_degrades_survey_still_returns(tmp_path, monkeypatch):
    from systemu.runtime.mcp import connections
    vault = _make_vault(tmp_path)
    connections.add_server(vault, "https://mcp.example.com/a")
    _store, root = _grant_root_with_pdf(vault, tmp_path)

    def _boom(_vault):
        raise RuntimeError("builder down")

    monkeypatch.setattr(si, "build_credentials", _boom, raising=True)

    report, _ = await survey_situation(None, vault=vault)
    assert isinstance(report, SituationReport)
    # the failing slice degraded to empty; the others still surveyed.
    assert report.credentials == []
    assert {s.name for s in report.services} == {"https://mcp.example.com/a"}
    assert any(os.path.normcase(str(root)) == os.path.normcase(r.path)
               for r in report.roots)


# --------------------------------------------------------------------------- #
# Never-block proof — builders run via to_thread; a slow one doesn't stall a
# concurrent coroutine, and survey_situation is a coroutine.
# --------------------------------------------------------------------------- #
def test_survey_situation_is_a_coroutine_function():
    assert asyncio.iscoroutinefunction(survey_situation)


@pytest.mark.asyncio
async def test_slow_builder_does_not_block_the_event_loop(tmp_path, monkeypatch):
    vault = _make_vault(tmp_path)
    _grant_root_with_pdf(vault, tmp_path)

    # A builder that blocks ~0.5s: if it were awaited directly (not to_thread), it
    # would stall the loop and starve the ticker coroutine below.
    def _slow(_vault):
        time.sleep(0.5)
        return []

    monkeypatch.setattr(si, "build_capabilities", _slow)

    ticks = {"n": 0}

    async def _ticker():
        # If the loop is NOT blocked, this coroutine keeps ticking while the
        # blocking builder runs in a worker thread.
        for _ in range(30):
            await asyncio.sleep(0.02)
            ticks["n"] += 1

    survey_task = asyncio.create_task(survey_situation(None, vault=vault))
    ticker_task = asyncio.create_task(_ticker())
    (report, _stamps), _ = await asyncio.gather(survey_task, ticker_task)

    assert isinstance(report, SituationReport)
    # The loop stayed responsive during the ~0.5s blocking builder → many ticks.
    assert ticks["n"] >= 10, (
        f"event loop was blocked during the builder (only {ticks['n']} ticks)")


@pytest.mark.asyncio
async def test_builders_invoked_via_to_thread(tmp_path, monkeypatch):
    """Proof of the to_thread contract: every builder runs on a WORKER thread, not
    the event-loop thread."""
    vault = _make_vault(tmp_path)
    connections_import = __import__(
        "systemu.runtime.mcp.connections", fromlist=["add_server"])
    connections_import.add_server(vault, "https://mcp.example.com/a")
    _grant_root_with_pdf(vault, tmp_path)

    import threading
    main_thread = threading.get_ident()
    threads = {"services": None, "capabilities": None, "profile": None,
               "credentials": None, "roots": None}
    orig = {name: getattr(si, f"build_{name}") for name in threads}

    def _record(name):
        def wrapper(*a, **k):
            threads[name] = threading.get_ident()
            return orig[name](*a, **k)
        return wrapper

    for name in threads:
        monkeypatch.setattr(si, f"build_{name}", _record(name))

    await survey_situation(None, vault=vault)

    for name, tid in threads.items():
        assert tid is not None, f"{name} builder was not invoked"
        assert tid != main_thread, (
            f"{name} builder ran on the event-loop thread, not via to_thread")


# --------------------------------------------------------------------------- #
# Fix (HIGH) — thread-leak containment: survey builders run on a DEDICATED
# bounded pool (_SURVEY_EXECUTOR, thread_name_prefix="ra9-survey"), NOT the
# process-wide default asyncio pool. wait_for cancels the AWAIT but cannot stop a
# wedged OS thread, so a builder stuck on a dead FS mount would leak a worker; on
# the SHARED default pool that leak (repeated) starves the whole daemon (runtime/
# UI/tool-registry all use it). Pinning the builder to the dedicated pool contains
# the leak to surveys. The thread name proves the pool: default-pool workers are
# named "asyncio_*"/"ThreadPoolExecutor-*", ours "ra9-survey_*".
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_builders_run_on_dedicated_survey_executor(tmp_path, monkeypatch):
    vault = _make_vault(tmp_path)
    connections_import = __import__(
        "systemu.runtime.mcp.connections", fromlist=["add_server"])
    connections_import.add_server(vault, "https://mcp.example.com/a")
    _grant_root_with_pdf(vault, tmp_path)

    import threading
    names = {"services": None, "capabilities": None, "profile": None,
             "credentials": None, "roots": None}
    orig = {name: getattr(si, f"build_{name}") for name in names}

    def _record(name):
        def wrapper(*a, **k):
            names[name] = threading.current_thread().name
            return orig[name](*a, **k)
        return wrapper

    for name in names:
        monkeypatch.setattr(si, f"build_{name}", _record(name))

    await survey_situation(None, vault=vault)

    for name, tname in names.items():
        assert tname is not None, f"{name} builder was not invoked"
        # CONTAINMENT: the builder ran on the dedicated pool, provably NOT the
        # process-wide default asyncio pool (whose threads are asyncio_*/
        # ThreadPoolExecutor-*). A leaked wedge here can only starve surveys.
        assert tname.startswith("ra9-survey"), (
            f"{name} builder ran on thread {tname!r}, not the dedicated "
            f"_SURVEY_EXECUTOR (containment lost — a wedged builder would starve "
            f"the process-wide default pool)")


def test_survey_executor_is_a_dedicated_bounded_pool():
    """The module owns a dedicated, bounded ThreadPoolExecutor (not the default
    asyncio pool) so a leaked wedge is contained."""
    from concurrent.futures import ThreadPoolExecutor
    assert isinstance(si._SURVEY_EXECUTOR, ThreadPoolExecutor)
    # bounded (max_workers set) and namespaced so a leak is contained + provable.
    assert si._SURVEY_EXECUTOR._max_workers == 8
    assert si._SURVEY_EXECUTOR._thread_name_prefix == "ra9-survey"


# --------------------------------------------------------------------------- #
# Fix 4 — return contract is now (report, stamps); the stamps are NOT on the
# model (so they never leak into model_dump / Task 8 persistence).
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_survey_returns_report_and_stamps_tuple(tmp_path):
    vault = _make_vault(tmp_path)
    _grant_root_with_pdf(vault, tmp_path)

    result = await survey_situation(None, vault=vault)
    assert isinstance(result, tuple) and len(result) == 2
    report, stamps = result
    assert isinstance(report, SituationReport)
    assert isinstance(stamps, dict)
    # stamps carry a key per slice + the table stamp.
    for key in ("services", "capabilities", "profile", "credentials", "roots", "table"):
        assert key in stamps
    # freshness metadata is OUT of the inventory model: it must not survive dump.
    assert "_situation_stamps" not in report.model_dump()


# --------------------------------------------------------------------------- #
# Fix 1 (HIGH) — a malformed persisted cache must NEVER crash the survey.
# The cache is a persisted-then-deserialized snapshot (Task 8), so a non-dict
# `stamps`, a garbage stamps value, or a wrong-typed cached slice are all
# reachable and must degrade, never raise.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
@pytest.mark.parametrize("bad_cache", [
    {"stamps": "corrupt"},                       # stamps is a str, not a dict
    {"stamps": ["x"]},                           # stamps is a list, not a dict
    {"stamps": 12345},                           # stamps is an int
    {"report": None, "stamps": None},            # both None
    "not-a-dict-at-all",                         # whole cache is a str
])
async def test_corrupt_cache_never_raises(tmp_path, bad_cache):
    from systemu.runtime.mcp import connections
    vault = _make_vault(tmp_path)
    connections.add_server(vault, "https://mcp.example.com/a")
    _grant_root_with_pdf(vault, tmp_path)

    # Must not raise; returns a valid (degraded) report + a fresh stamps dict.
    report, stamps = await survey_situation(None, vault=vault, cache=bad_cache)
    assert isinstance(report, SituationReport)
    assert isinstance(stamps, dict)
    # a corrupt cache does not poison a cold survey — real sources still surface.
    assert {s.name for s in report.services} == {"https://mcp.example.com/a"}


@pytest.mark.asyncio
async def test_stamp_matched_but_wrongtyped_cached_slice_degrades(tmp_path):
    """A stamp-matched cached slice whose VALUE is the wrong type (a poisoned
    snapshot) must degrade to empty, never flow into SituationReport(...) and
    raise ValidationError."""
    from systemu.runtime.mcp import connections
    vault = _make_vault(tmp_path)
    connections.add_server(vault, "https://mcp.example.com/a")
    _grant_root_with_pdf(vault, tmp_path)

    # Compute a REAL stamps dict so every slice's stamp MATCHES → the cached
    # (corrupted) slice value would be reused verbatim under the old code.
    _r0, real_stamps = await survey_situation(None, vault=vault)

    cache = {
        "report": {
            "services": "corrupted",       # should be a list → wrong type
            "capabilities": {"bad": 1},    # should be a list
            "roots": 7,                    # should be a list
            "credentials": "nope",         # should be a list
            "profile": ["not", "a", "dict"],  # should be a dict
        },
        "stamps": real_stamps,
    }
    # Under the old code the wrong-typed reused slices reach SituationReport(...)
    # and raise ValidationError. Fixed: each poisoned slice degrades to empty.
    report, _ = await survey_situation(None, vault=vault, cache=cache)
    assert isinstance(report, SituationReport)
    assert report.services == []
    assert report.capabilities == []
    assert report.roots == []
    assert report.credentials == []
    assert report.profile == {}


# --------------------------------------------------------------------------- #
# Fix 2 (HIGH) — the capabilities stamp must watch the tool BODIES, not just
# tools/index.json. A backfill_effect_tags rewrites tool_*.json bodies WITHOUT
# touching index.json, so an index-mtime-only stamp serves STALE effect_tags.
# --------------------------------------------------------------------------- #
def _seed_deployed_tool(vault, *, name="alpha_tool", effect_tags=None):
    """Save a DEPLOYED + enabled tool with the given effect_tags. Returns tool_id."""
    from systemu.core.models import Tool, ToolStatus, ToolType
    tool = Tool(
        id=name,
        name=name,
        description="a seed tool",
        tool_type=ToolType.PYTHON_FUNCTION,
        status=ToolStatus.DEPLOYED,
        enabled=True,
        effect_tags=list(effect_tags or []),
    )
    vault.save_tool(tool)
    return tool.id


@pytest.mark.asyncio
async def test_capabilities_stamp_watches_tool_bodies_not_just_index(tmp_path):
    """Cold survey → rewrite a tool_*.json BODY to add an effect_tag WITHOUT
    touching index.json → survey again with the prior cache → build_capabilities
    RE-RAN and the report reflects the new effect_tags (not served stale).

    This fails against the old index.json-mtime-only stamp and passes after the
    fix (max-mtime over all tools/ entries)."""
    import json
    from systemu.runtime import situational_inventory as si

    vault = _make_vault(tmp_path)
    tool_id = _seed_deployed_tool(vault, name="alpha_tool", effect_tags=[])

    report1, stamps1 = await survey_situation(None, vault=vault)
    cap1 = next(c for c in report1.capabilities if c.tool_id == tool_id)
    assert cap1.effect_tags == []  # cold: no tags yet

    # Rewrite ONLY the tool BODY (as backfill_effect_tags does) — index.json is
    # left untouched. Bump the body mtime to be safe against same-second writes.
    body_path = vault.root / "tools" / f"tool_{tool_id}.json"
    body = json.loads(body_path.read_text(encoding="utf-8"))
    body["effect_tags"] = ["filesystem_write"]
    body_path.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    _bump = os.stat(body_path).st_mtime + 5
    os.utime(body_path, (_bump, _bump))

    # Count build_capabilities invocations on the 2nd survey.
    calls = {"n": 0}
    orig = si.build_capabilities

    def _spy(*a, **k):
        calls["n"] += 1
        return orig(*a, **k)

    import unittest.mock
    with unittest.mock.patch.object(si, "build_capabilities", _spy):
        cache = {"report": report1, "stamps": stamps1}
        report2, _ = await survey_situation(None, vault=vault, cache=cache)

    # The body rewrite bumped the capabilities stamp → build_capabilities RE-RAN.
    assert calls["n"] == 1, (
        "capabilities stamp missed a tool-body rewrite (stale effect_tags)")
    cap2 = next(c for c in report2.capabilities if c.tool_id == tool_id)
    assert cap2.effect_tags == ["filesystem_write"], (
        "survey served STALE effect_tags after a tool-body rewrite")


@pytest.mark.asyncio
async def test_capabilities_stamp_via_real_backfill(tmp_path):
    """End-to-end with the REAL backfill_effect_tags: it rewrites tool bodies and
    the capabilities stamp must notice."""
    from systemu.runtime import vault_migrator

    vault = _make_vault(tmp_path)
    # Seed a tool whose implementation writes files (so classify yields a tag).
    tool_id = _seed_deployed_tool(vault, name="writer_tool", effect_tags=[])
    impl_dir = vault.root / "tools" / "implementations"
    impl_dir.mkdir(parents=True, exist_ok=True)
    (impl_dir / "writer_tool.py").write_text(
        "def run(path):\n"
        "    with open(path, 'w') as f:\n"
        "        f.write('hi')\n",
        encoding="utf-8",
    )
    # Point the tool body at that implementation.
    import json
    body_path = vault.root / "tools" / f"tool_{tool_id}.json"
    body = json.loads(body_path.read_text(encoding="utf-8"))
    # Relative to the vault root's PARENT — the shape `tool_forge` writes and
    # the runtime resolves. A bare filename does not resolve at execution time.
    body["implementation_path"] = str(
        (impl_dir / "writer_tool.py").relative_to(vault.root.parent))
    body_path.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")

    report1, stamps1 = await survey_situation(None, vault=vault)
    stamp_before = stamps1["capabilities"]

    # Run the real backfill: it rewrites the tool BODY with classified tags.
    vault_migrator.backfill_effect_tags(vault.root, version="test-v1")

    _r, stamps2 = await survey_situation(
        None, vault=vault, cache={"report": report1, "stamps": stamps1})
    assert stamps2["capabilities"] != stamp_before, (
        "backfill rewrote tool bodies but the capabilities stamp didn't change")


# --------------------------------------------------------------------------- #
# Fix 3 (MEDIUM) — root_freshness_stamp must catch an in-place edit of an
# existing top-level file (same name, same count) via max_file_mtime.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_inplace_file_edit_reruns_roots(tmp_path, monkeypatch):
    from systemu.runtime.mcp import connections
    vault = _make_vault(tmp_path)
    connections.add_server(vault, "https://mcp.example.com/a")
    _store, root = _grant_root_with_pdf(vault, tmp_path)

    report1, stamps1 = await survey_situation(None, vault=vault)

    # EDIT an existing top-level file IN PLACE — same name, same entry count.
    # Neither dir-mtime nor entry_count changes; only the file's own mtime does.
    target = root / "report.pdf"
    target.write_text("%PDF-1.4 ... EDITED with more bytes")
    _bump = os.stat(target).st_mtime + 5
    os.utime(target, (_bump, _bump))

    calls = {"services": 0, "capabilities": 0, "profile": 0,
             "credentials": 0, "roots": 0}
    orig = {name: getattr(si, f"build_{name}") for name in calls}

    def _spy(name):
        def wrapper(*a, **k):
            calls[name] += 1
            return orig[name](*a, **k)
        return wrapper

    for name in calls:
        monkeypatch.setattr(si, f"build_{name}", _spy(name))

    cache = {"report": report1, "stamps": stamps1}
    report2, _ = await survey_situation(None, vault=vault, cache=cache)

    # The in-place edit bumped max_file_mtime → the roots stamp changed → roots
    # re-surveyed. (Under the old dir-mtime+count-only stamp this stays 0.)
    assert calls["roots"] == 1, (
        f"in-place file edit must rebuild roots; got {calls}")
    assert calls["services"] == 0  # everything else untouched → reused


def test_root_freshness_stamp_includes_max_file_mtime(tmp_path):
    """Direct unit check: an in-place top-level edit changes the stamp."""
    from systemu.runtime.situational_inventory import root_freshness_stamp
    d = tmp_path / "root"
    d.mkdir()
    f = d / "a.txt"
    f.write_text("one")
    s1 = root_freshness_stamp(str(d))
    assert "max_file_mtime" in s1

    # In-place edit: same name, same entry count.
    f.write_text("two-longer")
    _bump = os.stat(f).st_mtime + 5
    os.utime(f, (_bump, _bump))
    s2 = root_freshness_stamp(str(d))
    assert s2 != s1, "in-place edit must change the freshness stamp"
    assert s2["entry_count"] == s1["entry_count"]  # count unchanged
