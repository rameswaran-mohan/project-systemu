---
name: clinical_trial_search
description: Proficiency in searching ClinicalTrials.gov for clinical trials by condition, phase, and recruitment status, and extracting structured trial listings
category: browser
proficiency_level: intermediate
required_tools:
  - web_extract_text
  - web_extract_table
  - browser_navigate
---

# clinical_trial_search

## Description

Proficiency in searching ClinicalTrials.gov for clinical trials by condition, phase, and recruitment status, and extracting structured trial listings

## Procedural Instructions

1) Use browser_navigate to load the ClinicalTrials.gov search URL with query parameters for condition, phase, and status. 2) Wait for the results list to fully render. 3) Use web_extract_table to capture the search results table, or web_extract_text with appropriate selectors to extract trial titles and identifiers. 4) Limit results to the specified count (e.g., 25) by truncating the extracted list. 5) Return the structured list of trial records for downstream navigation.

## Required Tools

- web_extract_text
- web_extract_table
- browser_navigate

## Evidence Scrolls

- scroll_abb228dc
