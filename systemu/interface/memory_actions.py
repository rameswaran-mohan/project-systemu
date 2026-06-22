"""One async (off-loop) consolidation action, shared by every surface.

P11: the three consolidation handlers — ``memory_consolidation_page._run_all``
/ ``_run_one`` and ``shadow_memory_page._trigger_consolidation`` — each ran the
multi-second LLM consolidation **synchronously on the NiceGUI event loop**,
freezing the dashboard for every other client while it ran.

This module converges them onto ONE off-loop action: the (unchanged) engine
primitives run in a ``threading.Thread(daemon=True)`` worker (the v0.8.5
``insights.py`` pattern), and the result is marshalled back to the UI thread
via a ``ui.timer`` poll — so the event loop is never blocked.

The decision logic (what message to show, what to save/clear) lives in the
PURE ``consolidate_*_result`` functions (no NiceGUI, no threads) so it is
unit-testable; ``run_*_async`` are the thin notify→thread→timer shells.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# (notify_type, message) — notify_type ∈ {"positive","info","warning","negative"}
Result = Tuple[str, str]


# ─────────────────────────────────────────────────────────────────────────────
#  Pure result logic (no NiceGUI / no threads) — unit-testable
# ─────────────────────────────────────────────────────────────────────────────

def consolidate_all_result(config, vault) -> Result:
    """Run the all-shadows consolidation engine; return the (type, message)."""
    from systemu.scheduler.jobs import run_consolidation_for_all

    updated = run_consolidation_for_all(config, vault)
    if updated:
        return ("positive", f"✓ {updated} shadow(s) consolidated.")
    return ("info", "No shadows needed consolidation.")


def consolidate_one_result(shadow, md_text: str, buffer_entries: Sequence,
                           config, vault, shadow_id: str) -> Result:
    """Consolidate a single shadow (engine unchanged); return (type, message).

    Mirrors the prior ``_run_one``/``_trigger_consolidation`` body exactly:
    invalid LLM output leaves the buffer intact; on success the memory is
    saved, the buffer cleared, and skills graduated (best-effort).
    """
    from systemu.scheduler.jobs import _consolidate_one, _graduate_memory_to_skills

    new_md = _consolidate_one(shadow, md_text, buffer_entries, config)
    if not new_md or not new_md.lstrip().startswith("---"):
        return ("negative", "LLM returned invalid output — buffer left intact.")
    vault.save_shadow_memory(shadow_id, new_md)
    vault.clear_memory_buffer(shadow_id)
    try:
        _graduate_memory_to_skills(shadow, new_md, vault)
    except Exception as exc:  # graduation is best-effort
        logger.warning("[MemoryActions] skill graduation failed for %s: %s", shadow_id, exc)
    return ("positive", f"✓ '{getattr(shadow, 'name', shadow_id)}' memory consolidated.")


# ─────────────────────────────────────────────────────────────────────────────
#  Off-loop shell (NiceGUI thread + ui.timer marshal-back)
# ─────────────────────────────────────────────────────────────────────────────

def _run_off_loop(work: Callable[[], Result], *, started_msg: str,
                  on_done: Optional[Callable[[], None]] = None) -> None:
    """Notify ``started_msg`` now, run ``work()`` in a daemon thread, then poll
    on the UI thread and notify the result + call ``on_done`` — never blocking
    the event loop. ``work`` returns a ``(notify_type, message)`` tuple.
    """
    from nicegui import ui

    ui.notify(started_msg, type="info")
    holder: dict[str, Any] = {}

    def _worker() -> None:
        try:
            holder["result"] = work()
        except Exception as exc:  # defensive — never lose the worker
            logger.exception("[MemoryActions] consolidation worker failed")
            holder["result"] = ("negative", f"Consolidation error: {exc}")

    threading.Thread(target=_worker, daemon=True).start()

    state = {"done": False}
    timer_holder: dict[str, Any] = {}

    def _poll() -> None:
        if state["done"] or "result" not in holder:
            return
        state["done"] = True
        notify_type, msg = holder["result"]
        try:
            ui.notify(msg, type=notify_type)
            if on_done is not None:
                on_done()
        finally:
            t = timer_holder.get("t")
            try:
                if t is not None:
                    t.active = False
            except Exception:
                pass  # UI-timer teardown guard — timer may already be gone

    timer_holder["t"] = ui.timer(0.5, _poll)


def run_all_async(config, vault, *, on_done: Optional[Callable[[], None]] = None) -> None:
    """Off-loop consolidation for all eligible shadows."""
    _run_off_loop(
        lambda: consolidate_all_result(config, vault),
        started_msg="Running consolidation for all shadows…",
        on_done=on_done,
    )


def run_one_async(shadow_id: str, config, vault, *,
                  on_done: Optional[Callable[[], None]] = None) -> None:
    """Off-loop consolidation for one shadow (fast pre-checks run synchronously)."""
    from nicegui import ui

    try:
        shadow = vault.get_shadow(shadow_id)
    except KeyError:
        ui.notify("Shadow not found.", type="negative")
        return
    md_text, buffer_entries = vault.load_shadow_memory(shadow_id)
    if not buffer_entries:
        ui.notify("No buffered lessons to consolidate.", type="warning")
        return

    _run_off_loop(
        lambda: consolidate_one_result(shadow, md_text, buffer_entries, config, vault, shadow_id),
        started_msg=f"Consolidating {len(buffer_entries)} lesson(s) for '{shadow.name}'…",
        on_done=on_done,
    )
