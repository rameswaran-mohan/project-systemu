---
name: burrito-delivery
description: "Find and rank the top burrito places near a given city, write a ranked JSON file."
version: 1.0.0
platforms: [linux, macos, windows]
metadata:
  systemu:
    tags: [food, scraping, restaurant-search, ranking]
    related_skills: [restaurant-research, location-aware-search]
prerequisites:
  commands: []
requires_toolsets: [file, web]
fallback_for_toolsets: []
---

# Burrito Delivery — Recipe

## When to Use

The user wants a ranked list of burrito places (or any restaurant kind in a city). The user has a known location (from `user_profile.location_text`). The outcome is a JSON file with at least 5 ranked entries that the user can review.

## When NOT to Use

- The user already has a specific URL they want scraped (use `web_extract` directly with the URL).
- The user is asking for restaurant *categories* in general (use a knowledge tool, not scraping).
- The user wants real-time information (reservations, hours) — those require live APIs, not scraping.

## Procedure

1. **Resolve the user's location.** Use `user_profile.location_text` if available; otherwise ask. For this recipe the location is referenced as `<city>`.

2. **Search a search engine FIRST.** Do NOT scrape Yelp / TripAdvisor / Zomato directly — those sites have aggressive anti-bot detection (Cloudflare/PerimeterX) and will return HTTP 403 with `error_type="anti_bot_blocked"`. Instead:
   - Call `web_extract` with URL `https://duckduckgo.com/html/?q=best+burrito+places+<city>` (use the DuckDuckGo HTML endpoint — it's scraper-friendly).
   - From the search-result page, identify 5-10 promising URLs that point at:
     - Food blogs (TimeOut, BlogTo, Eater, Condé Nast)
     - Wikipedia pages
     - General directory aggregators (NOT Yelp/TripAdvisor)

3. **Extract each result page.** For each candidate URL, call `web_extract` with `fields=["name", "address", "rating"]`. Skip any that return `error_type="anti_bot_blocked"` — pick another URL from the search results.

4. **Aggregate into a raw JSON file.** Combine all extracted records into a single list. Write to `{default_output_dir}/burrito_raw.json` using the `write_file` tool with `content=` set to the JSON-encoded list.

5. **Rank by quality signals.** Sort by `rating` descending; break ties by source authority (Eater > BlogTo > generic blogs).

6. **Write the ranked file.** Write to `{default_output_dir}/burrito_ranked.json` with the same records, sorted, plus a `rank` field on each.

7. **Reply to the user.** Summarize: `"I found N burrito places in <city>. Top 3: 1) <name>, 2) <name>, 3) <name>. Full list at burrito_ranked.json."`

## Pitfalls

- **Yelp/TripAdvisor/Zomato return 403.** Don't waste an iteration trying — go straight to search engine + content-rich blogs.
- **Aggressive sites time out.** If 60s isn't enough, the registry timeout will fire; pick a different URL.
- **DuckDuckGo HTML endpoint is `/html/?q=...`** (not `/?q=...` which is the JS-rendered version).
- **Don't fake completion.** The verifier subsystem rejects completion claims without files on disk. If web_extract returns `error_type="anti_bot_blocked"`, retry with a search engine URL — don't claim done.

## Verification

The skill is complete when:
- `{default_output_dir}/burrito_raw.json` exists with at least 5 entries
- `{default_output_dir}/burrito_ranked.json` exists with the same entries, sorted by rating, each with a `rank` field
- The chat reply summarizes the top 3 and points the user at the file
