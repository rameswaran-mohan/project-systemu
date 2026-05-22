"""LLM-powered instruction generator — converts detected steps into
human-readable, step-by-step instructions using OpenRouter API.

Uses the OpenAI-compatible client library with OpenRouter's base URL.
"""

from __future__ import annotations

import json
import logging
from typing import List, Optional

from openai import OpenAI

from sharing_on.analyzer.step_detector import Step
from sharing_on.events.models import EventAction, EventCategory
from sharing_on.redactor import redact

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """You are an expert Standard Operating Procedure (SOP) writer.
Your job is to analyze captured computer activity and produce a clear, narrative
document that another person can follow to reproduce the exact same task.

## Your Approach

1. **Infer the Overall Intent**: Before writing any steps, analyze ALL the captured
   events holistically. Determine WHAT the user was trying to accomplish at a high
   level (e.g., "Create a daily financial market summary report" or "Deploy a
   microservice to production"). State this intent clearly at the top of the document
   as a one-paragraph executive summary.

2. **Identify Subtasks**: Break the full activity into logical subtasks or phases
   (e.g., "Phase 1: Research — Gather market data", "Phase 2: Documentation —
   Compile findings into a report"). Each subtask should have a clear heading and
   a brief description of its purpose before the numbered steps.

3. **Write Narrative Steps**: Each step should read like a clear instruction from
   a knowledgeable colleague, not a robotic log. Use natural language.
   - BAD:  "Clicked 'Save' (ButtonControl) in Save As dialog"
   - GOOD: "Save the file by clicking the **Save** button in the Save As dialog."

## Rules

1. Write each step as a clear, action-oriented instruction
2. Include exact commands, URLs, and file paths when detected
3. Mention which application was used at the start of each step (e.g., "**In Google Chrome**, ...")
4. Be specific but concise — assume the reader is technically competent but unfamiliar with this task
5. If clipboard content was pasted, mention what was pasted and where
6. Format file changes as markdown diff blocks
7. Do NOT invent steps that weren't captured — only document what actually happened
8. Group closely related sub-actions (like repeated formatting clicks) into a single step and describe the intent
9. When a UI element was clicked, describe it by its visible label or name, NOT by coordinates
10. If a URL was navigated to, include the full URL
11. When a repeated action is noted (e.g., "clicked 15 times"), describe the intent
    (e.g., "Reduced the font size to approximately 10pt by clicking the decrease button repeatedly")
12. If an input field was changed, mention which field and what value was entered
13. If the user switched between apps to copy/reference information, describe the workflow
    (e.g., "Switch to the browser tab showing NSE India to note the closing Nifty value, then return to the Google Doc to enter it")

## Output Format

```markdown
# [Task Title — inferred from activity]

## Overview
[1-2 paragraph executive summary of what was accomplished and why]

## Subtask 1: [Phase Name]
[Brief description of this phase's purpose]

1. **[App Name]** — [Clear narrative instruction]
2. ...

## Subtask 2: [Phase Name]
...

## Result
[Brief description of the final outcome — e.g., what file was created, what was deployed, etc.]
```

Return ONLY the Markdown document. Do not add any preamble or explanation outside the document."""


def generate_instructions(
    steps: List[Step],
    session_name: str,
    platform_info: str,
    duration_seconds: float,
    api_key: str,
    base_url: str = "https://openrouter.ai/api/v1",
    model: str = "openai/gpt-4o-mini",
    intent: Optional["IntentExtraction"] = None,    # noqa: F821 — fwd ref
) -> str:
    """Send captured steps to the LLM and get back formatted instructions.

    Args:
        steps: List of detected steps with their events.
        session_name: Name of the capture session.
        platform_info: Platform description string.
        duration_seconds: Total session duration.
        api_key: OpenRouter API key.
        base_url: OpenRouter API base URL.
        model: LLM model identifier.
        intent: (v0.6.0-a) optional pre-extracted intent.  When present, the
            LLM is told the user's actual outcome up-front and instructed
            to anchor the narrative on that intent rather than re-inferring
            it from the click sequence.

    Returns:
        Markdown-formatted step-by-step instructions.
    """
    if not steps:
        return "_No activity was captured during this session._"

    # Build the structured step data for the LLM
    step_descriptions = []
    for step in steps:
        step_desc = _format_step_for_llm(step)
        step_descriptions.append(step_desc)

    # when intent is pre-extracted, surface it explicitly so the
    # narrative LLM doesn't have to re-derive it from clicks (which is the
    # whole reason the click-mirroring failure mode exists).
    intent_block = ""
    if intent is not None and getattr(intent, "is_usable", False):
        intent_block = (
            "## Pre-Inferred User Intent\n\n"
            f"- **Intent:** {intent.intent}\n"
            f"- **Expected outcome:** {intent.expected_outcome}\n"
            f"- **Success signal:** {intent.success_signal}\n\n"
            "Anchor your narrative on this stated intent.  The captured steps "
            "below describe HOW the user happened to do it; your job is to "
            "narrate them in a way that serves the stated intent, not to "
            "re-derive intent from the click sequence.\n\n"
            "---\n\n"
        )

    user_prompt = f"""Task Name: {session_name}
Platform: {platform_info}
Total Duration: {duration_seconds:.0f} seconds
Number of Steps Detected: {len(steps)}

{intent_block}Below are the captured steps with their raw events. Convert these into clear,
reproducible instructions.

---

{chr(10).join(step_descriptions)}
"""

    # Redact PII before sending to LLM
    user_prompt = redact(user_prompt)

    try:
        client = OpenAI(
            api_key=api_key,
            base_url=base_url,
        )

        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3,       # Low creativity, high accuracy
            max_tokens=4000,
            top_p=0.9,
        )

        instructions = response.choices[0].message.content or ""
        logger.info(
            f"Generated instructions: {len(instructions)} chars, "
            f"tokens used: {response.usage.total_tokens if response.usage else 'unknown'}"
        )
        return instructions.strip()

    except Exception as e:
        logger.error(f"LLM instruction generation failed: {e}")
        # Fallback: generate basic instructions without LLM
        return _generate_fallback_instructions(steps, session_name)


