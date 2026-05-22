---
name: notification_and_alerting
description: Ability to surface task completion status and important alerts to the user
category: productivity
proficiency_level: beginner
required_tools:
  - notify_desktop
---

# notification_and_alerting

## Description

Ability to surface task completion status and important alerts to the user

## Procedural Instructions

Use notify_desktop to alert the user when a long-running task completes or when their input is needed. Keep notification titles concise (under 50 characters) and messages informative but brief. Set timeout to 10 seconds for important alerts, 5 seconds for informational notifications. If plyer is not available, the tool falls back to a console print — the notification will still be recorded in the execution log. Do not spam notifications for intermediate steps — use them only for completion events or genuine alerts.

## Required Tools

- notify_desktop

## Evidence Scrolls

_No evidence scrolls._
