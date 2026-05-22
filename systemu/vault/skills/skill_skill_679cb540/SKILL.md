---
name: clinical_trial_detail_extraction
description: Proficiency in navigating to a specific clinical trial page on ClinicalTrials.gov and extracting detailed trial information including title, status, phase, interventions, outcomes, and eligibility criteria
category: browser
proficiency_level: intermediate
required_tools:
  - browser_navigate
  - web_extract_text
  - web_extract_table
---

# clinical_trial_detail_extraction

## Description

Proficiency in navigating to a specific clinical trial page on ClinicalTrials.gov and extracting detailed trial information including title, status, phase, interventions, outcomes, and eligibility criteria

## Procedural Instructions

1) Use browser_navigate to open the specific trial's detail page URL (e.g., https://clinicaltrials.gov/study/NCTXXXXXX). 2) Wait for the page to fully load. 3) Use web_extract_text with selectors targeting the trial title, status, phase, sponsor, interventions, and eligibility criteria sections. 4) Use web_extract_table for any structured data tables (e.g., outcome measures, arms). 5) Compile the extracted information into a structured dictionary or JSON object for downstream analysis.

## Required Tools

- browser_navigate
- web_extract_text
- web_extract_table

## Evidence Scrolls

- scroll_abb228dc
