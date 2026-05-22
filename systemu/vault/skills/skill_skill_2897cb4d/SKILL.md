---
name: clipboard_workflow
description: Ability to read from and write to the system clipboard to transfer data between applications
category: productivity
proficiency_level: beginner
required_tools:
  - clipboard_read
  - clipboard_write
---

# clipboard_workflow

## Description

Ability to read from and write to the system clipboard to transfer data between applications

## Procedural Instructions

Use clipboard_write to place text on the clipboard — content persists until overwritten. Use clipboard_read to retrieve the current clipboard contents — returns empty string if clipboard is empty. For workflows involving documents: write the value to clipboard, then use the target application's paste mechanism. For data extraction workflows: if a web tool copied data to clipboard, use clipboard_read to capture it programmatically.

## Required Tools

- clipboard_read
- clipboard_write

## Evidence Scrolls

_No evidence scrolls._
