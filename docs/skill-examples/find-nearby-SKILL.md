---
name: find-nearby
description: "Find a ranked list of <thing> near the user's location and write a JSON file."
version: 1.0.0
platforms: [linux, macos, windows]
metadata:
  systemu:
    tags: [search, ranking, location-aware]
    related_skills: [burrito-delivery]
prerequisites:
  commands: []
requires_toolsets: [file, web]
fallback_for_toolsets: []
---

# Find Nearby — Generic Recipe

## When to Use

User asks "find X near me" or "find top X in <city>" for any category — restaurants, parks, museums, shops. Outputs a JSON file with ranked entries.

## When NOT to Use

- The user wants to *book* something (this is a discovery skill, not a transaction skill).
- The category is highly time-sensitive (events, news) — use a different tool.

## Procedure

1. Resolve location from `user_profile.location_text` or ask the user.
2. Search via `https://duckduckgo.com/html/?q=top+<thing>+<location>`.
3. From the search-result page, extract candidate URLs.
4. For each URL, web_extract with `fields=["name", "address", "rating"]`. Skip 403s; try the next URL.
5. Aggregate into `{default_output_dir}/<thing>_raw.json`.
6. Rank by rating descending; tie-break by source authority.
7. Write `{default_output_dir}/<thing>_ranked.json` with a `rank` field.
8. Reply summarizing top 3 + file location.

## Pitfalls

- Don't scrape Yelp/TripAdvisor — anti-bot blocked.
- DuckDuckGo HTML endpoint: `/html/?q=...`.

## Verification

- `<thing>_raw.json` exists, ≥5 entries
- `<thing>_ranked.json` exists, sorted, with `rank` field
- Chat reply summarizes top 3
