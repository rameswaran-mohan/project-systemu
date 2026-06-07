# Diagnose Tool Inadequacy

You are diagnosing whether a tool is structurally inadequate for what a Shadow is trying to do.

A tool has failed multiple times for the same Shadow on the same task. The failures are NOT missing dependencies, NOT param errors, NOT timeouts — they look like the tool simply doesn't do what's needed.

Your job: determine whether the right response is **bump_version** (fix the tool — the flaw affects everyone) or **fork_new_tool** (create a specialised variant — only this shadow's use case needs it). Other shadows might be happily using the same tool for different things.

## What you receive

```json
{
  "tool_name":           "...",
  "tool_description":    "...",
  "current_spec": {
    "parameters_schema": { ... },
    "return_schema":     { ... },
    "implementation_notes": "..."
  },
  "shadow_id":           "...",
  "shadow_description":  "...",       // what this shadow specialises in
  "scroll_intent":       "...",       // what the shadow is trying to achieve
  "failing_objective":   "...",       // the specific objective the tool can't satisfy
  "recent_failures":     [ ... ],     // last 3 failure observations
  "other_shadows_using_tool": [ ... ], // [{shadow_id, recent_success_count}, ...]
  "cluster_signal": {                  // v0.5.1-d cross-shadow inadequacy data
    "is_cluster":        true|false,
    "distinct_shadows":  <int>,
    "total_flags":       <int>,
    "sample_rationales": [ ... ]
  }
}
```

## Your output (strict JSON, no markdown fences)

```json
{
  "recalibration_mode":  "bump_version" | "fork_new_tool",
  "rationale":           "<2-3 sentences explaining the decision>",
  "spec_diff_summary":   "<short summary of what the new spec should add/change>",
  "new_tool_name_suggestion": "...",       // only when mode=fork_new_tool; otherwise null
  "affected_shadows":    ["sh-id-1", ...], // who else uses this tool (echo input verbatim)
  "confidence":          "high" | "medium" | "low"
}
```

## Decision rules

1. **bump_version** when:
   - The flaw is a bug (unhandled edge case, missing error handling, broken assumption)
   - The flaw would also affect other shadows currently using the tool successfully (just lucky their inputs haven't hit it yet)
   - Fixing it makes the tool strictly better for everyone

2. **fork_new_tool** when:
   - This shadow's need is a *specialisation* — adding a new parameter, alternative output format, different behaviour entirely
   - Other shadows are succeeding with the current tool because their use case doesn't need the new behaviour
   - Forcing them onto the new behaviour might break their working flows

3. **Conservative bias toward fork** when:
   - `other_shadows_using_tool` is non-empty AND has recent successes
   - The diff_summary describes additive behaviour (new param, new mode) rather than fixing a bug
   - Confidence is low

4. **Strong bias toward bump when cluster_signal.is_cluster is True.**
   - ≥3 distinct shadows have independently flagged this tool inadequate within the recent window — that's a universal flaw, not a specialised need.
   - The flaw affects everyone using the tool; fixing once helps all of them.
   - Override the "non-empty other_shadows_using_tool" fork-bias rule when cluster_signal indicates the issue is shared.

5. **Never invent the answer.** When the input doesn't give you enough signal, set `confidence: "low"` and prefer `fork_new_tool` (safe default — adds a tool rather than modifying a working one).

5. Return only the JSON object. No prose outside it.
