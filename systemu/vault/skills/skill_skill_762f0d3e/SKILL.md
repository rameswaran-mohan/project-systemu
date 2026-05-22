---
name: reporting_and_documentation
description: Ability to compile collected data into structured Word or text reports with appropriate formatting
category: productivity
proficiency_level: intermediate
required_tools:
  - create_word_doc
  - file_write
  - image_resize
---

# reporting_and_documentation

## Description

Ability to compile collected data into structured Word or text reports with appropriate formatting

## Procedural Instructions

For report creation: 1) Determine the output format from the scroll's constraints (docx vs plain text vs both). 2) Use create_word_doc for rich documents — set title as the report heading and body_text for the main content. 3) If embedding images, resize them first with image_resize to a reasonable width (max 1200px) to avoid oversized documents. 4) Follow the naming_pattern from the scroll's observed_preferences exactly (date format, prefix, extension). 5) After writing, use file_read to verify the file exists and is non-empty.

## Required Tools

- create_word_doc
- file_write
- image_resize

## Evidence Scrolls

- scroll_4e6b7667