def _format_step_for_llm(step: Step) -> str:
    """Format a single step's events into a readable description for the LLM."""
    lines = []
    lines.append(f"### Step {step.step_number}")

    if step.label:
        lines.append(f"**User Label:** {step.label}")

    if step.primary_app:
        lines.append(f"**Primary Application:** {step.primary_app}")

    if step.start_time:
        lines.append(f"**Time:** {step.start_time.strftime('%H:%M:%S')}")
        lines.append(f"**Duration:** {step.duration_seconds:.1f}s")

    lines.append("")
    lines.append("**Events:**")

    # Emit relevant events (skip screenshots — they're referenced separately)
    for event in step.events:
        if event.category == EventCategory.SCREEN:
            continue

        if event.action == EventAction.WINDOW_FOCUS:
            app = event.application or "Unknown"
            title = event.window_title or ""
            lines.append(f"- Switched to **{app}**: {title}")

        elif event.action == EventAction.FILE_CREATED:
            lines.append(f"- Created file: `{event.file_path}`")

        elif event.action == EventAction.FILE_MODIFIED:
            lines.append(f"- Modified file: `{event.file_path}`")
            diff = event.data.get("diff")
            if diff:
                # Truncate very long diffs for the LLM
                if len(diff) > 2000:
                    diff = diff[:2000] + "\n... (truncated)"
                lines.append(f"  ```diff\n{diff}\n  ```")

        elif event.action == EventAction.FILE_DELETED:
            lines.append(f"- Deleted file: `{event.file_path}`")

        elif event.action == EventAction.FILE_MOVED:
            dest = event.data.get("dest_path", "unknown")
            lines.append(f"- Moved file: `{event.file_path}` → `{dest}`")

        elif event.action == EventAction.PROCESS_STARTED:
            cmdline = event.data.get("cmdline", event.process_name or "")
            lines.append(f"- Ran command: `{cmdline}`")

        elif event.action == EventAction.PROCESS_ENDED:
            lines.append(f"- Process ended: {event.process_name}")

        elif event.action == EventAction.CLIPBOARD_CHANGE:
            preview = event.data.get("preview", "")
            content_type = event.data.get("content_type", "text")
            if content_type == "command":
                lines.append(f"- Copied command: `{preview}`")
            elif content_type == "code":
                lines.append(f"- Copied code snippet: `{preview[:100]}`")
            elif content_type == "url":
                lines.append(f"- Copied URL: `{preview}`")
            else:
                lines.append(f"- Copied to clipboard: {preview[:100]}")

        elif event.action == EventAction.STEP_MARKER:
            label = event.data.get("label", "")
            key_name = event.data.get("key", "")
            if key_name:
                lines.append(f"- Pressed key: **{key_name}**")
            elif label:
                lines.append(f"- User note: {label}")

        elif event.action == EventAction.MOUSE_CLICK:
            app = event.application or "Unknown"
            el_name = event.data.get("element_name", "")
            ctrl_type = event.data.get("control_type", "")
            xpath = event.data.get("element_xpath", "")
            url = event.data.get("url", "")
            el_text = event.data.get("element_text", "")
            value = event.data.get("value", "")
            repeat = event.data.get("repeat_count", 1)

            # Build a clear, semantic description
            desc_parts = []
            if el_name and el_name != "Unknown":
                desc_parts.append(f"**{el_name}**")
            elif el_text:
                desc_parts.append(f"**{el_text}**")

            if ctrl_type and ctrl_type != "Unknown":
                desc_parts.append(f"({ctrl_type})")

            if url:
                desc_parts.append(f"on page `{url}`")

            if value:
                desc_parts.append(f"[value: `{value}`]")

            desc = " ".join(desc_parts) if desc_parts else "an element"

            if repeat and repeat > 1:
                lines.append(f"- Clicked {desc} **{repeat} times** in **{app}**")
            else:
                lines.append(f"- Clicked {desc} in **{app}**")

        elif event.action == EventAction.KEY_PRESS:
            el_text = event.data.get("element_text", "")
            value = event.data.get("value", "")
            url = event.data.get("url", "")
            if value:
                lines.append(f"- Typed `{value}` into a field")
                if url:
                    lines.append(f"  on page `{url}`")
            elif el_text:
                lines.append(f"- Interacted with input: {el_text}")

    lines.append("")
    return "\n".join(lines)


def _generate_fallback_instructions(steps: List[Step], session_name: str) -> str:
    """Generate basic instructions without LLM (fallback if API fails)."""
    lines = [
        f"# {session_name}",
        "",
        "_Note: LLM generation failed. Showing raw captured steps._",
        "",
    ]

    for step in steps:
        lines.append(f"## Step {step.step_number}")
        if step.label:
            lines.append(f"_{step.label}_")
        if step.primary_app:
            lines.append(f"**Application:** {step.primary_app}")
        lines.append("")

        for event in step.events:
            if event.category == EventCategory.SCREEN:
                continue
            lines.append(f"- {event.summary}")

        lines.append("")

    return "\n".join(lines)
