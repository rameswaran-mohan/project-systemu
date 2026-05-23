---
name: desktop-application-control
description: Fallback proficiency for controlling desktop applications via OS-level
  automation when no programmatic API exists
metadata:
  category: system
  proficiency_level: intermediate
  required_tools:
  - launch_application
  - close_application
  - keyboard_shortcut
  - type_text
---

# desktop_application_control

## Description

Fallback proficiency for controlling desktop applications via OS-level automation when no programmatic API exists

## Procedural Instructions

IMPORTANT: Use these tools only when no programmatic alternative exists (no API, no file format library). To launch an app: use launch_application with the friendly name or executable. Always wait (use THINK with a brief note about timing) after launching before sending keystrokes. Use keyboard_shortcut for menu shortcuts (Ctrl+S, Alt+F4) and type_text for entering text into focused fields. Use close_application to terminate apps after the task is complete. For file save operations, prefer creating files directly with create_word_doc or file_write instead.

## Required Tools

- launch_application
- close_application
- keyboard_shortcut
- type_text

## Evidence Scrolls

_No evidence scrolls._
