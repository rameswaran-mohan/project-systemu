---
name: weather_report_creation
description: Proficiency in capturing current weather data from a web source and compiling it into a dated Word document with an embedded screenshot
category: browser
proficiency_level: intermediate
required_tools:
  - web_screenshot
  - create_word_doc
---

# weather_report_creation

## Description

Proficiency in capturing current weather data from a web source and compiling it into a dated Word document with an embedded screenshot

## Procedural Instructions

1) Navigate to the weather search URL (e.g., https://www.google.com/search?q=weather) using web_screenshot to capture the full page or the weather card region. 2) Save the screenshot to a temporary file. 3) Use create_word_doc with the screenshot as the embedded image, setting the title to 'Weather Report' and the filename following the pattern 'Weather on MMDDYYYY.docx'. 4) The output path should be parameterised — the caller supplies the directory; the skill constructs the full path using the date.

## Required Tools

- web_screenshot
- create_word_doc

## Evidence Scrolls

- scroll_fb8c620f
