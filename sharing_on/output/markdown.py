"""Markdown renderer — assembles the final instructions.md document.

Combines:
- LLM-generated step instructions
- Embedded screenshots (relative paths)
- Session metadata header
- Raw event statistics footer
"""

from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from sharing_on.analyzer.step_detector import Step


def render_markdown(
    instructions: str,
    steps: List[Step],
    session_name: str,
    session_id: str,
    platform_info: str,
    start_time: Optional[datetime],
    end_time: Optional[datetime],
    output_dir: Path,
    event_count: int,
    intent: Optional["IntentExtraction"] = None,    # noqa: F821 — fwd ref
) -> Path:
    """Render the final Markdown document with screenshots.

    Screenshots are copied into an `assets/` subdirectory next to the
    Markdown file so the document is fully self-contained.

    Args:
        instructions: LLM-generated step text (already in Markdown).
        steps: Detected steps (for screenshot mapping).
        session_name: Human-readable session name.
        session_id: Unique session identifier.
        platform_info: Platform description string.
        start_time: When the capture session started.
        end_time: When the capture session ended.
        output_dir: Directory where the output files live.
        event_count: Total number of captured events.
        intent: (v0.6.0-a) optional pre-extracted intent.  When present, an
            explicit ``## Intent`` block is rendered right after the header
            so downstream pipeline stages (and human readers) see the
            outcome-oriented intent prominently, not buried in narrative.

    Returns:
        Path to the generated instructions.md file.
    """
    assets_dir = output_dir / "assets"

    # Copy screenshots into the assets directory and build a mapping
    # step_number -> relative asset path
    screenshot_map: dict[int, str] = {}
    for step in steps:
        if step.screenshot_path:
            src = Path(step.screenshot_path)
            if src.exists():
                assets_dir.mkdir(exist_ok=True)
                dest_name = f"step_{step.step_number:02d}_{src.name}"
                dest = assets_dir / dest_name
                try:
                    shutil.copy2(src, dest)
                    screenshot_map[step.step_number] = f"assets/{dest_name}"
                except Exception:
                    pass

    # Build the document
    sections = []

    # --- Header ---
    sections.append(_render_header(
        session_name=session_name,
        session_id=session_id,
        platform_info=platform_info,
        start_time=start_time,
        end_time=end_time,
        step_count=len(steps),
        event_count=event_count,
    ))

    # --- Intent block (v0.6.0-a) ---
    # When pre-extracted intent is available, render an explicit ## Intent
    # block right after the header.  Stage 2 (scroll refiner) preferentially
    # reads this block over re-deriving intent from narrative.
    if intent is not None and getattr(intent, "is_usable", False):
        sections.append(_render_intent_block(intent))

    # --- Main instructions ---
    # If the LLM returned numbered steps, inject screenshots after each step
    injected = _inject_screenshots(instructions, screenshot_map)
    sections.append(injected)

    # --- Raw step appendix ---
    sections.append(_render_step_appendix(steps))

    # --- Footer ---
    sections.append(_render_footer())

    document = "\n\n---\n\n".join(sections)

    # Write the file
    output_path = output_dir / "instructions.md"
    output_path.write_text(document, encoding="utf-8")

    return output_path


def _render_header(
    session_name: str,
    session_id: str,
    platform_info: str,
    start_time: Optional[datetime],
    end_time: Optional[datetime],
    step_count: int,
    event_count: int,
) -> str:
    """Render the document header with session metadata."""
    duration_str = ""
    if start_time and end_time:
        secs = int((end_time - start_time).total_seconds())
        m, s = divmod(secs, 60)
        h, m = divmod(m, 60)
        if h > 0:
            duration_str = f"{h}h {m}m {s}s"
        elif m > 0:
            duration_str = f"{m}m {s}s"
        else:
            duration_str = f"{s}s"

    recorded_at = ""
    if start_time:
        # Convert to "human" local-style display
        recorded_at = start_time.strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        f"# {session_name}",
        "",
        "> **Recorded with sharing_on** — automatically generated step-by-step instructions",
        "",
        "## Session Info",
        "",
        "| Field | Value |",
        "|:---|:---|",
        f"| **Session ID** | `{session_id}` |",
        f"| **Platform** | {platform_info} |",
    ]

    if recorded_at:
        lines.append(f"| **Recorded at** | {recorded_at} |")
    if duration_str:
        lines.append(f"| **Duration** | {duration_str} |")

    lines += [
        f"| **Steps detected** | {step_count} |",
        f"| **Events captured** | {event_count:,} |",
        "",
    ]

    return "\n".join(lines)


