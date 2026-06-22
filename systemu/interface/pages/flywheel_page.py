"""Data Flywheel Dashboard — shows how Shadows improve over time.

Metrics tracked (proving the flywheel spins):
  success_rate          — rising = shadow solves tasks more reliably
  avg_iterations        — falling = shadow needs fewer reasoning steps
  memory_entry_count    — rising = accumulated experience grows
  high_confidence_entries — rising = lessons reinforced by repetition
  objectives_completed_rate — rising = more goals achieved per run
"""

from __future__ import annotations

from nicegui import ui

from systemu.interface.dashboard_state import AppState, THEME


def build_flywheel_page() -> None:
    """Render the data flywheel visualization page."""
    state  = AppState.get()
    vault  = state.vault
    config = state.config

    from systemu.runtime.metrics_tracker import load_all_metrics
    all_metrics = load_all_metrics(config.vault_dir)

    ui.label("Data Flywheel").style(
        f"font-size: 28px; font-weight: 800; color: {THEME['text']}; margin-bottom: 4px;"
    )
    ui.label("How Shadows improve with every execution — the more they run, the better they get.").style(
        f"color: {THEME['text_muted']}; font-size: 14px; margin-bottom: 24px;"
    )

    # ── Flywheel overview cards ────────────────────────────────────────────────
    total_execs    = sum(m.get("total_executions", 0) for m in all_metrics)
    total_success  = sum(m.get("success_count", 0) for m in all_metrics)
    total_fail     = sum(m.get("failure_count", 0) for m in all_metrics)
    total_mem      = sum(m.get("memory_entry_count", 0) for m in all_metrics)
    total_high_conf= sum(m.get("high_confidence_entries", 0) for m in all_metrics)
    global_sr      = round(total_success / total_execs * 100, 1) if total_execs else 0.0

    with ui.row().classes("w-full gap-4 flex-wrap"):
        _metric_card("settings", "Total Executions",       str(total_execs),    THEME["primary"],  "lifetime")
        _metric_card("check_circle", "Global Success Rate",    f"{global_sr}%",     THEME["success"],  f"{total_success} successes")
        _metric_card("menu_book", "Memory Entries",         str(total_mem),       "#a78bfa",         "across all shadows")
        _metric_card("local_fire_department", "High-Confidence Lessons",str(total_high_conf), THEME["warning"],  "reinforced by repetition")
        _metric_card("groups", "Shadows Tracked",        str(len(all_metrics)), THEME["info"],    "with flywheel data")

    ui.separator().style(f"background:{THEME['border']}; margin: 24px 0;")

    if not all_metrics:
        with ui.column().classes("w-full items-center").style("padding: 60px 0;"):
            ui.icon("settings").style("font-size: 64px;")
            ui.label("No execution data yet — run a Shadow to start the flywheel.").style(
                f"color: {THEME['text_muted']}; font-size: 16px; margin-top: 16px; text-align: center;"
            )
            ui.label("Each execution builds memory → memory improves reasoning → reasoning reduces iterations.").style(
                f"color: {THEME['text_muted']}; font-size: 13px; margin-top: 8px; text-align: center;"
            )
        return

    # ── Flywheel diagram (SVG animation) ──────────────────────────────────────
    ui.html(_flywheel_svg()).style("margin: 0 auto; display: block;")

    ui.separator().style(f"background:{THEME['border']}; margin: 24px 0;")

    # ── Per-shadow detail panels ───────────────────────────────────────────────
    ui.label("Shadow Learning Curves").style(
        f"font-size: 18px; font-weight: 700; color: {THEME['text']}; margin-bottom: 16px;"
    )

    for metrics in all_metrics:
        _shadow_flywheel_card(metrics)

    ui.separator().style(f"background:{THEME['border']}; margin: 24px 0;")

    # ── Global learning curve chart ───────────────────────────────────────────
    if total_execs > 0:
        _global_trend_chart(all_metrics)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _metric_card(icon: str, label: str, value: str, color: str, sub: str) -> None:
    with ui.column().style(
        f"background: {THEME['surface']}; border: 1px solid {THEME['border']}; "
        f"border-radius: 12px; padding: 20px 24px; min-width: 160px; flex: 1; gap: 4px;"
    ):
        with ui.row().style("align-items: center; gap: 8px;"):
            ui.icon(icon).style("font-size: 20px;")
            ui.label(label).style(f"font-size: 12px; color: {THEME['text_muted']}; font-weight: 500;")
        ui.label(value).style(f"font-size: 28px; font-weight: 800; color: {color};")
        ui.label(sub).style(f"font-size: 11px; color: {THEME['text_muted']};")


