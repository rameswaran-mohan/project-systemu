"""End-to-end test: memory-tier contract against real Postgres.

CI runs this in the `integration` job with ``SYSTEMU_DATABASE_URL``
pointing at a live Postgres container.  Locally the test skips unless
the env var is set — there's no fakeredis-equivalent for SQLAlchemy
that would meaningfully exercise Postgres-specific behaviour.

Why this matters
    The Phase-4 + v0.2.2 buffer-writer helpers live on
    :class:`SqliteVault` (which talks to both SQLite and Postgres via
    SQLAlchemy).  An operator running ``SYSTEMU_STORAGE=postgres`` in
    docker-* modes hits this code path; this test pins the contract
    against a real database, not an in-memory mock.
"""

from __future__ import annotations

import os
import uuid

import pytest


_PG_URL = os.environ.get("SYSTEMU_DATABASE_URL", "")


pytestmark = pytest.mark.skipif(
    not _PG_URL.startswith("postgresql"),
    reason="set SYSTEMU_DATABASE_URL=postgresql+psycopg2://… to enable",
)


@pytest.fixture
def pg_vault(tmp_path):
    """A SqliteVault bound to the real Postgres URL.  Each test gets a
    unique shadow + buffer key prefix so concurrent runs don't collide."""
    from systemu.storage.sqlite.vault import SqliteVault
    return SqliteVault(_PG_URL, memory_dir=tmp_path / "memory")


@pytest.fixture
def shadow_id():
    """Per-test shadow ID — UUID-suffixed so rows from earlier runs
    don't conflict on the unique constraint."""
    return f"sh-{uuid.uuid4().hex[:8]}"


# ── Shadow tier — Postgres-backed writes obey the same contract ────────

def test_postgres_shadow_buffer_round_trips(pg_vault, shadow_id):
    """A valid Shadow-tier entry persists and reads back correctly."""
    out = pg_vault.append_shadow_memory_buffer(
        shadow_id,
        {"category": "tool_quirks", "lesson": "file_write needs absolute path"},
        source="refinery",
    )
    assert out["_tier"] == "shadow"
    assert out["_source"] == "refinery"

    md, buffer = pg_vault.load_shadow_memory(shadow_id)
    assert any(
        e.get("category") == "tool_quirks" and e.get("_tier") == "shadow"
        for e in buffer
    ), f"entry not found in Postgres buffer: {buffer}"


def test_postgres_shadow_buffer_rejects_elder_category(pg_vault, shadow_id):
    """Cross-tier wall fires against real Postgres just like file mode."""
    with pytest.raises(ValueError, match="opposite tier"):
        pg_vault.append_shadow_memory_buffer(
            shadow_id,
            {"category": "user_preference", "lesson": "wrong tier"},
            source="refinery",
        )


def test_postgres_shadow_buffer_rejects_unknown_under_strict(pg_vault, shadow_id):
    """Strict mode on by default — unknown Shadow categories rejected."""
    with pytest.raises(ValueError, match="unrecognised"):
        pg_vault.append_shadow_memory_buffer(
            shadow_id,
            {"category": "made_up_category", "lesson": "x"},
            source="refinery",
        )


def test_postgres_shadow_buffer_strict_off_accepts_unknown(tmp_path, shadow_id):
    """Operators replaying pre-audit data can opt out via constructor."""
    from systemu.storage.sqlite.vault import SqliteVault
    sv = SqliteVault(
        _PG_URL,
        memory_dir=tmp_path / "memory",
        strict_tier_types=False,
    )
    out = sv.append_shadow_memory_buffer(
        shadow_id,
        {"category": "ad_hoc", "lesson": "x"},
        source="legacy_replay",
    )
    assert out["category"] == "ad_hoc"


# ── Elder tier — open-ended categories accepted ────────────────────────

def test_postgres_elder_buffer_round_trips(pg_vault):
    pg_vault.append_elder_buffer(
        {"category": "Workflow Patterns", "observation": "user prefers cli over UI"},
        source="evolution_engine",
    )
    entries = pg_vault.load_elder_memory_buffer()
    assert any(
        e.get("category") == "Workflow Patterns" and e.get("_tier") == "elder"
        for e in entries
    ), f"entry not found in Postgres elder buffer: {entries}"


def test_postgres_elder_buffer_rejects_shadow_category(pg_vault):
    """Cross-tier wall is symmetric — Shadow types rejected from Elder."""
    with pytest.raises(ValueError, match="opposite tier"):
        pg_vault.append_elder_buffer(
            {"category": "tool_quirks", "observation": "wrong tier"},
            source="evolution_engine",
        )


# ── Production pipeline shapes work against Postgres ───────────────────

def test_postgres_refinery_production_entry_shape(pg_vault, shadow_id):
    """Exact entry shape produced by pipelines/refinery.py."""
    entry = {
        "created_at":             "2026-05-12T10:00:00",
        "exec_id":                f"exec_{uuid.uuid4().hex[:8]}",
        "category":               "heuristics",
        "lesson":                 "summary tasks prefer 5-bullet output",
        "evidence_action_blocks": [],
    }
    out = pg_vault.append_shadow_memory_buffer(shadow_id, entry, source="refinery")
    assert out["_tier"] == "shadow"
    assert out["category"] == "heuristics"


def test_postgres_evolution_engine_production_entry_shape(pg_vault):
    """Exact entry shape produced by pipelines/evolution_engine.py."""
    entry = {
        "category":    "Workflow Patterns",
        "observation": "user always alphabetises file lists",
        "confidence":  0.85,
        "shadow_id":   f"sh-{uuid.uuid4().hex[:8]}",
        "exec_id":     f"exec_{uuid.uuid4().hex[:8]}",
        "timestamp":   "2026-05-12T10:00:00",
    }
    out = pg_vault.append_elder_buffer(entry, source="evolution_engine")
    assert out["_tier"] == "elder"
    assert out["_source"] == "evolution_engine"


# ── Conflict detection works regardless of backend ─────────────────────

def test_postgres_conflicting_type_and_category_rejected(pg_vault, shadow_id):
    with pytest.raises(ValueError, match="conflicting"):
        pg_vault.append_shadow_memory_buffer(
            shadow_id,
            {"type": "tool_quirks", "category": "heuristics", "lesson": "x"},
            source="refinery",
        )