def _inject_screenshots(instructions: str, screenshot_map: dict[int, str]) -> str:
    """Inject screenshot images after each numbered step in the instructions.

    Finds lines like "1.", "2.", "Step 1", etc. and inserts the corresponding
    screenshot after the step's content block.
    """
    if not screenshot_map:
        return instructions

    lines = instructions.split("\n")
    result = []
    current_step: Optional[int] = None
    injected_steps: set[int] = set()

    for i, line in enumerate(lines):
        result.append(line)

        # Detect start of a new numbered step
        step_num = _detect_step_number(line)
        if step_num is not None:
            current_step = step_num

        # Detect end of a step block (next numbered step, horizontal rule, or heading)
        next_line = lines[i + 1] if i + 1 < len(lines) else None
        is_end_of_step = (
            current_step is not None
            and current_step not in injected_steps
            and next_line is not None
            and (
                _detect_step_number(next_line) is not None
                or next_line.startswith("---")
                or next_line.startswith("## ")
            )
        )

        if is_end_of_step and current_step in screenshot_map:
            img_path = screenshot_map[current_step]
            result.append("")
            result.append(
                f"![Step {current_step} screenshot]({img_path})"
            )
            result.append("")
            injected_steps.add(current_step)

    # Inject screenshot for the last step if not yet injected
    if (
        current_step is not None
        and current_step not in injected_steps
        and current_step in screenshot_map
    ):
        img_path = screenshot_map[current_step]
        result.append("")
        result.append(f"![Step {current_step} screenshot]({img_path})")

    return "\n".join(result)


def _detect_step_number(line: str) -> Optional[int]:
    """Extract step number from lines like '1.', '## Step 3', '**Step 2:**'."""
    import re

    # "1. Something" or "1) Something"
    m = re.match(r"^(\d+)[.)]\s", line)
    if m:
        return int(m.group(1))

    # "## Step 3" or "### Step 3"
    m = re.match(r"^#{1,4}\s+[Ss]tep\s+(\d+)", line)
    if m:
        return int(m.group(1))

    # "**Step 3:**" or "**Step 3**"
    m = re.match(r"^\*\*[Ss]tep\s+(\d+)", line)
    if m:
        return int(m.group(1))

    return None


def _render_step_appendix(steps: List[Step]) -> str:
    """Render a compact appendix with raw step metadata."""
    if not steps:
        return ""

    lines = [
        "## Appendix — Captured Step Details",
        "",
        "_Raw event breakdown per detected step_",
        "",
    ]

    for step in steps:
        duration = f"{step.duration_seconds:.1f}s" if step.duration_seconds else "—"
        app = step.primary_app or "—"
        label = f' _{step.label}_' if step.label else ""
        counts = step.event_summary

        lines.append(f"**Step {step.step_number}**{label}")
        lines.append(f"- App: `{app}` | Duration: {duration}")

        count_parts = []
        if counts.get("file"):
            count_parts.append(f"{counts['file']} file event(s)")
        if counts.get("process"):
            count_parts.append(f"{counts['process']} process event(s)")
        if counts.get("window"):
            count_parts.append(f"{counts['window']} window switch(es)")
        if counts.get("clipboard"):
            count_parts.append(f"{counts['clipboard']} clipboard change(s)")

        if count_parts:
            lines.append(f"- Events: {', '.join(count_parts)}")

        lines.append("")

    return "\n".join(lines)


def _render_footer() -> str:
    """Render document footer."""
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return (
        f"_Generated by [sharing_on](https://github.com/your-org/sharing_on) "
        f"on {generated_at}_"
    )


def _render_intent_block(intent: "IntentExtraction") -> str:    # noqa: F821
    """Render the ## Intent section (v0.6.0-a).

    Downstream pipeline stages (specifically the scroll refiner) parse this
    block out preferentially.  Keep the structure stable: a top-level
    ``## Intent`` header, bullet list with consistent labels, confidence
    annotation at the bottom.
    """
    lines = [
        "## Intent",
        "",
        "_This section is pre-extracted by the intent inferrer.  It states "
        "the user's outcome-oriented goal independent of the specific apps "
        "or GUI steps used.  Downstream automation should target this intent, "
        "not replicate the captured click sequence._",
        "",
        f"- **Intent:** {intent.intent}",
        f"- **Expected outcome:** {intent.expected_outcome}",
        f"- **Success signal:** {intent.success_signal}",
    ]
    if intent.abstracted_steps:
        lines.append("- **Abstracted steps (outcome-described):**")
        for step in intent.abstracted_steps:
            lines.append(f"  - {step}")
    lines.append("")
    lines.append(f"_Inference confidence: **{intent.confidence}**_")
    return "\n".join(lines)