def _shadow_flywheel_card(metrics: dict) -> None:
    name        = metrics.get("shadow_name", "Unknown Shadow")
    total_execs = metrics.get("total_executions", 0)
    success_rate= metrics.get("success_rate", 0.0)
    avg_iter    = metrics.get("avg_iterations", 0.0)
    mem_count   = metrics.get("memory_entry_count", 0)
    high_conf   = metrics.get("high_confidence_entries", 0)
    obj_rate    = metrics.get("objectives_completed_rate", 0.0)
    executions  = metrics.get("executions", [])

    # Trend indicators: compare first half vs second half
    def _trend(key: str, lower_is_better: bool = False) -> str:
        vals = [e.get(key, 0) for e in executions if e.get(key) is not None]
        if len(vals) < 4:
            return ""
        first_half  = sum(vals[:len(vals)//2]) / (len(vals)//2)
        second_half = sum(vals[len(vals)//2:]) / (len(vals) - len(vals)//2)
        if lower_is_better:
            improving = second_half < first_half * 0.95
            degrading = second_half > first_half * 1.05
        else:
            improving = second_half > first_half * 1.05
            degrading = second_half < first_half * 0.95
        return " ↑" if improving else (" ↓" if degrading else " →")

    iter_trend = _trend("iterations", lower_is_better=True)
    obj_trend  = _trend("objectives_completed", lower_is_better=False)

    # Color code success rate
    sr_color = THEME["success"] if success_rate >= 75 else (THEME["warning"] if success_rate >= 40 else "#ef4444")
    sr_bar_w = int(min(success_rate, 100))

    with ui.expansion(
        f"{name}  —  {total_execs} runs · {success_rate:.0f}% success",
        icon="settings",
    ).classes("w-full").style(
        f"background: {THEME['surface']}; border: 1px solid {THEME['border']}; "
        f"border-radius: 12px; margin-bottom: 12px;"
    ):
        with ui.column().style("padding: 16px; gap: 16px; width: 100%;"):

            # Key metrics row
            with ui.row().classes("w-full gap-6 flex-wrap"):
                _mini_metric("Success Rate", f"{success_rate:.1f}%", sr_color)
                _mini_metric("Avg Iterations", f"{avg_iter:.1f}{iter_trend}", THEME["info"])
                _mini_metric("Memory Entries", str(mem_count), "#a78bfa")
                _mini_metric("High-Confidence", str(high_conf), THEME["warning"])
                _mini_metric("Obj. Completion", f"{obj_rate:.1f}%{obj_trend}", THEME["success"])

            # Success rate bar
            with ui.column().style("width: 100%; gap: 4px;"):
                ui.label("Success Rate").style(f"font-size: 11px; color: {THEME['text_muted']};")
                with ui.element("div").style(
                    f"width: 100%; height: 8px; background: {THEME['surface2']}; border-radius: 4px; overflow: hidden;"
                ):
                    ui.element("div").style(
                        f"width: {sr_bar_w}%; height: 100%; background: {sr_color}; "
                        f"border-radius: 4px; transition: width 0.3s;"
                    )

            # Recent execution timeline
            if executions:
                ui.label("Recent Executions").style(
                    f"font-size: 12px; font-weight: 600; color: {THEME['text_muted']}; margin-top: 8px;"
                )
                with ui.row().style("flex-wrap: wrap; gap: 6px;"):
                    for e in executions[-20:]:  # last 20
                        status = e.get("status", "?")
                        color  = THEME["success"] if status == "success" else (
                            THEME["warning"] if status == "partial" else "#ef4444"
                        )
                        iters  = e.get("iterations", 0)
                        ui.element("div").tooltip(
                            f"{e.get('execution_id','?')} | {status} | {iters} iters"
                        ).style(
                            f"width: 20px; height: 20px; border-radius: 4px; background: {color}; "
                            f"opacity: {max(0.4, min(1.0, 0.4 + iters/20))}; cursor: pointer;"
                        )


def _mini_metric(label: str, value: str, color: str) -> None:
    with ui.column().style("gap: 2px; min-width: 100px;"):
        ui.label(label).style(f"font-size: 11px; color: {THEME['text_muted']};")
        ui.label(value).style(f"font-size: 18px; font-weight: 700; color: {color};")


def _global_trend_chart(all_metrics: list) -> None:
    """ECharts line chart showing global success rate trend over executions."""
    # Merge all executions sorted by timestamp
    all_execs = []
    for m in all_metrics:
        for e in m.get("executions", []):
            all_execs.append(e)
    all_execs.sort(key=lambda e: e.get("timestamp", ""))

    if len(all_execs) < 3:
        return

    # Rolling window success rate (window=5)
    labels = []
    sr_vals = []
    iter_vals = []
    window = max(3, min(5, len(all_execs) // 3))
    for i in range(len(all_execs)):
        window_slice = all_execs[max(0, i - window + 1):i + 1]
        sr = sum(1 for e in window_slice if e.get("status") == "success") / len(window_slice) * 100
        iters = [e.get("iterations", 0) for e in window_slice if e.get("iterations", 0) > 0]
        avg_i = sum(iters) / len(iters) if iters else 0
        labels.append(f"#{i+1}")
        sr_vals.append(round(sr, 1))
        iter_vals.append(round(avg_i, 1))

    ui.label("Global Learning Trend").style(
        f"font-size: 18px; font-weight: 700; color: {THEME['text']}; margin-bottom: 8px;"
    )
    ui.label(f"Rolling {window}-execution window · {len(all_execs)} total executions").style(
        f"color: {THEME['text_muted']}; font-size: 12px; margin-bottom: 16px;"
    )

    chart_options = {
        "backgroundColor": THEME["surface"],
        "grid": {"top": 40, "bottom": 40, "left": 60, "right": 60},
        "tooltip": {"trigger": "axis"},
        "legend": {"data": ["Success Rate (%)", "Avg Iterations"], "textStyle": {"color": THEME["text_muted"]}},
        "xAxis": {
            "type": "category",
            "data": labels,
            "axisLabel": {"color": THEME["text_muted"]},
            "axisLine": {"lineStyle": {"color": THEME["border"]}},
        },
        "yAxis": [
            {
                "type": "value",
                "name": "Success %",
                "min": 0, "max": 100,
                "axisLabel": {"color": THEME["text_muted"]},
                "splitLine": {"lineStyle": {"color": THEME["border"]}},
            },
            {
                "type": "value",
                "name": "Iterations",
                "axisLabel": {"color": THEME["text_muted"]},
                "splitLine": {"show": False},
            },
        ],
        "series": [
            {
                "name": "Success Rate (%)",
                "type": "line",
                "data": sr_vals,
                "smooth": True,
                "lineStyle": {"color": THEME["success"], "width": 2},
                "areaStyle": {"color": f"color-mix(in srgb, {THEME['success']} 20%, transparent)"},
                "itemStyle": {"color": THEME["success"]},
                "yAxisIndex": 0,
            },
            {
                "name": "Avg Iterations",
                "type": "line",
                "data": iter_vals,
                "smooth": True,
                "lineStyle": {"color": THEME["info"], "width": 2, "type": "dashed"},
                "itemStyle": {"color": THEME["info"]},
                "yAxisIndex": 1,
            },
        ],
    }

    ui.echart(chart_options).style(
        f"width: 100%; height: 300px; border: 1px solid {THEME['border']}; border-radius: 12px;"
    )


def _flywheel_svg() -> str:
    """SVG animated flywheel diagram showing the learning loop."""
    primary = THEME["primary"]
    success = THEME["success"]
    info    = THEME["info"]
    warning = THEME["warning"]
    text    = THEME["text"]
    muted   = THEME["text_muted"]
    bg      = THEME["surface"]
    border  = THEME["border"]

    return f"""
<svg width="640" height="260" viewBox="0 0 640 260" xmlns="http://www.w3.org/2000/svg"
     style="font-family: Inter, sans-serif; background: {bg}; border: 1px solid {border}; border-radius: 12px; padding: 8px;">

  <!-- Animated gear in center -->
  <g transform="translate(320,130)">
    <animateTransform attributeName="transform" type="rotate"
      from="0 320 130" to="360 320 130" dur="8s" repeatCount="indefinite"/>
    <circle cx="0" cy="0" r="32" fill="none" stroke="{primary}" stroke-width="3" opacity="0.6"/>
    <circle cx="0" cy="0" r="12" fill="{primary}" opacity="0.8"/>
    <!-- Gear teeth -->
    <rect x="-4" y="-42" width="8" height="12" rx="2" fill="{primary}" opacity="0.7"/>
    <rect x="-4" y="30" width="8" height="12" rx="2" fill="{primary}" opacity="0.7"/>
    <rect x="30" y="-4" width="12" height="8" rx="2" fill="{primary}" opacity="0.7"/>
    <rect x="-42" y="-4" width="12" height="8" rx="2" fill="{primary}" opacity="0.7"/>
    <rect x="20" y="-32" width="8" height="12" rx="2" fill="{primary}" opacity="0.6" transform="rotate(45)"/>
    <rect x="20" y="20" width="8" height="12" rx="2" fill="{primary}" opacity="0.6" transform="rotate(-45) translate(-30,10)"/>
  </g>

  <!-- Circular arrows (flywheel loop) -->
  <defs>
    <marker id="arrowhead" markerWidth="8" markerHeight="6" refX="6" refY="3" orient="auto">
      <polygon points="0 0, 8 3, 0 6" fill="{primary}" opacity="0.7"/>
    </marker>
  </defs>

  <!-- 4 stage nodes -->
  <!-- Execute (top) -->
  <g transform="translate(320,40)">
    <rect x="-55" y="-18" width="110" height="36" rx="8" fill="{success}" opacity="0.15" stroke="{success}" stroke-width="1.5"/>
    <text x="0" y="-3" text-anchor="middle" fill="{success}" font-size="11" font-weight="700">EXECUTE</text>
    <text x="0" y="12" text-anchor="middle" fill="{muted}" font-size="9">Shadow runs task</text>
  </g>

  <!-- Refinery (right) -->
  <g transform="translate(530,130)">
    <rect x="-55" y="-18" width="110" height="36" rx="8" fill="{warning}" opacity="0.15" stroke="{warning}" stroke-width="1.5"/>
    <text x="0" y="-3" text-anchor="middle" fill="{warning}" font-size="11" font-weight="700">REFINERY</text>
    <text x="0" y="12" text-anchor="middle" fill="{muted}" font-size="9">Extracts lessons</text>
  </g>

  <!-- Memory (bottom) -->
  <g transform="translate(320,220)">
    <rect x="-55" y="-18" width="110" height="36" rx="8" fill="#a78bfa" opacity="0.15" stroke="#a78bfa" stroke-width="1.5"/>
    <text x="0" y="-3" text-anchor="middle" fill="#a78bfa" font-size="11" font-weight="700">MEMORY</text>
    <text x="0" y="12" text-anchor="middle" fill="{muted}" font-size="9">Consolidates knowledge</text>
  </g>

  <!-- Recall (left) -->
  <g transform="translate(110,130)">
    <rect x="-55" y="-18" width="110" height="36" rx="8" fill="{info}" opacity="0.15" stroke="{info}" stroke-width="1.5"/>
    <text x="0" y="-3" text-anchor="middle" fill="{info}" font-size="11" font-weight="700">RECALL</text>
    <text x="0" y="12" text-anchor="middle" fill="{muted}" font-size="9">Guides next run</text>
  </g>

  <!-- Connecting arrows with animation -->
  <!-- Execute → Refinery (top-right arc) -->
  <path d="M 375 55 Q 490 70 475 112" fill="none" stroke="{primary}" stroke-width="1.5"
        marker-end="url(#arrowhead)" stroke-dasharray="6,3" opacity="0.6">
    <animate attributeName="stroke-dashoffset" from="0" to="-18" dur="2s" repeatCount="indefinite"/>
  </path>

  <!-- Refinery → Memory (right-bottom arc) -->
  <path d="M 475 148 Q 490 190 375 205" fill="none" stroke="{primary}" stroke-width="1.5"
        marker-end="url(#arrowhead)" stroke-dasharray="6,3" opacity="0.6">
    <animate attributeName="stroke-dashoffset" from="0" to="-18" dur="2s" begin="0.5s" repeatCount="indefinite"/>
  </path>

  <!-- Memory → Recall (bottom-left arc) -->
  <path d="M 265 205 Q 150 190 165 148" fill="none" stroke="{primary}" stroke-width="1.5"
        marker-end="url(#arrowhead)" stroke-dasharray="6,3" opacity="0.6">
    <animate attributeName="stroke-dashoffset" from="0" to="-18" dur="2s" begin="1s" repeatCount="indefinite"/>
  </path>

  <!-- Recall → Execute (left-top arc) -->
  <path d="M 165 112 Q 150 70 265 55" fill="none" stroke="{primary}" stroke-width="1.5"
        marker-end="url(#arrowhead)" stroke-dasharray="6,3" opacity="0.6">
    <animate attributeName="stroke-dashoffset" from="0" to="-18" dur="2s" begin="1.5s" repeatCount="indefinite"/>
  </path>

  <!-- Center label -->
  <text x="320" y="128" text-anchor="middle" fill="{text}" font-size="10" font-weight="600" opacity="0.5">LEARNING</text>
  <text x="320" y="141" text-anchor="middle" fill="{text}" font-size="10" font-weight="600" opacity="0.5">LOOP</text>
</svg>"""
