"""NiceGUI Dashboard — Memory Consolidation page.

Shows the health of every shadow's SHADOW_MEMORY.md, the pending buffer size,
and the scheduler status.  Provides:
  • Run All Now  — immediately consolidates every eligible shadow
  • Per-shadow Consolidate button — fold that shadow's buffer right now
  • Link to /memory/{shadow_id} for the full memory view
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta

from systemu.core.utils import utcnow
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from nicegui import ui

from systemu.interface.dashboard_state import AppState, THEME
from systemu.runtime.memory_rules import needs_consolidation

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
#  Public builder
# ─────────────────────────────────────────────────────────────────────────────

def build_memory_consolidation_page() -> None:
    state  = AppState.get()
    vault  = state.vault
    config = state.config

    # ── Header ────────────────────────────────────────────────────────────────
    with ui.row().classes("w-full items-center justify-between").style("margin-bottom: 20px;"):
        ui.label("💡 Memory Consolidation").style(
            f"font-size: 22px; font-weight: 800; color: {THEME['text']};"
        )
        ui.button(
            "🔄 Run All Now",
            on_click=lambda: _run_all(config, vault),
        ).style(
            f"background: {THEME['primary']}; color: white; border-radius: 8px; "
            f"font-weight: 600; padding: 8px 18px;"
        )

    # ── Scheduler status card ────────────────────────────────────────────────
    _render_scheduler_card(vault)

    # ── Summary stats ─────────────────────────────────────────────────────────
    shadow_rows = _collect_shadow_rows(vault)
    total_buffered  = sum(r["buffer_count"] for r in shadow_rows)
    needs_work      = sum(1 for r in shadow_rows if r["needs_consolidation"])

    with ui.row().style("gap: 12px; margin-bottom: 24px; flex-wrap: wrap;"):
        _stat_card("👥", str(len(shadow_rows)), "Total Shadows")
        _stat_card("📝", str(total_buffered),   "Buffered Lessons")
        _stat_card(
            "⚡",
            str(needs_work),
            "Ready to Consolidate",
            highlight=(needs_work > 0),
        )

    # ── Per-shadow table ──────────────────────────────────────────────────────
    if not shadow_rows:
        ui.label("No shadows found — create one from the Shadows page.").style(
            f"color: {THEME['text_muted']}; font-style: italic; padding: 20px 0;"
        )
        return

    # Table header
    with ui.row().classes("w-full").style(
        f"background: {THEME['surface2']}; border-radius: 8px 8px 0 0; "
        f"padding: 10px 16px; gap: 0;"
    ):
        _th("Shadow",            flex=3)
        _th("Buffer",            flex=1, align="center")
        _th("Last Consolidated", flex=2)
        _th("Status",            flex=1, align="center")
        _th("Actions",           flex=2, align="right")

    # Table rows
    for idx, row in enumerate(shadow_rows):
        _render_shadow_row(row, idx, config, vault)


# ─────────────────────────────────────────────────────────────────────────────
#  Scheduler status card
# ─────────────────────────────────────────────────────────────────────────────

def _render_scheduler_card(vault) -> None:
    from systemu.scheduler.jobs import (
        get_scheduler, BUFFER_THRESHOLD, STALE_AFTER_DAYS,
    )

    meta     = _load_meta(vault)
    sched    = get_scheduler()
    next_run = _next_run_label(sched)
    last_run = _last_run_label(meta)
    updated  = meta.get("shadows_updated") if meta else None

    with ui.card().style(
        f"background: {THEME['surface']}; border: 1px solid {THEME['border']}; "
        f"border-radius: 12px; padding: 18px 24px; margin-bottom: 20px; width: 100%;"
    ):
        with ui.row().style("align-items: center; gap: 6px; margin-bottom: 14px;"):
            ui.label("⏰").style("font-size: 16px;")
            ui.label("Scheduler Status").style(
                f"font-size: 14px; font-weight: 700; color: {THEME['text']};"
            )
            # Live / offline chip
            chip_color  = THEME["success"] if sched else THEME["danger"]
            chip_label  = "LIVE" if sched else "OFFLINE"
            ui.html(
                f'<span style="font-size: 10px; font-weight: 700; padding: 2px 8px; '
                f'border-radius: 6px; color: {chip_color}; '
                f'background: color-mix(in srgb, {chip_color} 15%, transparent);">'
                f'{chip_label}</span>'
            )

        with ui.row().style("gap: 36px; flex-wrap: wrap;"):
            _sched_stat("🕐 Next run",      next_run)
            _sched_stat("✅ Last run",       last_run)
            _sched_stat(
                "📊 Buffer threshold",
                f"{BUFFER_THRESHOLD} lessons",
            )
            _sched_stat(
                "⏳ Staleness window",
                f"{STALE_AFTER_DAYS} days",
            )
            if updated is not None:
                _sched_stat("🔁 Last updated", f"{updated} shadow(s)")


# ─────────────────────────────────────────────────────────────────────────────
#  Shadow row helpers
# ─────────────────────────────────────────────────────────────────────────────

def _collect_shadow_rows(vault) -> List[Dict[str, Any]]:
    from systemu.scheduler.jobs import BUFFER_THRESHOLD, STALE_AFTER_DAYS

    headers = vault.load_index("shadow_army")
    rows: List[Dict[str, Any]] = []
    now = utcnow()

    for h in headers:
        sid = h.get("id", "")
        if not sid:
            continue
        try:
            shadow = vault.get_shadow(sid)
        except KeyError:
            continue

        md_text, buf = vault.load_shadow_memory(sid)
        last_dt  = _parse_last_consolidated(md_text)
        is_stale = (now - last_dt) > timedelta(days=STALE_AFTER_DAYS)
        has_buf  = len(buf) >= BUFFER_THRESHOLD
        # ONE needs-consolidation rule, shared with memory_status + the engine.
        needs    = needs_consolidation(buf, md_text)

        rows.append({
            "id":                 sid,
            "name":               shadow.name,
            "description":        shadow.description,
            "buffer_count":       len(buf),
            "last_consolidated":  _date_label(last_dt),
            "needs_consolidation": needs,
            "is_stale":           is_stale,
            "has_buf":            has_buf,
        })

    # Sort: needing consolidation first, then by buffer count desc
    rows.sort(key=lambda r: (-int(r["needs_consolidation"]), -r["buffer_count"]))
    return rows


def _render_shadow_row(
    row: Dict[str, Any],
    idx: int,
    config,
    vault,
) -> None:
    bg = THEME["surface"] if idx % 2 == 0 else THEME["surface2"]
    sid = row["id"]

    with ui.row().classes("w-full items-center").style(
        f"background: {bg}; padding: 10px 16px; "
        f"border-bottom: 1px solid {THEME['border']}; gap: 0;"
    ):
        # Shadow name + description
        with ui.column().style(f"flex: 3; gap: 1px; min-width: 0;"):
            ui.element("a").props(f'href="/memory/{sid}"').style(
                f"font-size: 13px; font-weight: 600; color: {THEME['primary']}; "
                f"text-decoration: none;"
            ).text = row["name"]
            desc = (row["description"] or "")[:90]
            if desc:
                ui.label(desc + ("…" if len(row["description"]) > 90 else "")).style(
                    f"font-size: 11px; color: {THEME['text_muted']}; "
                    f"white-space: nowrap; overflow: hidden; text-overflow: ellipsis;"
                )

        # Buffer count badge
        with ui.row().style("flex: 1; justify-content: center;"):
            _buffer_badge(row["buffer_count"])

        # Last consolidated
        ui.label(row["last_consolidated"]).style(
            f"flex: 2; font-size: 12px; color: {THEME['text_muted']}; font-family: monospace;"
        )

        # Status chip
        with ui.row().style("flex: 1; justify-content: center;"):
            _status_chip(row)

        # Action buttons
        with ui.row().style("flex: 2; justify-content: flex-end; gap: 6px;"):
            if row["needs_consolidation"]:
                ui.button(
                    "🔄 Consolidate",
                    on_click=lambda _, s=sid: _run_one(s, config, vault),
                ).style(
                    f"background: {THEME['primary']}; color: white; "
                    f"border-radius: 6px; font-size: 12px; padding: 4px 10px;"
                )
            ui.button(
                "🔍 View",
                on_click=lambda _, s=sid: ui.navigate.to(f"/memory/{s}"),
            ).style(
                f"background: {THEME['surface2']}; color: {THEME['text']}; "
                f"border: 1px solid {THEME['border']}; border-radius: 6px; "
                f"font-size: 12px; padding: 4px 10px;"
            )


# ─────────────────────────────────────────────────────────────────────────────
#  Action handlers
# ─────────────────────────────────────────────────────────────────────────────

def _run_all(config, vault) -> None:
    """Trigger consolidation for all eligible shadows OFF-LOOP and refresh.

    P11: delegates to the shared async action so the multi-second LLM call
    runs in a worker thread instead of freezing the NiceGUI event loop.
    """
    from systemu.interface.memory_actions import run_all_async

    run_all_async(config, vault, on_done=lambda: ui.navigate.to("/memory"))


def _run_one(shadow_id: str, config, vault) -> None:
    """Consolidate a single shadow OFF-LOOP and refresh the page (P11)."""
    from systemu.interface.memory_actions import run_one_async

    run_one_async(shadow_id, config, vault, on_done=lambda: ui.navigate.to("/memory"))


# ─────────────────────────────────────────────────────────────────────────────
#  Small UI atoms
# ─────────────────────────────────────────────────────────────────────────────

def _stat_card(icon: str, value: str, label: str, *, highlight: bool = False) -> None:
    border = THEME["primary"] if highlight else THEME["border"]
    with ui.card().style(
        f"background: {THEME['surface']}; border: 1px solid {border}; "
        f"border-radius: 10px; padding: 14px 20px; min-width: 120px;"
    ):
        ui.label(f"{icon} {value}").style(
            f"font-size: 22px; font-weight: 800; color: "
            f"{'#f59e0b' if highlight and value != '0' else THEME['text']};"
        )
        ui.label(label).style(f"font-size: 11px; color: {THEME['text_muted']};")


def _sched_stat(label: str, value: str) -> None:
    with ui.column().style("gap: 1px;"):
        ui.label(label).style(f"font-size: 10px; color: {THEME['text_muted']}; font-weight: 600;")
        ui.label(value).style(f"font-size: 13px; color: {THEME['text']}; font-weight: 600;")


def _th(text: str, *, flex: int = 1, align: str = "left") -> None:
    ui.label(text).style(
        f"flex: {flex}; font-size: 10px; font-weight: 700; color: {THEME['text_muted']}; "
        f"text-transform: uppercase; letter-spacing: 0.08em; text-align: {align};"
    )


def _buffer_badge(count: int) -> None:
    if count == 0:
        color = THEME["success"]
        label = "0"
    elif count < 10:
        color = "#f59e0b"   # amber
        label = str(count)
    else:
        color = THEME["danger"]
        label = str(count)
    ui.html(
        f'<span style="font-size: 12px; font-weight: 700; padding: 2px 10px; '
        f'border-radius: 12px; color: {color}; '
        f'background: color-mix(in srgb, {color} 15%, transparent);">'
        f'{label}</span>'
    )


def _status_chip(row: Dict[str, Any]) -> None:
    if not row["needs_consolidation"]:
        color, label = THEME["success"], "READY"
    elif row["has_buf"] and row["is_stale"]:
        color, label = THEME["danger"],  "URGENT"
    elif row["has_buf"]:
        color, label = "#f59e0b",        "PENDING"
    else:
        color, label = THEME["warning"] if hasattr(THEME, "warning") else "#f59e0b", "STALE"
    ui.html(
        f'<span style="font-size: 10px; font-weight: 700; padding: 3px 10px; '
        f'border-radius: 6px; color: {color}; '
        f'background: color-mix(in srgb, {color} 15%, transparent); '
        f'white-space: nowrap;">{label}</span>'
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Utility helpers
# ─────────────────────────────────────────────────────────────────────────────

def _load_meta(vault) -> Optional[Dict[str, Any]]:
    try:
        p = Path(vault.root) / "memory_consolidation_meta.json"
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None


def _parse_last_consolidated(md_text: str) -> datetime:
    fallback = utcnow() - timedelta(days=365)
    if not md_text:
        return fallback
    m = re.search(r"^last_consolidated:\s*(.+)$", md_text, re.MULTILINE)
    if not m:
        return fallback
    try:
        return datetime.fromisoformat(m.group(1).strip().replace("Z", ""))
    except ValueError:
        return fallback


def _date_label(dt: datetime) -> str:
    """Human-readable date, or 'never' if the fallback sentinel year is detected."""
    if dt.year < utcnow().year - 1:
        return "never"
    return dt.strftime("%Y-%m-%d %H:%M")


def _last_run_label(meta: Optional[Dict[str, Any]]) -> str:
    if not meta or not meta.get("last_run"):
        return "never"
    try:
        dt  = datetime.fromisoformat(meta["last_run"].replace("Z", ""))
        ago = utcnow() - dt
        if ago.total_seconds() < 120:
            return "just now"
        if ago.total_seconds() < 3600:
            return f"{int(ago.total_seconds() // 60)}m ago"
        if ago.days < 1:
            return f"{int(ago.total_seconds() // 3600)}h ago"
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(meta.get("last_run", "—"))


def _next_run_label(sched) -> str:
    """Return the next fire time for the memory_consolidation job."""
    if sched is None:
        return "scheduler offline"
    try:
        job = sched.get_job("memory_consolidation")
        if job and job.next_run_time:
            return job.next_run_time.strftime("%Y-%m-%d %H:%M UTC")
        return "not scheduled"
    except Exception:
        return "unknown"
