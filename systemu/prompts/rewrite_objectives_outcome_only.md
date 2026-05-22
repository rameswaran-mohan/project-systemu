# Rewrite Objectives Outcome-Only

You are rewriting a small set of objectives to remove GUI-codification — replace verbs and noun mentions of specific apps / formats / GUI actions with outcome-focused language.

## Why this matters

Downstream tool selection picks tools by reading the `goal` text. If the goal says "capture screenshot", a screenshot-named tool gets picked even when the user actually wants the underlying data. Your rewrite must describe **what the user wants to ACHIEVE** so downstream can pick the right tool by capability, not by name-match.

## Input

```json
{
  "objectives": [
    {
      "id": <int>,
      "goal": "<current problematic goal text>",
      "matched_pattern": "<the GUI verb / app name / extension that triggered the rewrite>"
    }
  ]
}
```

## Task

For each input objective, rewrite its `goal` so it describes the desired OUTCOME (the data, file, or state-change the user wants) WITHOUT naming:

- Any GUI application (`Snipping Tool`, `Word`, `Chrome`, `Notepad`, `Excel`, etc.)
- Any GUI verb (`screenshot`, `snip`, `click`, `paste`, `drag`, `open`, `type into`)
- Any output format extension (`.docx`, `.png`, `.jpg`, `.xlsx`)

**Good rewrites focus on what the user wants to ACHIEVE:**

| Before (GUI-codified) | After (outcome-only) |
|---|---|
| "capture screenshot of weather" | "Acquire current weather data for the user's location" |
| "save the .docx file" | "Persist the report to a durable file" |
| "open Word and paste content" | "Compose a structured document from the captured data" |
| "click Submit button" | "Submit the form" |
| "drag the file to the trash" | "Delete the file" |

## Output format

Return JSON only (no markdown fences, no preamble):

```json
{
  "objectives": [
    {"id": 1, "goal": "<rewritten outcome-focused goal>", "success_criteria": "<keep from input or rewrite if it also codifies GUI>"}
  ]
}
```

Preserve `success_criteria` from the input unless it also codifies GUI means.
