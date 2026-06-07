"""Tests for systemu.runtime.dep_conflicts.

Pins the conservative conflict detector's behaviour:
  * Disjoint ranges flagged
  * Pinned-vs-pinned mismatches flagged
  * Overlapping ranges NOT flagged (false-positive control)
  * Bare names contribute nothing
  * Single-tool deps don't flag (need ≥2 sources to conflict)
"""
from __future__ import annotations

import pytest

from systemu.core.models import Tool, ToolStatus, ToolType
from systemu.runtime.dep_conflicts import find_conflicts


def _tool(name, deps, tool_id=None):
    return {
        "id":           tool_id or f"tool_{name}",
        "name":         name,
        "enabled":      True,
        "dependencies": deps,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Conflicts are correctly flagged

class TestFlagsConflicts:
    def test_disjoint_ranges(self):
        conflicts = find_conflicts([
            _tool("A", ["requests>=2.0,<3.0"]),
            _tool("B", ["requests>=4.0,<5.0"]),
        ])
        assert len(conflicts) == 1
        assert conflicts[0].package == "requests"
        names = {s.tool_name for s in conflicts[0].specs}
        assert names == {"A", "B"}

    def test_pinned_mismatch(self):
        conflicts = find_conflicts([
            _tool("A", ["requests==1.4"]),
            _tool("B", ["requests==1.5"]),
        ])
        assert len(conflicts) == 1

    def test_pinned_vs_excluding_range(self):
        conflicts = find_conflicts([
            _tool("A", ["requests==2.5"]),
            _tool("B", ["requests<2.0"]),
        ])
        assert len(conflicts) == 1

    def test_three_way_conflict_collapsed(self):
        """A package mentioned by 3 tools shows up as ONE conflict entry
        listing all three contributors."""
        conflicts = find_conflicts([
            _tool("A", ["requests>=2.0,<3.0"]),
            _tool("B", ["requests>=4.0,<5.0"]),
            _tool("C", ["requests==9.9"]),
        ])
        assert len(conflicts) == 1
        names = {s.tool_name for s in conflicts[0].specs}
        assert names == {"A", "B", "C"}


# ─────────────────────────────────────────────────────────────────────────────
# Non-conflicts must NOT flag

class TestNoFalsePositives:
    def test_overlapping_ranges(self):
        assert find_conflicts([
            _tool("A", ["requests>=2.0"]),
            _tool("B", ["requests>=3.0"]),
        ]) == []

    def test_same_pin(self):
        assert find_conflicts([
            _tool("A", ["requests==2.5"]),
            _tool("B", ["requests==2.5"]),
        ]) == []

    def test_bare_names(self):
        """Two tools that both say `python-docx` (no version) impose no
        version constraint and cannot conflict."""
        assert find_conflicts([
            _tool("A", ["python-docx"]),
            _tool("B", ["python-docx"]),
        ]) == []

    def test_bare_plus_constraint(self):
        """A bare name doesn't conflict with any range — it accepts all."""
        assert find_conflicts([
            _tool("A", ["python-docx"]),
            _tool("B", ["python-docx>=1.0"]),
        ]) == []

    def test_single_tool_with_two_constraints(self):
        """A single tool with multiple constraints on a package is its
        own concern — not a cross-tool conflict."""
        assert find_conflicts([
            _tool("A", ["requests>=2.0,<3.0,>=2.5"]),
        ]) == []

    def test_unrelated_packages(self):
        assert find_conflicts([
            _tool("A", ["requests==1.0"]),
            _tool("B", ["flask==2.0"]),
        ]) == []


# ─────────────────────────────────────────────────────────────────────────────
# Resilience to bad inputs

class TestRobustness:
    def test_empty_input(self):
        assert find_conflicts([]) == []

    def test_tool_with_no_deps(self):
        assert find_conflicts([_tool("A", [])]) == []

    def test_unparseable_dep_is_skipped(self):
        # Mixed: one valid, one garbage.  Should report nothing because
        # the bad entry is silently dropped.
        assert find_conflicts([
            _tool("A", ["requests>=2.0", "this is not a spec"]),
            _tool("B", ["requests>=2.5"]),
        ]) == []

    def test_accepts_pydantic_tool_instances(self):
        # v0.6.1-a: names must match ^[a-z][a-z0-9_]{0,63}$ — use snake_case
        t = Tool(
            id="tool_x", name="tool_a", description="-",
            tool_type=ToolType.PYTHON_FUNCTION, status=ToolStatus.DEPLOYED,
            enabled=True,
            dependencies=["requests>=4.0"],
        )
        t2 = Tool(
            id="tool_y", name="tool_b", description="-",
            tool_type=ToolType.PYTHON_FUNCTION, status=ToolStatus.DEPLOYED,
            enabled=True,
            dependencies=["requests<3.0"],
        )
        conflicts = find_conflicts([t, t2])
        assert len(conflicts) == 1
